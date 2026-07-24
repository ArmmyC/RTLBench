import shutil
from pathlib import Path

import pytest

from rtlbench.mutation_verification import verify_mutations


@pytest.mark.integration
def test_real_tool_fixture_smoke(tmp_path: Path):
    required = ("iverilog", "vvp")
    missing = [name for name in required if shutil.which(name) is None]
    if missing:
        pytest.skip(f"missing optional integration tools: {', '.join(missing)}")
    fixture = Path(__file__).parent / "fixtures" / "mutation_verification"
    output = tmp_path / "evidence.jsonl"
    verify_mutations(
        manifest=fixture / "manifest.jsonl",
        output=output,
        workspace_root=fixture,
        work_dir=tmp_path / "work",
    )
    assert output.read_text(encoding="utf-8").count("\n") == 1
