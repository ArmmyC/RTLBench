import hashlib
import json
import os
import stat
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

import rtlbench.candidate_verification as verification
from rtlbench.candidate_manifest import load_manifest


FIXTURE = Path(__file__).parent / "fixtures" / "candidate_verification"
TOOL_NAMES = ("iverilog", "vvp", "verilator", "yosys")


def _make_tool(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nset -u\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _fake_tools(tmp_path: Path) -> dict[str, verification.ToolInfo]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    iverilog = _make_tool(
        tmp_path / "iverilog",
        """
        if [ "${1:-}" = "-V" ]; then echo 'fake iverilog 1.0'; exit 0; fi
        if [ "${FAKE_IVERILOG_SLEEP:-0}" != "0" ]; then sleep "$FAKE_IVERILOG_SLEEP"; fi
        if [ "${FAKE_IVERILOG_FAIL:-0}" != "0" ]; then echo 'compile error'; exit 1; fi
        output=''
        previous=''
        for arg in "$@"; do
          if [ "$previous" = "-o" ]; then output="$arg"; fi
          previous="$arg"
        done
        if [ -n "$output" ]; then : > "$output"; fi
        exit 0
        """,
    )
    vvp = _make_tool(
        tmp_path / "vvp",
        """
        if [ "${1:-}" = "-V" ]; then echo 'fake vvp 1.0'; exit 0; fi
        if [ "${FAKE_VVP_SLEEP:-0}" != "0" ]; then sleep "$FAKE_VVP_SLEEP"; fi
        if [ -n "${FAKE_VVP_OUTPUT:-}" ]; then printf '%s\\n' "$FAKE_VVP_OUTPUT"; fi
        exit "${FAKE_VVP_RETURN:-0}"
        """,
    )
    verilator = _make_tool(
        tmp_path / "verilator",
        """
        if [ "${1:-}" = "--version" ]; then echo 'fake verilator 1.0'; exit 0; fi
        if [ "${FAKE_VERILATOR_FAIL:-0}" != "0" ]; then echo 'lint failure'; exit 1; fi
        exit 0
        """,
    )
    yosys = _make_tool(
        tmp_path / "yosys",
        """
        if [ "${1:-}" = "--version" ]; then echo 'fake yosys 1.0'; exit 0; fi
        if [ "${FAKE_YOSYS_FAIL:-0}" != "0" ]; then echo 'synthesis failure'; exit 1; fi
        exit 0
        """,
    )
    return {
        "iverilog": verification.ToolInfo(str(iverilog), "fake iverilog 1.0"),
        "vvp": verification.ToolInfo(str(vvp), "fake vvp 1.0"),
        "verilator": verification.ToolInfo(str(verilator), "fake verilator 1.0"),
        "yosys": verification.ToolInfo(str(yosys), "fake yosys 1.0"),
    }


def _row(index: int = 0, *, requested=None):
    row = load_manifest(FIXTURE / "manifest.jsonl", FIXTURE)[index]
    if requested is not None:
        row = replace(row, requested_checks=requested)
    return row


def _evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
def _direct_row_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, row=None, toolchain=None, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    tools = _fake_tools(tools_dir)
    evidence = verification.verify_row(
        row or _row(),
        input_hashes=verification.hash_inputs(row or _row()),
        toolchain=toolchain or tools,
        work_dir=tmp_path / "work",
        timeout=0.5,
        workspace_root=FIXTURE,
        manifest_path=FIXTURE / "manifest.jsonl",
    )
    return evidence


def test_accepted_compile_and_simulation_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    evidence = _direct_row_evidence(tmp_path, monkeypatch)
    assert evidence["accepted"] is True
    assert evidence["failure_category"] == "passed"
    assert evidence["checks"]["compile"]["candidate"] == {"attempted": True, "passed": True, "reason": None}
    assert evidence["checks"]["simulation"]["candidate_passes"]["passed"] is True
    assert evidence["mismatch_summary"] == {"reported_counts": [], "maximum_count": None}


@pytest.mark.parametrize("output", ["", "Mismatches: 0", "  mIsMaTcHeS  :  0  "])
def test_zero_or_no_mismatch_with_zero_return_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str):
    evidence = _direct_row_evidence(tmp_path, monkeypatch, FAKE_VVP_OUTPUT=output)
    assert evidence["accepted"] is True
    assert evidence["mismatch_summary"]["reported_counts"] == ([] if not output else [0])


