"""Deterministic local verification of one RTL candidate per manifest row."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
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
_MISMATCH_COUNT_RE = re.compile(
    r"(?im)^[ \t]*mismatches[ \t]*:[ \t]*([0-9]+)[ \t]*\r?$"
)
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


@dataclass(frozen=True)
class MismatchReport:
    counts: tuple[int, ...]

    @property
    def has_positive_count(self) -> bool:
        return any(count > 0 for count in self.counts)


def discover_toolchain(
    timeout: float = 5.0,
    which: Callable[[str], str | None] | None = None,
) -> dict[str, ToolInfo]:
    """Discover and version each executable once for a verification run."""

    resolver = which or shutil.which
    result: dict[str, ToolInfo] = {}
    for name in _TOOL_NAMES:
        path = resolver(name)
        version = _capture_version(path, TOOL_VERSION_ARGS[name], timeout) if path else None
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


def verify_candidates(
    *,
    manifest: Path,
    output: Path,
    workspace_root: Path,
    work_dir: Path,
    timeout: float = 30.0,
    force: bool = False,
) -> dict[str, int]:
    """Validate, execute, and atomically publish deterministic candidate evidence."""

    if timeout <= 0:
        raise CandidateManifestValidationError("timeout must be greater than zero")
    try:
        rows = load_manifest(Path(manifest), Path(workspace_root))
    except KeyboardInterrupt as exc:
        raise CandidateVerificationInterrupted("interrupted during manifest validation") from exc
    final_path, partial_path = _validate_output_paths(Path(output), force)
    _reject_output_input_collisions(final_path, partial_path, Path(manifest), rows)
    scratch = _prepare_work_dir(Path(work_dir))

    try:
        input_hashes = [hash_inputs(row) for row in rows]
    except KeyboardInterrupt as exc:
        raise CandidateVerificationInterrupted("interrupted during input hashing") from exc
    except (OSError, ValueError) as exc:
        raise CandidateVerificationPreflightError(
            f"could not hash manifest artifacts: {exc}"
        ) from exc

    try:
        with partial_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.flush()
    except OSError as exc:
        raise CandidateVerificationPreflightError(
            f"could not prepare evidence output: {exc}"
        ) from exc

    try:
        toolchain = discover_toolchain(timeout=timeout)
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
            for index, (row, hashes) in enumerate(zip(rows, input_hashes), 1):
                try:
                    evidence = verify_row(
                        row,
                        input_hashes=hashes,
                        toolchain=toolchain,
                        work_dir=scratch,
                        timeout=timeout,
                        row_index=index,
                        workspace_root=Path(workspace_root),
                        manifest_path=Path(manifest),
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    evidence = _internal_evidence(
                        row,
                        hashes,
                        toolchain,
                        str(exc),
                        Path(workspace_root),
                        scratch,
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
) -> dict[str, Any]:
    row_work = work_dir / (
        f"{row_index:06d}_{_safe_name(row.candidate_id)}_"
        f"{hashlib.sha256(row.candidate_id.encode()).hexdigest()[:12]}"
    )
    row_work.mkdir(parents=True, exist_ok=True)
    checks = _initial_checks(row.requested_checks)
    diagnostics: list[str] = []
    mismatch_counts: list[int] = []
    artifact_paths = [path for _, path in row.artifact_paths]

    iverilog = toolchain["iverilog"].path
    vvp = toolchain["vvp"].path
    if iverilog is None:
        checks["compile"]["candidate"] = _unavailable()
        checks["simulation"]["candidate_passes"] = _unavailable()
    else:
        compile_status, diagnostic = _compile_design(
            iverilog,
            row,
            row_work / "candidate_compile.out",
            row_work,
            timeout,
            workspace_root,
            manifest_path,
            artifact_paths,
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
                row_work / "candidate_simulation.out",
                row_work,
                timeout,
                workspace_root,
                manifest_path,
                artifact_paths,
            )
            checks["simulation"]["candidate_passes"] = simulation_status
            mismatch_counts.extend(report.counts)
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
                row_work,
                timeout,
                workspace_root,
                manifest_path,
                artifact_paths,
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
                row_work,
                timeout,
                workspace_root,
                manifest_path,
                artifact_paths,
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
        "requested_checks": dict(row.requested_checks),
        "input_hashes": input_hashes,
        "toolchain": toolchain_json(toolchain),
        "checks": checks,
        "mismatch_summary": {
            "reported_counts": mismatch_counts,
            "maximum_count": max(mismatch_counts) if mismatch_counts else None,
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
    binary: Path,
    cwd: Path,
    timeout: float,
    workspace_root: Path | None,
    manifest_path: Path | None,
    artifact_paths: list[Path],
) -> tuple[dict[str, Any], str | None]:
    command = [
        iverilog,
        "-g2012",
        "-s",
        row.top_module,
        "-o",
        str(binary),
        str(row.resolved_paths["candidate_rtl_path"]),
    ]
    command.extend(str(row.resolved_support_paths[path]) for path in row.support_files)
    result = _run(command, cwd, timeout)
    if result.timed_out:
        return _failed("timeout"), _diagnostic(
            "compile", result, workspace_root, cwd, manifest_path, artifact_paths
        )
    if result.startup_error or result.returncode != 0:
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
    binary: Path,
    cwd: Path,
    timeout: float,
    workspace_root: Path | None,
    manifest_path: Path | None,
    artifact_paths: list[Path],
) -> tuple[dict[str, Any], MismatchReport, str | None]:
    compile_command = [
        iverilog,
        "-g2012",
        "-o",
        str(binary),
        str(row.resolved_paths["candidate_rtl_path"]),
    ]
    compile_command.extend(str(row.resolved_support_paths[path]) for path in row.support_files)
    compile_command.append(str(row.resolved_paths["testbench_path"]))
    compiled = _run(compile_command, cwd, timeout)
    if compiled.timed_out:
        return (
            _failed("timeout"),
            MismatchReport(()),
            _diagnostic(
                "simulation compile",
                compiled,
                workspace_root,
                cwd,
                manifest_path,
                artifact_paths,
            ),
        )
    if compiled.startup_error or compiled.returncode != 0:
        return (
            _failed("compile_failure"),
            MismatchReport(()),
            _diagnostic(
                "simulation compile",
                compiled,
                workspace_root,
                cwd,
                manifest_path,
                artifact_paths,
            ),
        )

    simulated = _run([vvp, str(binary)], cwd, timeout)
    report = _parse_mismatch_report(simulated.output)
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
    if simulated.startup_error:
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
    cwd: Path,
    timeout: float,
    workspace_root: Path | None,
    manifest_path: Path | None,
    artifact_paths: list[Path],
) -> tuple[dict[str, Any], str | None]:
    command = [
        verilator,
        "--lint-only",
        "--timing",
        "--top-module",
        row.top_module,
        str(row.resolved_paths["candidate_rtl_path"]),
    ]
    command.extend(str(row.resolved_support_paths[path]) for path in row.support_files)
    result = _run(command, cwd, timeout)
    if result.timed_out:
        return _failed("timeout"), _diagnostic(
            "lint", result, workspace_root, cwd, manifest_path, artifact_paths
        )
    if result.startup_error or result.returncode != 0:
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
    cwd: Path,
    timeout: float,
    workspace_root: Path | None,
    manifest_path: Path | None,
    artifact_paths: list[Path],
) -> tuple[dict[str, Any], str | None]:
    script = cwd / "candidate_synthesis.ys"
    files = [
        row.resolved_paths["candidate_rtl_path"],
        *(row.resolved_support_paths[path] for path in row.support_files),
    ]
    script.write_text(
        "\n".join(
            [f"read_verilog -sv {_yosys_quote(path)}" for path in files]
            + [f"hierarchy -top {row.top_module}", "proc", "opt", "stat"]
        )
        + "\n",
        encoding="utf-8",
    )
    result = _run([yosys, str(script)], cwd, timeout)
    if result.timed_out:
        return _failed("timeout"), _diagnostic(
            "synthesis", result, workspace_root, cwd, manifest_path, artifact_paths
        )
    if result.startup_error or result.returncode != 0:
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


def _run(command: list[str], cwd: Path, timeout: float) -> CommandResult:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            None,
            f"{exc.stdout or ''}\n{exc.stderr or ''}",
            timed_out=True,
        )
    except OSError as exc:
        return CommandResult(None, str(exc), startup_error=True)
    return CommandResult(completed.returncode, f"{completed.stdout}\n{completed.stderr}")


def _capture_version(path: str, args: tuple[str, ...], timeout: float) -> str | None:
    result = _run([path, *args], Path.cwd(), timeout)
    if result.timed_out or result.startup_error or result.returncode != 0:
        return None
    return sanitize_diagnostic(" ".join(result.output.split()), limit=512) or None


def _parse_mismatch_report(output: str) -> MismatchReport:
    return MismatchReport(tuple(int(value) for value in _MISMATCH_COUNT_RE.findall(output)))


def _diagnostic(
    stage: str,
    result: CommandResult,
    workspace_root: Path | None,
    work_dir: Path,
    manifest_path: Path | None,
    artifact_paths: list[Path],
    *,
    only_if_output: bool = False,
) -> str | None:
    if only_if_output and not result.output.strip():
        return None
    if result.timed_out:
        message = f"{stage}: timeout"
    elif result.startup_error:
        message = f"{stage}: process_start_failed"
    else:
        message = f"{stage}: returncode={result.returncode} {result.output}"
    return _sanitize_candidate_diagnostic(
        message,
        workspace_root=workspace_root,
        work_dir=work_dir,
        manifest_path=manifest_path,
        artifact_paths=artifact_paths,
    )


def _sanitize_candidate_diagnostic(
    value: str,
    *,
    workspace_root: Path | None,
    work_dir: Path | None,
    manifest_path: Path | None,
    artifact_paths: list[Path],
) -> str:
    text = str(value).replace("\x00", "")
    redactions: list[str] = []
    for path in artifact_paths:
        redactions.extend((str(path), str(path.resolve())))
        try:
            content = path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            content = ""
        if content:
            redactions.append(content)
            redactions.extend(line for line in content.splitlines() if line.strip())
            redactions.extend(
                token
                for token in re.findall(r"[A-Za-z_][A-Za-z0-9_$]{15,}", content)
            )
    for secret in sorted(set(redactions), key=len, reverse=True):
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
) -> dict[str, Any]:
    checks = _initial_checks(row.requested_checks)
    checks["compile"]["candidate"] = _failed("internal_error")
    checks["simulation"]["candidate_passes"] = _unavailable("internal_error")
    for check_name in ("lint", "synthesis"):
        if row.requested_checks[check_name]:
            checks[check_name]["candidate"] = _unavailable("internal_error")
    diagnostic = _sanitize_candidate_diagnostic(
        f"internal row error: {error}",
        workspace_root=workspace_root,
        work_dir=work_dir,
        manifest_path=None,
        artifact_paths=[path for _, path in row.artifact_paths],
    )
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "candidate_id": row.candidate_id,
        "task_id": row.task_id,
        "source_id": row.source_id,
        "attempt": row.attempt,
        "top_module": row.top_module,
        "requested_checks": dict(row.requested_checks),
        "input_hashes": input_hashes,
        "toolchain": toolchain_json(toolchain),
        "checks": checks,
        "mismatch_summary": {"reported_counts": [], "maximum_count": None},
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
