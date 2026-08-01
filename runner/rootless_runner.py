"""Fail-closed profile-aware launcher for RTLBench candidate verification."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
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
from rtlbench.candidate_verification import (  # noqa: E402
    DEFAULT_MAX_RUN_INPUT_BYTES,
)


CONFIG_PATH = Path(__file__).with_name("runner_config.json")
CONTAINER_PYTHONPATH = "/opt/rtlbench/src"
CONTAINER_ENVIRONMENT = (
    ("HOME", "/tmp"),
    ("TMPDIR", "/tmp"),
    ("LANG", "C"),
    ("LC_ALL", "C"),
    ("PYTHONPATH", CONTAINER_PYTHONPATH),
    ("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
)
MAX_RUNTIME_DIAGNOSTIC_BYTES = 65_536
ARCHIVE_STREAM_CHUNK_BYTES = 64 * 1024
PROBE_OUTPUT_LIMIT_BYTES = 8 * 1024
INPUT_VOLUME_NAME_RE = re.compile(r"^rtlbench-input-[0-9a-f]{32}$")
INPUT_VOLUME_LABEL = "io.rtlbench.runner.temporary-input"
INPUT_VOLUME_LABEL_VALUE = "rtlbench_runtime_input_v0.1"
MANAGED_CONTAINER_LABEL = "io.rtlbench.runner.managed-input-volume"
RUNTIME_DIAGNOSTIC_TRUNCATION_MARKER = (
    "[runtime diagnostic truncated to 65536 bytes]"
)
_ANSI_ESCAPE_RE = re.compile(
    r"(?:\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][0-2A-Z])"
)
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


@dataclass(frozen=True)
class ExecutionResult:
    returncode: int
    timed_out: bool
    diagnostic_output: str
    diagnostic_truncated: bool


@dataclass(frozen=True)
class InputSnapshot:
    archive_path: Path
    manifest_sha256: str
    workspace_tree_sha256: str
    payload_bytes: int


# These programs are fixed launcher code.  They are passed to the immutable
# runner image with ``python -c``; no host paths, user commands, or archive
# content is interpolated into them.
POPULATE_INPUT_VOLUME_PROGRAM = r'''
import os
import shutil
import stat
import sys
import tarfile
import tempfile

_ROOT = "/input"
_CHUNK = 65536
_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    _FLAGS |= os.O_NOFOLLOW


def _fail():
    raise SystemExit(1)


def _name(member):
    raw = member.name
    if not isinstance(raw, str) or not raw or "\\" in raw:
        _fail()
    if member.isdir():
        raw = raw.rstrip("/")
    if not raw or raw.startswith("/"):
        _fail()
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        _fail()
    return raw


def _allowed(name):
    return name == "candidate_manifest.jsonl" or name == "workspace" or name.startswith("workspace/")


def _mkdir(path):
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            _fail()


def _ensure_parent(path):
    parent = os.path.dirname(path)
    current = _ROOT
    relative = os.path.relpath(parent, _ROOT)
    if relative == ".":
        return
    for part in relative.split(os.sep):
        if part in ("", ".", ".."):
            _fail()
        current = os.path.join(current, part)
        _mkdir(current)


def _write_file(member, source, destination):
    _ensure_parent(destination)
    try:
        handle = os.open(destination, _FLAGS, 0o600)
    except OSError:
        _fail()
    try:
        remaining = member.size
        while remaining:
            chunk = source.read(min(_CHUNK, remaining))
            if not chunk:
                _fail()
            view = memoryview(chunk)
            while view:
                written = os.write(handle, view)
                if written <= 0:
                    _fail()
                view = view[written:]
            remaining -= len(chunk)
        if source.read(1):
            _fail()
        os.fsync(handle)
        os.fchmod(handle, 0o444)
    finally:
        os.close(handle)


def _normalize_tree(root):
    for current, directories, files in os.walk(root, topdown=False, followlinks=False):
        for name in files:
            path = os.path.join(current, name)
            if not stat.S_ISREG(os.lstat(path).st_mode):
                _fail()
            os.chmod(path, 0o444, follow_symlinks=False)
        for name in directories:
            path = os.path.join(current, name)
            if not stat.S_ISDIR(os.lstat(path).st_mode):
                _fail()
            os.chmod(path, 0o555, follow_symlinks=False)


try:
    if os.listdir(_ROOT):
        _fail()
    staging = tempfile.mkdtemp(prefix=".rtlbench-input-", dir=_ROOT)
    seen = set()
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as archive:
        previous_name = None
        for member in archive:
            name = _name(member)
            if not _allowed(name) or name in seen:
                _fail()
            if previous_name is not None and name < previous_name:
                _fail()
            if name.rsplit("/", 1)[-1].casefold() == "reference.sv":
                _fail()
            if (
                member.issym()
                or member.islnk()
                or member.isdev()
                or member.isfifo()
                or getattr(member, "issock", lambda: False)()
            ):
                _fail()
            if not (member.isdir() or member.isreg()):
                _fail()
            if member.uid != 0 or member.gid != 0 or member.uname or member.gname or member.mtime != 0:
                _fail()
            if member.isdir() and (member.mode & 0o7777) != 0o555:
                _fail()
            if member.isreg() and (member.mode & 0o7777) != 0o444:
                _fail()
            if member.isdir() and name == "candidate_manifest.jsonl":
                _fail()
            if member.isreg() and name == "workspace":
                _fail()
            seen.add(name)
            previous_name = name
            destination = os.path.join(staging, name)
            if member.isdir():
                _mkdir(destination)
            else:
                source = archive.extractfile(member)
                if source is None:
                    _fail()
                with source:
                    _write_file(member, source, destination)
    if "candidate_manifest.jsonl" not in seen or "workspace" not in seen:
        _fail()
    _normalize_tree(staging)
    # Keep the staging root directory writable until its two top-level
    # entries have been atomically moved into the volume root.
    os.chmod(os.path.join(staging, "workspace"), 0o700)
    os.replace(os.path.join(staging, "candidate_manifest.jsonl"), os.path.join(_ROOT, "candidate_manifest.jsonl"))
    os.replace(os.path.join(staging, "workspace"), os.path.join(_ROOT, "workspace"))
    os.rmdir(staging)
    os.chmod(os.path.join(_ROOT, "candidate_manifest.jsonl"), 0o444)
    os.chmod(os.path.join(_ROOT, "workspace"), 0o555)
    os.chmod(_ROOT, 0o555)
except Exception:
    try:
        if "staging" in globals():
            shutil.rmtree(staging)
    except Exception:
        pass
    raise SystemExit(1)
'''.strip()


PROBE_INPUT_VOLUME_PROGRAM = r'''
import hashlib
import json
import os
import stat

_ROOT = "/input"
_CHUNK = 65536


def _fail():
    raise SystemExit(1)


def _regular(path):
    try:
        metadata = os.lstat(path)
    except OSError:
        _fail()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        _fail()


def _directory(path):
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        _fail()
    if not stat.S_ISDIR(mode):
        _fail()


def _file_hash(path):
    _regular(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        handle = os.open(path, flags)
    except OSError:
        _fail()
    digest = hashlib.sha256()
    try:
        while True:
            chunk = os.read(handle, _CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(handle)
    return digest.hexdigest()


def _workspace_hash(root):
    _directory(root)
    digest = hashlib.sha256()
    def visit(current):
        try:
            entries = sorted(os.scandir(current), key=lambda item: item.name)
        except OSError:
            _fail()
        for entry in entries:
            path = entry.path
            if entry.is_symlink():
                _fail()
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                _fail()
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            if stat.S_ISDIR(metadata.st_mode):
                digest.update(b"D\0" + relative.encode() + b"\n")
                visit(path)
            elif stat.S_ISREG(metadata.st_mode):
                if entry.name.casefold() == "reference.sv":
                    _fail()
                digest.update(b"F\0" + relative.encode() + b"\0" + _file_hash(path).encode() + b"\n")
            else:
                _fail()
    visit(root)
    return digest.hexdigest()


try:
    _directory(_ROOT)
    names = sorted(os.listdir(_ROOT))
    if names != ["candidate_manifest.jsonl", "workspace"]:
        _fail()
    manifest = _file_hash(os.path.join(_ROOT, "candidate_manifest.jsonl"))
    workspace = _workspace_hash(os.path.join(_ROOT, "workspace"))
    print(json.dumps({"manifest_sha256": manifest, "workspace_tree_sha256": workspace}, sort_keys=True, separators=(",", ":")))
except Exception:
    raise SystemExit(1)
'''.strip()


class _DiagnosticTail:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._buffer = bytearray()
        self.truncated = False

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        if len(chunk) >= self._limit:
            discarded_previous = bool(self._buffer)
            self._buffer = bytearray(chunk[-self._limit :])
            self.truncated = (
                self.truncated or discarded_previous or len(chunk) > self._limit
            )
            return
        overflow = len(self._buffer) + len(chunk) - self._limit
        if overflow > 0:
            del self._buffer[:overflow]
            self.truncated = True
        self._buffer.extend(chunk)

    def bytes(self) -> bytes:
        return bytes(self._buffer)


def _drain_runtime_output(stream: Any, tail: _DiagnosticTail) -> None:
    """Drain a runtime pipe without retaining more than the bounded tail."""
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            tail.append(chunk)
    except (OSError, ValueError):
        return


def _sanitize_runtime_diagnostic(
    diagnostic: str, *, replacements: Mapping[str, str]
) -> tuple[str, bool]:
    """Remove terminal/control data and launcher-owned host paths."""
    normalized = diagnostic.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _ANSI_ESCAPE_RE.sub("", normalized).replace("\x00", "")
    normalized = "".join(
        character
        for character in normalized
        if character in {"\n", "\t"} or ord(character) >= 32
    )
    for source, replacement in sorted(
        ((source, replacement) for source, replacement in replacements.items() if source),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        normalized = normalized.replace(source, replacement)
    encoded = normalized.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_RUNTIME_DIAGNOSTIC_BYTES:
        return normalized, False
    return (
        encoded[-MAX_RUNTIME_DIAGNOSTIC_BYTES :].decode("utf-8", errors="replace"),
        True,
    )


def _runtime_diagnostic_replacements(
    *,
    input_root: Path | None,
    output: Path,
    staging: Path,
    runtime_environment: Path,
    input_volume: str | None = None,
    archive_path: Path | None = None,
) -> dict[str, str]:
    """Return stable placeholders for paths owned by this launcher."""
    replacements = {
        str(output.resolve()): "<output>",
        str(staging.resolve()): "<staging>",
        str(runtime_environment.resolve()): "<runtime-environment>",
        str(Path(tempfile.gettempdir()).resolve()): "<temporary>",
    }
    if input_root is not None:
        replacements[str(input_root.resolve())] = "<input>"
    if archive_path is not None:
        replacements[str(archive_path.resolve())] = "<temporary>"
    if input_volume:
        replacements[input_volume] = "<input-volume>"
    return replacements


def _format_runtime_diagnostic(diagnostic: str, truncated: bool) -> str:
    if diagnostic:
        result = f":\n{diagnostic}"
    else:
        result = "; no runtime diagnostic output"
    if truncated:
        result += f"\n{RUNTIME_DIAGNOSTIC_TRUNCATION_MARKER}"
    return result


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
    input_volume: str,
    output_root: Path,
    config: RunnerConfig,
) -> list[str]:
    """Build the only fixed candidate container command this runner permits."""
    _validate_input_volume_name(input_volume)
    command = [
        runtime,
        "run",
        *_pull_args(config),
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
        *_resource_arguments(config),
        "--mount",
        _volume_mount(input_volume, config, readonly=True),
        "--mount",
        f"type=bind,src={output_root},dst=/output",
        "--label",
        f"{MANAGED_CONTAINER_LABEL}={input_volume}",
        "--workdir",
        "/work",
        *_fixed_environment_arguments(include_bytecode_flag=False),
        "--entrypoint",
        "rtlbench",
        image,
        *config.inner_command[1:],
    ]
    if config.profile == PROFILE_PRODUCTION_ROOTLESS:
        index = command.index("--cap-drop")
        command[index:index] = ["--userns", "private"]
    return command


def _format_cpu_limit(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)


def _validate_input_volume_name(volume_name: str) -> str:
    if not isinstance(volume_name, str) or not INPUT_VOLUME_NAME_RE.fullmatch(volume_name):
        raise RunnerError("temporary input volume name is invalid")
    return volume_name


def _snapshot_entries(input_root: Path) -> list[tuple[str, Path, bool, int]]:
    """Return the only handoff entries that may enter the private archive."""

    entries: list[tuple[str, Path, bool, int]] = []

    def add_entry(relative: str, path: Path, is_directory: bool) -> None:
        if path.name.casefold() == "reference.sv":
            raise RunnerError("input handoff must not contain reference.sv")
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise RunnerError(f"could not inspect input handoff entry: {relative}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise RunnerError(f"input snapshot source contains symlink: {relative}")
        if is_directory:
            if not stat.S_ISDIR(metadata.st_mode):
                raise RunnerError(f"input snapshot directory changed type: {relative}")
            entries.append((relative, path, True, 0))
            return
        if not stat.S_ISREG(metadata.st_mode):
            raise RunnerError(f"input snapshot source is not a regular file: {relative}")
        if metadata.st_nlink != 1:
            raise RunnerError(f"input snapshot source contains a hard link: {relative}")
        entries.append((relative, path, False, metadata.st_size))

    manifest = input_root / "candidate_manifest.jsonl"
    workspace = input_root / "workspace"
    add_entry("candidate_manifest.jsonl", manifest, False)
    add_entry("workspace", workspace, True)

    def visit(directory: Path, prefix: str) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise RunnerError(f"could not enumerate input snapshot directory: {prefix}") from exc
        for child in children:
            relative = f"{prefix}/{child.name}"
            if child.is_symlink():
                raise RunnerError(f"input snapshot source contains symlink: {relative}")
            try:
                mode = child.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                raise RunnerError(f"could not inspect input snapshot entry: {relative}") from exc
            if stat.S_ISDIR(mode):
                add_entry(relative, Path(child.path), True)
                visit(Path(child.path), relative)
            elif stat.S_ISREG(mode):
                add_entry(relative, Path(child.path), False)
            else:
                raise RunnerError(f"input snapshot source contains special file: {relative}")

    visit(workspace, "workspace")
    entries.sort(key=lambda item: item[0])
    return entries


class _BoundedFileReader:
    def __init__(self, handle: Any, size: int) -> None:
        self._handle = handle
        self._remaining = size
        self.read_bytes = 0

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        if size < 0 or size > ARCHIVE_STREAM_CHUNK_BYTES:
            size = ARCHIVE_STREAM_CHUNK_BYTES
        size = min(size, self._remaining)
        data = self._handle.read(size)
        if data:
            self.read_bytes += len(data)
            self._remaining -= len(data)
        return data


def _open_snapshot_file(input_root: Path, relative: str) -> int:
    """Open a snapshot file without following any directory or file symlink."""

    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    file_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        file_flags |= os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        current = os.open(input_root, directory_flags)
        descriptors.append(current)
        parts = relative.split("/")
        for part in parts[:-1]:
            current = os.open(part, directory_flags, dir_fd=current)
            descriptors.append(current)
        return os.open(parts[-1], file_flags, dir_fd=current)
    finally:
        # The returned file descriptor owns its own open file description; all
        # directory descriptors used for traversal can be closed immediately.
        if descriptors:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _create_input_snapshot_archive(
    input_root: Path,
    archive_path: Path,
    *,
    expected_manifest_sha256: str,
    expected_workspace_tree_sha256: str,
) -> InputSnapshot:
    """Create a deterministic, private archive of only verifier input files."""

    input_root = Path(input_root).resolve()
    archive_path = Path(archive_path)
    if archive_path.exists() or archive_path.is_symlink():
        raise RunnerError("input snapshot archive path already exists")
    _validate_no_reference_rtl(input_root)
    entries = _snapshot_entries(input_root)
    payload_bytes = sum(size for _, _, is_directory, size in entries if not is_directory)
    if payload_bytes > DEFAULT_MAX_RUN_INPUT_BYTES:
        raise RunnerError(
            f"input snapshot exceeds {DEFAULT_MAX_RUN_INPUT_BYTES} bytes"
        )
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        archive_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as archive_handle:
            descriptor = -1
            with tarfile.open(
                fileobj=archive_handle,
                mode="w",
                format=tarfile.PAX_FORMAT,
            ) as archive:
                for relative, path, is_directory, expected_size in entries:
                    normalized_name = relative + "/" if is_directory else relative
                    info = tarfile.TarInfo(normalized_name)
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mtime = 0
                    info.mode = 0o555 if is_directory else 0o444
                    if is_directory:
                        info.type = tarfile.DIRTYPE
                        archive.addfile(info)
                        continue
                    info.type = tarfile.REGTYPE
                    info.size = expected_size
                    try:
                        file_descriptor = _open_snapshot_file(input_root, relative)
                    except OSError as exc:
                        raise RunnerError(
                            f"could not open input snapshot file: {relative}"
                        ) from exc
                    try:
                        before = os.fstat(file_descriptor)
                        if (
                            not stat.S_ISREG(before.st_mode)
                            or before.st_nlink != 1
                            or before.st_size != expected_size
                        ):
                            raise RunnerError(
                                f"input snapshot file changed before archive: {relative}"
                            )
                        with os.fdopen(file_descriptor, "rb", closefd=True) as source:
                            file_descriptor = -1
                            bounded = _BoundedFileReader(source, expected_size)
                            archive.addfile(info, fileobj=bounded)
                            after = os.fstat(source.fileno())
                            if bounded.read_bytes != expected_size or (
                                not stat.S_ISREG(after.st_mode)
                                or after.st_nlink != 1
                                or after.st_size != expected_size
                                or after.st_ino != before.st_ino
                                or after.st_dev != before.st_dev
                            ):
                                raise RunnerError(
                                    f"input snapshot file changed while archiving: {relative}"
                                )
                    finally:
                        if file_descriptor >= 0:
                            os.close(file_descriptor)
            archive_handle.flush()
            os.fsync(archive_handle.fileno())
        os.chmod(archive_path, 0o600)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            archive_path.unlink()
        except FileNotFoundError:
            pass
        raise
    try:
        manifest_sha256 = sha256_file(input_root / "candidate_manifest.jsonl")
        workspace_tree_sha256 = sha256_workspace_tree(input_root / "workspace")
        if manifest_sha256 != expected_manifest_sha256:
            raise RunnerError("input manifest changed while creating snapshot archive")
        if workspace_tree_sha256 != expected_workspace_tree_sha256:
            raise RunnerError("input workspace changed while creating snapshot archive")
    except Exception:
        try:
            archive_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return InputSnapshot(
        archive_path=archive_path,
        manifest_sha256=manifest_sha256,
        workspace_tree_sha256=workspace_tree_sha256,
        payload_bytes=payload_bytes,
    )


def _pull_args(config: RunnerConfig) -> list[str]:
    return (
        ["--pull=never"]
        if config.profile == PROFILE_PRODUCTION_ROOTLESS
        else ["--pull", "never"]
    )


def _volume_mount(
    volume_name: str,
    config: RunnerConfig,
    *,
    readonly: bool,
) -> str:
    _validate_input_volume_name(volume_name)
    options = ["type=volume", f"src={volume_name}", "dst=/input"]
    if readonly:
        options.append("ro" if config.profile == PROFILE_PRODUCTION_ROOTLESS else "readonly")
    options.append("volume-nocopy")
    return ",".join(options)


def _resource_arguments(config: RunnerConfig) -> list[str]:
    return [
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
    ]


def _fixed_environment_arguments(*, include_bytecode_flag: bool) -> list[str]:
    arguments = [
        argument
        for name, value in CONTAINER_ENVIRONMENT
        for argument in ("--env", f"{name}={value}")
    ]
    if include_bytecode_flag:
        arguments.extend(("--env", "PYTHONDONTWRITEBYTECODE=1"))
    return arguments


def _helper_command(
    runtime: str,
    image: str,
    volume_name: str,
    config: RunnerConfig,
    *,
    program: str,
    user: str,
    readonly_volume: bool,
    interactive: bool,
) -> list[str]:
    command = [
        runtime,
        "run",
        *_pull_args(config),
        "--rm",
    ]
    if interactive:
        command.append("--interactive")
    command.extend(
        [
            "--network",
            "none",
            "--user",
            user,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            *_resource_arguments(config),
            "--mount",
            _volume_mount(volume_name, config, readonly=readonly_volume),
            "--label",
            f"{MANAGED_CONTAINER_LABEL}={volume_name}",
            *_fixed_environment_arguments(include_bytecode_flag=True),
            "--entrypoint",
            "/usr/local/bin/python",
            image,
            "-c",
            program,
        ]
    )
    if config.profile == PROFILE_PRODUCTION_ROOTLESS:
        index = command.index("--cap-drop")
        command[index:index] = ["--userns", "private"]
    return command


def _build_population_command(
    runtime: str, image: str, volume_name: str, config: RunnerConfig
) -> list[str]:
    return _helper_command(
        runtime,
        image,
        volume_name,
        config,
        program=POPULATE_INPUT_VOLUME_PROGRAM,
        user="0:0",
        readonly_volume=False,
        interactive=True,
    )


def _build_probe_command(
    runtime: str, image: str, volume_name: str, config: RunnerConfig
) -> list[str]:
    return _helper_command(
        runtime,
        image,
        volume_name,
        config,
        program=PROBE_INPUT_VOLUME_PROGRAM,
        user=config.runtime_user,
        readonly_volume=True,
        interactive=False,
    )


def _new_input_volume_name() -> str:
    return f"rtlbench-input-{secrets.token_hex(16)}"


def _create_input_volume(
    runtime: str,
    volume_name: str,
    config: RunnerConfig,
) -> None:
    _validate_input_volume_name(volume_name)
    environment = _runtime_environment(None, include_runtime_dir=config.rootless)
    existing = _run_checked(
        [runtime, "volume", "inspect", volume_name],
        timeout=10.0,
        environment=environment,
    )
    if existing.returncode == 0:
        raise RunnerError("temporary input volume already exists")
    created = False
    try:
        result = _run_checked(
            [
                runtime,
                "volume",
                "create",
                "--label",
                f"{INPUT_VOLUME_LABEL}={INPUT_VOLUME_LABEL_VALUE}",
                volume_name,
            ],
            timeout=10.0,
            environment=environment,
        )
        if result.returncode != 0:
            raise RunnerError("container runtime did not create the requested input volume")
        created = True
        if result.stdout.strip() != volume_name:
            raise RunnerError("container runtime did not return the requested input volume name")
        inspected = _run_checked(
            [
                runtime,
                "volume",
                "inspect",
                "--format",
                "{{json .Labels}}",
                volume_name,
            ],
            timeout=10.0,
            environment=environment,
        )
        if inspected.returncode != 0:
            raise RunnerError("could not inspect the temporary input volume")
        try:
            labels = json.loads(inspected.stdout.strip())
        except json.JSONDecodeError as exc:
            raise RunnerError("temporary input volume labels are not valid JSON") from exc
        if not isinstance(labels, dict) or labels.get(INPUT_VOLUME_LABEL) != INPUT_VOLUME_LABEL_VALUE:
            raise RunnerError("temporary input volume label is invalid")
    except Exception:
        if created:
            _remove_input_volume(runtime, volume_name, config)
        raise


def _remove_input_volume(
    runtime: str,
    volume_name: str,
    config: RunnerConfig,
) -> None:
    environment = _runtime_environment(None, include_runtime_dir=config.rootless)
    removed = _run_checked(
        [runtime, "volume", "rm", volume_name],
        timeout=10.0,
        environment=environment,
    )
    if removed.returncode == 0:
        remaining = _run_checked(
            [runtime, "volume", "inspect", volume_name],
            timeout=10.0,
            environment=environment,
        )
        if remaining.returncode != 0:
            return
    managed = _run_checked(
        [
            runtime,
            "ps",
            "-aq",
            "--filter",
            f"label={MANAGED_CONTAINER_LABEL}={volume_name}",
        ],
        timeout=10.0,
        environment=environment,
    )
    container_ids = [item for item in managed.stdout.split() if item]
    if container_ids:
        _run_checked(
            [runtime, "rm", "-f", *container_ids],
            timeout=10.0,
            environment=environment,
        )
        removed = _run_checked(
            [runtime, "volume", "rm", volume_name],
            timeout=10.0,
            environment=environment,
        )
        remaining = _run_checked(
            [runtime, "volume", "inspect", volume_name],
            timeout=10.0,
            environment=environment,
        )
        if removed.returncode == 0 and remaining.returncode != 0:
            return
    raise RunnerError("could not remove temporary input volume <input-volume>")


def _run_input_helper(
    command: Sequence[str],
    *,
    archive_path: Path | None,
    timeout: float,
    environment: Mapping[str, str],
    replacements: Mapping[str, str],
    description: str,
) -> ExecutionResult:
    result = _execute(
        command,
        timeout=timeout,
        environment=environment,
        diagnostic_replacements=replacements,
        stdin_path=archive_path,
    )
    if result.timed_out:
        raise RunnerError(
            f"{description} timed out"
            + _format_runtime_diagnostic(
                result.diagnostic_output, result.diagnostic_truncated
            )
        )
    if result.returncode != 0:
        raise RunnerError(
            f"{description} failed with status {result.returncode}"
            + _format_runtime_diagnostic(
                result.diagnostic_output, result.diagnostic_truncated
            )
        )
    return result


def _probe_input_volume(
    command: Sequence[str],
    *,
    expected_manifest_sha256: str,
    expected_workspace_tree_sha256: str,
    timeout: float,
    environment: Mapping[str, str],
    replacements: Mapping[str, str],
) -> None:
    result = _run_input_helper(
        command,
        archive_path=None,
        timeout=timeout,
        environment=environment,
        replacements=replacements,
        description="runtime-user input probe",
    )
    encoded = result.diagnostic_output.encode("utf-8", errors="replace")
    if result.diagnostic_truncated or len(encoded) > PROBE_OUTPUT_LIMIT_BYTES:
        raise RunnerError("runtime-user input probe produced excessive output")
    try:
        value = json.loads(result.diagnostic_output)
    except json.JSONDecodeError as exc:
        raise RunnerError("runtime-user input probe returned malformed output") from exc
    if not isinstance(value, dict) or set(value) != {
        "manifest_sha256",
        "workspace_tree_sha256",
    }:
        raise RunnerError("runtime-user input probe returned unexpected output")
    if (
        value["manifest_sha256"] != expected_manifest_sha256
        or value["workspace_tree_sha256"] != expected_workspace_tree_sha256
    ):
        raise RunnerError("staged input hashes do not match canonical handoff hashes")


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
            runtime_environment = Path(env_tmp)
            staged_output = Path(output_tmp)
            staged_output.chmod(0o777)
            runtime_environment_values = _runtime_environment(
                runtime_environment, include_runtime_dir=config.rootless
            )
            execution: ExecutionResult
            final: Path
            partial: Path
            final_exists = False
            partial_exists = False
            volume_name = _new_input_volume_name()
            volume_created = False
            with tempfile.TemporaryDirectory(prefix=".rtlbench-runner-input-") as input_tmp:
                archive_path = Path(input_tmp) / "input.tar"
                replacements = _runtime_diagnostic_replacements(
                    input_root=input_root,
                    output=output,
                    staging=staged_output,
                    runtime_environment=runtime_environment,
                    input_volume=volume_name,
                    archive_path=archive_path,
                )
                try:
                    _create_input_snapshot_archive(
                        input_root,
                        archive_path,
                        expected_manifest_sha256=manifest_sha256,
                        expected_workspace_tree_sha256=workspace_tree_sha256,
                    )
                    _create_input_volume(runtime_path, volume_name, config)
                    volume_created = True
                    population = _run_input_helper(
                        _build_population_command(
                            runtime_path, image, volume_name, config
                        ),
                        archive_path=archive_path,
                        timeout=timeout,
                        environment=runtime_environment_values,
                        replacements=replacements,
                        description="input population helper",
                    )
                    if population.diagnostic_output:
                        raise RunnerError("input population helper produced unexpected output")
                    _probe_input_volume(
                        _build_probe_command(runtime_path, image, volume_name, config),
                        expected_manifest_sha256=manifest_sha256,
                        expected_workspace_tree_sha256=workspace_tree_sha256,
                        timeout=timeout,
                        environment=runtime_environment_values,
                        replacements=replacements,
                    )
                    execution = _execute(
                        build_run_command(
                            runtime_path, image, volume_name, staged_output, config
                        ),
                        timeout=timeout,
                        environment=runtime_environment_values,
                        diagnostic_replacements=replacements,
                    )
                    _reject_unexpected_output(staged_output, config)
                    final = staged_output / "candidate_evidence.jsonl"
                    partial = staged_output / "candidate_evidence.jsonl.rtlbench-partial"
                    final_exists = final.exists()
                    partial_exists = partial.exists()
                    if final_exists:
                        validate_candidate_evidence_file(
                            final, max_bytes=config.evidence_bytes
                        )
                    if not final_exists and not partial_exists:
                        if execution.timed_out:
                            raise RunnerError(
                                "runner wall timeout expired without preserved evidence"
                                + _format_runtime_diagnostic(
                                    execution.diagnostic_output,
                                    execution.diagnostic_truncated,
                                )
                            )
                        raise RunnerError(
                            f"isolated RTLBench exited with status {execution.returncode} without evidence"
                            + _format_runtime_diagnostic(
                                execution.diagnostic_output,
                                execution.diagnostic_truncated,
                            )
                        )
                    if sha256_file(manifest_path) != manifest_sha256:
                        raise RunnerError("input manifest changed during isolated execution")
                    if sha256_workspace_tree(workspace_path) != workspace_tree_sha256:
                        raise RunnerError("input workspace changed during isolated execution")
                finally:
                    if volume_created:
                        _remove_input_volume(runtime_path, volume_name, config)
            # The input volume, private archive, and input temporary directory
            # are gone before any managed evidence is published.
            if sha256_file(manifest_path) != manifest_sha256:
                raise RunnerError("input manifest changed before evidence publication")
            if sha256_workspace_tree(workspace_path) != workspace_tree_sha256:
                raise RunnerError("input workspace changed before evidence publication")
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
                returncode=124 if execution.timed_out else execution.returncode,
                evidence_output=output if final_exists else None,
                partial_output=partial_destination if partial_exists else None,
                identity_output=identity_destination,
                timed_out=execution.timed_out,
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
    command: Sequence[str],
    *,
    timeout: float,
    environment: Mapping[str, str],
    diagnostic_replacements: Mapping[str, str] | None = None,
    stdin_path: Path | None = None,
) -> ExecutionResult:
    replacements = diagnostic_replacements or {}
    stdin_handle: Any | None = None
    try:
        if stdin_path is not None:
            stdin_handle = Path(stdin_path).open("rb")
        process = subprocess.Popen(
            list(command),
            stdin=stdin_handle if stdin_handle is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=dict(environment),
        )
    except OSError as exc:
        diagnostic, truncated = _sanitize_runtime_diagnostic(
            str(exc), replacements=replacements
        )
        raise RunnerError(
            "could not start isolated RTLBench"
            + _format_runtime_diagnostic(diagnostic, truncated)
        ) from exc
    finally:
        if stdin_handle is not None:
            try:
                stdin_handle.close()
            except OSError:
                pass
    if process.stdout is None:
        _terminate_process_group(process)
        raise RunnerError("isolated RTLBench runtime did not provide a diagnostic pipe")
    tail = _DiagnosticTail(MAX_RUNTIME_DIAGNOSTIC_BYTES)
    reader = threading.Thread(
        target=_drain_runtime_output,
        args=(process.stdout, tail),
        name="rtlbench-runtime-diagnostic",
        daemon=True,
    )
    reader.start()
    timed_out = False
    try:
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_process_group(process)
            returncode = 124
            timed_out = True
        except KeyboardInterrupt:
            _terminate_process_group(process)
            raise
    finally:
        reader.join(timeout=5.0)
        if reader.is_alive():
            try:
                process.stdout.close()
            except (OSError, ValueError):
                pass
            reader.join(timeout=5.0)
        if reader.is_alive():
            raise RunnerError("isolated RTLBench diagnostic reader did not terminate")
        try:
            process.stdout.close()
        except (OSError, ValueError):
            pass
    diagnostic, sanitized_truncated = _sanitize_runtime_diagnostic(
        tail.bytes().decode("utf-8", errors="replace"),
        replacements=replacements,
    )
    return ExecutionResult(
        returncode=returncode,
        timed_out=timed_out,
        diagnostic_output=diagnostic,
        diagnostic_truncated=tail.truncated or sanitized_truncated,
    )


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
