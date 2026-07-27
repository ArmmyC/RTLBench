import hashlib
import json
import os
import stat
import textwrap
import time
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
        if [ -n "${FAKE_RECORD_INPUT_DIR:-}" ]; then
          mkdir -p "$FAKE_RECORD_INPUT_DIR"
          for arg in "$@"; do
            case "$arg" in
              *.sv) name="${arg##*/}"; cat "$arg" > "$FAKE_RECORD_INPUT_DIR/$name" ;;
            esac
          done
        fi
        if [ -n "${FAKE_IVERILOG_TRACE:-}" ]; then printf '%s\n' "$*" >> "$FAKE_IVERILOG_TRACE"; fi
        if [ "${FAKE_IVERILOG_FLOOD:-0}" != "0" ]; then
          i=0
          while [ "$i" -lt "$FAKE_IVERILOG_FLOOD" ]; do printf 'compile-flood'; i=$((i + 1)); done
        fi
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
        if [ "${FAKE_PRINT_SECRET_ENV:-0}" != "0" ]; then
          printf 'SECRET_ENV=%s\\n' "${SECRET_ENV_SENTINEL:-not-visible}"
        fi
        if [ "${FAKE_VVP_FLOOD:-0}" != "0" ]; then
          i=0
          while [ "$i" -lt "$FAKE_VVP_FLOOD" ]; do printf 'simulation-flood'; i=$((i + 1)); done
        fi
        exit "${FAKE_VVP_RETURN:-0}"
        """,
    )
    verilator = _make_tool(
        tmp_path / "verilator",
        """
        if [ "${1:-}" = "--version" ]; then echo 'fake verilator 1.0'; exit 0; fi
        if [ "${FAKE_VERILATOR_FAIL:-0}" != "0" ]; then echo 'lint failure'; exit 1; fi
        if [ "${FAKE_VERILATOR_FLOOD:-0}" != "0" ]; then
          i=0
          while [ "$i" -lt "$FAKE_VERILATOR_FLOOD" ]; do printf 'lint-flood'; i=$((i + 1)); done
        fi
        exit 0
        """,
    )
    yosys = _make_tool(
        tmp_path / "yosys",
        """
        if [ "${1:-}" = "--version" ]; then echo 'fake yosys 1.0'; exit 0; fi
        if [ "${FAKE_YOSYS_FAIL:-0}" != "0" ]; then echo 'synthesis failure'; exit 1; fi
        if [ "${FAKE_YOSYS_FLOOD:-0}" != "0" ]; then
          i=0
          while [ "$i" -lt "$FAKE_YOSYS_FLOOD" ]; do printf 'synthesis-flood'; i=$((i + 1)); done
        fi
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
    return _direct_row_evidence(tmp_path, monkeypatch, **env)


def _direct_row_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    row=None,
    toolchain=None,
    max_output_bytes=verification.DEFAULT_MAX_OUTPUT_BYTES,
    **env,
):
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
        max_output_bytes=max_output_bytes,
        environment={key: str(value) for key, value in env.items()},
    )
    return evidence


