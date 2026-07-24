import json
import hashlib
import os
import stat
import sys
from dataclasses import replace
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
        "print(pathlib.Path.cwd(), file=sys.stderr)\n"
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


def _simulation_fake_tools(
    tmp_path: Path,
    *,
    compile_failure: str | None = None,
    outputs: dict[str, str] | None = None,
    returncodes: dict[str, int] | None = None,
) -> tuple[Path, Path]:
    bindir = tmp_path / "simulation-bin"
    bindir.mkdir()
    compile_failure_literal = repr(compile_failure)
    simulation_outputs = {
        "original.sv": "Mismatches: 0",
        "mutated.sv": "Mismatches: 1",
        "repaired.sv": "Mismatches: 0",
    }
    simulation_outputs.update(outputs or {})
    outputs_literal = repr(simulation_outputs)
    returncodes_literal = repr(returncodes or {})
    iverilog = bindir / "iverilog"
    iverilog.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "source = next(path for path in sys.argv if pathlib.Path(path).name in {'original.sv', 'mutated.sv', 'repaired.sv'})\n"
        f"if {compile_failure_literal} and pathlib.Path(source).name == {compile_failure_literal}: raise SystemExit(1)\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
        "out.write_text(pathlib.Path(source).name)\n",
        encoding="utf-8",
    )
    vvp = bindir / "vvp"
    vvp.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "marker = pathlib.Path(sys.argv[1]).read_text()\n"
        f"outputs = {outputs_literal}\n"
        f"returncodes = {returncodes_literal}\n"
        "print(outputs[marker])\n"
        "raise SystemExit(returncodes.get(marker, 0))\n",
        encoding="utf-8",
    )
    for path in (iverilog, vvp):
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return iverilog, vvp


def _simulation_evidence(
    tmp_path: Path,
    *,
    compile_failure: str | None = None,
    outputs: dict[str, str] | None = None,
    returncodes: dict[str, int] | None = None,
) -> dict:
    row = load_manifest(FIXTURE / "manifest.jsonl", FIXTURE)[0]
    row = replace(
        row,
        requested_checks={
            "compile": False,
            "lint": False,
            "simulation": True,
            "synthesis": False,
            "equivalence": False,
            "activity": False,
        },
    )
    iverilog, vvp = _simulation_fake_tools(
        tmp_path,
        compile_failure=compile_failure,
        outputs=outputs,
        returncodes=returncodes,
    )
    toolchain = {
        "iverilog": verification.ToolInfo(str(iverilog), None),
        "vvp": verification.ToolInfo(str(vvp), None),
        "verilator": verification.ToolInfo(None, None),
        "yosys": verification.ToolInfo(None, None),
    }
    return verification.verify_row(
        row,
        input_hashes=verification.hash_inputs(row),
        toolchain=toolchain,
        work_dir=tmp_path / "work",
        timeout=5.0,
        workspace_root=FIXTURE,
        manifest_path=FIXTURE / "manifest.jsonl",
    )


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


@pytest.mark.parametrize("artifact", ["original", "mutated", "repaired"])
def test_simulation_compile_failure_is_compile_failure(
    tmp_path: Path, artifact: str
):
    evidence = _simulation_evidence(tmp_path, compile_failure=f"{artifact}.sv")
    meaning = {
        "original": "original_passes",
        "mutated": "mutated_detects_mutation",
        "repaired": "repaired_passes",
    }[artifact]
    assert evidence["checks"]["simulation"][meaning]["reason"] == "compile_failure"
    assert evidence["failure_category"] == "compile_failure"


def test_mutated_mismatch_count_detects_even_with_nonzero_exit(tmp_path: Path):
    evidence = _simulation_evidence(
        tmp_path,
        returncodes={"mutated.sv": 1},
    )
    leaf = evidence["checks"]["simulation"]["mutated_detects_mutation"]
    assert leaf == {"attempted": True, "passed": True, "reason": None}
    assert evidence["failure_category"] == "passed"


@pytest.mark.parametrize("returncode", [0, 1])
def test_mutated_zero_mismatch_count_does_not_detect(
    tmp_path: Path, returncode: int
):
    evidence = _simulation_evidence(
        tmp_path,
        outputs={"mutated.sv": "Mismatches: 0"},
        returncodes={"mutated.sv": returncode},
    )
    leaf = evidence["checks"]["simulation"]["mutated_detects_mutation"]
    assert leaf["reason"] == "simulation_not_detected"
    assert evidence["failure_category"] == "simulation_not_detected"


@pytest.mark.parametrize("output", ["ERROR", "FATAL"])
def test_mutated_generic_failure_with_nonzero_exit_is_simulation_failure(
    tmp_path: Path, output: str
):
    evidence = _simulation_evidence(
        tmp_path,
        outputs={"mutated.sv": output},
        returncodes={"mutated.sv": 1},
    )
    leaf = evidence["checks"]["simulation"]["mutated_detects_mutation"]
    assert leaf["reason"] == "simulation_failure"
    assert evidence["failure_category"] == "simulation_failure"


def test_mutated_generic_failure_with_zero_exit_does_not_detect(tmp_path: Path):
    evidence = _simulation_evidence(
        tmp_path,
        outputs={"mutated.sv": "failure"},
    )
    leaf = evidence["checks"]["simulation"]["mutated_detects_mutation"]
    assert leaf["reason"] == "simulation_not_detected"
    assert evidence["failure_category"] == "simulation_not_detected"