@pytest.mark.parametrize("returncode", [0, 1])
def test_positive_mismatch_is_functional_mismatch_even_on_nonzero_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int):
    evidence = _direct_row_evidence(
        tmp_path,
        monkeypatch,
        FAKE_VVP_OUTPUT="Mismatches: 2\nMISMATCHES: 0",
        FAKE_VVP_RETURN=returncode,
    )
    assert evidence["accepted"] is False
    assert evidence["failure_category"] == "functional_mismatch"
    assert evidence["mismatch_summary"] == {"reported_counts": [2, 0], "maximum_count": 2}


@pytest.mark.parametrize("output", ["Mismatches: 0", "generic failure"])
def test_nonzero_without_positive_mismatch_is_simulation_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str):
    evidence = _direct_row_evidence(tmp_path, monkeypatch, FAKE_VVP_OUTPUT=output, FAKE_VVP_RETURN=1)
    assert evidence["accepted"] is False
    assert evidence["failure_category"] == "simulation_failure"


def test_compile_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    evidence = _direct_row_evidence(tmp_path, monkeypatch, FAKE_IVERILOG_FAIL=1)
    assert evidence["failure_category"] == "compile_failure"
    assert evidence["accepted"] is False
    assert evidence["checks"]["simulation"]["candidate_passes"] == {
        "attempted": False,
        "passed": None,
        "reason": "compile_failure",
    }


def test_compile_and_simulation_timeouts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    row = _row()
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    tools = _fake_tools(tools_dir)
    monkeypatch.setenv("FAKE_IVERILOG_SLEEP", "1")
    evidence = verification.verify_row(
        row,
        input_hashes=verification.hash_inputs(row),
        toolchain=tools,
        work_dir=tmp_path / "work",
        timeout=0.01,
        workspace_root=FIXTURE,
        manifest_path=FIXTURE / "manifest.jsonl",
    )
    assert evidence["failure_category"] == "timeout"

    monkeypatch.delenv("FAKE_IVERILOG_SLEEP")
    monkeypatch.setenv("FAKE_VVP_SLEEP", "1")
    evidence = verification.verify_row(
        row,
        input_hashes=verification.hash_inputs(row),
        toolchain=tools,
        work_dir=tmp_path / "work2",
        timeout=0.01,
        workspace_root=FIXTURE,
        manifest_path=FIXTURE / "manifest.jsonl",
    )
    assert evidence["failure_category"] == "timeout"


@pytest.mark.parametrize(
    "missing,expected",
    [("iverilog", "compile"), ("vvp", "simulation")],
)
def test_missing_required_tools_are_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str, expected: str):
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    tools = _fake_tools(tools_dir)
    tools[missing] = verification.ToolInfo(None, None)
    evidence = _direct_row_evidence(tmp_path, monkeypatch, toolchain=tools)
    assert evidence["accepted"] is False
    assert evidence["failure_category"] == "tool_unavailable"
    assert evidence["checks"][expected]["candidate" if expected == "compile" else "candidate_passes"]["reason"] == "tool_unavailable"


def test_optional_checks_and_failures_do_not_change_acceptance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    requested = {"compile": True, "simulation": True, "lint": True, "synthesis": True}
    evidence = _direct_row_evidence(tmp_path, monkeypatch, row=_row(requested=requested))
    assert evidence["accepted"] is True
    assert evidence["checks"]["lint"]["candidate"]["passed"] is True
    assert evidence["checks"]["synthesis"]["candidate"]["passed"] is True

    monkeypatch.setenv("FAKE_VERILATOR_FAIL", "1")
    monkeypatch.setenv("FAKE_YOSYS_FAIL", "1")
    evidence = _direct_row_evidence(tmp_path / "failed", monkeypatch, row=_row(requested=requested))
    assert evidence["accepted"] is True
    assert evidence["failure_category"] == "passed"
    assert evidence["checks"]["lint"]["candidate"]["reason"] == "lint_failure"
    assert evidence["checks"]["synthesis"]["candidate"]["reason"] == "synthesis_failure"