def _custom_snapshot_fixture(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    files = {
        "candidate.sv": "CANDIDATE_ORIGINAL_SENTINEL\n",
        "testbench.sv": "TESTBENCH_ORIGINAL_SENTINEL\n",
        "support.sv": "SUPPORT_ORIGINAL_SENTINEL\n",
    }
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")
    row = {
        "schema_version": "rtl_candidate_manifest_v0.1",
        "candidate_id": "snapshot-candidate",
        "task_id": "snapshot-task",
        "source_id": "snapshot-source",
        "attempt": 1,
        "top_module": "TopModule",
        "testbench_top": "tb",
        "candidate_rtl_path": "candidate.sv",
        "testbench_path": "testbench.sv",
        "support_files": ["support.sv"],
        "simulation_result_contract": "mismatch_count_v1",
        "requested_checks": {
            "compile": True,
            "simulation": True,
            "lint": False,
            "synthesis": False,
        },
    }
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(json.dumps(row) + "\n", encoding="utf-8")
    return manifest, root, files


@pytest.mark.parametrize("action", ["modify", "delete"])
def test_execution_uses_prepared_snapshot_bytes_after_workspace_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
):
    manifest, root, files = _custom_snapshot_fixture(tmp_path)
    record_dir = tmp_path / "recorded-inputs"
    tools = _fake_tools(tmp_path / "tools")

    def discover(**kwargs):
        for name, original in files.items():
            path = root / name
            if action == "delete":
                path.unlink()
            else:
                path.write_text(f"{name.upper()}_MODIFIED_SENTINEL\n", encoding="utf-8")
        return tools

    monkeypatch.setattr(verification, "discover_toolchain", discover)
    output = tmp_path / "evidence.jsonl"
    verification.verify_candidates(
        manifest=manifest,
        output=output,
        workspace_root=root,
        work_dir=tmp_path / "work",
        force=True,
        environment={
            "FAKE_RECORD_INPUT_DIR": str(record_dir),
            "FAKE_VVP_OUTPUT": "Mismatches: 0 in 1 samples",
        },
    )
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["accepted"] is True
    assert evidence["input_hashes"]["candidate_rtl_sha256"] == hashlib.sha256(
        files["candidate.sv"].encode()
    ).hexdigest()
    assert evidence["input_hashes"]["testbench_sha256"] == hashlib.sha256(
        files["testbench.sv"].encode()
    ).hexdigest()
    assert evidence["input_hashes"]["support_files"][0]["sha256"] == hashlib.sha256(
        files["support.sv"].encode()
    ).hexdigest()
    assert (record_dir / "candidate.sv").read_text(encoding="utf-8") == files[
        "candidate.sv"
    ]
    assert (record_dir / "testbench.sv").read_text(encoding="utf-8") == files[
        "testbench.sv"
    ]
    support_record = next(record_dir.glob("*support.sv"))
    assert support_record.read_text(encoding="utf-8") == files["support.sv"]


@pytest.mark.parametrize("artifact", ["candidate.sv", "testbench.sv", "support.sv"])
def test_artifact_limit_rejects_each_input_before_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artifact: str
):
    manifest, root, _ = _custom_snapshot_fixture(tmp_path)
    (root / artifact).write_bytes(b"12345")
    called = []
    monkeypatch.setattr(verification, "discover_toolchain", lambda **kwargs: called.append(True))
    output = tmp_path / "evidence.jsonl"
    with pytest.raises(verification.CandidateVerificationPreflightError, match="max-artifact-bytes"):
        verification.verify_candidates(
            manifest=manifest,
            output=output,
            workspace_root=root,
            work_dir=tmp_path / "work",
            force=True,
            max_artifact_bytes=4,
            max_row_input_bytes=100,
            max_run_input_bytes=100,
        )
    assert called == []
    assert not output.exists()


def test_row_and_run_limits_are_enforced_before_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest, root, _ = _custom_snapshot_fixture(tmp_path)
    called = []
    monkeypatch.setattr(verification, "discover_toolchain", lambda **kwargs: called.append(True))
    with pytest.raises(verification.CandidateVerificationPreflightError, match="max-row-input-bytes"):
        verification.verify_candidates(
            manifest=manifest,
            output=tmp_path / "row-evidence.jsonl",
            workspace_root=root,
            work_dir=tmp_path / "row-work",
            force=True,
            max_artifact_bytes=100,
            max_row_input_bytes=10,
            max_run_input_bytes=100,
        )
    assert called == []

    with pytest.raises(verification.CandidateVerificationPreflightError, match="max-run-input-bytes"):
        verification.verify_candidates(
            manifest=manifest,
            output=tmp_path / "run-evidence.jsonl",
            workspace_root=root,
            work_dir=tmp_path / "run-work",
            force=True,
            max_artifact_bytes=100,
            max_row_input_bytes=100,
            max_run_input_bytes=10,
        )
    assert called == []


