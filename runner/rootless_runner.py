"""Fail-closed rootless Podman launcher for RTLBench candidate verification."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


CONFIG_PATH = Path(__file__).with_name("runner_config.json")
EVIDENCE_SCHEMA_VERSION = "rtl_candidate_evidence_v0.1"
IMAGE_DIGEST_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
EXPECTED_INNER_COMMAND = (
    "rtlbench",
    "verify-candidates",
    "--manifest",
    "/input/candidate_manifest.jsonl",
    "--output",
    "/output/candidate_evidence.jsonl",
    "--workspace-root",
    "/input/workspace",
    "--work-dir",
    "/work",
    "--force",
)
EVIDENCE_FIELDS = {
    "schema_version",
    "candidate_id",
    "task_id",
    "source_id",
    "attempt",
    "top_module",
    "testbench_top",
    "simulation_result_contract",
    "requested_checks",
    "input_hashes",
    "toolchain",
    "checks",
    "mismatch_summary",
    "failure_category",
    "accepted",
    "diagnostics",
}
CHECK_FIELDS = {"compile", "simulation", "lint", "synthesis"}
FAILURE_CATEGORIES = {
    "passed",
    "tool_unavailable",
    "compile_failure",
    "functional_mismatch",
    "simulation_result_missing",
    "simulation_failure",
    "timeout",
    "internal_error",
    "partial_failure",
}
MISMATCH_FIELDS = {
    "contract",
    "reported_counts",
    "reported_sample_counts",
    "maximum_count",
    "timeout_reported",
}
IDENTITY_LABELS = {
    "rtlbench_commit": "org.opencontainers.image.revision",
    "runner_config_version": "io.rtlbench.runner.config_version",
    "python_version": "io.rtlbench.python_version",
    "iverilog_version": "io.rtlbench.iverilog_version",
    "vvp_version": "io.rtlbench.vvp_version",
    "verilator_version": "io.rtlbench.verilator_version",
    "yosys_version": "io.rtlbench.yosys_version",
}


class RunnerError(RuntimeError):
    """Raised for a runner preflight or publication failure."""


@dataclass(frozen=True)
class RunnerConfig:
    version: str
    runtime: str
    runtime_user: str
    inner_command: tuple[str, ...]
    cpus: float
    memory_bytes: int
    pids: int
    file_size_bytes: int
    open_files: int
    work_bytes: int
    tmp_bytes: int
    wall_seconds: float
    evidence_bytes: int
    allowed_output_names: frozenset[str]


@dataclass(frozen=True)
class RunResult:
    returncode: int
    evidence_output: Path | None
    partial_output: Path | None
    identity_output: Path | None
    timed_out: bool


def load_config(path: Path = CONFIG_PATH) -> RunnerConfig:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerError(f"could not read runner configuration: {exc}") from exc
    try:
        limits = value["limits"]
        config = RunnerConfig(
            version=_string(value["runner_config_version"], "runner_config_version"),
            runtime=_string(value["runtime"], "runtime"),
            runtime_user=_string(value["runtime_user"], "runtime_user"),
            inner_command=tuple(
                _string(item, "inner_command item") for item in value["inner_command"]
            ),
            cpus=float(limits["cpus"]),
            memory_bytes=int(limits["memory_bytes"]),
            pids=int(limits["pids"]),
            file_size_bytes=int(limits["file_size_bytes"]),
            open_files=int(limits["open_files"]),
            work_bytes=int(limits["work_bytes"]),
            tmp_bytes=int(limits["tmp_bytes"]),
            wall_seconds=float(limits["wall_seconds"]),
            evidence_bytes=int(limits["evidence_bytes"]),
            allowed_output_names=frozenset(
                _string(item, "allowed_output_names item")
                for item in value["allowed_output_names"]
            ),
        )
        if config.runtime != "podman-rootless":
            raise ValueError("runtime must be podman-rootless")
        if config.runtime_user != "65532:65532":
            raise ValueError("runtime_user must be 65532:65532")
        if config.inner_command != EXPECTED_INNER_COMMAND:
            raise ValueError("inner_command does not match the fixed verifier command")
        if config.allowed_output_names != frozenset(
            {"candidate_evidence.jsonl", "candidate_evidence.jsonl.rtlbench-partial"}
        ):
            raise ValueError("allowed_output_names is not the fixed evidence set")
        if any(
            value <= 0
            for value in (
                config.cpus,
                config.memory_bytes,
                config.pids,
                config.file_size_bytes,
                config.open_files,
                config.work_bytes,
                config.tmp_bytes,
                config.wall_seconds,
                config.evidence_bytes,
            )
        ):
            raise ValueError("runner limits must be positive")
        return config
    except (KeyError, TypeError, ValueError) as exc:
        raise RunnerError(f"invalid runner configuration: {exc}") from exc


def build_run_command(
    runtime: str,
    image: str,
    input_root: Path,
    output_root: Path,
    config: RunnerConfig,
) -> list[str]:
    """Build the only container command this runner permits."""

    return [
        runtime,
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        config.runtime_user,
        "--userns",
        "private",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--cpus",
        str(config.cpus),
        "--memory",
        str(config.memory_bytes),
        "--pids-limit",
        str(config.pids),
        "--ulimit",
        f"fsize={config.file_size_bytes}:{config.file_size_bytes}",
        "--ulimit",
        f"nofile={config.open_files}:{config.open_files}",
        "--ulimit",
        f"nproc={config.pids}:{config.pids}",
        "--tmpfs",
        f"/tmp:rw,nosuid,nodev,noexec,size={config.tmp_bytes},mode=1777",
        "--tmpfs",
        f"/work:rw,nosuid,nodev,size={config.work_bytes},uid=65532,gid=65532,mode=700",
        "--mount",
        f"type=bind,src={input_root},dst=/input,ro",
        "--mount",
        f"type=bind,src={output_root},dst=/output,rw",
        "--workdir",
        "/work",
        "--env",
        "HOME=/tmp",
        "--env",
        "TMPDIR=/tmp",
        "--env",
        "LANG=C",
        "--env",
        "LC_ALL=C",
        "--env",
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "--entrypoint",
        "rtlbench",
        image,
        *config.inner_command[1:],
    ]


def run_isolated(
    *,
    image: str,
    input_root: Path,
    output: Path,
    runtime: str = "podman",
    wall_timeout: float | None = None,
    force_output: bool = False,
    config_path: Path = CONFIG_PATH,
) -> RunResult:
    config = load_config(config_path)
    runtime_path = _validate_runtime(runtime, config)
    image = _validate_image(image)
    input_root = _validate_input_root(input_root)
    output = _validate_host_output(output, input_root, config, force_output)
    _validate_no_reference_rtl(input_root)
    identity = _read_image_identity(runtime_path, image, config)
    timeout = config.wall_seconds if wall_timeout is None else wall_timeout
    if timeout <= 0:
        raise RunnerError("wall timeout must be greater than zero")

    with tempfile.TemporaryDirectory(prefix=".rtlbench-runner-output-") as output_tmp:
        with tempfile.TemporaryDirectory(prefix=".rtlbench-runner-env-") as env_tmp:
            staged_output = Path(output_tmp)
            staged_output.chmod(0o777)
            command = build_run_command(runtime_path, image, input_root, staged_output, config)
            returncode, timed_out = _execute(
                command,
                timeout=timeout,
                environment=_runtime_environment(Path(env_tmp)),
            )
            _reject_unexpected_output(staged_output, config)
            final = staged_output / "candidate_evidence.jsonl"
            partial = staged_output / "candidate_evidence.jsonl.rtlbench-partial"
            final_exists = final.exists()
            partial_exists = partial.exists()
            if final_exists:
                validate_candidate_evidence_file(final, max_bytes=config.evidence_bytes)
            if not final_exists and not partial_exists:
                if timed_out:
                    raise RunnerError("runner wall timeout expired without preserved evidence")
                raise RunnerError(
                    f"isolated RTLBench exited with status {returncode} without evidence"
                )
            if output.exists() and not force_output:
                raise RunnerError(f"output already exists: {output}")
            partial_destination = Path(str(output) + ".rtlbench-partial")
            identity_destination = Path(str(output) + ".runner.json")
            if not force_output and (
                partial_destination.exists() or identity_destination.exists()
            ):
                raise RunnerError("managed runner output already exists; use --force-output")
            if final_exists:
                _publish_file(final, output, max_bytes=config.evidence_bytes)
            if partial_exists:
                _publish_file(partial, partial_destination, max_bytes=config.evidence_bytes)
            _publish_json(identity_destination, identity)
            result = RunResult(
                returncode=124 if timed_out else returncode,
                evidence_output=output if final_exists else None,
                partial_output=partial_destination if partial_exists else None,
                identity_output=identity_destination,
                timed_out=timed_out,
            )
    return result


def validate_candidate_evidence_file(path: Path, *, max_bytes: int) -> list[dict[str, Any]]:
    if not path.is_file() or path.is_symlink():
        raise RunnerError("candidate evidence must be a regular non-symlink file")
    if path.stat().st_size > max_bytes:
        raise RunnerError("candidate evidence exceeds the configured size limit")
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, 1):
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise RunnerError(f"evidence line {line_number}: malformed JSON") from exc
                _validate_evidence_row(row, line_number)
                candidate_id = row["candidate_id"]
                if candidate_id in seen_ids:
                    raise RunnerError(f"evidence line {line_number}: duplicate candidate_id")
                seen_ids.add(candidate_id)
                rows.append(row)
    except UnicodeDecodeError as exc:
        raise RunnerError("candidate evidence is not valid UTF-8") from exc
    if not rows:
        raise RunnerError("candidate evidence contains no rows")
    return rows


def _validate_evidence_row(row: Any, line_number: int) -> None:
    if not isinstance(row, dict) or set(row) != EVIDENCE_FIELDS:
        raise RunnerError(f"evidence line {line_number}: top-level schema mismatch")
    if row["schema_version"] != EVIDENCE_SCHEMA_VERSION:
        raise RunnerError(f"evidence line {line_number}: unsupported schema_version")
    for key in ("candidate_id", "task_id", "source_id", "top_module", "testbench_top"):
        if not isinstance(row[key], str) or not row[key]:
            raise RunnerError(f"evidence line {line_number}: invalid {key}")
    if type(row["attempt"]) is not int or row["attempt"] < 1:
        raise RunnerError(f"evidence line {line_number}: invalid attempt")
    if row["simulation_result_contract"] not in {"mismatch_count_v1", "exit_code_v1"}:
        raise RunnerError(f"evidence line {line_number}: invalid simulation contract")
    checks = row["checks"]
    if not isinstance(checks, dict) or set(checks) != CHECK_FIELDS:
        raise RunnerError(f"evidence line {line_number}: invalid checks")
    for check_name, leaf_name in (
        ("compile", "candidate"),
        ("simulation", "candidate_passes"),
        ("lint", "candidate"),
        ("synthesis", "candidate"),
    ):
        container = checks[check_name]
        if not isinstance(container, dict) or set(container) != {leaf_name}:
            raise RunnerError(f"evidence line {line_number}: invalid {check_name} check")
        leaf = container[leaf_name]
        if not isinstance(leaf, dict) or set(leaf) != {"attempted", "passed", "reason"}:
            raise RunnerError(f"evidence line {line_number}: invalid {check_name} leaf")
        if type(leaf["attempted"]) is not bool or type(leaf["passed"]) not in {bool, type(None)}:
            raise RunnerError(f"evidence line {line_number}: invalid {check_name} status")
        if leaf["reason"] is not None and not isinstance(leaf["reason"], str):
            raise RunnerError(f"evidence line {line_number}: invalid {check_name} reason")
    requested = row["requested_checks"]
    if (
        not isinstance(requested, dict)
        or set(requested) != {"compile", "simulation", "lint", "synthesis"}
        or any(type(value) is not bool for value in requested.values())
    ):
        raise RunnerError(f"evidence line {line_number}: invalid requested_checks")
    mismatch = row["mismatch_summary"]
    if not isinstance(mismatch, dict) or set(mismatch) != MISMATCH_FIELDS:
        raise RunnerError(f"evidence line {line_number}: invalid mismatch_summary")
    if (
        mismatch["contract"] not in {"mismatch_count_v1", "exit_code_v1"}
        or not isinstance(mismatch["reported_counts"], list)
        or not isinstance(mismatch["reported_sample_counts"], list)
        or any(type(value) is not int or value < 0 for value in mismatch["reported_counts"])
        or any(
            value is not None
            and (type(value) is not int or value < 0)
            for value in mismatch["reported_sample_counts"]
        )
        or (
            mismatch["maximum_count"] is not None
            and (type(mismatch["maximum_count"]) is not int or mismatch["maximum_count"] < 0)
        )
        or type(mismatch["timeout_reported"]) is not bool
    ):
        raise RunnerError(f"evidence line {line_number}: invalid mismatch_summary values")
    if row["failure_category"] not in FAILURE_CATEGORIES:
        raise RunnerError(f"evidence line {line_number}: invalid failure_category")
    if type(row["accepted"]) is not bool or not isinstance(row["diagnostics"], list):
        raise RunnerError(f"evidence line {line_number}: invalid result fields")
    if any(not isinstance(item, str) for item in row["diagnostics"]):
        raise RunnerError(f"evidence line {line_number}: invalid diagnostics")
    if not isinstance(row["input_hashes"], dict) or not isinstance(row["toolchain"], dict):
        raise RunnerError(f"evidence line {line_number}: invalid evidence metadata")


def _validate_runtime(runtime: str, config: RunnerConfig) -> str:
    runtime_path = shutil.which(runtime)
    if runtime_path is None:
        raise RunnerError("rootless Podman is required but podman was not found")
    if Path(runtime_path).name != "podman":
        raise RunnerError("only the podman rootless runtime is supported")
    result = _run_checked(
        [runtime_path, "info", "--format", "{{.Host.Security.Rootless}}"],
        timeout=10.0,
        environment=_runtime_environment(None),
    )
    if result.returncode != 0 or result.stdout.strip().lower() != "true":
        raise RunnerError("podman must report rootless=true")
    if config.runtime != "podman-rootless":
        raise RunnerError("runner configuration does not require podman-rootless")
    return runtime_path


def _validate_image(image: str) -> str:
    if not IMAGE_DIGEST_RE.fullmatch(image):
        raise RunnerError("image must be referenced by name@sha256:<64 hex digits>")
    return image


def _validate_input_root(input_root: Path) -> Path:
    path = Path(input_root).expanduser()
    if _contains_symlink(path):
        raise RunnerError("input handoff must not contain symlinks")
    if not path.exists() or not path.is_dir():
        raise RunnerError("input handoff must be a directory")
    if not (path / "candidate_manifest.jsonl").is_file() or not (path / "workspace").is_dir():
        raise RunnerError("input handoff must contain candidate_manifest.jsonl and workspace")
    for current, directories, files in os.walk(path, topdown=True, followlinks=False):
        for name in directories:
            child = Path(current) / name
            if child.is_symlink():
                raise RunnerError(f"input handoff contains symlink: {child.name}")
        for name in files:
            child = Path(current) / name
            if child.is_symlink():
                raise RunnerError(f"input handoff contains symlink: {child.name}")
            if not stat.S_ISREG(child.stat().st_mode):
                raise RunnerError(f"input handoff contains non-regular file: {child.name}")
    return path.resolve()


def _validate_no_reference_rtl(input_root: Path) -> None:
    for current, _, files in os.walk(input_root, topdown=True, followlinks=False):
        for name in files:
            if name.casefold() == "reference.sv":
                raise RunnerError("input handoff must not contain reference.sv")


def _validate_host_output(
    output: Path,
    input_root: Path,
    config: RunnerConfig,
    force_output: bool,
) -> Path:
    path = Path(output).expanduser()
    if _contains_symlink(path):
        raise RunnerError("evidence output must not contain symlinks")
    if not path.parent.exists() or not path.parent.is_dir():
        raise RunnerError("evidence output parent must be an existing directory")
    resolved = path.resolve()
    if resolved == input_root or input_root in resolved.parents:
        raise RunnerError("evidence output must not be inside the read-only input handoff")
    if path.exists() and path.is_dir():
        raise RunnerError("evidence output must be a regular file path")
    managed = [path, Path(str(path) + ".rtlbench-partial"), Path(str(path) + ".runner.json")]
    if not force_output and any(item.exists() for item in managed):
        raise RunnerError("managed evidence output already exists; use --force-output")
    if config.evidence_bytes <= 0:
        raise RunnerError("evidence size limit must be positive")
    return resolved


def _read_image_identity(runtime: str, image: str, config: RunnerConfig) -> dict[str, str]:
    result = _run_checked(
        [runtime, "image", "inspect", "--format", "{{json .Config.Labels}}", image],
        timeout=10.0,
        environment=_runtime_environment(None),
    )
    if result.returncode != 0:
        raise RunnerError("could not inspect the immutable RTLBench image")
    try:
        labels = json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        raise RunnerError("RTLBench image labels are not valid JSON") from exc
    if not isinstance(labels, dict):
        raise RunnerError("RTLBench image labels are missing")
    identity: dict[str, str] = {"runner_config_version": config.version, "image": image}
    for identity_name, label_name in IDENTITY_LABELS.items():
        value = labels.get(label_name)
        if not isinstance(value, str) or not value:
            raise RunnerError(f"RTLBench image is missing immutable label: {label_name}")
        identity[identity_name] = value
    if not GIT_SHA_RE.fullmatch(identity["rtlbench_commit"]):
        raise RunnerError("RTLBench image revision label is not a 40-character git SHA")
    if identity["runner_config_version"] != config.version:
        raise RunnerError("RTLBench image runner configuration does not match the launcher")
    return identity


def _execute(
    command: Sequence[str], *, timeout: float, environment: Mapping[str, str]
) -> tuple[int, bool]:
    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=dict(environment),
        )
    except OSError as exc:
        raise RunnerError(f"could not start rootless RTLBench: {exc}") from exc
    try:
        returncode = process.wait(timeout=timeout)
        return returncode, False
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        return 124, True


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        process.wait(timeout=5.0)


def _runtime_environment(env_root: Path | None) -> dict[str, str]:
    if env_root is None:
        environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "XDG_CONFIG_HOME": "/nonexistent",
            "REGISTRY_AUTH_FILE": "/nonexistent",
            "TMPDIR": "/tmp",
            "LANG": "C",
            "LC_ALL": "C",
        }
    else:
        env_root.mkdir(parents=True, exist_ok=True)
        environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(env_root),
            "XDG_CONFIG_HOME": str(env_root / "config"),
            "REGISTRY_AUTH_FILE": str(env_root / "no-auth.json"),
            "TMPDIR": str(env_root),
            "LANG": "C",
            "LC_ALL": "C",
        }
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime and Path(xdg_runtime).is_dir():
        environment["XDG_RUNTIME_DIR"] = xdg_runtime
    return environment


def _run_checked(
    command: Sequence[str], *, timeout: float, environment: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=timeout,
            env=dict(environment),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RunnerError(f"rootless runtime probe failed: {exc}") from exc


def _reject_unexpected_output(staged_output: Path, config: RunnerConfig) -> None:
    names = {child.name for child in staged_output.iterdir()}
    unexpected = names - config.allowed_output_names
    if unexpected:
        raise RunnerError(f"isolated runtime wrote non-evidence output: {sorted(unexpected)}")
    for child in staged_output.iterdir():
        if child.is_symlink() or not child.is_file():
            raise RunnerError("isolated runtime evidence output must contain regular files only")


def _publish_file(source: Path, destination: Path, *, max_bytes: int) -> None:
    if source.stat().st_size > max_bytes:
        raise RunnerError(f"managed output exceeds {max_bytes} bytes")
    temporary = destination.with_name(f".{destination.name}.runner-partial")
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    with source.open("rb") as source_handle, temporary.open("xb") as destination_handle:
        while chunk := source_handle.read(1024 * 1024):
            destination_handle.write(chunk)
        destination_handle.flush()
        os.fsync(destination_handle.fileno())
    os.replace(temporary, destination)


def _publish_json(destination: Path, value: Mapping[str, str]) -> None:
    temporary = destination.with_name(f".{destination.name}.runner-partial")
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    temporary.write_text(
        json.dumps(dict(value), sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, destination)


def _contains_symlink(path: Path) -> bool:
    current = path
    while current != current.parent:
        if current.is_symlink():
            return True
        current = current.parent
    return current.is_symlink()


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run RTLBench in rootless Podman")
    parser.add_argument("--image", required=True, help="immutable IMAGE@sha256:<digest>")
    parser.add_argument("--input", type=Path, required=True, help="read-only candidate handoff")
    parser.add_argument("--output", type=Path, required=True, help="host evidence JSONL output")
    parser.add_argument("--runtime", default="podman")
    parser.add_argument("--wall-timeout", type=float)
    parser.add_argument("--force-output", action="store_true")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = run_isolated(
            image=args.image,
            input_root=args.input,
            output=args.output,
            runtime=args.runtime,
            wall_timeout=args.wall_timeout,
            force_output=args.force_output,
            config_path=args.config,
        )
    except RunnerError as exc:
        print(f"rtlbench rootless runner: {exc}", file=os.sys.stderr)
        return 125
    if result.timed_out:
        print(
            "rtlbench rootless runner: wall timeout; preserved partial evidence",
            file=os.sys.stderr,
        )
        return 124
    print(f"wrote isolated evidence to {result.evidence_output or result.partial_output}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