def test_optional_unrequested_and_unavailable_states(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    evidence = _direct_row_evidence(tmp_path, monkeypatch)
    assert evidence["checks"]["lint"]["candidate"]["reason"] == "not_requested"
    assert evidence["checks"]["synthesis"]["candidate"]["reason"] == "not_requested"
    tools = _fake_tools(tmp_path / "tools2")
    tools["verilator"] = verification.ToolInfo(None, None)
    tools["yosys"] = verification.ToolInfo(None, None)
    requested = {"compile": True, "simulation": True, "lint": True, "synthesis": True}
    evidence = _direct_row_evidence(tmp_path / "unavailable", monkeypatch, row=_row(requested=requested), toolchain=tools)
    assert evidence["checks"]["lint"]["candidate"]["reason"] == "tool_unavailable"
    assert evidence["checks"]["synthesis"]["candidate"]["reason"] == "tool_unavailable"


def test_tool_version_probe_failure_keeps_executable_available(tmp_path: Path):
    executable = _make_tool(tmp_path / "probe-fails", """if [ "${1:-}" = "--version" ]; then exit 1; fi; exit 0""")
    toolchain = verification.discover_toolchain(timeout=0.5, which=lambda name: str(executable))
    assert all(info.to_dict() == {"available": True, "version": None} for info in toolchain.values())


def test_hashes_and_json_are_deterministic(tmp_path: Path):
    row = {
        "schema_version": "rtl_candidate_manifest_v0.1",
        "candidate_id": "candidate-1",
        "task_id": "task-1",
        "source_id": "source-1",
        "attempt": 1,
        "top_module": "TopModule",
        "candidate_rtl_path": "candidate.sv",
        "testbench_path": "testbench.sv",
        "support_files": ["z.sv", "a.sv"],
        "requested_checks": {"compile": True, "simulation": True, "lint": False, "synthesis": False},
    }
    root = tmp_path / "root"
    root.mkdir()
    (root / "candidate.sv").write_bytes(b"candidate")
    (root / "testbench.sv").write_bytes(b"testbench")
    (root / "a.sv").write_bytes(b"a")
    (root / "z.sv").write_bytes(b"z")
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(json.dumps(row) + "\n", encoding="utf-8")
    loaded = load_manifest(manifest, root)[0]
    hashes = verification.hash_inputs(loaded)
    assert [item["path"] for item in hashes["support_files"]] == ["a.sv", "z.sv"]
    assert hashes["candidate_rtl_sha256"] == hashlib.sha256(b"candidate").hexdigest()
    assert verification.deterministic_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_diagnostics_are_bounded_and_private(tmp_path: Path):
    candidate = tmp_path / "candidate.sv"
    candidate.write_text("module TopModule; // CANDIDATE_SOURCE_SENTINEL\nendmodule\n", encoding="utf-8")
    message = "CANDIDATE_SOURCE_SENTINEL /private/absolute/path api_key=FAKE_KEY " + ("x" * 6000)
    diagnostic = verification._sanitize_candidate_diagnostic(
        message,
        workspace_root=tmp_path,
        work_dir=tmp_path / ".rtlbench-run-random",
        manifest_path=tmp_path / "manifest.jsonl",
        artifact_paths=[candidate],
    )
    assert len(diagnostic) <= 4096
    assert "CANDIDATE_SOURCE_SENTINEL" not in diagnostic
    assert "FAKE_KEY" not in diagnostic
    assert "/private/absolute/path" not in diagnostic
    assert ".rtlbench-run-random" not in diagnostic


def test_unexpected_row_exception_is_internal_and_later_rows_continue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rows = [load_manifest(FIXTURE / "manifest.jsonl", FIXTURE)[0], load_manifest(FIXTURE / "manifest.jsonl", FIXTURE)[2]]
    rows[1] = replace(rows[1], candidate_id="later", task_id="later-task")
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("\n".join(json.dumps({
        "schema_version": row.schema_version,
        "candidate_id": row.candidate_id,
        "task_id": row.task_id,
        "source_id": row.source_id,
        "attempt": row.attempt,
        "top_module": row.top_module,
        "candidate_rtl_path": str(Path(row.candidate_rtl_path)),
        "testbench_path": str(Path(row.testbench_path)),
        "support_files": list(row.support_files),
        "requested_checks": row.requested_checks,
    }) for row in rows) + "\n", encoding="utf-8")
    output = tmp_path / "evidence.jsonl"
    calls = []
    original = verification.verify_row
    def fail_first(*args, **kwargs):
        calls.append(args[0].candidate_id)
        if len(calls) == 1:
            raise RuntimeError("unexpected ROW_INTERNAL_SENTINEL")
        return original(*args, **kwargs)
    monkeypatch.setattr(verification, "verify_row", fail_first)
    monkeypatch.setattr(verification, "discover_toolchain", lambda **kwargs: {name: verification.ToolInfo(None, None) for name in TOOL_NAMES})
    verification.verify_candidates(
        manifest=manifest,
        output=output,
        workspace_root=FIXTURE,
        work_dir=tmp_path / "work",
        force=True,
    )
    evidence = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [row["candidate_id"] for row in evidence] == ["rtlgen_prob001_attempt_01", "later"]
    assert evidence[0]["failure_category"] == "internal_error"
    assert evidence[1]["failure_category"] == "tool_unavailable"


def test_output_force_aliases_and_interruption_safety(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    output = tmp_path / "evidence.jsonl"
    output.write_text("existing\n", encoding="utf-8")
    monkeypatch.setattr(verification, "discover_toolchain", lambda **kwargs: {name: verification.ToolInfo(None, None) for name in TOOL_NAMES})
    with pytest.raises(verification.CandidateVerificationPreflightError):
        verification.verify_candidates(
            manifest=FIXTURE / "manifest.jsonl", output=output, workspace_root=FIXTURE, work_dir=tmp_path / "work"
        )
    verification.verify_candidates(
        manifest=FIXTURE / "manifest.jsonl", output=output, workspace_root=FIXTURE, work_dir=tmp_path / "work", force=True
    )
    assert output.is_file()

    output.write_text("preserve\n", encoding="utf-8")
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(verification, "verify_row", interrupt)
    with pytest.raises(verification.CandidateVerificationInterrupted):
        verification.verify_candidates(
            manifest=FIXTURE / "manifest.jsonl", output=output, workspace_root=FIXTURE, work_dir=tmp_path / "work2", force=True
        )
    assert output.read_text(encoding="utf-8") == "preserve\n"
    assert Path(str(output) + ".rtlbench-partial").is_file()


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_output_and_partial_hard_link_aliases_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    output = tmp_path / "evidence.jsonl"
    partial = Path(str(output) + ".rtlbench-partial")
    output.write_text("old\n", encoding="utf-8")
    os.link(output, partial)
    monkeypatch.setattr(verification, "discover_toolchain", lambda **kwargs: {name: verification.ToolInfo(None, None) for name in TOOL_NAMES})
    with pytest.raises(verification.CandidateVerificationPreflightError, match="alias"):
        verification.verify_candidates(
            manifest=FIXTURE / "manifest.jsonl", output=output, workspace_root=FIXTURE, work_dir=tmp_path / "work", force=True
        )


def test_discovery_interruption_retains_partial_and_does_not_replace_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    output = tmp_path / "evidence.jsonl"
    output.write_text("preserve\n", encoding="utf-8")

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(verification, "discover_toolchain", interrupt)
    with pytest.raises(verification.CandidateVerificationInterrupted):
        verification.verify_candidates(
            manifest=FIXTURE / "manifest.jsonl",
            output=output,
            workspace_root=FIXTURE,
            work_dir=tmp_path / "work",
            force=True,
        )
    assert output.read_text(encoding="utf-8") == "preserve\n"
    assert Path(str(output) + ".rtlbench-partial").is_file()