def test_exact_artifact_boundary_is_accepted(tmp_path: Path):
    manifest, root, _ = _custom_snapshot_fixture(tmp_path)
    rows = load_manifest(manifest, root)
    managed_run = tmp_path / "run"
    managed_run.mkdir()
    prepared = verification.prepare_candidate_rows(
        rows,
        managed_run,
        max_artifact_bytes=len("CANDIDATE_ORIGINAL_SENTINEL\n"),
        max_row_input_bytes=100,
        max_run_input_bytes=100,
    )
    assert prepared[0].input_hashes["candidate_rtl_sha256"] == hashlib.sha256(
        b"CANDIDATE_ORIGINAL_SENTINEL\n"
    ).hexdigest()


def test_input_limit_arguments_reject_booleans_and_hard_cap_overflow(tmp_path: Path):
    manifest, root, _ = _custom_snapshot_fixture(tmp_path)
    for value in (True, 0, verification.MAX_ARTIFACT_BYTES_LIMIT + 1):
        with pytest.raises(verification.CandidateManifestValidationError):
            verification.verify_candidates(
                manifest=manifest,
                output=tmp_path / f"evidence-{value}.jsonl",
                workspace_root=root,
                work_dir=tmp_path / f"work-{value}",
                max_artifact_bytes=value,
            )


def test_file_growing_during_copy_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest, root, _ = _custom_snapshot_fixture(tmp_path)
    original_fstat = verification.os.fstat
    calls = {"count": 0}

    def fstat_with_growth(descriptor):
        calls["count"] += 1
        if calls["count"] == 3:
            (root / "candidate.sv").write_bytes(b"CANDIDATE_ORIGINAL_SENTINEL\nGROWN")
        return original_fstat(descriptor)

    monkeypatch.setattr(verification.os, "fstat", fstat_with_growth)
    rows = load_manifest(manifest, root)
    managed_run = tmp_path / "run"
    managed_run.mkdir()
    with pytest.raises(verification.CandidateVerificationPreflightError, match="source changed"):
        verification.prepare_candidate_rows(rows, managed_run)


def test_diagnostic_index_limit_omits_unproven_raw_tool_text(tmp_path: Path):
    manifest, root, _ = _custom_snapshot_fixture(tmp_path)
    long_sentinel = "LONG-SOURCE-SENTINEL-WITHOUT_VALID_IDENTIFIER_CHARS"
    (root / "candidate.sv").write_text(
        "module TopModule; // " + ("x" * 1100) + long_sentinel + "\nendmodule\n",
        encoding="utf-8",
    )
    row = load_manifest(manifest, root)[0]
    managed_run = tmp_path / "run"
    managed_run.mkdir()
    prepared = verification.prepare_candidate_rows([row], managed_run)[0]
    context = verification._build_redaction_context(row, prepared)
    diagnostic = verification._diagnostic(
        "compile",
        verification.CommandResult(1, long_sentinel + " compiler context"),
        root,
        managed_run,
        manifest,
        context,
    )
    assert diagnostic is not None
    assert long_sentinel not in diagnostic
    assert "returncode=1" in diagnostic


def test_accepted_compile_and_simulation_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    evidence = _direct_row_evidence(
        tmp_path,
        monkeypatch,
        FAKE_VVP_OUTPUT="Mismatches: 0 in 4 samples",
    )
    assert evidence["accepted"] is True
    assert evidence["failure_category"] == "passed"
    assert evidence["checks"]["compile"]["candidate"] == {"attempted": True, "passed": True, "reason": None}
    assert evidence["checks"]["simulation"]["candidate_passes"]["passed"] is True
    assert evidence["testbench_top"] == "tb"
    assert evidence["simulation_result_contract"] == "mismatch_count_v1"
    assert evidence["mismatch_summary"] == {
        "contract": "mismatch_count_v1",
        "reported_counts": [0],
        "reported_sample_counts": [4],
        "maximum_count": 0,
        "timeout_reported": False,
    }


@pytest.mark.parametrize("output", ["Mismatches: 0", "Mismatches: 0 in 20 samples", "  mIsMaTcHeS  :  0  "])
def test_zero_mismatch_with_zero_return_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str):
    evidence = _direct_row_evidence(tmp_path, monkeypatch, FAKE_VVP_OUTPUT=output)
    assert evidence["accepted"] is True
    assert evidence["mismatch_summary"]["reported_counts"] == [0]


