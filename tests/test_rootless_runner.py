import copy
import hashlib
import json
import os
import shutil
import subprocess
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from runner import rootless_runner as runner
from rtlbench import build_identity
import rtlbench.candidate_verification as verification


IMAGE = "registry.example/rtlbench-runner@sha256:" + "a" * 64
LABELS = {
    "org.opencontainers.image.revision": "0" * 40,
    "io.rtlbench.runner.config_version": "rtlbench_rootless_runner_v0.1",
    "io.rtlbench.python_version": "3.11.9",
    "io.rtlbench.iverilog_version": "11.0-1.1+b1",
    "io.rtlbench.vvp_version": "11.0-1.1+b1",
    "io.rtlbench.verilator_version": "5.006-3",
    "io.rtlbench.yosys_version": "0.23-6",
}


def _evidence_for_category(failure_category: str = "passed") -> dict:
    requested = {"compile": True, "simulation": True, "lint": False, "synthesis": False}
    compile_status = {"attempted": True, "passed": True, "reason": None}
    simulation_status = {"attempted": True, "passed": True, "reason": None}
    not_requested = {"attempted": False, "passed": None, "reason": "not_requested"}
    reported_counts = [0]
    reported_sample_counts = [1]
    maximum_count = 0
    timeout_reported = False
    accepted = True
    if failure_category == "compile_failure":
        compile_status = {"attempted": True, "passed": False, "reason": "compile_failure"}
        simulation_status = {"attempted": False, "passed": None, "reason": "compile_failure"}
        reported_counts = []
        reported_sample_counts = []
        maximum_count = None
        accepted = False
    elif failure_category == "functional_mismatch":
        simulation_status = {"attempted": True, "passed": False, "reason": "functional_mismatch"}
        reported_counts = [1]
        maximum_count = 1
        accepted = False
    elif failure_category == "timeout":
        simulation_status = {"attempted": True, "passed": False, "reason": "timeout"}
        reported_counts = []
        reported_sample_counts = []
        maximum_count = None
        timeout_reported = True
        accepted = False
    elif failure_category == "simulation_result_missing":
        simulation_status = {
            "attempted": True,
            "passed": False,
            "reason": "simulation_result_missing",
        }
        reported_counts = []
        reported_sample_counts = []
        maximum_count = None
        accepted = False
    elif failure_category == "simulation_failure":
        simulation_status = {"attempted": True, "passed": False, "reason": "simulation_failure"}
        reported_counts = []
        reported_sample_counts = []
        maximum_count = None
        accepted = False
    elif failure_category == "internal_error":
        compile_status = {"attempted": True, "passed": False, "reason": "internal_error"}
        simulation_status = {"attempted": False, "passed": None, "reason": "internal_error"}
        reported_counts = []
        reported_sample_counts = []
        maximum_count = None
        accepted = False
    elif failure_category != "passed":
        raise AssertionError(f"unsupported synthetic category: {failure_category}")
    return {
        "schema_version": "rtl_candidate_evidence_v0.1",
        "candidate_id": "synthetic_candidate_attempt_01",
        "task_id": "synthetic_task",
        "source_id": "synthetic_source",
        "attempt": 1,
        "top_module": "TopModule",
        "testbench_top": "tb",
        "simulation_result_contract": "mismatch_count_v1",
        "requested_checks": requested,
        "input_hashes": {
            "candidate_rtl_sha256": "1" * 64,
            "testbench_sha256": "2" * 64,
            "support_files": [],
        },
        "toolchain": {
            name: {"available": True, "version": "fake"}
            for name in ("iverilog", "vvp", "verilator", "yosys")
        },
        "checks": {
            "compile": {"candidate": compile_status},
            "simulation": {"candidate_passes": simulation_status},
            "lint": {"candidate": not_requested},
            "synthesis": {"candidate": not_requested},
        },
        "mismatch_summary": {
            "contract": "mismatch_count_v1",
            "reported_counts": reported_counts,
            "reported_sample_counts": reported_sample_counts,
            "maximum_count": maximum_count,
            "timeout_reported": timeout_reported,
        },
        "failure_category": failure_category,
        "accepted": accepted,
        "diagnostics": [],
    }


