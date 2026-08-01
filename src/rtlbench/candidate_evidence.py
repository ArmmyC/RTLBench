"""Strict validation and deterministic identity helpers for candidate evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping


EVIDENCE_SCHEMA_VERSION = "rtl_candidate_evidence_v0.1"
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
CHECK_LEAVES = {
    "compile": "candidate",
    "simulation": "candidate_passes",
    "lint": "candidate",
    "synthesis": "candidate",
}
CHECK_NAMES = ("compile", "simulation", "lint", "synthesis")
SIMULATION_CONTRACTS = {"mismatch_count_v1", "exit_code_v1"}
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
TOOL_NAMES = ("iverilog", "vvp", "verilator", "yosys")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CandidateEvidenceValidationError(ValueError):
    """Raised when candidate evidence is malformed or internally contradictory."""


def validate_candidate_evidence_file(
    path: Path, *, max_bytes: int | None = None
) -> list[dict[str, Any]]:
    """Validate every row and relationship in a candidate evidence JSONL file."""

    candidate_path = Path(path)
    if candidate_path.is_symlink() or not candidate_path.is_file():
        raise CandidateEvidenceValidationError(
            "candidate evidence must be a regular non-symlink file"
        )
    if max_bytes is not None and candidate_path.stat().st_size > max_bytes:
        raise CandidateEvidenceValidationError(
            "candidate evidence exceeds the configured size limit"
        )
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    try:
        with candidate_path.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, 1):
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise CandidateEvidenceValidationError(
                        f"evidence line {line_number}: malformed JSON"
                    ) from exc
                validate_candidate_evidence_row(row, line_number=line_number)
                candidate_id = row["candidate_id"]
                if candidate_id in seen_ids:
                    raise CandidateEvidenceValidationError(
                        f"evidence line {line_number}: duplicate candidate_id"
                    )
                seen_ids.add(candidate_id)
                rows.append(row)
    except UnicodeDecodeError as exc:
        raise CandidateEvidenceValidationError(
            "candidate evidence is not valid UTF-8"
        ) from exc
    except OSError as exc:
        raise CandidateEvidenceValidationError(
            f"could not read candidate evidence: {exc}"
        ) from exc
    if not rows:
        raise CandidateEvidenceValidationError("candidate evidence contains no rows")
    return rows


def validate_candidate_evidence_row(row: Any, *, line_number: int = 1) -> None:
    prefix = f"evidence line {line_number}"
    if not isinstance(row, dict) or set(row) != EVIDENCE_FIELDS:
        raise CandidateEvidenceValidationError(f"{prefix}: top-level schema mismatch")
    if row["schema_version"] != EVIDENCE_SCHEMA_VERSION:
        raise CandidateEvidenceValidationError(f"{prefix}: unsupported schema_version")
    for key in ("candidate_id", "task_id", "source_id", "top_module", "testbench_top"):
        if not isinstance(row[key], str) or not row[key]:
            raise CandidateEvidenceValidationError(f"{prefix}: invalid {key}")
    if type(row["attempt"]) is not int or row["attempt"] < 1:
        raise CandidateEvidenceValidationError(f"{prefix}: invalid attempt")

    contract = row["simulation_result_contract"]
    if contract not in SIMULATION_CONTRACTS:
        raise CandidateEvidenceValidationError(
            f"{prefix}: invalid simulation_result_contract"
        )
    requested = _validate_requested_checks(row["requested_checks"], prefix)
    checks = _validate_checks(row["checks"], requested, prefix)
    mismatch = _validate_mismatch_summary(row["mismatch_summary"], contract, prefix)
    _validate_input_hashes(row["input_hashes"], prefix)
    _validate_toolchain(row["toolchain"], prefix)

    failure_category = row["failure_category"]
    if failure_category not in FAILURE_CATEGORIES:
        raise CandidateEvidenceValidationError(f"{prefix}: invalid failure_category")
    if type(row["accepted"]) is not bool:
        raise CandidateEvidenceValidationError(f"{prefix}: invalid accepted")
    if not isinstance(row["diagnostics"], list) or any(
        not isinstance(item, str) for item in row["diagnostics"]
    ):
        raise CandidateEvidenceValidationError(f"{prefix}: invalid diagnostics")

    compile_passed = checks["compile"]["candidate"]["passed"] is True
    simulation_passed = checks["simulation"]["candidate_passes"]["passed"] is True
    if row["accepted"] != (compile_passed and simulation_passed):
        raise CandidateEvidenceValidationError(
            f"{prefix}: accepted must equal compile_passed and simulation_passed"
        )
    if failure_category == "passed" and row["accepted"] is not True:
        raise CandidateEvidenceValidationError(
            f"{prefix}: passed category requires accepted=true"
        )
    if contract == "mismatch_count_v1" and row["accepted"]:
        if not mismatch["reported_counts"] or any(
            count != 0 for count in mismatch["reported_counts"]
        ):
            raise CandidateEvidenceValidationError(
                f"{prefix}: accepted mismatch-count evidence requires zero reports"
            )

    expected_category = select_failure_category(checks, row["accepted"])
    if failure_category != expected_category:
        raise CandidateEvidenceValidationError(
            f"{prefix}: failure_category {failure_category!r} contradicts "
            f"canonical category {expected_category!r}"
        )
    _validate_category_contract(
        failure_category,
        checks,
        mismatch,
        contract,
        prefix,
    )


def select_failure_category(checks: Mapping[str, Any], accepted: bool) -> str:
    """Apply the same priority used by RTLBench evidence generation."""

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


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_workspace_tree(root: Path) -> str:
    """Hash regular files and directory names without host-specific paths."""

    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise CandidateEvidenceValidationError("workspace root must be a directory")
    records: list[bytes] = []
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        directories.sort()
        files.sort()
        for name in directories:
            path = Path(current) / name
            if path.is_symlink():
                raise CandidateEvidenceValidationError("workspace contains a symlink")
            records.append(b"D\0" + path.relative_to(root).as_posix().encode() + b"\n")
        for name in files:
            path = Path(current) / name
            if path.is_symlink() or not path.is_file():
                raise CandidateEvidenceValidationError(
                    "workspace contains a non-regular file"
                )
            records.append(
                b"F\0"
                + path.relative_to(root).as_posix().encode()
                + b"\0"
                + sha256_file(path).encode()
                + b"\n"
            )
    return hashlib.sha256(b"".join(records)).hexdigest()


def _validate_requested_checks(value: Any, prefix: str) -> dict[str, bool]:
    if not isinstance(value, dict) or set(value) != set(CHECK_NAMES):
        raise CandidateEvidenceValidationError(f"{prefix}: invalid requested_checks")
    if any(type(item) is not bool for item in value.values()):
        raise CandidateEvidenceValidationError(f"{prefix}: invalid requested_checks")
    return {name: value[name] for name in CHECK_NAMES}


def _validate_checks(
    value: Any, requested: Mapping[str, bool], prefix: str
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != CHECK_FIELDS:
        raise CandidateEvidenceValidationError(f"{prefix}: invalid checks")
    for check_name, leaf_name in CHECK_LEAVES.items():
        container = value[check_name]
        if not isinstance(container, dict) or set(container) != {leaf_name}:
            raise CandidateEvidenceValidationError(
                f"{prefix}: invalid {check_name} check"
            )
        leaf = container[leaf_name]
        if not isinstance(leaf, dict) or set(leaf) != {"attempted", "passed", "reason"}:
            raise CandidateEvidenceValidationError(f"{prefix}: invalid {check_name} leaf")
        attempted = leaf["attempted"]
        passed = leaf["passed"]
        reason = leaf["reason"]
        if type(attempted) is not bool or type(passed) not in {bool, type(None)}:
            raise CandidateEvidenceValidationError(
                f"{prefix}: invalid {check_name} status"
            )
        if reason is not None and (not isinstance(reason, str) or not reason):
            raise CandidateEvidenceValidationError(
                f"{prefix}: invalid {check_name} reason"
            )
        if not attempted and passed is not None:
            raise CandidateEvidenceValidationError(
                f"{prefix}: unattempted {check_name} must have passed=null"
            )
        if attempted and passed is True and reason is not None:
            raise CandidateEvidenceValidationError(
                f"{prefix}: passing {check_name} must have reason=null"
            )
        if attempted and passed is False and reason is None:
            raise CandidateEvidenceValidationError(
                f"{prefix}: failed {check_name} requires a reason"
            )
        if not attempted and reason is None:
            raise CandidateEvidenceValidationError(
                f"{prefix}: unattempted {check_name} requires a reason"
            )
        if not requested[check_name]:
            if leaf != {"attempted": False, "passed": None, "reason": "not_requested"}:
                raise CandidateEvidenceValidationError(
                    f"{prefix}: unrequested {check_name} has attempted evidence"
                )
    return value


def _validate_mismatch_summary(
    value: Any, contract: str, prefix: str
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != MISMATCH_FIELDS:
        raise CandidateEvidenceValidationError(f"{prefix}: invalid mismatch_summary")
    if value["contract"] != contract:
        raise CandidateEvidenceValidationError(
            f"{prefix}: mismatch contract does not match simulation_result_contract"
        )
    counts = value["reported_counts"]
    sample_counts = value["reported_sample_counts"]
    if not isinstance(counts, list) or not isinstance(sample_counts, list):
        raise CandidateEvidenceValidationError(
            f"{prefix}: mismatch counts must be lists"
        )
    if any(type(item) is not int or item < 0 for item in counts):
        raise CandidateEvidenceValidationError(f"{prefix}: invalid reported_counts")
    if any(
        item is not None and (type(item) is not int or item < 0)
        for item in sample_counts
    ):
        raise CandidateEvidenceValidationError(
            f"{prefix}: invalid reported_sample_counts"
        )
    if len(counts) != len(sample_counts):
        raise CandidateEvidenceValidationError(
            f"{prefix}: mismatch count lists must have equal length"
        )
    maximum = value["maximum_count"]
    if maximum is not None and (type(maximum) is not int or maximum < 0):
        raise CandidateEvidenceValidationError(f"{prefix}: invalid maximum_count")
    expected_maximum = max(counts) if counts else None
    if maximum != expected_maximum:
        raise CandidateEvidenceValidationError(
            f"{prefix}: maximum_count does not match reported_counts"
        )
    if type(value["timeout_reported"]) is not bool:
        raise CandidateEvidenceValidationError(f"{prefix}: invalid timeout_reported")
    if contract == "exit_code_v1" and counts:
        raise CandidateEvidenceValidationError(
            f"{prefix}: exit_code_v1 must not contain mismatch counts"
        )
    return value


def _validate_category_contract(
    category: str,
    checks: Mapping[str, Any],
    mismatch: Mapping[str, Any],
    contract: str,
    prefix: str,
) -> None:
    compile_leaf = checks["compile"]["candidate"]
    simulation_leaf = checks["simulation"]["candidate_passes"]
    positive_mismatch = any(count > 0 for count in mismatch["reported_counts"])
    if positive_mismatch and category not in {"functional_mismatch", "timeout"}:
        raise CandidateEvidenceValidationError(
            f"{prefix}: positive mismatch requires functional_mismatch"
        )
    if category == "compile_failure" and compile_leaf != {
        "attempted": True,
        "passed": False,
        "reason": "compile_failure",
    }:
        raise CandidateEvidenceValidationError(
            f"{prefix}: compile_failure requires a failed compile leaf"
        )
    if category == "compile_failure" and simulation_leaf != {
        "attempted": False,
        "passed": None,
        "reason": "compile_failure",
    }:
        raise CandidateEvidenceValidationError(
            f"{prefix}: compile_failure must not contain simulation evidence"
        )

    simulation_categories = {
        "functional_mismatch",
        "simulation_result_missing",
        "simulation_failure",
    }
    if category in simulation_categories and compile_leaf != {
        "attempted": True,
        "passed": True,
        "reason": None,
    }:
        raise CandidateEvidenceValidationError(
            f"{prefix}: {category} requires a successful compile leaf"
        )
    simulation_timeout = simulation_leaf["attempted"] and (
        simulation_leaf["reason"] == "timeout" or mismatch["timeout_reported"]
    )
    if category == "timeout" and simulation_timeout and compile_leaf["reason"] != "timeout":
        if compile_leaf != {
            "attempted": True,
            "passed": True,
            "reason": None,
        }:
            raise CandidateEvidenceValidationError(
                f"{prefix}: simulation-stage timeout requires a successful compile leaf"
            )
    if category == "functional_mismatch" and not (
        simulation_leaf["attempted"]
        and simulation_leaf["passed"] is False
        and simulation_leaf["reason"] == "functional_mismatch"
        and positive_mismatch
    ):
        raise CandidateEvidenceValidationError(
            f"{prefix}: functional_mismatch requires failed simulation with mismatches"
        )
    if category == "simulation_result_missing" and not (
        contract == "mismatch_count_v1"
        and simulation_leaf["attempted"]
        and simulation_leaf["passed"] is False
        and simulation_leaf["reason"] == "simulation_result_missing"
        and not mismatch["reported_counts"]
        and not mismatch["timeout_reported"]
    ):
        raise CandidateEvidenceValidationError(
            f"{prefix}: simulation_result_missing requires no mismatch report"
        )
    if category == "timeout" and not (
        compile_leaf["reason"] == "timeout"
        or simulation_timeout
    ):
        raise CandidateEvidenceValidationError(
            f"{prefix}: timeout requires timeout evidence"
        )
    if category == "simulation_failure" and not (
        simulation_leaf["attempted"]
        and simulation_leaf["passed"] is False
        and simulation_leaf["reason"] == "simulation_failure"
        and not positive_mismatch
    ):
        raise CandidateEvidenceValidationError(
            f"{prefix}: simulation_failure requires failed simulation"
        )


def _validate_input_hashes(value: Any, prefix: str) -> None:
    if not isinstance(value, dict) or set(value) != {
        "candidate_rtl_sha256",
        "testbench_sha256",
        "support_files",
    }:
        raise CandidateEvidenceValidationError(f"{prefix}: invalid input_hashes")
    for name in ("candidate_rtl_sha256", "testbench_sha256"):
        if not isinstance(value[name], str) or not SHA256_RE.fullmatch(value[name]):
            raise CandidateEvidenceValidationError(f"{prefix}: invalid {name}")
    support = value["support_files"]
    if not isinstance(support, list):
        raise CandidateEvidenceValidationError(f"{prefix}: invalid support_files hashes")
    paths: list[str] = []
    for item in support:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise CandidateEvidenceValidationError(
                f"{prefix}: invalid support file hash record"
            )
        path = item["path"]
        if (
            not isinstance(path, str)
            or not path
            or "\\" in path
            or Path(path).is_absolute()
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise CandidateEvidenceValidationError(
                f"{prefix}: invalid support file hash path"
            )
        if not isinstance(item["sha256"], str) or not SHA256_RE.fullmatch(item["sha256"]):
            raise CandidateEvidenceValidationError(
                f"{prefix}: invalid support file hash"
            )
        paths.append(path)
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise CandidateEvidenceValidationError(
            f"{prefix}: support file hashes must be sorted and unique"
        )


def _validate_toolchain(value: Any, prefix: str) -> None:
    if not isinstance(value, dict) or set(value) != set(TOOL_NAMES):
        raise CandidateEvidenceValidationError(f"{prefix}: invalid toolchain")
    for name in TOOL_NAMES:
        item = value[name]
        if not isinstance(item, dict) or set(item) != {"available", "version"}:
            raise CandidateEvidenceValidationError(
                f"{prefix}: invalid {name} toolchain record"
            )
        if type(item["available"]) is not bool or (
            item["version"] is not None and not isinstance(item["version"], str)
        ):
            raise CandidateEvidenceValidationError(
                f"{prefix}: invalid {name} toolchain values"
            )