def test_mismatch_contract_requires_a_machine_readable_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    evidence = _direct_row_evidence(tmp_path, monkeypatch, FAKE_VVP_OUTPUT="")
    assert evidence["accepted"] is False
    assert evidence["failure_category"] == "simulation_result_missing"


def test_exit_code_contract_does_not_require_a_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    evidence = _direct_row_evidence(
        tmp_path,
        monkeypatch,
        row=replace(_row(), simulation_result_contract="exit_code_v1"),
    )
    assert evidence["accepted"] is True
    assert evidence["failure_category"] == "passed"
    assert evidence["mismatch_summary"] == {
        "contract": "exit_code_v1",
        "reported_counts": [],
        "reported_sample_counts": [],
        "maximum_count": None,
        "timeout_reported": False,
    }


@pytest.mark.parametrize("returncode", [0, 1])
def test_positive_mismatch_is_functional_mismatch_even_on_nonzero_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int):
    evidence = _direct_row_evidence(
        tmp_path,
        monkeypatch,
        FAKE_VVP_OUTPUT="Mismatches: 2 in 40 samples\nMISMATCHES: 0\nMismatches: 1 in 7 samples",
        FAKE_VVP_RETURN=returncode,
    )
    assert evidence["accepted"] is False
    assert evidence["failure_category"] == "functional_mismatch"
    assert evidence["mismatch_summary"] == {
        "contract": "mismatch_count_v1",
        "reported_counts": [2, 0, 1],
        "reported_sample_counts": [40, None, 7],
        "maximum_count": 2,
        "timeout_reported": False,
    }


@pytest.mark.parametrize(
    "output",
    [
        "Mismatches: 4 in 20 samples trailing",
        "prefix Mismatches: 4",
        "mismatch: 4",
        "there was a mismatch",
    ],
)
def test_malformed_and_generic_mismatch_text_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str
):
    report = verification._parse_mismatch_report(output, "mismatch_count_v1")
    assert report.reported_counts == ()
    evidence = _direct_row_evidence(tmp_path, monkeypatch, FAKE_VVP_OUTPUT=output)
    assert evidence["failure_category"] == "simulation_result_missing"


@pytest.mark.parametrize("returncode", [0, 1])
def test_positive_long_form_mismatch_is_functional_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int
):
    evidence = _direct_row_evidence(
        tmp_path,
        monkeypatch,
        FAKE_VVP_OUTPUT="Mismatches: 4 in 20 samples",
        FAKE_VVP_RETURN=returncode,
    )
    assert evidence["failure_category"] == "functional_mismatch"
    assert evidence["mismatch_summary"]["reported_sample_counts"] == [20]


def test_timeout_marker_takes_priority_over_counts_and_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    evidence = _direct_row_evidence(
        tmp_path,
        monkeypatch,
        FAKE_VVP_OUTPUT="Mismatches: 0 in 4 samples\nTIMEOUT",
        FAKE_VVP_RETURN=0,
    )
    assert evidence["accepted"] is False
    assert evidence["failure_category"] == "timeout"
    assert evidence["mismatch_summary"]["timeout_reported"] is True


@pytest.mark.parametrize("output", ["timeout while waiting", "a TIMEOUT occurred"])
def test_timeout_prose_is_not_timeout_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str
):
    evidence = _direct_row_evidence(tmp_path, monkeypatch, FAKE_VVP_OUTPUT=output)
    assert evidence["failure_category"] == "simulation_result_missing"
    assert evidence["mismatch_summary"]["timeout_reported"] is False


