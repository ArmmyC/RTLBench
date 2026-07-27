"""Strict JSONL manifest loading for RTL candidate verification."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MANIFEST_SCHEMA_VERSION = "rtl_candidate_manifest_v0.1"
REQUESTED_CHECKS = ("compile", "simulation", "lint", "synthesis")
SIMULATION_RESULT_CONTRACTS = ("mismatch_count_v1", "exit_code_v1")
_TOP_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_TOP_LEVEL_FIELDS = {
    "schema_version",
    "candidate_id",
    "task_id",
    "source_id",
    "attempt",
    "top_module",
    "testbench_top",
    "candidate_rtl_path",
    "testbench_path",
    "support_files",
    "simulation_result_contract",
    "requested_checks",
}


class CandidateManifestValidationError(ValueError):
    """Raised when a candidate manifest or its declared inputs are invalid."""


class CandidateWorkspaceValidationError(CandidateManifestValidationError):
    """Raised when the workspace root fails filesystem preflight."""


@dataclass(frozen=True)
class CandidateRow:
    schema_version: str
    candidate_id: str
    task_id: str
    source_id: str
    attempt: int
    top_module: str
    testbench_top: str
    candidate_rtl_path: str
    testbench_path: str
    support_files: tuple[str, ...]
    simulation_result_contract: str
    requested_checks: dict[str, bool]
    resolved_paths: dict[str, Path]
    resolved_support_paths: dict[str, Path]

    @property
    def artifact_paths(self) -> tuple[tuple[str, Path], ...]:
        paths = [
            ("candidate_rtl_path", self.resolved_paths["candidate_rtl_path"]),
            ("testbench_path", self.resolved_paths["testbench_path"]),
        ]
        paths.extend(
            (path, self.resolved_support_paths[path]) for path in self.support_files
        )
        return tuple(paths)


def load_manifest(path: Path, workspace_root: Path) -> list[CandidateRow]:
    """Load and validate every row before any tool is discovered or executed."""

    manifest_path = Path(path).expanduser()
    root = _validate_workspace_root(Path(workspace_root).expanduser())
    if not manifest_path.exists() or not manifest_path.is_file():
        raise CandidateManifestValidationError(
            f"manifest is not a regular file: {manifest_path}"
        )
    if manifest_path.is_symlink() or _contains_symlink(manifest_path):
        raise CandidateManifestValidationError("manifest must not be a symlink")

    rows: list[CandidateRow] = []
    seen_candidate_ids: set[str] = set()
    seen_task_attempts: set[tuple[str, int]] = set()
    try:
        with manifest_path.open("r", encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                if not raw_line.strip():
                    continue
                try:
                    value = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    raise CandidateManifestValidationError(
                        f"line {line_number}: malformed JSON: {exc.msg}"
                    ) from exc
                row = _validate_row(value, line_number, root)
                if row.candidate_id in seen_candidate_ids:
                    raise CandidateManifestValidationError(
                        f"line {line_number}: duplicate candidate_id {row.candidate_id!r}"
                    )
                task_attempt = (row.task_id, row.attempt)
                if task_attempt in seen_task_attempts:
                    raise CandidateManifestValidationError(
                        f"line {line_number}: duplicate (task_id, attempt) {task_attempt!r}"
                    )
                seen_candidate_ids.add(row.candidate_id)
                seen_task_attempts.add(task_attempt)
                rows.append(row)
    except UnicodeDecodeError as exc:
        raise CandidateManifestValidationError(
            f"manifest is not valid UTF-8: {exc}"
        ) from exc
    except OSError as exc:
        raise CandidateManifestValidationError(
            f"could not read manifest: {exc}"
        ) from exc

    if not rows:
        raise CandidateManifestValidationError("manifest contains no nonblank rows")
    return rows


def _validate_row(value: Any, line_number: int, root: Path) -> CandidateRow:
    prefix = f"line {line_number}"
    if not isinstance(value, dict):
        raise CandidateManifestValidationError(f"{prefix}: row must be a JSON object")
    unknown = set(value) - _TOP_LEVEL_FIELDS
    missing = _TOP_LEVEL_FIELDS - set(value)
    if unknown:
        raise CandidateManifestValidationError(
            f"{prefix}: unknown fields: {sorted(unknown)}"
        )
    if missing:
        raise CandidateManifestValidationError(
            f"{prefix}: missing fields: {sorted(missing)}"
        )

    _string(value["schema_version"], "schema_version", prefix, exact=MANIFEST_SCHEMA_VERSION)
    candidate_id = _string(value["candidate_id"], "candidate_id", prefix, nonempty=True)
    task_id = _string(value["task_id"], "task_id", prefix, nonempty=True)
    source_id = _string(value["source_id"], "source_id", prefix, nonempty=True)
    attempt = _positive_int(value["attempt"], "attempt", prefix)
    top_module = _string(value["top_module"], "top_module", prefix, nonempty=True)
    if not _TOP_MODULE_RE.fullmatch(top_module):
        raise CandidateManifestValidationError(
            f"{prefix}: invalid top_module {top_module!r}"
        )
    testbench_top = _string(
        value["testbench_top"], "testbench_top", prefix, nonempty=True
    )
    if not _TOP_MODULE_RE.fullmatch(testbench_top):
        raise CandidateManifestValidationError(
            f"{prefix}: invalid testbench_top {testbench_top!r}"
        )

    candidate_rtl_path = _string(
        value["candidate_rtl_path"], "candidate_rtl_path", prefix, nonempty=True
    )
    testbench_path = _string(
        value["testbench_path"], "testbench_path", prefix, nonempty=True
    )

    support_value = value["support_files"]
    if not isinstance(support_value, list):
        raise CandidateManifestValidationError(
            f"{prefix}: support_files must be a list"
        )
    support_files = tuple(
        _string(item, f"support_files[{index}]", prefix, nonempty=True)
        for index, item in enumerate(support_value)
    )
    if len(set(support_files)) != len(support_files):
        raise CandidateManifestValidationError(
            f"{prefix}: support_files must not contain duplicates"
        )

    role_paths = (candidate_rtl_path, testbench_path, *support_files)
    if len(set(role_paths)) != len(role_paths):
        raise CandidateManifestValidationError(
            f"{prefix}: artifact paths must be unique across roles"
        )

    simulation_result_contract = _string(
        value["simulation_result_contract"],
        "simulation_result_contract",
        prefix,
        nonempty=True,
    )
    if simulation_result_contract not in SIMULATION_RESULT_CONTRACTS:
        raise CandidateManifestValidationError(
            f"{prefix}: unsupported simulation_result_contract "
            f"{simulation_result_contract!r}"
        )

    checks_value = value["requested_checks"]
    if not isinstance(checks_value, dict):
        raise CandidateManifestValidationError(
            f"{prefix}: requested_checks must be an object"
        )
    if set(checks_value) != set(REQUESTED_CHECKS):
        raise CandidateManifestValidationError(
            f"{prefix}: requested_checks must contain exactly {list(REQUESTED_CHECKS)}"
        )
    requested_checks: dict[str, bool] = {}
    for check_name in REQUESTED_CHECKS:
        check_value = checks_value[check_name]
        if type(check_value) is not bool:
            raise CandidateManifestValidationError(
                f"{prefix}: requested_checks.{check_name} must be boolean"
            )
        requested_checks[check_name] = check_value
    if not requested_checks["compile"]:
        raise CandidateManifestValidationError(
            f"{prefix}: requested_checks.compile must be true"
        )
    if not requested_checks["simulation"]:
        raise CandidateManifestValidationError(
            f"{prefix}: requested_checks.simulation must be true"
        )

    resolved_paths: dict[str, Path] = {}
    resolved_support_paths: dict[str, Path] = {}
    named_paths = {
        "candidate_rtl_path": candidate_rtl_path,
        "testbench_path": testbench_path,
    }
    for field_name, relative_path in named_paths.items():
        resolved_paths[field_name] = _resolve_artifact(
            root, relative_path, prefix, field_name
        )
        if relative_path not in resolved_paths:
            resolved_paths[relative_path] = resolved_paths[field_name]
    for support_file in support_files:
        resolved_support_paths[support_file] = _resolve_artifact(
            root, support_file, prefix, "support_files"
        )
        if support_file not in resolved_paths:
            resolved_paths[support_file] = resolved_support_paths[support_file]

    artifact_entries = [
        ("candidate_rtl_path", resolved_paths["candidate_rtl_path"]),
        ("testbench_path", resolved_paths["testbench_path"]),
    ]
    artifact_entries.extend(
        (f"support_files[{index}]", resolved_support_paths[path])
        for index, path in enumerate(support_files)
    )
    for index, (left_label, left_path) in enumerate(artifact_entries):
        for right_label, right_path in artifact_entries[index + 1 :]:
            if _paths_alias(left_path, right_path):
                raise CandidateManifestValidationError(
                    f"{prefix}: artifact paths alias: {left_label} and {right_label}"
                )

    return CandidateRow(
        schema_version=MANIFEST_SCHEMA_VERSION,
        candidate_id=candidate_id,
        task_id=task_id,
        source_id=source_id,
        attempt=attempt,
        top_module=top_module,
        testbench_top=testbench_top,
        candidate_rtl_path=candidate_rtl_path,
        testbench_path=testbench_path,
        support_files=support_files,
        simulation_result_contract=simulation_result_contract,
        requested_checks=requested_checks,
        resolved_paths=resolved_paths,
        resolved_support_paths=resolved_support_paths,
    )


def _validate_workspace_root(root: Path) -> Path:
    if not root.exists() or not root.is_dir():
        raise CandidateWorkspaceValidationError(
            f"workspace-root is not a directory: {root}"
        )
    if _contains_symlink(root):
        raise CandidateWorkspaceValidationError(
            "workspace-root must not contain symlinked path components"
        )
    return root.resolve()


def _resolve_artifact(
    root: Path, value: str, prefix: str, field_name: str
) -> Path:
    _validate_relative_path(value, prefix, field_name)
    target = root / value
    if _contains_symlink(target):
        raise CandidateManifestValidationError(
            f"{prefix}: symlinked artifact path rejected: {value!r}"
        )
    resolved = target.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CandidateManifestValidationError(
            f"{prefix}: path escapes workspace-root: {value!r}"
        ) from exc
    if not resolved.is_file():
        raise CandidateManifestValidationError(
            f"{prefix}: artifact is not a regular file: {value!r}"
        )
    return resolved


def _validate_relative_path(value: str, prefix: str, field_name: str) -> None:
    if (
        "\x00" in value
        or "\\" in value
        or value.startswith("/")
        or _WINDOWS_DRIVE_RE.match(value)
    ):
        raise CandidateManifestValidationError(
            f"{prefix}: {field_name} must be a normalized POSIX relative path"
        )
    parts = value.split("/")
    if not parts or any(part == "" for part in parts):
        raise CandidateManifestValidationError(
            f"{prefix}: {field_name} contains an empty path component"
        )
    if any(part in {".", ".."} for part in parts):
        raise CandidateManifestValidationError(
            f"{prefix}: {field_name} contains an unsafe path component"
        )


def _contains_symlink(path: Path) -> bool:
    """Inspect existing components without following symlinks."""

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


def _string(
    value: Any,
    field_name: str,
    prefix: str,
    *,
    nonempty: bool = False,
    exact: str | None = None,
) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise CandidateManifestValidationError(
            f"{prefix}: {field_name} must be a string"
        )
    if exact is not None and value != exact:
        raise CandidateManifestValidationError(
            f"{prefix}: {field_name} must equal {exact!r}"
        )
    return value


def _positive_int(value: Any, field_name: str, prefix: str) -> int:
    if type(value) is not int or value < 1:
        raise CandidateManifestValidationError(
            f"{prefix}: {field_name} must be an integer greater than or equal to 1"
        )
    return value


def _paths_alias(left: Path, right: Path) -> bool:
    if left == right:
        return True
    try:
        if left.samefile(right):
            return True
    except OSError:
        pass
    try:
        left_stat = left.stat()
        right_stat = right.stat()
    except OSError:
        return False
    return (
        getattr(left_stat, "st_dev", None) == getattr(right_stat, "st_dev", None)
        and getattr(left_stat, "st_ino", None) == getattr(right_stat, "st_ino", None)
    )
