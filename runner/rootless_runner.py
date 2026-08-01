"""Fail-closed profile-aware launcher for RTLBench candidate verification."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from rtlbench.candidate_evidence import (  # noqa: E402
    CandidateEvidenceValidationError,
    sha256_file,
    sha256_workspace_tree,
    validate_candidate_evidence_file as _validate_candidate_evidence_file,
)


CONFIG_PATH = Path(__file__).with_name("runner_config.json")
PROFILE_PILOT_DOCKER = "pilot-docker"
PROFILE_PRODUCTION_ROOTLESS = "production-rootless"
PROFILES = frozenset({PROFILE_PILOT_DOCKER, PROFILE_PRODUCTION_ROOTLESS})
IDENTITY_SCHEMA_VERSION = "rtlbench_runner_identity_v0.2"
EXPECTED_PROFILE_LIMITS = {
    PROFILE_PRODUCTION_ROOTLESS: {
        "cpus": 2.0,
        "memory_bytes": 536870912,
        "memory_swap_bytes": 536870912,
        "pids": 128,
        "file_size_bytes": 33554432,
        "open_files": 256,
        "work_bytes": 268435456,
        "tmp_bytes": 134217728,
        "wall_seconds": 120.0,
        "evidence_bytes": 33554432,
    },
    PROFILE_PILOT_DOCKER: {
        "cpus": 1.0,
        "memory_bytes": 536870912,
        "memory_swap_bytes": 536870912,
        "pids": 64,
        "file_size_bytes": 33554432,
        "open_files": 256,
        "work_bytes": 134217728,
        "tmp_bytes": 67108864,
        "wall_seconds": 120.0,
        "evidence_bytes": 33554432,
    },
}
REPOSITORY_IMAGE_DIGEST_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
LOCAL_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_DIGEST_RE = REPOSITORY_IMAGE_DIGEST_RE
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
    profile: str
    version: str
    runtime: str
    runtime_name: str
    runtime_mode: str
    rootless: bool
    runtime_user: str
    inner_command: tuple[str, ...]
    cpus: float
    memory_bytes: int
    memory_swap_bytes: int
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


def load_config(
    path: Path = CONFIG_PATH, profile: str = PROFILE_PRODUCTION_ROOTLESS
) -> RunnerConfig:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerError(f"could not read runner configuration: {exc}") from exc
    try:
        if profile not in PROFILES:
            raise ValueError(f"unknown isolation profile: {profile}")
        profile_values = value.get("profiles", {}).get(profile)
        if profile_values is None:
            if profile != PROFILE_PRODUCTION_ROOTLESS:
                raise ValueError(f"runner configuration has no profile: {profile}")
            profile_values = {
                "runtime": value["runtime"],
                "runtime_name": "podman",
                "runtime_mode": "rootless",
                "rootless": True,
                "limits": value["limits"],
            }
        limits = profile_values["limits"]
        config = RunnerConfig(
            profile=profile,
            version=_string(value["runner_config_version"], "runner_config_version"),
            runtime=_string(profile_values["runtime"], "runtime"),
            runtime_name=_string(profile_values["runtime_name"], "runtime_name"),
            runtime_mode=_string(profile_values["runtime_mode"], "runtime_mode"),
            rootless=profile_values["rootless"],
            runtime_user=_string(value["runtime_user"], "runtime_user"),
            inner_command=tuple(
                _string(item, "inner_command item") for item in value["inner_command"]
            ),
            cpus=float(limits["cpus"]),
            memory_bytes=int(limits["memory_bytes"]),
            memory_swap_bytes=int(limits["memory_swap_bytes"]),
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
        if type(config.rootless) is not bool:
            raise ValueError("rootless must be boolean")
        expected_profile = {
            PROFILE_PRODUCTION_ROOTLESS: (
                "podman-rootless",
                "podman",
                "rootless",
                True,
            ),
            PROFILE_PILOT_DOCKER: (
                "docker",
                "docker",
                "rootful-daemon",
                False,
            ),
        }[profile]
        if (
            config.runtime,
            config.runtime_name,
            config.runtime_mode,
            config.rootless,
        ) != expected_profile:
            raise ValueError(f"invalid runtime profile configuration: {profile}")
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
                config.memory_swap_bytes,
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
        for field, expected in EXPECTED_PROFILE_LIMITS[profile].items():
            if getattr(config, field) != expected:
                raise ValueError(f"{profile} limit {field} must be {expected}")
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
    pull_args = (
        ["--pull=never"]
        if config.profile == PROFILE_PRODUCTION_ROOTLESS
        else ["--pull", "never"]
    )
    command = [
        runtime,
        "run",
        *pull_args,
        "--rm",
        "--network",
        "none",
        "--user",
        config.runtime_user,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--cpus",
        _format_cpu_limit(config.cpus),
        "--memory",
        str(config.memory_bytes),
        "--memory-swap",
        str(config.memory_swap_bytes),
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
        f"type=bind,src={input_root},dst=/input,{_input_mount_mode(config)}",
        "--mount",
        f"type=bind,src={output_root},dst=/output",
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
    if config.profile == PROFILE_PRODUCTION_ROOTLESS:
        index = command.index("--cap-drop")
        command[index:index] = ["--userns", "private"]
    return command


def _input_mount_mode(config: RunnerConfig) -> str:
    return "ro" if config.profile == PROFILE_PRODUCTION_ROOTLESS else "readonly"


def _format_cpu_limit(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)


def run_isolated(
    *,
    image: str,
    input_root: Path,
    output: Path,
    profile: str = PROFILE_PRODUCTION_ROOTLESS,
    acknowledge_rootful_runtime: bool = False,
    runtime: str | None = None,
    wall_timeout: float | None = None,
    force_output: bool = False,
    config_path: Path = CONFIG_PATH,
) -> RunResult:
    _validate_profile_selection(profile, acknowledge_rootful_runtime)
    config = load_config(config_path, profile=profile)
    runtime_path = _validate_runtime(runtime, config)
    image = _validate_image(image, config)
    input_root = _validate_input_root(input_root)
    output = _validate_host_output(output, input_root, config, force_output)
    _validate_no_reference_rtl(input_root)
    identity = _read_image_identity(runtime_path, image, config)
    manifest_path = input_root / "candidate_manifest.jsonl"
    workspace_path = input_root / "workspace"
    manifest_sha256 = sha256_file(manifest_path)
    workspace_tree_sha256 = sha256_workspace_tree(workspace_path)
    identity.update(
        {
            "network_policy": "none",
            "resource_limits": {
                "cpus": config.cpus,
                "memory_bytes": config.memory_bytes,
                "memory_swap_bytes": config.memory_swap_bytes,
                "pids": config.pids,
                "file_size_bytes": config.file_size_bytes,
                "open_files": config.open_files,
                "work_bytes": config.work_bytes,
                "tmp_bytes": config.tmp_bytes,
                "wall_seconds": config.wall_seconds,
                "evidence_bytes": config.evidence_bytes,
            },
            "manifest_sha256": manifest_sha256,
            "workspace_tree_sha256": workspace_tree_sha256,
            "evidence_sha256": None,
            "partial_evidence_sha256": None,
        }
    )
    timeout = config.wall_seconds if wall_timeout is None else wall_timeout
    if timeout <= 0 or timeout > config.wall_seconds:
        raise RunnerError(
            f"wall timeout must be between zero and {config.wall_seconds} seconds"
        )

    with tempfile.TemporaryDirectory(prefix=".rtlbench-runner-output-") as output_tmp:
        with tempfile.TemporaryDirectory(prefix=".rtlbench-runner-env-") as env_tmp:
            staged_output = Path(output_tmp)
            staged_output.chmod(0o777)
            command = build_run_command(runtime_path, image, input_root, staged_output, config)
            returncode, timed_out = _execute(
                command,
                timeout=timeout,
                environment=_runtime_environment(
                    Path(env_tmp), include_runtime_dir=config.rootless
                ),
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
            if sha256_file(manifest_path) != manifest_sha256:
                raise RunnerError("input manifest changed during isolated execution")
            if sha256_workspace_tree(workspace_path) != workspace_tree_sha256:
                raise RunnerError("input workspace changed during isolated execution")
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
                identity["evidence_sha256"] = sha256_file(output)
            if partial_exists:
                _publish_file(partial, partial_destination, max_bytes=config.evidence_bytes)
                identity["partial_evidence_sha256"] = sha256_file(partial_destination)
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
    try:
        return _validate_candidate_evidence_file(path, max_bytes=max_bytes)
    except CandidateEvidenceValidationError as exc:
        raise RunnerError(str(exc)) from exc


def _validate_profile_selection(profile: str, acknowledge_rootful_runtime: bool) -> None:
    if profile not in PROFILES:
        raise RunnerError(f"unknown isolation profile: {profile}")
    if profile == PROFILE_PILOT_DOCKER and not acknowledge_rootful_runtime:
        raise RunnerError(
            "pilot-docker requires --acknowledge-rootful-runtime"
        )
    if profile == PROFILE_PRODUCTION_ROOTLESS and acknowledge_rootful_runtime:
        raise RunnerError(
            "--acknowledge-rootful-runtime is only valid with pilot-docker"
        )


def _validate_runtime(runtime: str | None, config: RunnerConfig) -> str:
    runtime = runtime or config.runtime_name
    runtime_path = shutil.which(runtime)
    if runtime_path is None:
        raise RunnerError(f"{config.profile} container runtime executable was not found")
    if Path(runtime_path).name != config.runtime_name:
        raise RunnerError(
            f"{config.profile} requires the {config.runtime_name} runtime executable"
        )
    if config.profile == PROFILE_PRODUCTION_ROOTLESS:
        result = _run_checked(
            [runtime_path, "info", "--format", "{{.Host.Security.Rootless}}"],
            timeout=10.0,
            environment=_runtime_environment(None, include_runtime_dir=True),
        )
        if result.returncode != 0 or result.stdout.strip().lower() != "true":
            raise RunnerError("production-rootless requires Podman rootless=true")
    else:
        result = _run_checked(
            [runtime_path, "version", "--format", "{{json .Server.Version}}"],
            timeout=10.0,
            environment=_runtime_environment(None, include_runtime_dir=False),
        )
        if result.returncode != 0:
            raise RunnerError("pilot-docker Docker container runtime is not usable")
        try:
            version = json.loads(result.stdout.strip())
        except json.JSONDecodeError as exc:
            raise RunnerError("docker version probe did not return JSON") from exc
        if not isinstance(version, str) or not version:
            raise RunnerError("docker version probe did not return a server version")
    return runtime_path


def _validate_image(image: str, config: RunnerConfig | None = None) -> str:
    config = config or load_config()
    if config.profile == PROFILE_PRODUCTION_ROOTLESS:
        if not REPOSITORY_IMAGE_DIGEST_RE.fullmatch(image):
            raise RunnerError(
                "production-rootless image must be referenced by name@sha256:<64 hex digits>"
            )
    elif not (
        LOCAL_IMAGE_ID_RE.fullmatch(image)
        or REPOSITORY_IMAGE_DIGEST_RE.fullmatch(image)
    ):
        raise RunnerError(
            "pilot-docker image must be a local sha256:<64 hex image ID or name@sha256:<64 hex digest>"
        )
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


def _read_image_identity(runtime: str, image: str, config: RunnerConfig) -> dict[str, Any]:
    image_id_result = _run_checked(
        [runtime, "image", "inspect", "--format", "{{.Id}}", image],
        timeout=10.0,
        environment=_runtime_environment(None, include_runtime_dir=config.rootless),
    )
    if image_id_result.returncode != 0:
        raise RunnerError("could not inspect the immutable RTLBench image")
    image_id_lines = image_id_result.stdout.strip().splitlines()
    if len(image_id_lines) != 1 or not LOCAL_IMAGE_ID_RE.fullmatch(image_id_lines[0]):
        raise RunnerError("RTLBench image inspection did not return one valid local image ID")
    image_id = image_id_lines[0]
    if LOCAL_IMAGE_ID_RE.fullmatch(image) and image_id != image:
        raise RunnerError("supplied local image ID does not match the inspected image ID")

    labels_result = _run_checked(
        [runtime, "image", "inspect", "--format", "{{json .Config.Labels}}", image],
        timeout=10.0,
        environment=_runtime_environment(None, include_runtime_dir=config.rootless),
    )
    if labels_result.returncode != 0:
        raise RunnerError("could not inspect the immutable RTLBench image")
    try:
        labels = json.loads(labels_result.stdout.strip())
    except json.JSONDecodeError as exc:
        raise RunnerError("RTLBench image labels are not valid JSON") from exc
    if not isinstance(labels, dict):
        raise RunnerError("RTLBench image labels are missing")
    local_image = LOCAL_IMAGE_ID_RE.fullmatch(image) is not None
    identity: dict[str, Any] = {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "profile": config.profile,
        "runtime": config.runtime_name,
        "runtime_mode": config.runtime_mode,
        "rootless": config.rootless,
        "runner_config_version": config.version,
        "image_identity_kind": "local-image-id" if local_image else "repository-digest",
        "image": image,
        "image_id": image_id,
        "image_digest": None if local_image else image.split("@", 1)[1],
    }
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
        raise RunnerError(f"could not start isolated RTLBench: {exc}") from exc
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


def _runtime_environment(
    env_root: Path | None, *, include_runtime_dir: bool = True
) -> dict[str, str]:
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
    if include_runtime_dir:
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
        raise RunnerError(f"container runtime probe failed: {exc}") from exc


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


def _publish_json(destination: Path, value: Mapping[str, Any]) -> None:
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


def _build_parser(*, require_profile: bool = False, allow_profile: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run RTLBench in an isolated container")
    if allow_profile:
        parser.add_argument(
            "--profile",
            required=require_profile,
            choices=sorted(PROFILES),
            help="isolation profile",
        )
    parser.add_argument(
        "--image",
        required=True,
        help="immutable IMAGE@sha256:<digest> or pilot sha256:<local-image-id>",
    )
    parser.add_argument("--input", type=Path, required=True, help="read-only candidate handoff")
    parser.add_argument("--output", type=Path, required=True, help="host evidence JSONL output")
    parser.add_argument("--acknowledge-rootful-runtime", action="store_true")
    parser.add_argument("--runtime")
    parser.add_argument("--wall-timeout", type=float)
    parser.add_argument("--force-output", action="store_true")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    require_profile: bool = False,
    default_profile: str = PROFILE_PRODUCTION_ROOTLESS,
    allow_profile: bool = True,
) -> int:
    args = _build_parser(
        require_profile=require_profile, allow_profile=allow_profile
    ).parse_args(argv)
    profile = args.profile if allow_profile else default_profile
    try:
        result = run_isolated(
            image=args.image,
            input_root=args.input,
            output=args.output,
            profile=profile,
            acknowledge_rootful_runtime=args.acknowledge_rootful_runtime,
            runtime=args.runtime,
            wall_timeout=args.wall_timeout,
            force_output=args.force_output,
            config_path=args.config,
        )
    except RunnerError as exc:
        print(f"isolated RTLBench runner: {exc}", file=os.sys.stderr)
        return 125
    if result.timed_out:
        print(
            "isolated RTLBench runner: wall timeout; preserved partial evidence",
            file=os.sys.stderr,
        )
        return 124
    print(f"wrote isolated evidence to {result.evidence_output or result.partial_output}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
