"""Deterministic evidence data and classification helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

EVIDENCE_SCHEMA_VERSION = "rtl_mutation_evidence_v0.1"
FAILURE_CATEGORIES = (
    "passed",
    "tool_unavailable",
    "compile_failure",
    "simulation_failure",
    "lint_failure",
    "simulation_not_detected",
    "original_simulation_failure",
    "repaired_simulation_failure",
    "synthesis_failure",
    "equivalence_failure",
    "activity_failure",
    "timeout",
    "path_validation_failure",
    "hash_failure",
    "internal_error",
    "partial_failure",
)
FAILURE_PRIORITY = (
    "timeout",
    "compile_failure",
    "original_simulation_failure",
    "simulation_failure",
    "simulation_not_detected",
    "repaired_simulation_failure",
    "lint_failure",
    "synthesis_failure",
    "equivalence_failure",
    "activity_failure",
    "tool_unavailable",
    "internal_error",
    "partial_failure",
    "passed",
)


@dataclass(frozen=True)
class CheckStatus:
    attempted: bool
    passed: bool | None
    reason: str | None

    def __post_init__(self) -> None:
        if self.attempted and type(self.passed) is not bool:
            raise ValueError("attempted check must have a boolean passed value")
        if not self.attempted and self.passed is not None:
            raise ValueError("unattempted check must have passed=null")
        if self.attempted and self.reason is not None and self.passed is True:
            raise ValueError("passing check must have reason=null")
        if not self.attempted and not self.reason:
            raise ValueError("unattempted check requires a reason")

    def to_dict(self) -> dict[str, Any]:
        return {"attempted": self.attempted, "passed": self.passed, "reason": self.reason}


def passed() -> dict[str, Any]:
    return CheckStatus(True, True, None).to_dict()


def failed(reason: str) -> dict[str, Any]:
    return CheckStatus(True, False, reason).to_dict()


def unavailable(reason: str = "tool_unavailable") -> dict[str, Any]:
    return CheckStatus(False, None, reason).to_dict()


def not_requested() -> dict[str, Any]:
    return unavailable("not_requested")


def compute_evidence_tier(
    checks: Mapping[str, Any], hashes_present: bool = True, metadata_valid: bool = True
) -> Literal["A", "B", "C"] | None:
    """Compute executable evidence tier without using wall-clock data."""

    if not metadata_valid or not hashes_present:
        return None
    compile_statuses = [
        _get_status(checks, "compile", name)
        for name in ("original", "mutated", "repaired")
    ]
    structural_statuses = [
        _get_status(checks, "lint", "mutated"),
        _get_status(checks, "lint", "repaired"),
        _get_status(checks, "synthesis", "original"),
        _get_status(checks, "synthesis", "mutated"),
        _get_status(checks, "synthesis", "repaired"),
    ]
    repaired_structural = [
        _get_status(checks, "lint", "repaired"),
        _get_status(checks, "synthesis", "repaired"),
    ]
    functional = [
        _get_status(checks, "simulation", "original_passes"),
        _get_status(checks, "simulation", "mutated_detects_mutation"),
        _get_status(checks, "simulation", "repaired_passes"),
    ]
    equivalence = [_get_status(checks, "equivalence", "original_vs_repaired")]
    if all(_is_pass(item) for item in compile_statuses + functional):
        return "A"

    repaired_structural_attempted = [
        item for item in repaired_structural if item.get("attempted") is True
    ]
    if (
        all(_is_pass(item) for item in compile_statuses)
        and any(_is_pass(item) for item in structural_statuses)
        and all(_is_pass(item) for item in repaired_structural_attempted)
        and not all(_is_pass(item) for item in functional)
    ):
        return "B"
    executable_checks = compile_statuses + structural_statuses + functional + equivalence
    if any(item.get("attempted") is True for item in executable_checks):
        return "C"
    return None


def select_failure_category(checks: Mapping[str, Any]) -> str:
    """Select the top-level category using an explicit stable priority."""

    for category in FAILURE_PRIORITY[:-2]:
        present = _has_reason(checks, category)
        if present:
            return category
    leaves = list(_walk_statuses(checks))
    if any(item.get("attempted") is False and item.get("reason") not in {"not_requested"} for item in leaves):
        return "partial_failure"
    if leaves and all(item.get("attempted") is False for item in leaves):
        return "partial_failure"
    if any(item.get("attempted") and item.get("passed") is False for item in leaves):
        return "partial_failure"
    return "passed"


def sanitize_diagnostic(
    value: str,
    *,
    workspace_root: Path | None = None,
    work_dir: Path | None = None,
    manifest_path: Path | None = None,
    limit: int = 4096,
) -> str:
    """Bound and de-identify tool text before it enters evidence."""

    text = str(value).replace("\x00", "")
    replacements = [
        (work_dir, "<work>"),
        (manifest_path, "<manifest>"),
        (workspace_root, "<workspace>"),
    ]
    replacements.sort(
        key=lambda item: len(str(item[0].resolve())) if item[0] is not None else 0,
        reverse=True,
    )
    for path, placeholder in replacements:
        if path is not None:
            raw = str(path.resolve())
            text = text.replace(raw, placeholder).replace(str(path), placeholder)
    # Tool output can mention private paths outside the declared workspace too.
    text = re.sub(r"(?<![A-Za-z0-9_])/(?:[^\s]+(?:/[^\s]+)*)", "<path>", text)
    text = re.sub(r"(?i)(?<![A-Za-z0-9_])[A-Z]:\\(?:[^\s]+(?:\\[^\s]+)*)", "<path>", text)
    text = re.sub(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+", r"\1=<redacted>", text)
    text = re.sub(r"(?m)^[ \t]*(module|endmodule|always|assign|wire|logic|reg)\b.*$", "[rtl line omitted]", text)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: max(0, limit - 14)] + "...[truncated]"
    return text


def deterministic_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _get_status(checks: Mapping[str, Any], *parts: str) -> dict[str, Any]:
    value: Any = checks
    for part in parts:
        if not isinstance(value, Mapping):
            return unavailable("missing_evidence")
        value = value.get(part)
    return value if isinstance(value, dict) and "attempted" in value else unavailable("missing_evidence")


def _is_pass(value: Mapping[str, Any]) -> bool:
    return value.get("attempted") is True and value.get("passed") is True


def _walk_statuses(value: Any):
    if isinstance(value, Mapping):
        if "attempted" in value and "passed" in value and "reason" in value:
            yield value
            return
        for key in sorted(value):
            yield from _walk_statuses(value[key])
    elif isinstance(value, list):
        for item in value:
            yield from _walk_statuses(item)


def _has_reason(checks: Mapping[str, Any], reason: str) -> bool:
    return any(item.get("reason") == reason for item in _walk_statuses(checks))