def test_simulation_selects_declared_testbench_top(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    trace = tmp_path / "iverilog-args.txt"
    evidence = _direct_row_evidence(
        tmp_path,
        monkeypatch,
        FAKE_IVERILOG_TRACE=str(trace),
        FAKE_VVP_OUTPUT="Mismatches: 0 in 4 samples",
    )
    assert evidence["accepted"] is True
    invocations = trace.read_text(encoding="utf-8").splitlines()
    assert any("-s TopModule" in invocation for invocation in invocations)
    assert any("-s tb" in invocation for invocation in invocations)


def test_output_flood_is_terminated_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    evidence = _direct_row_evidence(
        tmp_path,
        monkeypatch,
        max_output_bytes=128,
        FAKE_VVP_FLOOD=100_000,
    )
    assert evidence["accepted"] is False
    assert evidence["failure_category"] == "simulation_failure"
    assert any("output_limit_exceeded" in item for item in evidence["diagnostics"])
    assert sum(len(item) for item in evidence["diagnostics"]) <= 4096


def test_compile_output_flood_is_compile_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    evidence = _direct_row_evidence(
        tmp_path,
        monkeypatch,
        max_output_bytes=128,
        FAKE_IVERILOG_FLOOD=100_000,
    )
    assert evidence["accepted"] is False
    assert evidence["failure_category"] == "compile_failure"
    assert "output_limit_exceeded" in evidence["diagnostics"][0]


@pytest.mark.parametrize(
    "tool_env,check_name",
    [
        ({"FAKE_VERILATOR_FLOOD": 100_000}, "lint"),
        ({"FAKE_YOSYS_FLOOD": 100_000}, "synthesis"),
    ],
)
def test_optional_output_flood_is_bounded_and_non_acceptance_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_env: dict[str, int],
    check_name: str,
):
    requested = {"compile": True, "simulation": True, "lint": True, "synthesis": True}
    evidence = _direct_row_evidence(
        tmp_path,
        monkeypatch,
        row=_row(requested=requested),
        max_output_bytes=128,
        FAKE_VVP_OUTPUT="Mismatches: 0 in 4 samples",
        **tool_env,
    )
    assert evidence["accepted"] is True
    assert evidence["failure_category"] == "passed"
    assert evidence["checks"][check_name]["candidate"]["reason"] == f"{check_name}_failure"
    assert any("output_limit_exceeded" in item for item in evidence["diagnostics"])


def test_parent_environment_is_not_forwarded_to_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SECRET_ENV_SENTINEL", "PRIVATE_SECRET_SENTINEL")
    evidence = _direct_row_evidence(
        tmp_path,
        monkeypatch,
        FAKE_PRINT_SECRET_ENV=1,
    )
    serialized = json.dumps(evidence, sort_keys=True)
    assert "PRIVATE_SECRET_SENTINEL" not in serialized
    assert evidence["diagnostics"] == ["simulation: returncode=0"]


def test_minimal_path_retains_discovered_tool_directory_without_parent_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    tool_dir = tmp_path / "absolute-tool-dir"
    helper = _make_tool(tool_dir / "fake-helper", "echo helper-visible\n")
    tool = _make_tool(
        tool_dir / "fake-tool",
        """
        helper_output=$(fake-helper)
        printf 'helper=%s secret=%s\\n' "$helper_output" "${PARENT_SECRET_SENTINEL:-not-visible}"
        exit 0
        """,
    )
    monkeypatch.setenv("PARENT_SECRET_SENTINEL", "PRIVATE_PARENT_SECRET")
    result = verification._run([str(tool)], tmp_path, 0.5)
    assert result.returncode == 0
    assert "helper=helper-visible" in result.output
    assert "PRIVATE_PARENT_SECRET" not in result.output
    assert helper.exists()


@pytest.mark.skipif(os.name != "posix", reason="process-group test requires POSIX")
@pytest.mark.parametrize("mode", ["timeout", "output"])
def test_process_group_cleanup_kills_descendants(
    tmp_path: Path, mode: str
):
    marker = tmp_path / f"descendant-{mode}.marker"
    tool = _make_tool(
        tmp_path / f"tree-tool-{mode}",
        """
        (sleep 0.3; : > "$FAKE_DESCENDANT_MARKER") &
        if [ "$FAKE_TREE_MODE" = "output" ]; then
          i=0
          while [ "$i" -lt 100000 ]; do printf 'tree-flood'; i=$((i + 1)); done
        else
          sleep 5
        fi
        """,
    )
    result = verification._run(
        [str(tool)],
        tmp_path,
        0.05 if mode == "timeout" else 1.0,
        128 if mode == "output" else verification.DEFAULT_MAX_OUTPUT_BYTES,
        {"FAKE_DESCENDANT_MARKER": str(marker), "FAKE_TREE_MODE": mode},
    )
    assert result.timed_out is (mode == "timeout")
    assert result.output_limit_exceeded is (mode == "output")
    time.sleep(0.5)
    assert not marker.exists()


