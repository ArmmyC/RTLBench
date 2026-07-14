import json
import os
import stat
import sys
from pathlib import Path

import pytest

import rtlbench.mutation_verification as verification
from rtlbench.mutation_manifest import load_manifest


FIXTURE = Path(__file__).parent / "fixtures" / "mutation_verification"


def _fake_tools(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    iverilog = bindir / "iverilog"
    iverilog.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
        "if any('mutated.sv' in arg for arg in sys.argv): out.write_text('mutated')\n"
        "else: out.write_text('passing')\n"
        "if any('broken.sv' in arg for arg in sys.argv): raise SystemExit(1)\n",
        encoding="utf-8",
    )
    vvp = bindir / "vvp"
    vvp.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "print('Mismatches: 1' if pathlib.Path(sys.argv[1]).read_text() == 'mutated' else 'Mismatches: 0')\n",
        encoding="utf-8",
    )
    (bindir / "verilator").write_text("#!/usr/bin/env python3\nprint('Verilator 5.0')\n", encoding="utf-8")
    (bindir / "yosys").write_text("#!/usr/bin/env python3\nprint('Yosys 0.40')\n", encoding="utf-8")
    for path in bindir.iterdir():
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return bindir


def test_fake_tools_produce_hashes_and_simulation_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bindir = _fake_tools(tmp_path)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    output = tmp_path / "evidence.jsonl"
    summary = verification.verify_mutations(
        manifest=FIXTURE / "manifest.jsonl",
        output=output,
        workspace_root=FIXTURE,
        work_dir=tmp_path / "work",
        force=True,
    )
    assert summary == {"rows": 1, "passed": 1, "failed": 0}
    row = json.loads(output.read_text(encoding="utf-8"))
    assert row["evidence_tier"] == "A"
    assert row["failure_category"] == "passed"
    assert row["checks"]["simulation"]["mutated_detects_mutation"]["passed"] is True
    assert len(row["input_hashes"]["original_sha256"]) == 64
    assert row["toolchain"]["yosys"]["available"] is True


def test_unavailable_tools_are_not_passes_and_later_rows_continue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(verification.shutil, "which", lambda name: None)
    root = tmp_path / "root"
    root.mkdir()
    rows = []
    for index in (1, 2):
        source = root / f"s{index}"
        source.mkdir()
        for name in ("original.sv", "mutated.sv", "repaired.sv", "testbench.sv"):
            (source / name).write_text("module top; endmodule\n", encoding="utf-8")
        rows.append(
            {
                "schema_version": "rtl_mutation_manifest_v0.1", "mutation_id": f"m{index}", "source_id": f"s{index}", "top_module": "top",
                "original_rtl_path": f"s{index}/original.sv", "mutated_rtl_path": f"s{index}/mutated.sv", "repaired_rtl_path": f"s{index}/repaired.sv", "testbench_path": f"s{index}/testbench.sv", "support_files": [], "mutation_type": "x", "mutated_signal": "y",
                "changed_location": {"file": f"s{index}/mutated.sv", "line_start": 1, "line_end": 1},
                "requested_checks": {"compile": True, "lint": True, "simulation": True, "synthesis": True, "equivalence": True, "activity": True},
            }
        )
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    output = tmp_path / "evidence.jsonl"
    summary = verification.verify_mutations(manifest=manifest, output=output, workspace_root=root, work_dir=tmp_path / "work")
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert summary["rows"] == 2
    assert len(records) == 2
    assert all(record["failure_category"] == "tool_unavailable" for record in records)
    assert records[0]["checks"]["compile"]["original"]["passed"] is None


def test_output_requires_force_and_force_replaces_managed_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(verification.shutil, "which", lambda name: None)
    output = tmp_path / "evidence.jsonl"
    kwargs = dict(manifest=FIXTURE / "manifest.jsonl", output=output, workspace_root=FIXTURE, work_dir=tmp_path / "work")
    verification.verify_mutations(**kwargs)
    with pytest.raises(verification.VerificationPreflightError):
        verification.verify_mutations(**kwargs)
    output.write_text("old\n", encoding="utf-8")
    verification.verify_mutations(**kwargs, force=True)
    assert output.read_text(encoding="utf-8") != "old\n"


def test_interruption_preserves_existing_final_and_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    output = tmp_path / "evidence.jsonl"
    output.write_text("existing\n", encoding="utf-8")
    monkeypatch.setattr(verification, "discover_toolchain", lambda: {name: verification.ToolInfo(None, None) for name in ("iverilog", "vvp", "verilator", "yosys")})
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(verification, "verify_row", interrupt)
    with pytest.raises(verification.VerificationInterrupted):
        verification.verify_mutations(manifest=FIXTURE / "manifest.jsonl", output=output, workspace_root=FIXTURE, work_dir=tmp_path / "work", force=True)
    assert output.read_text(encoding="utf-8") == "existing\n"
    assert Path(str(output) + ".rtlbench-partial").is_file()


def test_hash_support_order_is_deterministic(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    source = root / "s"
    source.mkdir()
    for name in ("original.sv", "mutated.sv", "repaired.sv", "testbench.sv", "z.sv", "a.sv"):
        (source / name).write_text(name, encoding="utf-8")
    row = {
        "schema_version": "rtl_mutation_manifest_v0.1", "mutation_id": "m", "source_id": "s", "top_module": "top",
        "original_rtl_path": "s/original.sv", "mutated_rtl_path": "s/mutated.sv", "repaired_rtl_path": "s/repaired.sv", "testbench_path": "s/testbench.sv", "support_files": ["s/z.sv", "s/a.sv"], "mutation_type": "x", "mutated_signal": "y",
        "changed_location": {"file": "s/mutated.sv", "line_start": 1, "line_end": 1},
        "requested_checks": {"compile": False, "lint": False, "simulation": False, "synthesis": False, "equivalence": False, "activity": False},
    }
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(json.dumps(row) + "\n", encoding="utf-8")
    loaded = load_manifest(manifest, root)[0]
    hashes = verification.hash_inputs(loaded)
    assert [item["path"] for item in hashes["support_files"]] == ["s/a.sv", "s/z.sv"]
