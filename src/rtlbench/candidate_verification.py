"""Deterministic local verification of one RTL candidate per manifest row."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import signal
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from rtlbench.candidate_manifest import (
    CandidateManifestValidationError,
    CandidateRow,
    load_manifest,
)
from rtlbench.evidence import sanitize_diagnostic


EVIDENCE_SCHEMA_VERSION = "rtl_candidate_evidence_v0.1"
TOOL_VERSION_ARGS = {
    "iverilog": ("-V",),
    "vvp": ("-V",),
    "verilator": ("--version",),
    "yosys": ("--version",),
}
_TOOL_NAMES = ("iverilog", "vvp", "verilator", "yosys")
DEFAULT_MAX_OUTPUT_BYTES = 65_536
MAX_OUTPUT_BYTES_LIMIT = 4 * 1024 * 1024
DEFAULT_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_ROW_INPUT_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_RUN_INPUT_BYTES = 256 * 1024 * 1024
MAX_ARTIFACT_BYTES_LIMIT = 32 * 1024 * 1024
MAX_ROW_INPUT_BYTES_LIMIT = 128 * 1024 * 1024
MAX_RUN_INPUT_BYTES_LIMIT = 512 * 1024 * 1024
_COPY_CHUNK_BYTES = 64 * 1024
_MAX_DIAGNOSTIC_INDEX_LINES = 20_000
_MAX_DIAGNOSTIC_INDEX_LINE_BYTES = 1_024
_MAX_DIAGNOSTIC_INDEX_TOKENS = 10_000
_MAX_DIAGNOSTIC_FALLBACK_BYTES = DEFAULT_MAX_ARTIFACT_BYTES
_PROCESS_GROUP_GRACE_SECONDS = 0.2
_READER_JOIN_SECONDS = 1.0
_MISMATCH_LONG_RE = re.compile(
    r"(?im)^[ \t]*mismatches[ \t]*:[ \t]*([0-9]+)[ \t]+in[ \t]+([0-9]+)[ \t]+samples[ \t]*\r?$"
)
_MISMATCH_SHORT_RE = re.compile(
    r"(?im)^[ \t]*mismatches[ \t]*:[ \t]*([0-9]+)[ \t]*\r?$"
)
_TIMEOUT_RE = re.compile(r"(?im)^[ \t]*timeout[ \t]*\r?$")
_MANAGED_RUN_RE = re.compile(r"\.rtlbench-run-[A-Za-z0-9_.-]+")


class CandidateVerificationPreflightError(RuntimeError):
    """A workspace, output, hashing, or publication preflight error."""


class CandidateVerificationInterrupted(RuntimeError):
    """Raised after interruption while retaining the managed partial output."""


class CandidateVerificationInternalError(RuntimeError):
    """Raised for an unrecoverable batch-level internal failure."""


@dataclass(frozen=True)
class ToolInfo:
    path: str | None
    version: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"available": self.path is not None, "version": self.version}


@dataclass(frozen=True)
class CommandResult:
    returncode: int | None
    output: str
    timed_out: bool = False
    startup_error: bool = False
    output_limit_exceeded: bool = False


@dataclass(frozen=True)
class PreparedCandidateRow:
    """A manifest row whose inputs are immutable managed snapshots."""

    manifest_row: CandidateRow
    candidate_snapshot: Path
    testbench_snapshot: Path
    support_snapshots: dict[str, Path]
    input_hashes: dict[str, Any]


@dataclass(frozen=True)
class DiagnosticRedactionContext:
    path_strings: tuple[str, ...]
    source_lines: frozenset[str]
    long_tokens: frozenset[str]
    conservative: bool = False


@dataclass(frozen=True)
class MismatchReport:
    contract: str
    reported_counts: tuple[int, ...]
    reported_sample_counts: tuple[int | None, ...]
    timeout_reported: bool = False

    @property
    def has_positive_count(self) -> bool:
        return any(count > 0 for count in self.reported_counts)

    @property
    def maximum_count(self) -> int | None:
        return max(self.reported_counts) if self.reported_counts else None

    @property
    def has_report(self) -> bool:
        return bool(self.reported_counts)


def discover_toolchain(
    timeout: float = 5.0,
    which: Callable[[str], str | None] | None = None,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> dict[str, ToolInfo]:
    """Discover and version each executable once for a verification run."""

    resolver = which or shutil.which
    probe_cwd = cwd or Path.cwd()
    result: dict[str, ToolInfo] = {}
    for name in _TOOL_NAMES:
        path = resolver(name)
        if path and not os.path.isabs(path):
            path = str(Path(path).resolve())
        version = (
            _capture_version(
                path,
                TOOL_VERSION_ARGS[name],
                timeout,
                probe_cwd,
                environment,
                max_output_bytes,
            )
            if path
            else None
        )
        result[name] = ToolInfo(path, version)
    return result


def toolchain_json(toolchain: Mapping[str, ToolInfo]) -> dict[str, Any]:
    return {name: toolchain[name].to_dict() for name in _TOOL_NAMES}


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def hash_inputs(row: CandidateRow) -> dict[str, Any]:
    return {
        "candidate_rtl_sha256": sha256_file(
            row.resolved_paths["candidate_rtl_path"]
        ),
        "testbench_sha256": sha256_file(row.resolved_paths["testbench_path"]),
        "support_files": [
            {
                "path": path,
                "sha256": sha256_file(row.resolved_support_paths[path]),
            }
            for path in sorted(row.support_files)
        ],
    }


def prepare_candidate_rows(
    rows: list[CandidateRow],
    managed_run: Path,
    *,
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    max_row_input_bytes: int = DEFAULT_MAX_ROW_INPUT_BYTES,
    max_run_input_bytes: int = DEFAULT_MAX_RUN_INPUT_BYTES,
) -> list[PreparedCandidateRow]:
    """Snapshot every validated row before any tool discovery or execution."""

    _validate_input_limits(
        max_artifact_bytes, max_row_input_bytes, max_run_input_bytes
    )
    snapshot_root = managed_run / "inputs"
    if _contains_symlink(snapshot_root):
        raise CandidateVerificationPreflightError(
            "managed snapshot root must not contain symlinked path components"
        )
    try:
        snapshot_root.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise CandidateVerificationPreflightError(
            "managed snapshot root already exists"
        ) from exc
    except OSError as exc:
        raise CandidateVerificationPreflightError(
            f"could not create managed snapshot root: {exc}"
        ) from exc

    budget = [0]
    prepared: list[PreparedCandidateRow] = []
    for row_index, row in enumerate(rows, 1):
        prepared.append(
            _prepare_candidate_row(
                row,
                snapshot_root,
                row_index,
                budget,
                max_artifact_bytes,
                max_row_input_bytes,
                max_run_input_bytes,
            )
        )
    return prepared


def _prepare_candidate_row(
    row: CandidateRow,
    snapshot_root: Path,
    row_index: int,
    run_budget: list[int],
    max_artifact_bytes: int,
    max_row_input_bytes: int,
    max_run_input_bytes: int,
) -> PreparedCandidateRow:
    row_dir = snapshot_root / (
        f"{row_index:06d}_{hashlib.sha256(row.candidate_id.encode()).hexdigest()[:12]}"
    )
    support_dir = row_dir / "support"
    try:
        row_dir.mkdir(mode=0o700)
        support_dir.mkdir(mode=0o700)
    except OSError as exc:
        raise CandidateVerificationPreflightError(
            f"could not create managed snapshot directory: {exc}"
        ) from exc

    row_bytes = [0]
    candidate_snapshot = row_dir / "candidate.sv"
    candidate_hash = _copy_snapshot(
        row.resolved_paths["candidate_rtl_path"],
        candidate_snapshot,
        row_bytes,
        run_budget,
        max_artifact_bytes,
        max_row_input_bytes,
        max_run_input_bytes,
    )
    testbench_snapshot = row_dir / "testbench.sv"
    testbench_hash = _copy_snapshot(
        row.resolved_paths["testbench_path"],
        testbench_snapshot,
        row_bytes,
        run_budget,
        max_artifact_bytes,
        max_row_input_bytes,
        max_run_input_bytes,
    )
    support_snapshots: dict[str, Path] = {}
    support_hashes: list[dict[str, str]] = []
    for support_index, logical_path in enumerate(sorted(row.support_files)):
        snapshot = support_dir / (
            f"{support_index:04d}_{_safe_name(Path(logical_path).name)}"
        )
        support_snapshots[logical_path] = snapshot
        support_hashes.append(
            {
                "path": logical_path,
                "sha256": _copy_snapshot(
                    row.resolved_support_paths[logical_path],
                    snapshot,
                    row_bytes,
                    run_budget,
                    max_artifact_bytes,
                    max_row_input_bytes,
                    max_run_input_bytes,
                ),
            }
        )
    return PreparedCandidateRow(
        manifest_row=row,
        candidate_snapshot=candidate_snapshot,
        testbench_snapshot=testbench_snapshot,
        support_snapshots=support_snapshots,
        input_hashes={
            "candidate_rtl_sha256": candidate_hash,
            "testbench_sha256": testbench_hash,
            "support_files": support_hashes,
        },
    )


def _copy_snapshot(
    source: Path,
    destination: Path,
    row_bytes: list[int],
    run_budget: list[int],
    max_artifact_bytes: int,
    max_row_input_bytes: int,
    max_run_input_bytes: int,
) -> str:
    digest = hashlib.sha256()
    copied = 0
    source_fd = _open_regular_source(source)
    destination_fd: int | None = None
    source_handle = None
    destination_handle = None
    try:
        destination_fd = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0),
            0o600,
        )
        source_handle = os.fdopen(source_fd, "rb", closefd=True)
        source_fd = -1
        destination_handle = os.fdopen(destination_fd, "wb", closefd=True)
        destination_fd = None
        initial_size = os.fstat(source_handle.fileno()).st_size
        while True:
            chunk = source_handle.read(_COPY_CHUNK_BYTES)
            if not chunk:
                break
            next_size = copied + len(chunk)
            if next_size > max_artifact_bytes:
                raise CandidateVerificationPreflightError(
                    f"artifact exceeds max-artifact-bytes: {source}"
                )
            if row_bytes[0] + len(chunk) > max_row_input_bytes:
                raise CandidateVerificationPreflightError(
                    f"row inputs exceed max-row-input-bytes: {source}"
                )
            if run_budget[0] + len(chunk) > max_run_input_bytes:
                raise CandidateVerificationPreflightError(
                    f"run inputs exceed max-run-input-bytes: {source}"
                )
            digest.update(chunk)
            written = destination_handle.write(chunk)
            if written != len(chunk):
                raise CandidateVerificationPreflightError(
                    f"short snapshot write: {source}"
                )
            copied = next_size
            row_bytes[0] += len(chunk)
            run_budget[0] += len(chunk)
        final_size = os.fstat(source_handle.fileno()).st_size
        if copied < final_size or copied < initial_size:
            raise CandidateVerificationPreflightError(
                f"source changed during snapshot: {source}"
            )
        destination_handle.flush()
        os.fsync(destination_handle.fileno())
        os.chmod(destination, stat.S_IRUSR)
    except OSError as exc:
        raise CandidateVerificationPreflightError(
            f"could not snapshot input {source}: {exc}"
        ) from exc
    finally:
        if source_handle is not None:
            source_handle.close()
        if destination_handle is not None:
            destination_handle.close()
        if source_fd >= 0:
            os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)
    return digest.hexdigest()


def _open_regular_source(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow and path.is_symlink():
        raise CandidateVerificationPreflightError(
            f"snapshot source must not be a symlink: {path}"
        )
    try:
        descriptor = os.open(path, flags | nofollow)
        mode = os.fstat(descriptor).st_mode
        if not stat.S_ISREG(mode):
            os.close(descriptor)
            raise CandidateVerificationPreflightError(
                f"snapshot source is not a regular file: {path}"
            )
        return descriptor
    except CandidateVerificationPreflightError:
        raise
    except OSError as exc:
        raise CandidateVerificationPreflightError(
            f"could not open snapshot source {path}: {exc}"
        ) from exc


def _validate_input_limits(
    max_artifact_bytes: int,
    max_row_input_bytes: int,
    max_run_input_bytes: int,
) -> None:
    values = (
        ("max-artifact-bytes", max_artifact_bytes, MAX_ARTIFACT_BYTES_LIMIT),
        ("max-row-input-bytes", max_row_input_bytes, MAX_ROW_INPUT_BYTES_LIMIT),
        ("max-run-input-bytes", max_run_input_bytes, MAX_RUN_INPUT_BYTES_LIMIT),
    )
    for name, value, hard_cap in values:
        if type(value) is not int or not 0 < value <= hard_cap:
            raise CandidateManifestValidationError(
                f"{name} must be between 1 and {hard_cap}"
            )


def verify_candidates(
    *,
    manifest: Path,
    output: Path,
    workspace_root: Path,
    work_dir: Path,
    timeout: float = 30.0,
    force: bool = False,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    max_row_input_bytes: int = DEFAULT_MAX_ROW_INPUT_BYTES,
    max_run_input_bytes: int = DEFAULT_MAX_RUN_INPUT_BYTES,
    environment: Mapping[str, str] | None = None,
) -> dict[str, int]:
    """Validate, execute, and atomically publish deterministic candidate evidence."""

    if timeout <= 0:
        raise CandidateManifestValidationError("timeout must be greater than zero")
    _validate_max_output_bytes(max_output_bytes)
    _validate_input_limits(
        max_artifact_bytes, max_row_input_bytes, max_run_input_bytes
    )
    try:
        rows = load_manifest(Path(manifest), Path(workspace_root))
    except KeyboardInterrupt as exc:
        raise CandidateVerificationInterrupted("interrupted during manifest validation") from exc
    final_path, partial_path = _validate_output_paths(Path(output), force)
    _reject_output_input_collisions(final_path, partial_path, Path(manifest), rows)
    _reject_work_dir_input_collisions(Path(work_dir), Path(manifest), rows)
    scratch = _prepare_work_dir(Path(work_dir))

    try:
        prepared_rows = prepare_candidate_rows(
            rows,
            scratch,
            max_artifact_bytes=max_artifact_bytes,
            max_row_input_bytes=max_row_input_bytes,
            max_run_input_bytes=max_run_input_bytes,
        )
    except KeyboardInterrupt as exc:
        raise CandidateVerificationInterrupted("interrupted during input preparation") from exc
    except CandidateVerificationPreflightError:
        raise
    except (OSError, ValueError) as exc:
        raise CandidateVerificationPreflightError(
            f"could not prepare manifest artifacts: {exc}"
        ) from exc

    _reject_output_snapshot_collisions(
        final_path,
        partial_path,
        [prepared for prepared in prepared_rows],
    )

    try:
        with partial_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.flush()
    except OSError as exc:
        raise CandidateVerificationPreflightError(
            f"could not prepare evidence output: {exc}"
        ) from exc

    try:
        toolchain = discover_toolchain(
            timeout=timeout,
            cwd=scratch,
            environment=environment,
            max_output_bytes=max_output_bytes,
        )
    except KeyboardInterrupt as exc:
        raise CandidateVerificationInterrupted(
            f"interrupted; partial evidence retained at {partial_path}"
        ) from exc
    except Exception as exc:
        raise CandidateVerificationInternalError(
            f"could not discover toolchain: {exc}"
        ) from exc

    counts = {"rows": len(rows), "passed": 0, "failed": 0}
    try:
        with partial_path.open("w", encoding="utf-8", newline="\n") as handle:
            for index, prepared in enumerate(prepared_rows, 1):
                row = prepared.manifest_row
                try:
                    evidence = verify_row(
                        row,
                        input_hashes=prepared.input_hashes,
                        toolchain=toolchain,
                        work_dir=scratch,
                        timeout=timeout,
                        row_index=index,
                        workspace_root=Path(workspace_root),
                        manifest_path=Path(manifest),
                        max_output_bytes=max_output_bytes,
                        prepared=prepared,
                        environment=environment,
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    evidence = _internal_evidence(
                        row,
                        prepared.input_hashes,
                        toolchain,
                        str(exc),
                        Path(workspace_root),
                        scratch,
                        Path(manifest),
                        prepared=prepared,
                    )
                handle.write(deterministic_json(evidence) + "\n")
                handle.flush()
                if evidence["accepted"]:
                    counts["passed"] += 1
                else:
                    counts["failed"] += 1
            os.fsync(handle.fileno())
        os.replace(partial_path, final_path)
    except KeyboardInterrupt as exc:
        raise CandidateVerificationInterrupted(
            f"interrupted; partial evidence retained at {partial_path}"
        ) from exc
    except OSError as exc:
        raise CandidateVerificationPreflightError(
            f"could not write evidence output: {exc}"
        ) from exc
    return counts


def verify_row(
    row: CandidateRow,
    *,
    input_hashes: dict[str, Any],
    toolchain: Mapping[str, ToolInfo],
    work_dir: Path,
    timeout: float,
    row_index: int = 1,
    workspace_root: Path | None = None,
    manifest_path: Path | None = None,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    prepared: PreparedCandidateRow | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    _validate_max_output_bytes(max_output_bytes)
    row_work = work_dir / (
        f"{row_index:06d}_{_safe_name(row.candidate_id)}_"
        f"{hashlib.sha256(row.candidate_id.encode()).hexdigest()[:12]}"
    )
    row_work.mkdir(parents=True, exist_ok=True)
    if prepared is None:
        snapshot_root = row_work / "inputs"
        snapshot_root.mkdir(mode=0o700, parents=True, exist_ok=False)
        prepared = _prepare_candidate_row(
            row,
            snapshot_root,
            row_index,
            [0],
            DEFAULT_MAX_ARTIFACT_BYTES,
            DEFAULT_MAX_ROW_INPUT_BYTES,
            DEFAULT_MAX_RUN_INPUT_BYTES,
        )
    elif prepared.manifest_row is not row and prepared.manifest_row != row:
        raise CandidateVerificationPreflightError(
            "prepared candidate row does not match manifest row"
        )
    checks = _initial_checks(row.requested_checks)
    diagnostics: list[str] = []
    report = MismatchReport(row.simulation_result_contract, (), (), False)
    artifact_paths = _build_redaction_context(row, prepared)

    iverilog = toolchain["iverilog"].path
    vvp = toolchain["vvp"].path
    if iverilog is None:
        checks["compile"]["candidate"] = _unavailable()
        checks["simulation"]["candidate_passes"] = _unavailable()
    else:
        compile_status, diagnostic = _compile_design(
            iverilog,
            row,
            prepared,
            row_work / "candidate_compile.out",
            row_work,
            timeout,
            workspace_root,
            manifest_path,
            artifact_paths,
            max_output_bytes,
            environment,
        )
        checks["compile"]["candidate"] = compile_status
        if diagnostic:
            diagnostics.append(diagnostic)

        if compile_status["passed"] is not True:
            checks["simulation"]["candidate_passes"] = _unavailable(
                "compile_failure"
            )
        elif vvp is None:
            checks["simulation"]["candidate_passes"] = _unavailable()
        else:
            simulation_status, report, diagnostic = _simulate_design(
                iverilog,
                vvp,
                row,
                prepared,
                row_work / "candidate_simulation.out",
                row_work,
                timeout,
                workspace_root,
                manifest_path,
                artifact_paths,
                max_output_bytes,
                environment,
            )
            checks["simulation"]["candidate_passes"] = simulation_status
            if diagnostic:
                diagnostics.append(diagnostic)

    if row.requested_checks["lint"]:
        verilator = toolchain["verilator"].path
        if verilator is None:
            checks["lint"]["candidate"] = _unavailable()
        else:
            status, diagnostic = _lint_design(
                verilator,
                row,
                prepared,
                row_work,
                timeout,
                workspace_root,
                manifest_path,
                artifact_paths,
                max_output_bytes,
                environment,
            )
            checks["lint"]["candidate"] = status
            if diagnostic:
                diagnostics.append(diagnostic)

    if row.requested_checks["synthesis"]:
        yosys = toolchain["yosys"].path
        if yosys is None:
            checks["synthesis"]["candidate"] = _unavailable()
        else:
            status, diagnostic = _synthesize_design(
                yosys,
                row,
                prepared,
                row_work,
                timeout,
                workspace_root,
                manifest_path,
                artifact_paths,
                max_output_bytes,
                environment,
            )
            checks["synthesis"]["candidate"] = status
            if diagnostic:
                diagnostics.append(diagnostic)

    compile_passed = checks["compile"]["candidate"]["passed"] is True
    simulation_passed = checks["simulation"]["candidate_passes"]["passed"] is True
    accepted = compile_passed and simulation_passed
    evidence = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "candidate_id": row.candidate_id,
        "task_id": row.task_id,
        "source_id": row.source_id,
        "attempt": row.attempt,
        "top_module": row.top_module,
        "testbench_top": row.testbench_top,
        "simulation_result_contract": row.simulation_result_contract,
        "requested_checks": dict(row.requested_checks),
        "input_hashes": prepared.input_hashes,
        "toolchain": toolchain_json(toolchain),
        "checks": checks,
        "mismatch_summary": {
            "contract": report.contract,
            "reported_counts": list(report.reported_counts),
            "reported_sample_counts": list(report.reported_sample_counts),
            "maximum_count": report.maximum_count,
            "timeout_reported": report.timeout_reported,
        },
        "failure_category": _select_failure_category(checks, accepted),
        "accepted": accepted,
        "diagnostics": _dedupe_diagnostics(diagnostics),
    }
    return evidence


def _initial_checks(requested: Mapping[str, bool]) -> dict[str, Any]:
    return {
        "compile": {
            "candidate": _not_requested() if not requested["compile"] else _unavailable("pending")
        },
        "simulation": {
            "candidate_passes": _not_requested()
            if not requested["simulation"]
            else _unavailable("pending")
        },
        "lint": {
            "candidate": _not_requested() if not requested["lint"] else _unavailable("pending")
        },
        "synthesis": {
            "candidate": _not_requested()
            if not requested["synthesis"]
            else _unavailable("pending")
        },
    }


def _compile_design(
    iverilog: str,
    row: CandidateRow,
    prepared: PreparedCandidateRow,
    binary: Path,
    cwd: Path,
    timeout: float,
    workspace_root: Path | None,
    manifest_path: Path | None,
    artifact_paths: list[Path],
    max_output_bytes: int,
    environment: Mapping[str, str] | None,
) -> tuple[dict[str, Any], str | None]:
    command = [
        iverilog,
        "-g2012",
        "-s",
        row.top_module,
        "-o",
        str(binary),
        str(prepared.candidate_snapshot),
    ]
    command.extend(str(prepared.support_snapshots[path]) for path in row.support_files)
    result = _run(command, cwd, timeout, max_output_bytes, environment)
    if result.timed_out:
        return _failed("timeout"), _diagnostic(
            "compile", result, workspace_root, cwd, manifest_path, artifact_paths
        )
    if result.output_limit_exceeded or result.startup_error or result.returncode != 0:
        return _failed("compile_failure"), _diagnostic(
            "compile", result, workspace_root, cwd, manifest_path, artifact_paths
        )
    return _passed(), _diagnostic(
        "compile",
        result,
        workspace_root,
        cwd,
        manifest_path,
        artifact_paths,
        only_if_output=True,
    )


def _simulate_design(
    iverilog: str,
    vvp: str,
    row: CandidateRow,
    prepared: PreparedCandidateRow,
    binary: Path,
    cwd: Path,
    timeout: float,
    workspace_root: Path | None,
    manifest_path: Path | None,
    artifact_paths: list[Path],
    max_output_bytes: int,
    environment: Mapping[str, str] | None,
) -> tuple[dict[str, Any], MismatchReport, str | None]:
    compile_command = [
        iverilog,
        "-g2012",
        "-s",
        row.testbench_top,
        "-o",
        str(binary),
        str(prepared.candidate_snapshot),
    ]
    compile_command.extend(str(prepared.support_snapshots[path]) for path in row.support_files)
    compile_command.append(str(prepared.testbench_snapshot))
    compiled = _run(compile_command, cwd, timeout, max_output_bytes, environment)
    if compiled.timed_out:
        return (
            _failed("timeout"),
            MismatchReport(row.simulation_result_contract, (), (), False),
            _diagnostic(
                "simulation compile",
                compiled,
                workspace_root,
                cwd,
                manifest_path,
                artifact_paths,
            ),
        )
    if compiled.output_limit_exceeded or compiled.startup_error or compiled.returncode != 0:
        return (
            _failed("compile_failure"),
            MismatchReport(row.simulation_result_contract, (), (), False),
            _diagnostic(
                "simulation compile",
                compiled,
                workspace_root,
                cwd,
                manifest_path,
                artifact_paths,
            ),
        )

    simulated = _run([vvp, str(binary)], cwd, timeout, max_output_bytes, environment)
    report = _parse_mismatch_report(simulated.output, row.simulation_result_contract)
    if simulated.timed_out:
        return (
            _failed("timeout"),
            report,
            _diagnostic(
                "simulation",
                simulated,
                workspace_root,
                cwd,
                manifest_path,
                artifact_paths,
            ),
        )
    if report.timeout_reported:
        return (
            _failed("timeout"),
            report,
            _diagnostic(
                "simulation",
                simulated,
                workspace_root,
                cwd,
                manifest_path,
                artifact_paths,
            ),
        )
    if simulated.output_limit_exceeded or simulated.startup_error:
        return (
            _failed("simulation_failure"),
            report,
            _diagnostic(
                "simulation",
                simulated,
                workspace_root,
                cwd,
                manifest_path,
                artifact_paths,
            ),
        )
    if row.simulation_result_contract == "mismatch_count_v1" and not report.has_report:
        return (
            _failed("simulation_result_missing"),
            report,
            _diagnostic(
                "simulation",
                simulated,
                workspace_root,
                cwd,
                manifest_path,
                artifact_paths,
            ),
        )
    if report.has_positive_count:
        return (
            _failed("functional_mismatch"),
            report,
            _diagnostic(
                "simulation",
                simulated,
                workspace_root,
                cwd,
                manifest_path,
                artifact_paths,
            ),
        )
    if simulated.returncode == 0:
        return _passed(), report, None
    return (
        _failed("simulation_failure"),
        report,
        _diagnostic(
            "simulation",
            simulated,
            workspace_root,
            cwd,
            manifest_path,
            artifact_paths,
        ),
    )


def _lint_design(
    verilator: str,
    row: CandidateRow,
    prepared: PreparedCandidateRow,
    cwd: Path,
    timeout: float,
    workspace_root: Path | None,
    manifest_path: Path | None,
    artifact_paths: list[Path],
    max_output_bytes: int,
    environment: Mapping[str, str] | None,
) -> tuple[dict[str, Any], str | None]:
    command = [
        verilator,
        "--lint-only",
        "--timing",
        "--top-module",
        row.top_module,
        str(prepared.candidate_snapshot),
    ]
    command.extend(str(prepared.support_snapshots[path]) for path in row.support_files)
    result = _run(command, cwd, timeout, max_output_bytes, environment)
    if result.timed_out:
        return _failed("timeout"), _diagnostic(
            "lint", result, workspace_root, cwd, manifest_path, artifact_paths
        )
    if result.output_limit_exceeded or result.startup_error or result.returncode != 0:
        return _failed("lint_failure"), _diagnostic(
            "lint", result, workspace_root, cwd, manifest_path, artifact_paths
        )
    return _passed(), _diagnostic(
        "lint",
        result,
        workspace_root,
        cwd,
        manifest_path,
        artifact_paths,
        only_if_output=True,
    )


def _synthesize_design(
    yosys: str,
    row: CandidateRow,
    prepared: PreparedCandidateRow,
    cwd: Path,
    timeout: float,
    workspace_root: Path | None,
    manifest_path: Path | None,
    artifact_paths: list[Path],
    max_output_bytes: int,
    environment: Mapping[str, str] | None,
) -> tuple[dict[str, Any], str | None]:
    script = cwd / "candidate_synthesis.ys"
    files = [
        prepared.candidate_snapshot,
        *(prepared.support_snapshots[path] for path in row.support_files),
    ]
    script.write_text(
        "\n".join(
            [f"read_verilog -sv {_yosys_quote(path)}" for path in files]
            + [f"hierarchy -top {row.top_module}", "proc", "opt", "stat"]
        )
        + "\n",
        encoding="utf-8",
    )
    result = _run([yosys, str(script)], cwd, timeout, max_output_bytes, environment)
    if result.timed_out:
        return _failed("timeout"), _diagnostic(
            "synthesis", result, workspace_root, cwd, manifest_path, artifact_paths
        )
    if result.output_limit_exceeded or result.startup_error or result.returncode != 0:
        return _failed("synthesis_failure"), _diagnostic(
            "synthesis", result, workspace_root, cwd, manifest_path, artifact_paths
        )
    return _passed(), _diagnostic(
        "synthesis",
        result,
        workspace_root,
        cwd,
        manifest_path,
        artifact_paths,
        only_if_output=True,
    )


def _run(
    command: list[str],
    cwd: Path,
    timeout: float,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    environment: Mapping[str, str] | None = None,
) -> CommandResult:
    _validate_max_output_bytes(max_output_bytes)
    child_environment = _minimal_environment(cwd, environment, command)
    try:
        process_kwargs: dict[str, Any] = {
            "cwd": cwd,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "env": child_environment,
            "shell": False,
        }
        if os.name == "posix":
            process_kwargs["start_new_session"] = True
        elif os.name == "nt":
            process_kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        process = subprocess.Popen(command, **process_kwargs)
    except OSError as exc:
        return CommandResult(None, str(exc), startup_error=True)

    output_queue: queue.Queue[bytes] = queue.Queue(maxsize=16)
    reader_done = threading.Event()

    def drain_output() -> None:
        try:
            if process.stdout is None:
                return
            while True:
                chunk = process.stdout.read(8192)
                if not chunk:
                    return
                output_queue.put(chunk)
        finally:
            reader_done.set()

    reader = threading.Thread(target=drain_output, name="rtlbench-output-reader", daemon=True)
    reader.start()
    output = bytearray()
    timed_out = False
    output_limit_exceeded = False
    deadline = time.monotonic() + timeout

    def collect(chunk: bytes) -> None:
        nonlocal output_limit_exceeded
        remaining = max_output_bytes - len(output)
        if len(chunk) > remaining:
            output_limit_exceeded = True
            if remaining > 0:
                output.extend(chunk[:remaining])
        else:
            output.extend(chunk)

    try:
        while True:
            if process.poll() is not None and reader_done.is_set() and output_queue.empty():
                break
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                timed_out = True
                _terminate_process_tree(process)
                break
            try:
                chunk = output_queue.get(timeout=min(remaining_time, 0.05))
            except queue.Empty:
                continue
            collect(chunk)
            if output_limit_exceeded:
                _terminate_process_tree(process)
                break

        drain_deadline = time.monotonic() + 1.0
        while time.monotonic() < drain_deadline and (
            not reader_done.is_set() or not output_queue.empty()
        ):
            try:
                collect(output_queue.get(timeout=0.05))
            except queue.Empty:
                pass
        try:
            returncode = process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(process)
            returncode = process.wait(timeout=1.0)
    except KeyboardInterrupt:
        _terminate_process_tree(process)
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
        reader.join(timeout=_READER_JOIN_SECONDS)
    return CommandResult(
        returncode,
        bytes(output).decode("utf-8", errors="replace"),
        timed_out=timed_out,
        output_limit_exceeded=output_limit_exceeded,
    )


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Terminate a verifier-owned process group without touching our group."""

    if os.name == "posix":
        for termination_signal in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, termination_signal)
            except ProcessLookupError:
                pass
            except OSError:
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass
            if termination_signal == signal.SIGTERM:
                deadline = time.monotonic() + _PROCESS_GROUP_GRACE_SECONDS
                while process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
        return

    if os.name == "nt":
        try:
            process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
        except (OSError, ValueError):
            pass
        deadline = time.monotonic() + _PROCESS_GROUP_GRACE_SECONDS
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        return

    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def _minimal_environment(
    cwd: Path,
    extra: Mapping[str, str] | None = None,
    command: list[str] | None = None,
) -> dict[str, str]:
    path_entries: list[str] = []
    for raw_path in (os.defpath, os.environ.get("PATH", "")):
        for entry in raw_path.split(os.pathsep):
            if entry and os.path.isabs(entry):
                path_entries.append(entry)
    if command:
        executable = Path(command[0])
        if executable.is_absolute():
            path_entries.insert(0, str(executable.parent))
    path_entries = list(dict.fromkeys(path_entries))
    environment = {
        "PATH": os.pathsep.join(path_entries),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(cwd),
        "TEMP": str(cwd),
        "TMP": str(cwd),
        "HOME": str(cwd),
    }
    if os.name == "nt" and os.environ.get("SystemRoot"):
        environment["SystemRoot"] = os.environ["SystemRoot"]
    if extra:
        # ``extra`` is intentionally an explicit test hook.  Preserve the
        # scratch-directed values even if a caller accidentally supplies them.
        for key, value in extra.items():
            key = str(key)
            if key not in {"PATH", "TMPDIR", "TEMP", "TMP", "HOME", "SystemRoot"}:
                environment[key] = str(value)
    return environment