@pytest.mark.parametrize("output", ["Mismatches: 0", "Mismatches: 0 in 20 samples"])
def test_nonzero_without_positive_mismatch_is_simulation_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str):
    evidence = _direct_row_evidence(tmp_path, monkeypatch, FAKE_VVP_OUTPUT=output, FAKE_VVP_RETURN=1)
    assert evidence["accepted"] is False
    assert evidence["failure_category"] == "simulation_failure"


def test_nonzero_without_any_result_is_result_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    evidence = _direct_row_evidence(
        tmp_path,
        monkeypatch,
        FAKE_VVP_OUTPUT="generic failure",
        FAKE_VVP_RETURN=1,
    )
    assert evidence["failure_category"] == "simulation_result_missing"


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
    evidence = verification.verify_row(
        row,
        input_hashes=verification.hash_inputs(row),
        toolchain=tools,
        work_dir=tmp_path / "work",
        timeout=0.01,
        workspace_root=FIXTURE,
        manifest_path=FIXTURE / "manifest.jsonl",
        environment={"FAKE_IVERILOG_SLEEP": "1"},
    )
    assert evidence["failure_category"] == "timeout"

    evidence = verification.verify_row(
        row,
        input_hashes=verification.hash_inputs(row),
        toolchain=tools,
        work_dir=tmp_path / "work2",
        timeout=0.01,
        workspace_root=FIXTURE,
        manifest_path=FIXTURE / "manifest.jsonl",
        environment={"FAKE_VVP_SLEEP": "1"},
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
    evidence = _direct_row_evidence(
        tmp_path,
        monkeypatch,
        row=_row(requested=requested),
        FAKE_VVP_OUTPUT="Mismatches: 0 in 4 samples",
    )
    assert evidence["accepted"] is True
    assert evidence["checks"]["lint"]["candidate"]["passed"] is True
    assert evidence["checks"]["synthesis"]["candidate"]["passed"] is True

    evidence = _direct_row_evidence(
        tmp_path / "failed",
        monkeypatch,
        row=_row(requested=requested),
        FAKE_VERILATOR_FAIL=1,
        FAKE_YOSYS_FAIL=1,
        FAKE_VVP_OUTPUT="Mismatches: 0 in 4 samples",
    )
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
        "testbench_top": "tb",
        "candidate_rtl_path": "candidate.sv",
        "testbench_path": "testbench.sv",
        "support_files": ["z.sv", "a.sv"],
        "simulation_result_contract": "mismatch_count_v1",
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


def test_evidence_does_not_leak_candidate_testbench_support_or_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "workspace"
    root.mkdir()
    candidate = "CANDIDATE_SOURCE_SENTINEL_123456"
    testbench = "TESTBENCH_SOURCE_SENTINEL_123456"
    support = "SUPPORT_SOURCE_SENTINEL_123456"
    (root / "candidate.sv").write_text(
        f"module TopModule; // {candidate}\nendmodule\n", encoding="utf-8"
    )
    (root / "testbench.sv").write_text(
        f"module tb; // {testbench}\nendmodule\n", encoding="utf-8"
    )
    (root / "support.sv").write_text(
        f"module helper; // {support}\nendmodule\n", encoding="utf-8"
    )
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "rtl_candidate_manifest_v0.1",
                "candidate_id": "privacy-candidate",
                "task_id": "privacy-task",
                "source_id": "privacy-source",
                "attempt": 1,
                "top_module": "TopModule",
                "testbench_top": "tb",
                "candidate_rtl_path": "candidate.sv",
                "testbench_path": "testbench.sv",
                "support_files": ["support.sv"],
                "simulation_result_contract": "mismatch_count_v1",
                "requested_checks": {
                    "compile": True,
                    "simulation": True,
                    "lint": False,
                    "synthesis": False,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    row = load_manifest(manifest, root)[0]
    tools = _fake_tools(tmp_path / "tools")
    evidence = verification.verify_row(
        row,
        input_hashes=verification.hash_inputs(row),
        toolchain=tools,
        work_dir=tmp_path / "work",
        timeout=0.5,
        workspace_root=root,
        manifest_path=manifest,
        environment={
            "FAKE_VVP_OUTPUT": (
                f"{candidate} {testbench} {support} {tmp_path}/private/path "
                "api_key=FAKE_CREDENTIAL_SENTINEL"
            )
        },
    )
    serialized = json.dumps(evidence, sort_keys=True)
    for sentinel in (candidate, testbench, support, "FAKE_CREDENTIAL_SENTINEL"):
        assert sentinel not in serialized
    assert str(tmp_path) not in serialized


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
        "testbench_top": row.testbench_top,
        "candidate_rtl_path": str(Path(row.candidate_rtl_path)),
        "testbench_path": str(Path(row.testbench_path)),
        "support_files": list(row.support_files),
        "simulation_result_contract": row.simulation_result_contract,
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


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_output_and_partial_symlinks_are_rejected(tmp_path: Path):
    target = tmp_path / "target"
    target.write_text("target\n", encoding="utf-8")
    output = tmp_path / "evidence.jsonl"
    output.symlink_to(target)
    with pytest.raises(verification.CandidateVerificationPreflightError, match="symlink"):
        verification._validate_output_paths(output, force=True)

    output.unlink()
    partial = Path(str(output) + ".rtlbench-partial")
    partial.symlink_to(target)
    with pytest.raises(verification.CandidateVerificationPreflightError, match="symlink"):
        verification._validate_output_paths(output, force=True)


@pytest.mark.parametrize("input_path", ["manifest.jsonl", "passing/candidate.sv"])
def test_output_cannot_alias_manifest_or_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, input_path: str
):
    monkeypatch.setattr(
        verification,
        "discover_toolchain",
        lambda **kwargs: {name: verification.ToolInfo(None, None) for name in TOOL_NAMES},
    )
    with pytest.raises(verification.CandidateVerificationPreflightError, match="aliases protected input"):
        verification.verify_candidates(
            manifest=FIXTURE / "manifest.jsonl",
            output=FIXTURE / input_path,
            workspace_root=FIXTURE,
            work_dir=tmp_path / "work",
            force=True,
        )


def test_work_dir_cannot_alias_regular_input(tmp_path: Path):
    fixture = FIXTURE / "passing"
    with pytest.raises(verification.CandidateVerificationPreflightError, match="work-dir"):
        verification.verify_candidates(
            manifest=FIXTURE / "manifest.jsonl",
            output=tmp_path / "evidence.jsonl",
            workspace_root=FIXTURE,
            work_dir=fixture / "candidate.sv",
            force=True,
        )


def test_preexisting_predictable_snapshot_path_is_not_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manifest, root, _ = _custom_snapshot_fixture(tmp_path)
    work_dir = tmp_path / "work"
    predictable = work_dir / "inputs" / "000001_predictable"
    predictable.mkdir(parents=True)
    marker = predictable / "marker"
    marker.write_text("caller-owned", encoding="utf-8")
    tools = _fake_tools(tmp_path / "tools")
    monkeypatch.setattr(verification, "discover_toolchain", lambda **kwargs: tools)
    output = tmp_path / "evidence.jsonl"
    verification.verify_candidates(
        manifest=manifest,
        output=output,
        workspace_root=root,
        work_dir=work_dir,
        force=True,
        environment={"FAKE_VVP_OUTPUT": "Mismatches: 0 in 1 samples"},
    )
    assert marker.read_text(encoding="utf-8") == "caller-owned"


def test_output_cannot_alias_prepared_snapshot(tmp_path: Path):
    manifest, root, _ = _custom_snapshot_fixture(tmp_path)
    row = load_manifest(manifest, root)[0]
    managed_run = tmp_path / "run"
    managed_run.mkdir()
    prepared = verification.prepare_candidate_rows([row], managed_run)[0]
    with pytest.raises(verification.CandidateVerificationPreflightError, match="snapshot"):
        verification._reject_output_snapshot_collisions(
            prepared.candidate_snapshot,
            Path(str(prepared.candidate_snapshot) + ".rtlbench-partial"),
            [prepared],
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
