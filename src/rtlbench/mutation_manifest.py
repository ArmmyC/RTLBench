"""Strict JSONL manifest loading for mutation verification."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

MANIFEST_SCHEMA_VERSION = "rtl_mutation_manifest_v0.1"
REQUESTED_CHECKS = (
    "compile",
    "lint",
    "simulation",
    "synthesis",
    "equivalence",
    "activity",
)
_TOP_MODULE_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_TOP_LEVEL_FIELDS = {
    "schema_version",
    "mutation_id",
    "source_id",
    "top_module",
    "original_rtl_path",
    "mutated_rtl_path",
    "repaired_rtl_path",
    "testbench_path",
    "support_files",
    "mutation_type",
    "mutated_signal",
    "changed_location",
    "requested_checks",
}
_LOCATION_FIELDS = {"file", "line_start", "line_end"}


class ManifestValidationError(ValueError):
    """Raised when the complete manifest fails strict validation."""


@dataclass(frozen=True)
class ChangedLocation:
    file: str
    line_start: int
    line_end: int


@dataclass(frozen=True)
class MutationRow:
    schema_version: str
    mutation_id: str
    source_id: str
    top_module: str
    original_rtl_path: str
    mutated_rtl_path: str
    repaired_rtl_path: str
    testbench_path: str
    support_files: tuple[str, ...]
    mutation_type: str
    mutated_signal: str
    changed_location: ChangedLocation
    requested_checks: dict[str, bool]
    resolved_paths: dict[str, Path]

    @property
    def artifact_paths(self) -> tuple[tuple[str, Path], ...]:
        paths = [
            ("original", self.resolved_paths["original_rtl_path"]),
            ("mutated", self.resolved_paths["mutated_rtl_path"]),
            ("repaired", self.resolved_paths["repaired_rtl_path"]),
            ("testbench", self.resolved_paths["testbench_path"]),
        ]
        paths.extend((path, self.resolved_paths[path]) for path in self.support_files)
        return tuple(paths)


def load_manifest(path: Path, workspace_root: Path) -> list[MutationRow]:
    """Load and validate every JSONL row before any tool is executed."""

    manifest_path = Path(path)
    root = _validate_root(Path(workspace_root))
    if not manifest_path.exists() or not manifest_path.is_file():
        raise ManifestValidationError(f"manifest is not a regular file: {manifest_path}")
    if manifest_path.is_symlink():
        raise ManifestValidationError("manifest must not be a symlink")

    rows: list[MutationRow] = []
    seen_ids: set[str] = set()
    try:
        with manifest_path.open("r", encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                if not raw_line.strip():
                    continue
                try:
                    value = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    raise ManifestValidationError(
                        f"line {line_number}: malformed JSON: {exc.msg}"
                    ) from exc
                row = _validate_row(value, line_number, root)
                if row.mutation_id in seen_ids:
                    raise ManifestValidationError(
                        f"line {line_number}: duplicate mutation_id {row.mutation_id!r}"
                    )
                seen_ids.add(row.mutation_id)
                rows.append(row)
    except UnicodeDecodeError as exc:
        raise ManifestValidationError(f"manifest is not valid UTF-8: {exc}") from exc

    if not rows:
        raise ManifestValidationError("manifest contains no nonblank rows")
    return rows


def _validate_row(value: Any, line_number: int, root: Path) -> MutationRow:
    prefix = f"line {line_number}"
    if not isinstance(value, dict):
        raise ManifestValidationError(f"{prefix}: row must be a JSON object")
    unknown = set(value) - _TOP_LEVEL_FIELDS
    missing = _TOP_LEVEL_FIELDS - set(value)
    if unknown:
        raise ManifestValidationError(f"{prefix}: unknown fields: {sorted(unknown)}")
    if missing:
        raise ManifestValidationError(f"{prefix}: missing fields: {sorted(missing)}")

    _string(value["schema_version"], "schema_version", prefix, exact=MANIFEST_SCHEMA_VERSION)
    mutation_id = _string(value["mutation_id"], "mutation_id", prefix, nonempty=True)
    source_id = _string(value["source_id"], "source_id", prefix, nonempty=True)
    top_module = _string(value["top_module"], "top_module", prefix, nonempty=True)
    if not _TOP_MODULE_RE.fullmatch(top_module):
        raise ManifestValidationError(f"{prefix}: invalid top_module {top_module!r}")
    mutation_type = _string(value["mutation_type"], "mutation_type", prefix, nonempty=True)
    mutated_signal = _string(value["mutated_signal"], "mutated_signal", prefix, nonempty=True)

    path_values: dict[str, str] = {}
    for field_name in (
        "original_rtl_path",
        "mutated_rtl_path",
        "repaired_rtl_path",
        "testbench_path",
    ):
        path_value = _string(value[field_name], field_name, prefix, nonempty=True)
        path_values[field_name] = path_value

    support_value = value["support_files"]
    if not isinstance(support_value, list):
        raise ManifestValidationError(f"{prefix}: support_files must be a list")
    support_files: list[str] = []
    for index, item in enumerate(support_value):
        support_files.append(
            _string(item, f"support_files[{index}]", prefix, nonempty=True)
        )
    if len(set(support_files)) != len(support_files):
        raise ManifestValidationError(f"{prefix}: support_files must not contain duplicates")

    location_value = value["changed_location"]
    if not isinstance(location_value, dict):
        raise ManifestValidationError(f"{prefix}: changed_location must be an object")
    location_unknown = set(location_value) - _LOCATION_FIELDS
    location_missing = _LOCATION_FIELDS - set(location_value)
    if location_unknown or location_missing:
        raise ManifestValidationError(
            f"{prefix}: changed_location fields invalid; unknown={sorted(location_unknown)}, "
            f"missing={sorted(location_missing)}"
        )
    location_file = _string(location_value["file"], "changed_location.file", prefix, nonempty=True)
    line_start = _positive_int(location_value["line_start"], "changed_location.line_start", prefix)
    line_end = _positive_int(location_value["line_end"], "changed_location.line_end", prefix)
    if line_end < line_start:
        raise ManifestValidationError(f"{prefix}: changed_location.line_end precedes line_start")

    checks_value = value["requested_checks"]
    if not isinstance(checks_value, dict):
        raise ManifestValidationError(f"{prefix}: requested_checks must be an object")
    if set(checks_value) != set(REQUESTED_CHECKS):
        raise ManifestValidationError(
            f"{prefix}: requested_checks must contain exactly {list(REQUESTED_CHECKS)}"
        )
    requested_checks: dict[str, bool] = {}
    for check_name in REQUESTED_CHECKS:
        check_value = checks_value[check_name]
        if type(check_value) is not bool:
            raise ManifestValidationError(
                f"{prefix}: requested_checks.{check_name} must be boolean"
            )
        requested_checks[check_name] = check_value

    all_paths = {
        **path_values,
        "changed_location.file": location_file,
    }
    for index, support_file in enumerate(support_files):
        all_paths[f"support_files[{index}]"] = support_file
    resolved_paths: dict[str, Path] = {}
    for field_name, relative_path in all_paths.items():
        resolved_paths[field_name] = _resolve_artifact(root, relative_path, prefix, field_name)

    declared_paths = set(path_values.values()) | set(support_files)
    if location_file not in declared_paths:
        raise ManifestValidationError(
            f"{prefix}: changed_location.file must name a declared artifact"
        )

    # Expose path lookup by the manifest path, while retaining named fields.
    for field_name, relative_path in path_values.items():
        resolved_paths[relative_path] = resolved_paths[field_name]
    for index, support_file in enumerate(support_files):
        resolved_paths[support_file] = resolved_paths[f"support_files[{index}]"]
    resolved_paths[location_file] = _resolve_artifact(root, location_file, prefix, "changed_location.file")

    return MutationRow(
        schema_version=MANIFEST_SCHEMA_VERSION,
        mutation_id=mutation_id,
        source_id=source_id,
        top_module=top_module,
        original_rtl_path=path_values["original_rtl_path"],
        mutated_rtl_path=path_values["mutated_rtl_path"],
        repaired_rtl_path=path_values["repaired_rtl_path"],
        testbench_path=path_values["testbench_path"],
        support_files=tuple(support_files),
        mutation_type=mutation_type,
        mutated_signal=mutated_signal,
        changed_location=ChangedLocation(location_file, line_start, line_end),
        requested_checks=requested_checks,
        resolved_paths=resolved_paths,
    )


def _validate_root(root: Path) -> Path:
    if not root.exists() or not root.is_dir():
        raise ManifestValidationError(f"workspace-root is not a directory: {root}")
    if _contains_symlink(root):
        raise ManifestValidationError("workspace-root must not contain symlinked path components")
    return root.resolve()


def _resolve_artifact(root: Path, value: str, prefix: str, field_name: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or "\x00" in value:
        raise ManifestValidationError(f"{prefix}: {field_name} must be a safe relative path")
    if not candidate.parts or any(part in {".", ".."} for part in candidate.parts):
        raise ManifestValidationError(f"{prefix}: unsafe path in {field_name}: {value!r}")
    target = root / candidate
    if _contains_symlink(target):
        raise ManifestValidationError(f"{prefix}: symlinked artifact path rejected: {value!r}")
    resolved = target.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ManifestValidationError(f"{prefix}: path escapes workspace-root: {value!r}") from exc
    if not resolved.is_file():
        raise ManifestValidationError(f"{prefix}: artifact is not a regular file: {value!r}")
    return resolved


def _contains_symlink(path: Path) -> bool:
    """Check existing components without following any symlink."""

    current = Path(path.anchor) if path.is_absolute() else Path()
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return False
    return False


def _string(value: Any, field_name: str, prefix: str, *, nonempty: bool = False, exact: str | None = None) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ManifestValidationError(f"{prefix}: {field_name} must be a string")
    if exact is not None and value != exact:
        raise ManifestValidationError(f"{prefix}: {field_name} must equal {exact!r}")
    return value

def _positive_int(value: Any, field_name: str, prefix: str) -> int:
    if type(value) is not int or value <= 0:
        raise ManifestValidationError(f"{prefix}: {field_name} must be a positive integer")
    return value