def _make_handoff(root: Path) -> Path:
    root.mkdir()
    (root / "candidate_manifest.jsonl").write_text("manifest sentinel\n", encoding="utf-8")
    workspace = root / "workspace" / "candidate"
    workspace.mkdir(parents=True)
    (workspace / "candidate.sv").write_text("module TopModule; endmodule\n", encoding="utf-8")
    (workspace / "testbench.sv").write_text("module tb; endmodule\n", encoding="utf-8")
    (root / "verification_plan.jsonl").write_text("plan sentinel\n", encoding="utf-8")
    return root


def _tree_hashes(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(path for path in root.rglob("*") if path.is_file()):
        result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _fake_podman(tmp_path: Path, *, mode: str) -> tuple[Path, Path]:
    log = tmp_path / f"podman-{mode}.json"
    script = tmp_path / f"podman-{mode}" / "podman"
    script.parent.mkdir()
    synthetic_categories = {
        "passing": "passed",
        "compile_failure": "compile_failure",
        "functional_mismatch": "functional_mismatch",
        "timeout": "timeout",
        "missing_result": "simulation_result_missing",
        "internal_error": "internal_error",
        "simulation_failure": "simulation_failure",
    }
    synthetic_row = repr(_evidence_for_category(synthetic_categories.get(mode, "passed")))
    body = textwrap.dedent(
        f"""
        #!{sys.executable}
        import json
        import os
        import sys
        from pathlib import Path

        args = sys.argv[1:]
        log = Path({str(log)!r})
        record = json.loads(log.read_text()) if log.exists() else []
        record.append({{"argv": args, "secret_present": "FAKE_HOST_SECRET" in os.environ}})
        log.write_text(json.dumps(record), encoding="utf-8")
        if args[:1] == ["info"]:
            print("true")
            raise SystemExit(0)
        if args[:2] == ["image", "inspect"]:
            print(json.dumps({LABELS!r}))
            raise SystemExit(0)
        if args[:1] != ["run"]:
            raise SystemExit(2)
        mounts = [args[index + 1] for index, value in enumerate(args[:-1]) if value == "--mount"]
        output_mount = next(value for value in mounts if "dst=/output" in value)
        output_root = Path(next(part[4:] for part in output_mount.split(",") if part.startswith("src=")))
        if {mode!r} == "partial":
            (output_root / "candidate_evidence.jsonl.rtlbench-partial").write_text("partial row\\n", encoding="utf-8")
            raise SystemExit(124)
        if {mode!r} == "invalid":
            (output_root / "candidate_evidence.jsonl").write_text('{{"invalid": true}}\\n', encoding="utf-8")
            raise SystemExit(0)
        if {mode!r} == "extra":
            (output_root / "unexpected.txt").write_text("not evidence\\n", encoding="utf-8")
        categories = {{
            "passing": ("passed", True),
            "compile_failure": ("compile_failure", False),
            "functional_mismatch": ("functional_mismatch", False),
            "timeout": ("timeout", False),
            "missing_result": ("simulation_result_missing", False),
            "internal_error": ("internal_error", False),
            "simulation_failure": ("simulation_failure", False),
        }}
        category, accepted = categories[{mode!r}]
        row = {synthetic_row}
        (output_root / "candidate_evidence.jsonl").write_text(json.dumps(row, sort_keys=True) + "\\n", encoding="utf-8")
        raise SystemExit(0)
        """
    )
    script.write_text(body.lstrip(), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, log


@pytest.mark.parametrize(
    ("mode", "expected_category", "accepted"),
    [
        ("passing", "passed", True),
        ("compile_failure", "compile_failure", False),
        ("functional_mismatch", "functional_mismatch", False),
        ("timeout", "timeout", False),
        ("missing_result", "simulation_result_missing", False),
        ("internal_error", "internal_error", False),
        ("simulation_failure", "simulation_failure", False),
    ],
)
def test_synthetic_runner_outcomes_preserve_evidence_and_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_category: str,
    accepted: bool,
):
    handoff = _make_handoff(tmp_path / "handoff")
    before = _tree_hashes(handoff)
    fake_podman, log = _fake_podman(tmp_path, mode=mode)
    output = tmp_path / "evidence" / "candidate_evidence.jsonl"
    output.parent.mkdir()
    monkeypatch.setenv("FAKE_HOST_SECRET", "must-not-cross-boundary")
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(runner.tempfile, "tempdir", str(temp_root))

    result = runner.run_isolated(
        image=IMAGE,
        input_root=handoff,
        output=output,
        runtime=str(fake_podman),
    )

    assert result.returncode == 0
    rows = runner.validate_candidate_evidence_file(output, max_bytes=1_000_000)
    assert rows[0]["failure_category"] == expected_category
    assert rows[0]["accepted"] is accepted
    sidecar = json.loads(Path(str(output) + ".runner.json").read_text())
    assert sidecar == {
        "evidence_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "image": IMAGE,
        "image_digest": "sha256:" + "a" * 64,
        "iverilog_version": LABELS["io.rtlbench.iverilog_version"],
        "manifest_sha256": hashlib.sha256(
            (handoff / "candidate_manifest.jsonl").read_bytes()
        ).hexdigest(),
        "network_policy": "none",
        "partial_evidence_sha256": None,
        "python_version": LABELS["io.rtlbench.python_version"],
        "resource_limits": {
            "cpus": 2.0,
            "evidence_bytes": 33554432,
            "file_size_bytes": 33554432,
            "memory_bytes": 536870912,
            "open_files": 256,
            "pids": 128,
            "tmp_bytes": 134217728,
            "wall_seconds": 120.0,
            "work_bytes": 268435456,
        },
        "rootless": True,
        "rtlbench_commit": LABELS["org.opencontainers.image.revision"],
        "runner_config_version": "rtlbench_rootless_runner_v0.1",
        "verilator_version": LABELS["io.rtlbench.verilator_version"],
        "vvp_version": LABELS["io.rtlbench.vvp_version"],
        "workspace_tree_sha256": runner.sha256_workspace_tree(handoff / "workspace"),
        "yosys_version": LABELS["io.rtlbench.yosys_version"],
    }
    assert _tree_hashes(handoff) == before
    assert not list((tmp_path / "tmp").glob(".rtlbench-runner-*"))
    record = json.loads(log.read_text())
    assert record[-1]["secret_present"] is False


def test_wrapper_forwards_every_argument_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    handoff = _make_handoff(tmp_path / "handoff")
    fake_podman, log = _fake_podman(tmp_path, mode="passing")
    output = tmp_path / "evidence.jsonl"
    runner.run_isolated(image=IMAGE, input_root=handoff, output=output, runtime=str(fake_podman))
    run_argv = json.loads(log.read_text())[-1]["argv"]
    image_index = run_argv.index(IMAGE)
    assert run_argv[image_index + 1 :] == list(runner.EXPECTED_INNER_COMMAND[1:])


@pytest.mark.parametrize(
    "mutation",
    [
        lambda row: row.update(accepted=False),
        lambda row: row.update(failure_category="compile_failure"),
        lambda row: row["checks"]["lint"].update(
            candidate={"attempted": True, "passed": True, "reason": None}
        ),
        lambda row: row["checks"]["simulation"]["candidate_passes"].update(
            passed=False, reason="simulation_failure"
        ),
    ],
)
def test_contradictory_evidence_is_rejected(tmp_path: Path, mutation):
    row = copy.deepcopy(_evidence_for_category("passed"))
    mutation(row)
    path = tmp_path / "contradictory.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(runner.RunnerError):
        runner.validate_candidate_evidence_file(path, max_bytes=1_000_000)


@pytest.mark.parametrize(
    "category",
    [
        "passed",
        "compile_failure",
        "functional_mismatch",
        "timeout",
        "simulation_result_missing",
        "simulation_failure",
        "internal_error",
    ],
)
def test_canonical_valid_evidence_categories_are_accepted(
    tmp_path: Path, category: str
):
    path = tmp_path / f"{category}.jsonl"
    path.write_text(
        json.dumps(_evidence_for_category(category)) + "\n", encoding="utf-8"
    )
    assert runner.validate_candidate_evidence_file(path, max_bytes=1_000_000)


def test_dockerfile_wrapper_and_build_identity_checks_are_explicit():
    dockerfile = (Path(__file__).parents[1] / "runner" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "'exec python -m rtlbench.cli \"$@\"'" in dockerfile
    assert "test \"$(sed -n '2p' /usr/local/bin/rtlbench)\"" in dockerfile
    assert "PYTHONPATH=/opt/rtlbench/src /usr/local/bin/rtlbench --help >/dev/null" in dockerfile


def test_built_cli_help_succeeds():
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-S", "-m", "rtlbench.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert result.returncode == 0


def test_build_rejects_false_rtlbench_commit_label():
    command = [
        "bash",
        "runner/build_rootless_image.sh",
        "--base-image",
        "python:3.11-slim@sha256:" + "a" * 64,
        "--tag",
        "localhost/rtlbench-runner:test",
        "--rtlbench-commit",
        "1" * 40,
        "--python-version",
        "3.11.9",
        "--iverilog-package-version",
        "11.0-1.1+b1",
        "--iverilog-version",
        "11.0",
        "--vvp-version",
        "11.0",
        "--verilator-package-version",
        "5.006-3",
        "--verilator-version",
        "Verilator 5.006",
        "--yosys-package-version",
        "0.23-6",
        "--yosys-version",
        "Yosys 0.23",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "does not match the checked-out source HEAD" in result.stderr


def test_build_rejects_false_tool_version_labels():
    expected = build_identity.ExpectedIdentity(
        python_version="3.11.9",
        iverilog_package_version="11.0-1.1+b1",
        iverilog_version="11.0",
        vvp_version="11.0",
        verilator_package_version="5.006-3",
        verilator_version="Verilator 5.006",
        yosys_package_version="0.23-6",
        yosys_version="Yosys 0.23",
    )
    packages = {
        "iverilog": "11.0-1.1+b1",
        "verilator": "5.006-3",
        "yosys": "0.23-6",
    }
    tools = {
        "iverilog": "Icarus Verilog version 11.0",
        "vvp": "vvp 11.0",
        "verilator": "Verilator 5.006",
        "yosys": "Yosys 0.23",
    }
    with pytest.raises(build_identity.BuildIdentityError, match="yosys runtime"):
        build_identity.validate_identity(
            expected,
            python_version="3.11.9",
            package_versions=packages,
            tool_outputs={**tools, "yosys": "Yosys 0.22"},
        )
    with pytest.raises(build_identity.BuildIdentityError, match="verilator package"):
        build_identity.validate_identity(
            expected,
            python_version="3.11.9",
            package_versions={**packages, "verilator": "5.005-1"},
            tool_outputs=tools,
        )


def test_readme_requires_output_outside_input_handoff():
    readme = (Path(__file__).parents[1] / "runner" / "README.md").read_text(
        encoding="utf-8"
    )
    assert "--output /path/to/isolated-output/candidate_evidence.jsonl" in readme
    assert "--output /path/to/attempt_01/candidate_evidence.jsonl" not in readme


def test_runner_applies_security_boundary_and_no_reference_rtl(tmp_path: Path):
    config = runner.load_config()
    command = runner.build_run_command(
        "/usr/bin/podman",
        IMAGE,
        tmp_path / "input",
        tmp_path / "output",
        config,
    )
    assert "--network" in command and command[command.index("--network") + 1] == "none"
    assert "--cap-drop" in command and command[command.index("--cap-drop") + 1] == "ALL"
    assert "--security-opt" in command and "no-new-privileges" in command
    assert "--read-only" in command
    assert "--user" in command and command[command.index("--user") + 1] == "65532:65532"
    assert "--pids-limit" in command
    assert sum(value.startswith("type=bind") for value in command) == 2
    assert not any(value.startswith("-v") for value in command)

    handoff = _make_handoff(tmp_path / "handoff")
    (handoff / "workspace" / "candidate" / "reference.sv").write_text(
        "module forbidden; endmodule\n", encoding="utf-8"
    )
    with pytest.raises(runner.RunnerError, match="reference.sv"):
        runner._validate_no_reference_rtl(handoff)


def test_evidence_validator_accepts_rtlbench_verifier_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fixture = Path(__file__).parent / "fixtures" / "candidate_verification"
    monkeypatch.setattr(
        verification,
        "discover_toolchain",
        lambda **_: {
            name: verification.ToolInfo(None, None)
            for name in ("iverilog", "vvp", "verilator", "yosys")
        },
    )
    output = tmp_path / "evidence.jsonl"
    verification.verify_candidates(
        manifest=fixture / "manifest.jsonl",
        output=output,
        workspace_root=fixture,
        work_dir=tmp_path / "work",
        force=True,
    )
    rows = runner.validate_candidate_evidence_file(output, max_bytes=1_000_000)
    assert len(rows) == 3


def test_partial_evidence_is_preserved(tmp_path: Path):
    handoff = _make_handoff(tmp_path / "handoff")
    fake_podman, _ = _fake_podman(tmp_path, mode="partial")
    output = tmp_path / "evidence.jsonl"
    result = runner.run_isolated(
        image=IMAGE,
        input_root=handoff,
        output=output,
        runtime=str(fake_podman),
    )
    assert result.returncode == 124
    assert result.evidence_output is None
    assert result.partial_output == Path(str(output) + ".rtlbench-partial")
    assert result.partial_output.read_text(encoding="utf-8") == "partial row\n"
    assert not output.exists()


def test_invalid_or_extra_runtime_output_is_not_published(tmp_path: Path):
    for mode, message in (("invalid", "top-level schema"), ("extra", "non-evidence")):
        handoff = _make_handoff(tmp_path / f"handoff-{mode}")
        fake_podman, _ = _fake_podman(tmp_path, mode=mode)
        output = tmp_path / f"{mode}.jsonl"
        with pytest.raises(runner.RunnerError, match=message):
            runner.run_isolated(
                image=IMAGE,
                input_root=handoff,
                output=output,
                runtime=str(fake_podman),
            )
        assert not output.exists()


@pytest.mark.integration
def test_rootless_image_acceptance_matrix(tmp_path: Path):
    """Run the real synthetic matrix when a digest-qualified image is supplied."""

    image = os.environ.get("RTLBench_RUNNER_IMAGE")
    if not image:
        pytest.skip("set RTLBench_RUNNER_IMAGE to run the immutable-image acceptance matrix")
    if shutil.which("podman") is None:
        pytest.skip("rootless Podman is unavailable")
    rootless_result = subprocess.run(
        ["podman", "info", "--format", "{{.Host.Security.Rootless}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    rootless = rootless_result.stdout.strip() if rootless_result.returncode == 0 else ""
    if rootless != "true":
        pytest.skip("Podman is not rootless")

    fixture_root = Path(__file__).parent / "fixtures" / "candidate_verification"
    handoff = tmp_path / "handoff"
    workspace = handoff / "workspace"
    workspace.mkdir(parents=True)
    rows = []
    directories = (
        ("passing", "passed"),
        ("compile_failure", "compile_failure"),
        ("functional_mismatch", "functional_mismatch"),
        ("timeout_marker", "timeout"),
        ("missing_result", "simulation_result_missing"),
    )
    for directory, _ in directories:
        shutil.copytree(fixture_root / directory, workspace / directory)
        rows.append(
            {
                "schema_version": "rtl_candidate_manifest_v0.1",
                "candidate_id": f"synthetic_{directory}_attempt_01",
                "task_id": f"synthetic_{directory}",
                "source_id": directory,
                "attempt": 1,
                "top_module": "TopModule",
                "testbench_top": "tb",
                "candidate_rtl_path": f"{directory}/candidate.sv",
                "testbench_path": f"{directory}/testbench.sv",
                "support_files": [],
                "simulation_result_contract": "mismatch_count_v1",
                "requested_checks": {
                    "compile": True,
                    "simulation": True,
                    "lint": False,
                    "synthesis": False,
                },
            }
        )
    (handoff / "candidate_manifest.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )
    before = _tree_hashes(handoff)
    output = tmp_path / "candidate_evidence.jsonl"
    result = runner.run_isolated(image=image, input_root=handoff, output=output)
    assert result.returncode == 0
    evidence = runner.validate_candidate_evidence_file(output, max_bytes=1_000_000)
    assert {row["failure_category"] for row in evidence} == {
        expected for _, expected in directories
    }
    assert _tree_hashes(handoff) == before
    assert not any(path.name == "reference.sv" for path in handoff.rglob("*"))