def _capture_version(
    path: str,
    args: tuple[str, ...],
    timeout: float,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> str | None:
    probe_cwd = cwd or Path.cwd()
    result = _run(
        [path, *args],
        probe_cwd,
        timeout,
        max_output_bytes,
        environment,
    )
    if result.timed_out or result.startup_error or result.returncode != 0:
        return None
    return sanitize_diagnostic(" ".join(result.output.split()), limit=512) or None


def _parse_mismatch_report(output: str, contract: str) -> MismatchReport:
    matches: list[tuple[int, int, int | None]] = []
    for match in _MISMATCH_LONG_RE.finditer(output):
        matches.append((match.start(), int(match.group(1)), int(match.group(2))))
    for match in _MISMATCH_SHORT_RE.finditer(output):
        matches.append((match.start(), int(match.group(1)), None))
    matches.sort(key=lambda item: item[0])
    return MismatchReport(
        contract,
        tuple(item[1] for item in matches),
        tuple(item[2] for item in matches),
        bool(_TIMEOUT_RE.search(output)),
    )


def _validate_max_output_bytes(value: int) -> None:
    if type(value) is not int or not 0 < value <= MAX_OUTPUT_BYTES_LIMIT:
        raise CandidateManifestValidationError(
            f"max-output-bytes must be between 1 and {MAX_OUTPUT_BYTES_LIMIT}"
        )


def _build_redaction_context(
    row: CandidateRow,
    prepared: PreparedCandidateRow | None = None,
) -> DiagnosticRedactionContext:
    logical_paths = [
        row.candidate_rtl_path,
        row.testbench_path,
        *row.support_files,
    ]
    original_paths = [path for _, path in row.artifact_paths]
    source_paths = (
        [
            prepared.candidate_snapshot,
            prepared.testbench_snapshot,
            *prepared.support_snapshots.values(),
        ]
        if prepared is not None
        else original_paths
    )
    context = _build_redaction_context_from_paths(
        [*logical_paths, *(str(path) for path in original_paths), *(str(path) for path in source_paths)],
        source_paths,
    )
    return context


def _build_redaction_context_from_paths(
    path_strings: list[str],
    source_paths: list[Path],
) -> DiagnosticRedactionContext:
    source_lines: set[str] = set()
    long_tokens: set[str] = set()
    conservative = False
    for source_path in source_paths:
        try:
            descriptor = _open_regular_source(source_path)
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                descriptor = -1
                line_count = 0
                scanned_bytes = 0
                pending = bytearray()
                while True:
                    chunk = handle.read(_COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    scanned_bytes += len(chunk)
                    if scanned_bytes > _MAX_DIAGNOSTIC_FALLBACK_BYTES:
                        conservative = True
                        break
                    pending.extend(chunk)
                    while True:
                        newline = pending.find(b"\n")
                        if newline < 0:
                            if len(pending) > _MAX_DIAGNOSTIC_INDEX_LINE_BYTES:
                                conservative = True
                                pending.clear()
                            break
                        raw_line = bytes(pending[:newline]).rstrip(b"\r")
                        del pending[: newline + 1]
                        line_count += 1
                        if line_count > _MAX_DIAGNOSTIC_INDEX_LINES:
                            conservative = True
                            pending.clear()
                            break
                        if len(raw_line) > _MAX_DIAGNOSTIC_INDEX_LINE_BYTES:
                            conservative = True
                            continue
                        line = raw_line.decode("utf-8", errors="replace")
                        if line:
                            source_lines.add(line)
                            for token in re.findall(r"\S{2,}", line):
                                if len(long_tokens) >= _MAX_DIAGNOSTIC_INDEX_TOKENS:
                                    conservative = True
                                    break
                                long_tokens.add(token)
                    if conservative and line_count > _MAX_DIAGNOSTIC_INDEX_LINES:
                        break
                if pending and len(pending) <= _MAX_DIAGNOSTIC_INDEX_LINE_BYTES:
                    line = bytes(pending).rstrip(b"\r").decode(
                        "utf-8", errors="replace"
                    )
                    if line:
                        source_lines.add(line)
                        for token in re.findall(r"\S{2,}", line):
                            if len(long_tokens) >= _MAX_DIAGNOSTIC_INDEX_TOKENS:
                                conservative = True
                                break
                            long_tokens.add(token)
        except (OSError, CandidateVerificationPreflightError):
            conservative = True
    return DiagnosticRedactionContext(
        tuple(dict.fromkeys(path_strings)),
        frozenset(source_lines),
        frozenset(long_tokens),
        conservative,
    )


def _diagnostic(
    stage: str,
    result: CommandResult,
    workspace_root: Path | None,
    work_dir: Path,
    manifest_path: Path | None,
    artifact_paths: list[Path] | DiagnosticRedactionContext,
    *,
    only_if_output: bool = False,
) -> str | None:
    if only_if_output and not result.output.strip() and not result.output_limit_exceeded:
        return None
    redaction_context = (
        artifact_paths
        if isinstance(artifact_paths, DiagnosticRedactionContext)
        else _build_redaction_context_from_paths(
            [str(path) for path in artifact_paths], artifact_paths
        )
    )
    include_output = stage != "simulation" and not redaction_context.conservative
    if result.output_limit_exceeded:
        message = f"{stage}: output_limit_exceeded returncode={result.returncode}"
        if include_output:
            message += f" {result.output}"
    elif result.timed_out:
        message = f"{stage}: timeout=true returncode={result.returncode}"
    elif result.startup_error:
        message = f"{stage}: process_start_failed returncode={result.returncode}"
    else:
        message = f"{stage}: returncode={result.returncode}"
        if include_output:
            message += f" {result.output}"
    return _sanitize_candidate_diagnostic(
        message,
        workspace_root=workspace_root,
        work_dir=work_dir,
        manifest_path=manifest_path,
        artifact_paths=[],
        redaction_context=redaction_context,
    )


def _sanitize_candidate_diagnostic(
    value: str,
    *,
    workspace_root: Path | None,
    work_dir: Path | None,
    manifest_path: Path | None,
    artifact_paths: list[Path],
    redaction_context: DiagnosticRedactionContext | None = None,
) -> str:
    text = str(value).replace("\x00", "")
    context = redaction_context or _build_redaction_context_from_paths(
        [str(path) for path in artifact_paths], artifact_paths
    )
    for secret in sorted(context.path_strings, key=len, reverse=True):
        text = text.replace(secret, "<source omitted>")
    for secret in sorted(context.source_lines, key=len, reverse=True):
        text = text.replace(secret, "<source omitted>")
    for secret in sorted(context.long_tokens, key=len, reverse=True):
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", secret):
            text = re.sub(
                rf"(?<![A-Za-z0-9_$]){re.escape(secret)}(?![A-Za-z0-9_$])",
                "<source omitted>",
                text,
            )
        else:
            text = text.replace(secret, "<source omitted>")
    text = _MANAGED_RUN_RE.sub("<work>", text)
    text = re.sub(
        r"(?i)\b(?:authorization\s*:\s*bearer|bearer)\s+[^\s,;]+",
        "authorization=<redacted>",
        text,
    )
    return sanitize_diagnostic(
        text,
        workspace_root=workspace_root,
        work_dir=work_dir,
        manifest_path=manifest_path,
        limit=4096,
    )


def _select_failure_category(checks: Mapping[str, Any], accepted: bool) -> str:
    if accepted:
        return "passed"
    required = [
        checks["compile"]["candidate"],
        checks["simulation"]["candidate_passes"],
    ]
    for category in (
        "timeout",
        "compile_failure",
        "functional_mismatch",
        "simulation_result_missing",
        "simulation_failure",
        "tool_unavailable",
        "internal_error",
    ):
        if any(status.get("reason") == category for status in required):
            return category
    return "partial_failure"


def _internal_evidence(
    row: CandidateRow,
    input_hashes: dict[str, Any],
    toolchain: Mapping[str, ToolInfo],
    error: str,
    workspace_root: Path,
    work_dir: Path,
    manifest_path: Path | None = None,
    prepared: PreparedCandidateRow | None = None,
) -> dict[str, Any]:
    checks = _initial_checks(row.requested_checks)
    checks["compile"]["candidate"] = _failed("internal_error")
    checks["simulation"]["candidate_passes"] = _unavailable("internal_error")
    for check_name in ("lint", "synthesis"):
        if row.requested_checks[check_name]:
            checks[check_name]["candidate"] = _unavailable("internal_error")
    redaction_context = (
        _build_redaction_context(row, prepared)
        if prepared is not None
        else None
    )
    diagnostic = _sanitize_candidate_diagnostic(
        f"internal row error: {error}",
        workspace_root=workspace_root,
        work_dir=work_dir,
        manifest_path=manifest_path,
        artifact_paths=[path for _, path in row.artifact_paths],
        redaction_context=redaction_context,
    )
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "candidate_id": row.candidate_id,
        "task_id": row.task_id,
        "source_id": row.source_id,
        "attempt": row.attempt,
        "top_module": row.top_module,
        "testbench_top": row.testbench_top,
        "simulation_result_contract": row.simulation_result_contract,
        "requested_checks": dict(row.requested_checks),
        "input_hashes": prepared.input_hashes,
        "toolchain": toolchain_json(toolchain),
        "checks": checks,
        "mismatch_summary": {
            "contract": row.simulation_result_contract,
            "reported_counts": [],
            "reported_sample_counts": [],
            "maximum_count": None,
            "timeout_reported": False,
        },
        "failure_category": "internal_error",
        "accepted": False,
        "diagnostics": [diagnostic] if diagnostic else [],
    }


def _validate_output_paths(output: Path, force: bool) -> tuple[Path, Path]:
    requested = output.expanduser()
    if _contains_symlink(requested):
        raise CandidateVerificationPreflightError("output path must not contain symlinks")
    final = requested.resolve()
    if not final.parent.exists() or not final.parent.is_dir():
        raise CandidateVerificationPreflightError(
            f"output parent is not a directory: {final.parent}"
        )
    if final.exists() and final.is_dir():
        raise CandidateVerificationPreflightError(f"output is a directory: {final}")
    partial = Path(str(final) + ".rtlbench-partial")
    if partial.is_symlink():
        raise CandidateVerificationPreflightError(
            "managed partial output must not be a symlink"
        )
    if partial.exists() and partial.is_dir():
        raise CandidateVerificationPreflightError(
            f"managed partial output is a directory: {partial}"
        )
    if final.exists() and partial.exists() and _paths_alias(final, partial):
        raise CandidateVerificationPreflightError(
            "output and managed partial output must not alias"
        )
    for label, path in (("output", final), ("managed partial output", partial)):
        if path.exists() and path.stat().st_nlink > 1:
            raise CandidateVerificationPreflightError(
                f"{label} must not be a hard-link alias: {path}"
            )
    if (final.exists() or partial.exists()) and not force:
        raise CandidateVerificationPreflightError(
            f"output already exists; use --force only for {final} and its managed partial"
        )
    return final, partial


def _reject_output_input_collisions(
    final: Path,
    partial: Path,
    manifest_path: Path,
    rows: list[CandidateRow],
) -> None:
    protected: list[tuple[str, Path]] = [
        ("manifest", manifest_path.expanduser().resolve())
    ]
    for row in rows:
        protected.extend(
            (f"{row.candidate_id}:{label}", path)
            for label, path in row.artifact_paths
        )
    for candidate_label, candidate in (
        ("output", final),
        ("managed partial output", partial),
    ):
        for protected_label, protected_path in protected:
            if _paths_alias(candidate, protected_path):
                raise CandidateVerificationPreflightError(
                    f"{candidate_label} aliases protected input {protected_label}: {candidate}"
                )


def _reject_work_dir_input_collisions(
    work_dir: Path,
    manifest_path: Path,
    rows: list[CandidateRow],
) -> None:
    candidates = [("manifest", manifest_path.expanduser().resolve())]
    for row in rows:
        candidates.extend(
            (f"{row.candidate_id}:{label}", path)
            for label, path in row.artifact_paths
        )
    for label, protected in candidates:
        if _paths_alias(work_dir.expanduser(), protected):
            raise CandidateVerificationPreflightError(
                f"work-dir aliases protected input {label}: {work_dir}"
            )


def _reject_output_snapshot_collisions(
    final: Path,
    partial: Path,
    prepared_rows: list[PreparedCandidateRow],
) -> None:
    snapshots: list[tuple[str, Path]] = []
    for prepared in prepared_rows:
        row = prepared.manifest_row
        snapshots.append((f"{row.candidate_id}:candidate_snapshot", prepared.candidate_snapshot))
        snapshots.append((f"{row.candidate_id}:testbench_snapshot", prepared.testbench_snapshot))
        snapshots.extend(
            (f"{row.candidate_id}:support_snapshot:{logical}", path)
            for logical, path in prepared.support_snapshots.items()
        )
    for output_label, output_path in (("output", final), ("managed partial output", partial)):
        for snapshot_label, snapshot_path in snapshots:
            if _paths_alias(output_path, snapshot_path):
                raise CandidateVerificationPreflightError(
                    f"{output_label} aliases snapshot {snapshot_label}: {output_path}"
                )


def _paths_alias(left: Path, right: Path) -> bool:
    if left.expanduser().resolve() == right.expanduser().resolve():
        return True
    if left.exists() and right.exists():
        try:
            return left.samefile(right)
        except OSError:
            return False
    return False


def _prepare_work_dir(work_dir: Path) -> Path:
    path = work_dir.expanduser()
    if _contains_symlink(path):
        raise CandidateVerificationPreflightError(
            "work-dir must not contain symlinked path components"
        )
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CandidateVerificationPreflightError(
            f"could not create work-dir: {exc}"
        ) from exc
    if not path.is_dir():
        raise CandidateVerificationPreflightError(f"work-dir is not a directory: {path}")
    try:
        return Path(tempfile.mkdtemp(prefix=".rtlbench-run-", dir=str(path.resolve())))
    except OSError as exc:
        raise CandidateVerificationPreflightError(
            f"could not create managed run directory: {exc}"
        ) from exc


def _contains_symlink(path: Path) -> bool:
    absolute = path if path.is_absolute() else path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return False
    return False


def _yosys_quote(path: Path) -> str:
    return '"' + str(path).replace("\\", "/").replace('"', '\\"') + '"'


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "row"


def _passed() -> dict[str, Any]:
    return {"attempted": True, "passed": True, "reason": None}


def _failed(reason: str) -> dict[str, Any]:
    return {"attempted": True, "passed": False, "reason": reason}


def _unavailable(reason: str = "tool_unavailable") -> dict[str, Any]:
    return {"attempted": False, "passed": None, "reason": reason}


def _not_requested() -> dict[str, Any]:
    return _unavailable("not_requested")


def _dedupe_diagnostics(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def deterministic_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
