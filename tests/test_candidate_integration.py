import json
import shutil
from pathlib import Path

import pytest

from rtlbench.candidate_verification import verify_candidates


FIXTURE = Path(__file__).parent / "fixtures" / "candidate_verification"


def _run_one(tmp_path: Path, line_number: int):
    rows = FIXTURE.joinpath("manifest.jsonl").read_text(encoding="utf-8").splitlines()
    manifest = tmp_path / f"manifest-{line_number}.jsonl"
    manifest.write_text(rows[line_number] + "\n", encoding="utf-8")
    output = tmp_path / f"evidence-{line_number}.jsonl"
    verify_candidates(
        manifest=manifest,
        output=output,
        workspace_root=FIXTURE,
        work_dir=tmp_path / f"work-{line_number}",
        force=True,
    )
    return json.loads(output.read_text(encoding="utf-8"))


def _run_contract_fixture(
    tmp_path: Path,
    directory: str,
    contract: str = "mismatch_count_v1",
    testbench_top: str = "tb",
):
    row = {
        "schema_version": "rtl_candidate_manifest_v0.1",
        "candidate_id": f"{directory}-candidate",
        "task_id": f"{directory}-task",
        "source_id": directory,
        "attempt": 1,
        "top_module": "TopModule",
        "testbench_top": testbench_top,
        "candidate_rtl_path": f"{directory}/candidate.sv",
        "testbench_path": f"{directory}/testbench.sv",
        "support_files": [],
        "simulation_result_contract": contract,
        "requested_checks": {
            "compile": True,
            "simulation": True,
            "lint": False,
            "synthesis": False,
        },
    }
    manifest = tmp_path / f"{directory}.jsonl"
    manifest.write_text(json.dumps(row) + "\n", encoding="utf-8")
    output = tmp_path / f"{directory}.evidence.jsonl"
    verify_candidates(
        manifest=manifest,
        output=output,
        workspace_root=FIXTURE,
        work_dir=tmp_path / f"{directory}.work",
        force=True,
    )
    return json.loads(output.read_text(encoding="utf-8"))


@pytest.mark.integration
def test_real_iverilog_accepts_passing_fixture(tmp_path: Path):
    missing = [name for name in ("iverilog", "vvp") if shutil.which(name) is None]
    if missing:
        pytest.skip(f"missing optional integration tools: {', '.join(missing)}")
    evidence = _run_one(tmp_path, 0)
    assert evidence["accepted"] is True
    assert evidence["failure_category"] == "passed"
    assert evidence["mismatch_summary"] == {
        "contract": "mismatch_count_v1",
        "reported_counts": [0],
        "reported_sample_counts": [4],
        "maximum_count": 0,
        "timeout_reported": False,
    }


@pytest.mark.integration
def test_real_iverilog_rejects_functional_mismatch(tmp_path: Path):
    missing = [name for name in ("iverilog", "vvp") if shutil.which(name) is None]
    if missing:
        pytest.skip(f"missing optional integration tools: {', '.join(missing)}")
    evidence = _run_one(tmp_path, 2)
    assert evidence["accepted"] is False
    assert evidence["failure_category"] == "functional_mismatch"
    assert evidence["mismatch_summary"]["reported_sample_counts"] == [4]


@pytest.mark.integration
def test_real_iverilog_reports_compile_failure(tmp_path: Path):
    missing = [name for name in ("iverilog", "vvp") if shutil.which(name) is None]
    if missing:
        pytest.skip(f"missing optional integration tools: {', '.join(missing)}")
    evidence = _run_one(tmp_path, 1)
    assert evidence["accepted"] is False
    assert evidence["failure_category"] == "compile_failure"


@pytest.mark.integration
def test_real_iverilog_timeout_marker_is_timeout(tmp_path: Path):
    missing = [name for name in ("iverilog", "vvp") if shutil.which(name) is None]
    if missing:
        pytest.skip(f"missing optional integration tools: {', '.join(missing)}")
    evidence = _run_contract_fixture(tmp_path, "timeout_marker")
    assert evidence["accepted"] is False
    assert evidence["failure_category"] == "timeout"
    assert evidence["mismatch_summary"]["timeout_reported"] is True


@pytest.mark.integration
def test_real_iverilog_missing_result_is_rejected(tmp_path: Path):
    missing = [name for name in ("iverilog", "vvp") if shutil.which(name) is None]
    if missing:
        pytest.skip(f"missing optional integration tools: {', '.join(missing)}")
    evidence = _run_contract_fixture(tmp_path, "missing_result")
    assert evidence["accepted"] is False
    assert evidence["failure_category"] == "simulation_result_missing"


@pytest.mark.integration
def test_real_iverilog_missing_simulation_top_is_compile_failure(tmp_path: Path):
    missing = [name for name in ("iverilog", "vvp") if shutil.which(name) is None]
    if missing:
        pytest.skip(f"missing optional integration tools: {', '.join(missing)}")
    evidence = _run_contract_fixture(
        tmp_path,
        "passing",
        testbench_top="does_not_exist",
    )
    assert evidence["accepted"] is False
    assert evidence["failure_category"] == "compile_failure"
