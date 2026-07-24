"""Executable, local verification of original/mutated/repaired RTL rows."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from rtlbench.evidence import (
    EVIDENCE_SCHEMA_VERSION,
    compute_evidence_tier,
    deterministic_json,
    failed,
    not_requested,
    passed,
    sanitize_diagnostic,
    select_failure_category,
    unavailable,
)
from rtlbench.mutation_manifest import ManifestValidationError, MutationRow, load_manifest


class VerificationPreflightError(ValueError):
    """A filesystem or output preflight error (CLI status 3)."""


class VerificationInterrupted(RuntimeError):
    """Raised after an interrupted run leaves its managed partial output."""


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


TOOL_VERSION_ARGS = {
    "iverilog": ("-V",),
    "vvp": ("-V",),
    "verilator": ("--version",),
    "yosys": ("--version",),
}
_MISMATCH_RE = re.compile(r"mismatches?\s*[:=]\s*(\d+)", re.IGNORECASE)
_FAILURE_RE = re.compile(r"\b(?:fail(?:ed|ure)?|error|fatal|mismatch)\b", re.IGNORECASE)


def discover_toolchain(timeout: float = 5.0, which: Callable[[str], str | None] | None = None) -> dict[str, ToolInfo]:
    resolver = which or shutil.which
    result: dict[str, ToolInfo] = {}
    for name in ("iverilog", "vvp", "verilator", "yosys"):
        path = resolver(name)
        version = _capture_version(path, TOOL_VERSION_ARGS[name], timeout) if path else None
        result[name] = ToolInfo(path, version)
    return result


def toolchain_json(toolchain: Mapping[str, ToolInfo]) -> dict[str, Any]:
    return {name: toolchain[name].to_dict() for name in ("iverilog", "vvp", "verilator", "yosys")}


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def hash_inputs(row: MutationRow) -> dict[str, Any]:
    support = [
        {"path": path, "sha256": sha256_file(row.resolved_paths[path])}
        for path in sorted(row.support_files)
    ]
    return {
        "original_sha256": sha256_file(row.resolved_paths["original_rtl_path"]),
        "mutated_sha256": sha256_file(row.resolved_paths["mutated_rtl_path"]),
        "repaired_sha256": sha256_file(row.resolved_paths["repaired_rtl_path"]),
        "testbench_sha256": sha256_file(row.resolved_paths["testbench_path"]),
        "support_files": support,
    }


def verify_mutations(
    *,
    manifest: Path,
    output: Path,
    workspace_root: Path,
    work_dir: Path,
    timeout: float = 30.0,
    force: bool = False,
) -> dict[str, int]:
    """Run a complete preflight and atomically publish deterministic JSONL."""

    if timeout <= 0:
        raise VerificationPreflightError("timeout must be greater than zero")
    try:
        rows = load_manifest(Path(manifest), Path(workspace_root))
    except ManifestValidationError:
        raise
    except OSError as exc:
        raise VerificationPreflightError(f"could not read manifest or workspace: {exc}") from exc
    final_path, partial_path = _validate_output_paths(Path(output), force)
    _reject_output_input_collisions(final_path, partial_path, Path(manifest), rows)
    scratch = _prepare_work_dir(Path(work_dir))

    # Hashing is part of preflight: a row without complete hashes is not valid evidence.
    try:
        hashes = [hash_inputs(row) for row in rows]
    except (OSError, ValueError) as exc:
        raise VerificationPreflightError(f"could not hash manifest artifacts: {exc}") from exc

    toolchain = discover_toolchain()
    counts = {"rows": len(rows), "passed": 0, "failed": 0}
    try:
        with partial_path.open("w", encoding="utf-8", newline="\n") as handle:
            for index, (row, input_hashes) in enumerate(zip(rows, hashes), 1):
                try:
                    evidence = verify_row(
                        row,
                        input_hashes=input_hashes,
                        toolchain=toolchain,
                        work_dir=scratch,
                        timeout=timeout,
                        row_index=index,
                        workspace_root=Path(workspace_root),
                        manifest_path=Path(manifest),
                    )
                except Exception as exc:  # Keep one unexpected row from losing later evidence.
                    evidence = _internal_evidence(
                        row,
                        input_hashes,
                        toolchain,
                        str(exc),
                        Path(workspace_root),
                        scratch,
                    )
                handle.write(deterministic_json(evidence) + "\n")
                handle.flush()
                if evidence["failure_category"] == "passed":
                    counts["passed"] += 1
                else:
                    counts["failed"] += 1
            os.fsync(handle.fileno())
        os.replace(partial_path, final_path)
    except KeyboardInterrupt as exc:
        raise VerificationInterrupted(f"interrupted; partial evidence retained at {partial_path}") from exc
    except OSError as exc:
        raise VerificationPreflightError(f"could not write evidence output: {exc}") from exc
    return counts


def verify_row(
    row: MutationRow,
    *,
    input_hashes: dict[str, Any],
    toolchain: Mapping[str, ToolInfo],
    work_dir: Path,
    timeout: float,
    row_index: int = 1,
    workspace_root: Path | None = None,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    row_work = work_dir / f"{row_index:06d}_{_safe_name(row.mutation_id)}_{hashlib.sha256(row.mutation_id.encode()).hexdigest()[:12]}"
    row_work.mkdir(parents=True, exist_ok=True)
    checks = _initial_checks(row.requested_checks)
    diagnostics: list[str] = []

    if row.requested_checks["compile"]:
        if toolchain["iverilog"].path is None:
            for name in ("original", "mutated", "repaired"):
                checks["compile"][name] = unavailable()
        else:
            for name, artifact_key in (
                ("original", "original_rtl_path"),
                ("mutated", "mutated_rtl_path"),
                ("repaired", "repaired_rtl_path"),
            ):
                status, diagnostic = _compile_design(
                    toolchain["iverilog"].path,
                    row,
                    row.resolved_paths[artifact_key],
                    row_work / f"compile_{name}.out",
                    row_work,
                    timeout,
                    workspace_root,
                    manifest_path,
                )
                checks["compile"][name] = status
                if diagnostic:
                    diagnostics.append(diagnostic)

    if row.requested_checks["simulation"]:
        if toolchain["iverilog"].path is None or toolchain["vvp"].path is None:
            for name in ("original_passes", "mutated_detects_mutation", "repaired_passes"):
                checks["simulation"][name] = unavailable()
        else:
            for name, artifact_key, meaning in (
                ("original", "original_rtl_path", "original_passes"),
                ("mutated", "mutated_rtl_path", "mutated_detects_mutation"),
                ("repaired", "repaired_rtl_path", "repaired_passes"),
            ):
                status, diagnostic = _simulate_design(
                    toolchain["iverilog"].path,
                    toolchain["vvp"].path,
                    row,
                    row.resolved_paths[artifact_key],
                    row_work / f"simulation_{name}.out",
                    row_work,
                    timeout,
                    meaning,
                    workspace_root,
                    manifest_path,
                )
                checks["simulation"][meaning] = status
                if diagnostic:
                    diagnostics.append(diagnostic)

    if row.requested_checks["lint"]:
        if toolchain["verilator"].path is None:
            checks["lint"]["mutated"] = unavailable()
            checks["lint"]["repaired"] = unavailable()
        else:
            for name, key in (("mutated", "mutated_rtl_path"), ("repaired", "repaired_rtl_path")):
                status, diagnostic = _lint_design(
                    toolchain["verilator"].path,
                    row,
                    row.resolved_paths[key],
                    row_work,
                    timeout,
                    workspace_root,
                    manifest_path,
                )
                checks["lint"][name] = status
                if diagnostic:
                    diagnostics.append(diagnostic)

    if row.requested_checks["synthesis"]:
        if toolchain["yosys"].path is None:
            for name in ("original", "mutated", "repaired"):
                checks["synthesis"][name] = unavailable()
        else:
            for name, key in (
                ("original", "original_rtl_path"),
                ("mutated", "mutated_rtl_path"),
                ("repaired", "repaired_rtl_path"),
            ):
                status, diagnostic = _synthesize_design(
                    toolchain["yosys"].path,
                    row,
                    row.resolved_paths[key],
                    row_work,
                    timeout,
                    name,
                    workspace_root,
                    manifest_path,
                )
                checks["synthesis"][name] = status
                if diagnostic:
                    diagnostics.append(diagnostic)

    if row.requested_checks["equivalence"]:
        if toolchain["yosys"].path is None:
            checks["equivalence"]["original_vs_repaired"] = unavailable()
        else:
            status, diagnostic = _equivalence(
                toolchain["yosys"].path,
                row,
                row_work,
                timeout,
                workspace_root,
                manifest_path,
            )
            checks["equivalence"]["original_vs_repaired"] = status
            if diagnostic:
                diagnostics.append(diagnostic)

    if row.requested_checks["activity"]:
        checks["activity"]["proxy"] = unavailable("unsupported")

    evidence = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "mutation_id": row.mutation_id,
        "source_id": row.source_id,
        "top_module": row.top_module,
        "mutation_type": row.mutation_type,
        "mutated_signal": row.mutated_signal,
        "changed_location": {
            "file": row.changed_location.file,
            "line_start": row.changed_location.line_start,
            "line_end": row.changed_location.line_end,
        },
        "requested_checks": dict(row.requested_checks),
        "input_hashes": input_hashes,
        "toolchain": toolchain_json(toolchain),
        "checks": checks,
        "evidence_tier": compute_evidence_tier(checks),
        "failure_category": select_failure_category(checks),
        "diagnostics": _dedupe_diagnostics(diagnostics),
    }
    return evidence


def _initial_checks(requested: Mapping[str, bool]) -> dict[str, Any]:
    return {
        "compile": {name: (not_requested() if not requested["compile"] else unavailable("pending")) for name in ("original", "mutated", "repaired")},
        "lint": {name: (not_requested() if not requested["lint"] else unavailable("pending")) for name in ("mutated", "repaired")},
        "simulation": {name: (not_requested() if not requested["simulation"] else unavailable("pending")) for name in ("original_passes", "mutated_detects_mutation", "repaired_passes")},
        "synthesis": {name: (not_requested() if not requested["synthesis"] else unavailable("pending")) for name in ("original", "mutated", "repaired")},
        "equivalence": {"original_vs_repaired": not_requested() if not requested["equivalence"] else unavailable("pending")},
        "activity": {"proxy": not_requested() if not requested["activity"] else unavailable("pending")},
    }


def _compile_design(
    iverilog: str,
    row: MutationRow,
    rtl_path: Path,
    binary: Path,
    cwd: Path,
    timeout: float,
    workspace_root: Path | None,
    manifest_path: Path | None,
) -> tuple[dict[str, Any], str | None]:
    command = [iverilog, "-g2012", "-s", row.top_module, "-o", str(binary), str(rtl_path)]
    command.extend(str(row.resolved_paths[path]) for path in row.support_files)
    result = _run(command, cwd, timeout)
    if result.timed_out:
        return failed("timeout"), _diagnostic("compile", result, workspace_root, cwd, manifest_path)
    if result.startup_error or result.returncode != 0:
        return failed("compile_failure"), _diagnostic("compile", result, workspace_root, cwd, manifest_path)
    return passed(), _diagnostic("compile", result, workspace_root, cwd, manifest_path, only_if_output=True)


def _simulate_design(
    iverilog: str,
    vvp: str,
    row: MutationRow,
    rtl_path: Path,
    binary: Path,
    cwd: Path,
    timeout: float,
    meaning: str,
    workspace_root: Path | None,
    manifest_path: Path | None,
) -> tuple[dict[str, Any], str | None]:
    compile_command = [iverilog, "-g2012", "-o", str(binary), str(rtl_path)]
    compile_command.extend(str(row.resolved_paths[path]) for path in row.support_files)
    compile_command.append(str(row.resolved_paths["testbench_path"]))
    compiled = _run(compile_command, cwd, timeout)
    if compiled.timed_out:
        return failed("timeout"), _diagnostic("simulation compile", compiled, workspace_root, cwd, manifest_path)
    if compiled.startup_error or compiled.returncode != 0:
        return failed("compile_failure"), _diagnostic("simulation compile", compiled, workspace_root, cwd, manifest_path)
    simulated = _run([vvp, str(binary)], cwd, timeout)
    if simulated.timed_out:
        return failed("timeout"), _diagnostic("simulation", simulated, workspace_root, cwd, manifest_path)
    output = simulated.output
    semantic_failure = _semantic_failure(output)
    if meaning == "mutated_detects_mutation":
        if simulated.startup_error:
            return failed("simulation_failure"), _diagnostic("simulation", simulated, workspace_root, cwd, manifest_path)
        if semantic_failure:
            return passed(), _diagnostic("simulation", simulated, workspace_root, cwd, manifest_path, only_if_output=True)
        if simulated.returncode != 0:
            return failed("simulation_failure"), _diagnostic("simulation", simulated, workspace_root, cwd, manifest_path)
        return failed("simulation_not_detected"), _diagnostic("simulation", simulated, workspace_root, cwd, manifest_path)
    if simulated.startup_error:
        reason = "original_simulation_failure" if meaning == "original_passes" else "repaired_simulation_failure"
        return failed(reason), _diagnostic("simulation", simulated, workspace_root, cwd, manifest_path)
    if simulated.returncode == 0 and not semantic_failure:
        return passed(), _diagnostic("simulation", simulated, workspace_root, cwd, manifest_path, only_if_output=True)
    reason = "original_simulation_failure" if meaning == "original_passes" else "repaired_simulation_failure"
    return failed(reason), _diagnostic("simulation", simulated, workspace_root, cwd, manifest_path)


def _lint_design(
    verilator: str,
    row: MutationRow,
    rtl_path: Path,
    cwd: Path,
    timeout: float,
    workspace_root: Path | None,
    manifest_path: Path | None,
) -> tuple[dict[str, Any], str | None]:
    command = [verilator, "--lint-only", "--timing", "--top-module", row.top_module, str(rtl_path)]
    command.extend(str(row.resolved_paths[path]) for path in row.support_files)
    result = _run(command, cwd, timeout)
    if result.timed_out:
        return failed("timeout"), _diagnostic("lint", result, workspace_root, cwd, manifest_path)
    if result.startup_error or result.returncode != 0:
        return failed("lint_failure"), _diagnostic("lint", result, workspace_root, cwd, manifest_path)
    return passed(), _diagnostic("lint", result, workspace_root, cwd, manifest_path, only_if_output=True)


def _synthesize_design(
    yosys: str,
    row: MutationRow,
    rtl_path: Path,
    cwd: Path,
    timeout: float,
    name: str,
    workspace_root: Path | None,
    manifest_path: Path | None,
) -> tuple[dict[str, Any], str | None]:
    script = cwd / f"synthesis_{name}.ys"
    script.write_text(_yosys_synthesis_script(row, rtl_path), encoding="utf-8")
    result = _run([yosys, str(script)], cwd, timeout)
    if result.timed_out:
        return failed("timeout"), _diagnostic("synthesis", result, workspace_root, cwd, manifest_path)
    if result.startup_error or result.returncode != 0:
        return failed("synthesis_failure"), _diagnostic("synthesis", result, workspace_root, cwd, manifest_path)
    return passed(), _diagnostic("synthesis", result, workspace_root, cwd, manifest_path, only_if_output=True)


def _equivalence(
    yosys: str,
    row: MutationRow,
    cwd: Path,
    timeout: float,
    workspace_root: Path | None,
    manifest_path: Path | None,
) -> tuple[dict[str, Any], str | None]:
    gold = cwd / "equiv_original.sv"
    gate = cwd / "equiv_repaired.sv"
    try:
        gold.write_text(_rename_module(row.resolved_paths["original_rtl_path"].read_text(encoding="utf-8"), "gold"), encoding="utf-8")
        gate.write_text(_rename_module(row.resolved_paths["repaired_rtl_path"].read_text(encoding="utf-8"), "gate"), encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return failed("equivalence_failure"), sanitize_diagnostic(str(exc), workspace_root=workspace_root, work_dir=cwd, manifest_path=manifest_path)
    script = cwd / "equivalence.ys"
    script.write_text(
        "\n".join(
            [
                f"read_verilog -sv {_yosys_quote(gold)}",
                "prep -top gold",
                "design -stash gold",
                "design -reset",
                f"read_verilog -sv {_yosys_quote(gate)}",
                "prep -top gate",
                "design -stash gate",
                "design -reset",
                "design -copy-from gold -as gold gold",
                "design -copy-from gate -as gate gate",
                "equiv_make gold gate equiv",
                "hierarchy -top equiv",
                "proc; opt",
                "equiv_simple",
                "equiv_status -assert",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    result = _run([yosys, str(script)], cwd, timeout)
    if result.timed_out:
        return failed("timeout"), _diagnostic("equivalence", result, workspace_root, cwd, manifest_path)
    if result.startup_error or result.returncode != 0:
        return failed("equivalence_failure"), _diagnostic("equivalence", result, workspace_root, cwd, manifest_path)
    return passed(), _diagnostic("equivalence", result, workspace_root, cwd, manifest_path, only_if_output=True)


def _run(command: list[str], cwd: Path, timeout: float) -> CommandResult:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(None, f"{exc.stdout or ''}\n{exc.stderr or ''}", timed_out=True)
    except OSError as exc:
        return CommandResult(None, str(exc), startup_error=True)
    return CommandResult(completed.returncode, f"{completed.stdout}\n{completed.stderr}")


def _capture_version(path: str, args: tuple[str, ...], timeout: float) -> str | None:
    result = _run([path, *args], Path.cwd(), timeout)
    if result.timed_out or result.startup_error or result.returncode not in {0, None}:
        return None
    value = " ".join(result.output.split())
    return sanitize_diagnostic(value, limit=512) or None


def _semantic_failure(output: str) -> bool:
    counts = [int(value) for value in _MISMATCH_RE.findall(output)]
    return any(value > 0 for value in counts) or bool(_FAILURE_RE.search(output))


def _diagnostic(
    stage: str,
    result: CommandResult,
    workspace_root: Path | None,
    work_dir: Path,
    manifest_path: Path | None,
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
    return sanitize_diagnostic(
        message,
        workspace_root=workspace_root,
        work_dir=work_dir,
        manifest_path=manifest_path,
    )


def _yosys_synthesis_script(row: MutationRow, rtl_path: Path) -> str:
    files = [rtl_path, *(row.resolved_paths[path] for path in row.support_files)]
    lines = [f"read_verilog -sv {_yosys_quote(path)}" for path in files]
    lines.extend([f"hierarchy -top {row.top_module}", "proc", "opt", "stat"])
    return "\n".join(lines) + "\n"


def _yosys_quote(path: Path) -> str:
    return '"' + str(path).replace("\\", "/").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _rename_module(source: str, new_name: str) -> str:
    replaced, count = re.subn(
        r"\bmodule\s+([A-Za-z_$][A-Za-z0-9_$]*)\b",
        f"module {new_name}",
        source,
        count=1,
    )
    if count != 1:
        raise ValueError("no module declaration found")
    return replaced


def _internal_evidence(
    row: MutationRow,
    input_hashes: dict[str, Any],
    toolchain: Mapping[str, ToolInfo],
    error: str,
    workspace_root: Path,
    work_dir: Path,
) -> dict[str, Any]:
    checks = _initial_checks({name: True for name in ("compile", "lint", "simulation", "synthesis", "equivalence", "activity")})
    checks["compile"] = {name: failed("internal_error") for name in ("original", "mutated", "repaired")}
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "mutation_id": row.mutation_id,
        "source_id": row.source_id,
        "top_module": row.top_module,
        "mutation_type": row.mutation_type,
        "mutated_signal": row.mutated_signal,
        "changed_location": {"file": row.changed_location.file, "line_start": row.changed_location.line_start, "line_end": row.changed_location.line_end},
        "requested_checks": dict(row.requested_checks),
        "input_hashes": input_hashes,
        "toolchain": toolchain_json(toolchain),
        "checks": checks,
        "evidence_tier": compute_evidence_tier(checks),
        "failure_category": "internal_error",
        "diagnostics": [sanitize_diagnostic(f"internal row error: {error}", workspace_root=workspace_root, work_dir=work_dir)],
    }


def _dedupe_diagnostics(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _validate_output_paths(output: Path, force: bool) -> tuple[Path, Path]:
    requested = output.expanduser()
    if requested.is_symlink():
        raise VerificationPreflightError("output must not be a symlink")
    final = requested.resolve()
    if not final.parent.exists() or not final.parent.is_dir():
        raise VerificationPreflightError(f"output parent is not a directory: {final.parent}")
    if final.exists() and final.is_dir():
        raise VerificationPreflightError(f"output is a directory: {final}")
    partial = Path(str(final) + ".rtlbench-partial")
    if partial.is_symlink():
        raise VerificationPreflightError("managed partial output must not be a symlink")
    if partial.exists() and partial.is_dir():
        raise VerificationPreflightError(f"managed partial output is a directory: {partial}")
    if (final.exists() or partial.exists()) and not force:
        raise VerificationPreflightError(
            f"output already exists; use --force only for {final} and its managed partial"
        )
    return final, partial


def _reject_output_input_collisions(
    final: Path,
    partial: Path,
    manifest_path: Path,
    rows: list[MutationRow],
) -> None:
    """Reject output paths that alias any input before output I/O begins."""

    protected: list[tuple[str, Path]] = [("manifest", manifest_path.expanduser().resolve())]
    for row in rows:
        for label, path in row.artifact_paths:
            protected.append((f"{row.mutation_id}:{label}", path))

    for candidate_label, candidate in (("output", final), ("managed partial output", partial)):
        for protected_label, protected_path in protected:
            if _paths_alias(candidate, protected_path):
                raise VerificationPreflightError(
                    f"{candidate_label} aliases protected input {protected_label}: {candidate}"
                )


def _paths_alias(left: Path, right: Path) -> bool:
    """Compare normalized paths and existing inodes, including hard links."""

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
    if path.exists() and path.is_symlink():
        raise VerificationPreflightError("work-dir must not be a symlink")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise VerificationPreflightError(f"could not create work-dir: {exc}") from exc
    if not path.is_dir():
        raise VerificationPreflightError(f"work-dir is not a directory: {path}")
    try:
        return Path(tempfile.mkdtemp(prefix=".rtlbench-run-", dir=str(path.resolve())))
    except OSError as exc:
        raise VerificationPreflightError(f"could not create managed run directory: {exc}") from exc


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "row"
