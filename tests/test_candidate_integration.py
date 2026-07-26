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


@pytest.mark.integration
def test_real_iverilog_accepts_passing_fixture(tmp_path: Path):
    missing = [name for name in ("iverilog", "vvp") if shutil.which(name) is None]
    if missing:
        pytest.skip(f"missing optional integration tools: {', '.join(missing)}")
    evidence = _run_one(tmp_path, 0)
    assert evidence["accepted"] is True
    assert evidence["failure_category"] == "passed"


@pytest.mark.integration
def test_real_iverilog_rejects_functional_mismatch(tmp_path: Path):
    missing = [name for name in ("iverilog", "vvp") if shutil.which(name) is None]
    if missing:
        pytest.skip(f"missing optional integration tools: {', '.join(missing)}")
    evidence = _run_one(tmp_path, 2)
    assert evidence["accepted"] is False
    assert evidence["failure_category"] == "functional_mismatch"


@pytest.mark.integration
def test_real_iverilog_reports_compile_failure(tmp_path: Path):
    missing = [name for name in ("iverilog", "vvp") if shutil.which(name) is None]
    if missing:
        pytest.skip(f"missing optional integration tools: {', '.join(missing)}")
    evidence = _run_one(tmp_path, 1)
    assert evidence["accepted"] is False
    assert evidence["failure_category"] == "compile_failure"