@pytest.mark.parametrize(
    ("artifact", "meaning", "reason"),
    [
        ("original.sv", "original_passes", "original_simulation_failure"),
        ("repaired.sv", "repaired_passes", "repaired_simulation_failure"),
    ],
)
def test_original_and_repaired_positive_mismatch_counts_fail(
    tmp_path: Path, artifact: str, meaning: str, reason: str
):
    evidence = _simulation_evidence(
        tmp_path,
        outputs={artifact: "Mismatches: 2"},
    )
    leaf = evidence["checks"]["simulation"][meaning]
    assert leaf["reason"] == reason
    assert evidence["failure_category"] == reason


def test_discovered_tool_remains_available_when_version_probe_fails(tmp_path: Path):
    executable = tmp_path / "probe-fails"
    executable.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    toolchain = verification.discover_toolchain(
        timeout=1.0,
        which=lambda name: str(executable),
    )
    assert all(info.path == str(executable) for info in toolchain.values())
    assert all(info.version is None for info in toolchain.values())
    assert all(info.to_dict() == {"available": True, "version": None} for info in toolchain.values())


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
    assert all(record["evidence_tier"] is None for record in records)
    assert records[0]["checks"]["compile"]["original"]["passed"] is None


def _collision_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, Path]]:
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "source_001"
    source.mkdir()
    inputs = {
        "original": source / "original.sv",
        "mutated": source / "mutated.sv",
        "repaired": source / "repaired.sv",
        "testbench": source / "testbench.sv",
        "support": root / "evidence.jsonl.rtlbench-partial",
    }
    for name, path in inputs.items():
        path.write_text(f"{name}\n", encoding="utf-8")
    row = {
        "schema_version": "rtl_mutation_manifest_v0.1",
        "mutation_id": "m1",
        "source_id": "source_001",
        "top_module": "top",
        "original_rtl_path": "source_001/original.sv",
        "mutated_rtl_path": "source_001/mutated.sv",
        "repaired_rtl_path": "source_001/repaired.sv",
        "testbench_path": "source_001/testbench.sv",
        "support_files": ["evidence.jsonl.rtlbench-partial"],
        "mutation_type": "wrong_operator",
        "mutated_signal": "y",
        "changed_location": {"file": "source_001/mutated.sv", "line_start": 1, "line_end": 1},
        "requested_checks": {
            "compile": False,
            "lint": False,
            "simulation": False,
            "synthesis": False,
            "equivalence": False,
            "activity": False,
        },
    }
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(json.dumps(row) + "\n", encoding="utf-8")
    return manifest, root, inputs


@pytest.mark.parametrize("target", ["manifest", "original", "mutated", "repaired", "testbench", "support"])
def test_force_rejects_output_aliasing_each_protected_input(
    tmp_path: Path, target: str, monkeypatch: pytest.MonkeyPatch
):
    manifest, root, inputs = _collision_fixture(tmp_path)
    protected = {"manifest": manifest, **inputs}
    output = protected[target]
    before = output.read_bytes()
    monkeypatch.setattr(verification.shutil, "which", lambda name: None)
    with pytest.raises(verification.VerificationPreflightError, match="protected input"):
        verification.verify_mutations(
            manifest=manifest,
            output=output,
            workspace_root=root,
            work_dir=tmp_path / "work",
            force=True,
        )
    assert output.read_bytes() == before


def test_force_rejects_hard_link_to_protected_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest, root, inputs = _collision_fixture(tmp_path)
    hard_link = tmp_path / "hard-link.jsonl"
    os.link(inputs["original"], hard_link)
    before = inputs["original"].read_bytes()
    monkeypatch.setattr(verification.shutil, "which", lambda name: None)
    with pytest.raises(verification.VerificationPreflightError, match="protected input"):
        verification.verify_mutations(
            manifest=manifest,
            output=hard_link,
            workspace_root=root,
            work_dir=tmp_path / "work",
            force=True,
        )
    assert inputs["original"].read_bytes() == before
    assert hard_link.read_bytes() == before


def test_force_rejects_input_matching_managed_partial_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manifest, root, inputs = _collision_fixture(tmp_path)
    output = root / "evidence.jsonl"
    before = inputs["support"].read_bytes()
    monkeypatch.setattr(verification.shutil, "which", lambda name: None)
    with pytest.raises(verification.VerificationPreflightError, match="managed partial output"):
        verification.verify_mutations(
            manifest=manifest,
            output=output,
            workspace_root=root,
            work_dir=tmp_path / "work",
            force=True,
        )
    assert inputs["support"].read_bytes() == before
    assert not output.exists()


def test_managed_run_directory_ignores_predictable_row_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bindir = _fake_tools(tmp_path)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    row = load_manifest(FIXTURE / "manifest.jsonl", FIXTURE)[0]
    predictable = work_dir / (
        f"000001_{verification._safe_name(row.mutation_id)}_"
        f"{hashlib.sha256(row.mutation_id.encode()).hexdigest()[:12]}"
    )
    predictable.symlink_to(outside, target_is_directory=True)
    output = tmp_path / "evidence.jsonl"
    verification.verify_mutations(
        manifest=FIXTURE / "manifest.jsonl",
        output=output,
        workspace_root=FIXTURE,
        work_dir=work_dir,
        force=True,
    )
    assert list(outside.iterdir()) == []
    assert any(path.name.startswith(".rtlbench-run-") for path in work_dir.iterdir())
    assert ".rtlbench-run-" not in output.read_text(encoding="utf-8")


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
