from pathlib import Path

from rtlbench.evidence import (
    compute_evidence_tier,
    failed,
    not_requested,
    passed,
    sanitize_diagnostic,
    select_failure_category,
    unavailable,
)


def _checks():
    return {
        "compile": {name: passed() for name in ("original", "mutated", "repaired")},
        "lint": {name: not_requested() for name in ("mutated", "repaired")},
        "simulation": {name: not_requested() for name in ("original_passes", "mutated_detects_mutation", "repaired_passes")},
        "synthesis": {name: not_requested() for name in ("original", "mutated", "repaired")},
        "equivalence": {"original_vs_repaired": not_requested()},
        "activity": {"proxy": not_requested()},
    }


def _all_unattempted(reason: str = "tool_unavailable"):
    return {
        "compile": {name: unavailable(reason) for name in ("original", "mutated", "repaired")},
        "lint": {name: unavailable(reason) for name in ("mutated", "repaired")},
        "simulation": {name: unavailable(reason) for name in ("original_passes", "mutated_detects_mutation", "repaired_passes")},
        "synthesis": {name: unavailable(reason) for name in ("original", "mutated", "repaired")},
        "equivalence": {"original_vs_repaired": unavailable(reason)},
        "activity": {"proxy": unavailable(reason)},
    }


def test_tier_a_requires_complete_functional_evidence():
    checks = _checks()
    checks["simulation"] = {name: passed() for name in ("original_passes", "mutated_detects_mutation", "repaired_passes")}
    assert compute_evidence_tier(checks) == "A"
    checks["simulation"]["mutated_detects_mutation"] = failed("simulation_not_detected")
    assert compute_evidence_tier(checks) == "C"


def test_tier_b_and_c_structural_rules():
    checks = _checks()
    checks["lint"] = {"mutated": passed(), "repaired": passed()}
    assert compute_evidence_tier(checks) == "B"
    checks["compile"]["mutated"] = failed("compile_failure")
    assert compute_evidence_tier(checks) == "C"


def test_unavailable_tools_do_not_award_a_tier():
    assert compute_evidence_tier(_all_unattempted()) is None


def test_no_requested_checks_do_not_award_a_tier():
    assert compute_evidence_tier(_all_unattempted("not_requested")) is None


def test_attempted_compile_failure_is_limited_evidence_tier_c():
    checks = _all_unattempted()
    checks["compile"]["original"] = failed("compile_failure")
    assert compute_evidence_tier(checks) == "C"


def test_attempted_simulation_without_mutation_detection_is_tier_c():
    checks = _all_unattempted()
    checks["simulation"]["mutated_detects_mutation"] = failed("simulation_not_detected")
    assert compute_evidence_tier(checks) == "C"


def test_invalid_metadata_or_hashes_have_no_emittable_tier():
    assert compute_evidence_tier(_checks(), hashes_present=False) is None
    assert compute_evidence_tier(_checks(), metadata_valid=False) is None


def test_failure_priority_is_explicit():
    checks = _checks()
    checks["compile"]["mutated"] = failed("compile_failure")
    checks["simulation"]["original_passes"] = failed("original_simulation_failure")
    checks["lint"]["mutated"] = unavailable("tool_unavailable")
    assert select_failure_category(checks) == "compile_failure"
    checks["compile"]["mutated"] = passed()
    assert select_failure_category(checks) == "original_simulation_failure"
    checks["simulation"]["original_passes"] = passed()
    assert select_failure_category(checks) == "tool_unavailable"


def test_three_state_values_and_diagnostic_sanitization(tmp_path: Path):
    assert passed() == {"attempted": True, "passed": True, "reason": None}
    assert failed("compile_failure")["passed"] is False
    assert unavailable()["passed"] is None
    assert not_requested()["reason"] == "not_requested"
    root = tmp_path / "private"
    value = sanitize_diagnostic(str(root) + " " + ("module top; " * 1000), workspace_root=root)
    assert str(root) not in value
    assert len(value) <= 4096


def test_nested_managed_work_path_is_fully_sanitized(tmp_path: Path):
    workspace = tmp_path / "workspace"
    managed_run = workspace / ".rtlbench-run-random"
    value = sanitize_diagnostic(
        str(managed_run / "row") + " " + str(workspace / "input.sv"),
        workspace_root=workspace,
        work_dir=managed_run,
    )
    assert ".rtlbench-run-random" not in value
    assert "<work>" in value
    assert "<workspace>" in value
