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
LOCAL_IMAGE_ID = "sha256:" + "b" * 64
INSPECTED_IMAGE_ID = LOCAL_IMAGE_ID
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
    return _fake_runtime(tmp_path, mode=mode, backend="podman")


def _fake_docker(tmp_path: Path, *, mode: str = "passing") -> tuple[Path, Path]:
    return _fake_runtime(tmp_path, mode=mode, backend="docker")


def _fake_runtime(
    tmp_path: Path, *, mode: str, backend: str
) -> tuple[Path, Path]:
    log = tmp_path / f"{backend}-{mode}.json"
    script = tmp_path / f"{backend}-{mode}" / backend
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
    inspected_image_id = (
        "sha256:" + "c" * 64 if mode == "id_mismatch" else INSPECTED_IMAGE_ID
    )
    synthetic_row = repr(_evidence_for_category(synthetic_categories.get(mode, "passed")))
    body = textwrap.dedent(
        f"""
        #!{sys.executable}
        import json
        import os
        import sys
        import time
        from pathlib import Path

        args = sys.argv[1:]
        log = Path({str(log)!r})
        record = json.loads(log.read_text()) if log.exists() else []
        container_env = {{
            item.split("=", 1)[0]: item.split("=", 1)[1]
            for index, value in enumerate(args[:-1])
            if value == "--env" and "=" in args[index + 1]
            for item in [args[index + 1]]
        }}
        record.append({{
            "argv": args,
            "secret_present": "FAKE_HOST_SECRET" in os.environ,
            "test_secret_present": "RTLBench_TEST_SECRET" in os.environ,
            "observed_pythonpath": os.environ.get("PYTHONPATH"),
            "container_env": container_env,
        }})
        log.write_text(json.dumps(record), encoding="utf-8")
        if {backend!r} == "podman" and args[:1] == ["info"]:
            print("true")
            raise SystemExit(0)
        if {backend!r} == "docker" and args[:2] == ["version", "--format"]:
            print(json.dumps("26.1.0"))
            raise SystemExit(0)
        if args[:2] == ["image", "inspect"]:
            if {mode!r} == "missing_image":
                raise SystemExit(1)
            if "{{{{.Id}}}}" in args:
                print({inspected_image_id!r})
                raise SystemExit(0)
            print(json.dumps({LABELS!r}))
            raise SystemExit(0)
        if args[:1] != ["run"]:
            raise SystemExit(2)
        mounts = [args[index + 1] for index, value in enumerate(args[:-1]) if value == "--mount"]
        output_mount = next(value for value in mounts if "dst=/output" in value)
        output_root = Path(next(part[4:] for part in output_mount.split(",") if part.startswith("src=")))
        input_mount = next(value for value in mounts if "dst=/input" in value)
        input_root = Path(next(part[4:] for part in input_mount.split(",") if part.startswith("src=")))
        if {mode!r} == "mutate":
            (input_root / "candidate_manifest.jsonl").write_text(
                "mutated by fake runtime\\n", encoding="utf-8"
            )
        if {mode!r} == "diagnostic":
            print("synthetic stdout marker", flush=True)
            print("synthetic container traceback", file=sys.stderr, flush=True)
            raise SystemExit(1)
        if {mode!r} == "large_diagnostic":
            os.write(1, b"diagnostic prefix\\n")
            os.write(2, b"x" * 70000)
            os.write(1, b"final failure marker\\n")
            raise SystemExit(1)
        if {mode!r} == "timeout_diagnostic":
            print("synthetic timeout marker", flush=True)
            time.sleep(30)
            raise SystemExit(1)
        if {mode!r} == "sanitize_diagnostic":
            print("input path: " + str(input_root), flush=True)
            print("output path: " + str(output_root), flush=True)
            print("environment path: " + os.environ.get("HOME", ""), flush=True)
            print(
                "temporary path: "
                + str(Path(os.environ.get("TMPDIR", "")).parent),
                flush=True,
            )
            print("secret: " + os.environ.get("FAKE_HOST_SECRET", "<missing>"), flush=True)
            print("escape: " + chr(27) + "[31mred" + chr(27) + "[0m", flush=True)
            os.write(1, b"nul\\x00marker\\n")
            raise SystemExit(1)
        if {mode!r} == "passing_diagnostic":
            print("success diagnostic should not be surfaced", file=sys.stderr, flush=True)
        if {mode!r} == "partial":
            print("partial runtime diagnostic", file=sys.stderr, flush=True)
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
            "mutate": ("passed", True),
            "passing_diagnostic": ("passed", True),
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


def _direct_fake_execution(
    tmp_path: Path, *, mode: str, timeout: float = 5.0
) -> runner.ExecutionResult:
    tmp_path.mkdir(parents=True, exist_ok=True)
    handoff = _make_handoff(tmp_path / "handoff")
    staging = tmp_path / "staging"
    staging.mkdir()
    output = tmp_path / "published" / "candidate_evidence.jsonl"
    output.parent.mkdir()
    runtime_environment = tmp_path / "runtime-environment"
    fake_runtime, _ = _fake_docker(tmp_path, mode=mode)
    command = [
        str(fake_runtime),
        "run",
        "--mount",
        f"type=bind,src={handoff},dst=/input,readonly",
        "--mount",
        f"type=bind,src={staging},dst=/output",
    ]
    return runner._execute(
        command,
        timeout=timeout,
        environment=runner._runtime_environment(
            runtime_environment, include_runtime_dir=False
        ),
        diagnostic_replacements=runner._runtime_diagnostic_replacements(
            input_root=handoff,
            output=output,
            staging=staging,
            runtime_environment=runtime_environment,
        ),
    )


def _builder_arguments(commit: str) -> list[str]:
    return [
        "--base-image",
        "python:3.11-slim@sha256:" + "a" * 64,
        "--tag",
        "rtlbench-runner:pilot",
        "--rtlbench-commit",
        commit,
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
        "5.006",
        "--yosys-package-version",
        "0.23-6",
        "--yosys-version",
        "0.23",
    ]


def _write_fake_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fake_builder_tools(
    tmp_path: Path, commit: str, *, podman_rootless: bool = False
) -> tuple[Path, Path, Path]:
    bin_root = tmp_path / "bin"
    bin_root.mkdir()
    git_source = textwrap.dedent(
        f"""
        #!{sys.executable}
        import sys
        if sys.argv[1:] == ["rev-parse", "HEAD"]:
            print({commit!r})
        elif sys.argv[1:] == ["status", "--porcelain"]:
            pass
        else:
            raise SystemExit(2)
        """
    )
    _write_fake_executable(bin_root / "git", git_source.lstrip())
    docker_log = tmp_path / "docker-builder.json"
    docker_source = textwrap.dedent(
        f"""
        #!{sys.executable}
        import json
        import sys
        from pathlib import Path
        args = sys.argv[1:]
        log = Path({str(docker_log)!r})
        rows = json.loads(log.read_text()) if log.exists() else []
        rows.append(args)
        log.write_text(json.dumps(rows), encoding="utf-8")
        if args[:2] == ["version", "--format"]:
            print(json.dumps("26.1.0"))
        elif args[:2] == ["image", "inspect"]:
            if "--format" in args:
                print("sha256:" + "d" * 64)
        elif args[:1] == ["build"]:
            pass
        else:
            raise SystemExit(2)
        """
    )
    _write_fake_executable(bin_root / "docker", docker_source.lstrip())
    podman_log = tmp_path / "podman-builder.json"
    podman_source = textwrap.dedent(
        f"""
        #!{sys.executable}
        import json
        import sys
        from pathlib import Path
        args = sys.argv[1:]
        log = Path({str(podman_log)!r})
        rows = json.loads(log.read_text()) if log.exists() else []
        rows.append(args)
        log.write_text(json.dumps(rows), encoding="utf-8")
        if args[:1] == ["info"]:
            print({"true" if podman_rootless else "false"!r})
        elif args[:2] == ["image", "inspect"]:
            if "--format" in args:
                print("sha256:" + "d" * 64)
        elif args[:1] == ["build"]:
            pass
        else:
            raise SystemExit(2)
        """
    )
    _write_fake_executable(bin_root / "podman", podman_source.lstrip())
    return bin_root, docker_log, podman_log


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
@pytest.mark.parametrize(
    ("profile", "backend", "acknowledge"),
    [
        (runner.PROFILE_PRODUCTION_ROOTLESS, "podman", False),
        (runner.PROFILE_PILOT_DOCKER, "docker", True),
    ],
)
def test_synthetic_runner_outcomes_preserve_evidence_and_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_category: str,
    accepted: bool,
    profile: str,
    backend: str,
    acknowledge: bool,
):
    handoff = _make_handoff(tmp_path / "handoff")
    before = _tree_hashes(handoff)
    fake_runtime, log = (
        _fake_podman(tmp_path, mode=mode)
        if backend == "podman"
        else _fake_docker(tmp_path, mode=mode)
    )
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
        profile=profile,
        acknowledge_rootful_runtime=acknowledge,
        runtime=str(fake_runtime),
    )

    assert result.returncode == 0
    rows = runner.validate_candidate_evidence_file(output, max_bytes=1_000_000)
    assert rows[0]["failure_category"] == expected_category
    assert rows[0]["accepted"] is accepted
    sidecar = json.loads(Path(str(output) + ".runner.json").read_text())
    config = runner.load_config(profile=profile)
    assert sidecar == {
        "schema_version": runner.IDENTITY_SCHEMA_VERSION,
        "profile": profile,
        "runtime": config.runtime_name,
        "runtime_mode": config.runtime_mode,
        "evidence_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "image": IMAGE,
        "image_id": INSPECTED_IMAGE_ID,
        "image_identity_kind": "repository-digest",
        "image_digest": "sha256:" + "a" * 64,
        "iverilog_version": LABELS["io.rtlbench.iverilog_version"],
        "manifest_sha256": hashlib.sha256(
            (handoff / "candidate_manifest.jsonl").read_bytes()
        ).hexdigest(),
        "network_policy": "none",
        "partial_evidence_sha256": None,
        "python_version": LABELS["io.rtlbench.python_version"],
        "resource_limits": {
            "cpus": config.cpus,
            "evidence_bytes": 33554432,
            "file_size_bytes": 33554432,
            "memory_bytes": 536870912,
            "memory_swap_bytes": config.memory_swap_bytes,
            "open_files": 256,
            "pids": config.pids,
            "tmp_bytes": config.tmp_bytes,
            "wall_seconds": 120.0,
            "work_bytes": config.work_bytes,
        },
        "rootless": config.rootless,
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
    run_argv = next(item["argv"] for item in reversed(record) if item["argv"][:1] == ["run"])
    if backend == "docker":
        assert ["--pull", "never"] == run_argv[1:3]
    else:
        assert run_argv[1] == "--pull=never"


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


def test_primary_cli_requires_an_explicit_profile():
    parser = runner._build_parser(require_profile=True)
    required = [
        "--image",
        IMAGE,
        "--input",
        "/input",
        "--output",
        "/output/evidence.jsonl",
    ]
    with pytest.raises(SystemExit):
        parser.parse_args(required)
    with pytest.raises(SystemExit):
        parser.parse_args(["--profile", "automatic", *required])


def test_primary_cli_help_succeeds():
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parents[1] / "runner" / "run_isolated.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--profile" in result.stdout


def test_profile_selection_requires_acknowledgement_and_fixed_backend(
    tmp_path: Path,
):
    handoff = _make_handoff(tmp_path / "handoff")
    fake_docker, docker_log = _fake_docker(tmp_path)
    with pytest.raises(runner.RunnerError, match="acknowledge-rootful-runtime"):
        runner.run_isolated(
            image=IMAGE,
            input_root=handoff,
            output=tmp_path / "docker-evidence.jsonl",
            profile=runner.PROFILE_PILOT_DOCKER,
            runtime=str(fake_docker),
        )
    assert not docker_log.exists()

    fake_podman, podman_log = _fake_podman(tmp_path, mode="passing")
    with pytest.raises(runner.RunnerError, match="only valid with pilot-docker"):
        runner.run_isolated(
            image=IMAGE,
            input_root=handoff,
            output=tmp_path / "rootless-evidence.jsonl",
            profile=runner.PROFILE_PRODUCTION_ROOTLESS,
            acknowledge_rootful_runtime=True,
            runtime=str(fake_podman),
        )
    assert not podman_log.exists()

    with pytest.raises(runner.RunnerError, match="requires the docker runtime"):
        runner.run_isolated(
            image=IMAGE,
            input_root=handoff,
            output=tmp_path / "wrong-docker-evidence.jsonl",
            profile=runner.PROFILE_PILOT_DOCKER,
            acknowledge_rootful_runtime=True,
            runtime=str(fake_podman),
        )
    with pytest.raises(runner.RunnerError, match="requires the podman runtime"):
        runner.run_isolated(
            image=IMAGE,
            input_root=handoff,
            output=tmp_path / "wrong-podman-evidence.jsonl",
            profile=runner.PROFILE_PRODUCTION_ROOTLESS,
            runtime=str(fake_docker),
        )


def _bind_mounts(command: list[str]) -> dict[str, str]:
    mounts = [
        command[index + 1]
        for index, item in enumerate(command[:-1])
        if item == "--mount"
    ]
    return {
        next(part[4:] for part in mount.split(",") if part.startswith("dst=")): mount
        for mount in mounts
    }


def _container_env(command: list[str]) -> dict[str, str]:
    values = [
        command[index + 1]
        for index, item in enumerate(command[:-1])
        if item == "--env"
    ]
    assert len(values) == len(set(value.split("=", 1)[0] for value in values))
    return dict(value.split("=", 1) for value in values)


EXPECTED_CONTAINER_ENVIRONMENT = {
    "HOME": "/tmp",
    "TMPDIR": "/tmp",
    "LANG": "C",
    "LC_ALL": "C",
    "PYTHONPATH": "/opt/rtlbench/src",
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
}


def test_docker_pilot_command_is_fixed_and_bounded(tmp_path: Path):
    config = runner.load_config(profile=runner.PROFILE_PILOT_DOCKER)
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    command = runner.build_run_command(
        "/usr/bin/docker",
        IMAGE,
        input_root,
        output_root,
        config,
    )

    def value(option: str) -> str:
        return command[command.index(option) + 1]

    assert value("--network") == "none"
    assert value("--pull") == "never"
    assert value("--user") == "65532:65532"
    assert "--read-only" in command
    assert value("--cap-drop") == "ALL"
    assert value("--security-opt") == "no-new-privileges"
    assert value("--cpus") == "1"
    assert value("--memory") == "536870912"
    assert value("--memory-swap") == "536870912"
    assert value("--pids-limit") == "64"
    assert _container_env(command) == EXPECTED_CONTAINER_ENVIRONMENT
    assert command.count("--env") == len(EXPECTED_CONTAINER_ENVIRONMENT)
    assert "fsize=33554432:33554432" in command
    assert "nofile=256:256" in command
    assert "nproc=64:64" in command
    assert "/tmp:rw,nosuid,nodev,noexec,size=67108864,mode=1777" in command
    assert "/work:rw,nosuid,nodev,size=134217728,uid=65532,gid=65532,mode=700" in command
    mounts = _bind_mounts(command)
    assert mounts["/input"] == f"type=bind,src={input_root},dst=/input,readonly"
    assert mounts["/output"] == f"type=bind,src={output_root},dst=/output"
    assert ",rw" not in mounts["/output"]
    assert ",readonly" not in mounts["/output"]
    assert ",ro" not in mounts["/output"]
    assert command[command.index(IMAGE) + 1 :] == list(runner.EXPECTED_INNER_COMMAND[1:])
    assert "--userns" not in command
    forbidden = {
        "--privileged",
        "--network=host",
        "/var/run/docker.sock",
        "--env-file",
        "--pid",
        "host",
        "--ipc",
        "--device",
        "--volumes-from",
    }
    assert not forbidden.intersection(command)


@pytest.mark.parametrize(
    "profile",
    [runner.PROFILE_PILOT_DOCKER, runner.PROFILE_PRODUCTION_ROOTLESS],
)
def test_both_profiles_use_the_same_fixed_container_environment(
    tmp_path: Path, profile: str
):
    command = runner.build_run_command(
        "docker" if profile == runner.PROFILE_PILOT_DOCKER else "podman",
        IMAGE,
        tmp_path / "input",
        tmp_path / "output",
        runner.load_config(profile=profile),
    )
    assert _container_env(command) == EXPECTED_CONTAINER_ENVIRONMENT
    assert command.count("PYTHONPATH=/opt/rtlbench/src") == 1


def test_host_pythonpath_and_environment_secrets_are_not_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    handoff = _make_handoff(tmp_path / "handoff")
    fake_docker, log = _fake_docker(tmp_path, mode="passing")
    output = tmp_path / "evidence.jsonl"
    monkeypatch.setenv("PYTHONPATH", "/host/private/path")
    monkeypatch.setenv("RTLBench_TEST_SECRET", "must-not-cross-boundary")

    runner.run_isolated(
        image=IMAGE,
        input_root=handoff,
        output=output,
        profile=runner.PROFILE_PILOT_DOCKER,
        acknowledge_rootful_runtime=True,
        runtime=str(fake_docker),
    )

    record = json.loads(log.read_text(encoding="utf-8"))[-1]
    assert record["container_env"] == EXPECTED_CONTAINER_ENVIRONMENT
    assert record["observed_pythonpath"] is None
    assert record["secret_present"] is False
    assert record["test_secret_present"] is False
    assert "/host/private/path" not in json.dumps(record)
    assert "must-not-cross-boundary" not in json.dumps(record)


def test_writable_output_mount_uses_portable_default_syntax(tmp_path: Path):
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    for runtime, profile, input_mode in (
        ("docker", runner.PROFILE_PILOT_DOCKER, "readonly"),
        ("podman", runner.PROFILE_PRODUCTION_ROOTLESS, "ro"),
    ):
        command = runner.build_run_command(
            runtime,
            IMAGE,
            input_root,
            output_root,
            runner.load_config(profile=profile),
        )
        mounts = _bind_mounts(command)
        assert mounts["/input"] == f"type=bind,src={input_root},dst=/input,{input_mode}"
        assert mounts["/output"] == f"type=bind,src={output_root},dst=/output"
        assert ",rw" not in mounts["/output"]
        assert ",readonly" not in mounts["/output"]
        assert ",ro" not in mounts["/output"]


def test_local_docker_image_id_is_published_as_distinct_identity(
    tmp_path: Path,
):
    handoff = _make_handoff(tmp_path / "handoff")
    fake_docker, _ = _fake_docker(tmp_path)
    output = tmp_path / "evidence.jsonl"
    runner.run_isolated(
        image=LOCAL_IMAGE_ID,
        input_root=handoff,
        output=output,
        profile=runner.PROFILE_PILOT_DOCKER,
        acknowledge_rootful_runtime=True,
        runtime=str(fake_docker),
    )
    sidecar = json.loads(Path(str(output) + ".runner.json").read_text())
    assert sidecar["schema_version"] == "rtlbench_runner_identity_v0.2"
    assert sidecar["image_identity_kind"] == "local-image-id"
    assert sidecar["image"] == LOCAL_IMAGE_ID
    assert sidecar["image_id"] == LOCAL_IMAGE_ID
    assert sidecar["image_digest"] is None


def test_local_docker_image_id_must_match_inspection(tmp_path: Path):
    handoff = _make_handoff(tmp_path / "handoff")
    fake_docker, log = _fake_runtime(tmp_path, mode="id_mismatch", backend="docker")
    with pytest.raises(runner.RunnerError, match="does not match"):
        runner.run_isolated(
            image=LOCAL_IMAGE_ID,
            input_root=handoff,
            output=tmp_path / "evidence.jsonl",
            profile=runner.PROFILE_PILOT_DOCKER,
            acknowledge_rootful_runtime=True,
            runtime=str(fake_docker),
        )
    assert not any(item["argv"][:1] == ["run"] for item in json.loads(log.read_text()))


@pytest.mark.parametrize(
    ("profile", "backend", "image", "acknowledge", "message"),
    [
        (
            runner.PROFILE_PRODUCTION_ROOTLESS,
            "podman",
            LOCAL_IMAGE_ID,
            False,
            "production-rootless",
        ),
        (
            runner.PROFILE_PRODUCTION_ROOTLESS,
            "podman",
            "rtlbench-runner:latest",
            False,
            "production-rootless",
        ),
        (
            runner.PROFILE_PILOT_DOCKER,
            "docker",
            "rtlbench-runner:latest",
            True,
            "pilot-docker",
        ),
    ],
)
def test_mutable_or_profile_incompatible_image_rejected(
    tmp_path: Path,
    profile: str,
    backend: str,
    image: str,
    acknowledge: bool,
    message: str,
):
    handoff = _make_handoff(tmp_path / "handoff")
    fake_runtime, log = _fake_runtime(tmp_path, mode="passing", backend=backend)
    with pytest.raises(runner.RunnerError, match=message):
        runner.run_isolated(
            image=image,
            input_root=handoff,
            output=tmp_path / "evidence.jsonl",
            profile=profile,
            acknowledge_rootful_runtime=acknowledge,
            runtime=str(fake_runtime),
        )
    assert not any(item["argv"][:1] == ["run"] for item in json.loads(log.read_text()))


def test_missing_local_image_fails_before_container_run(tmp_path: Path):
    handoff = _make_handoff(tmp_path / "handoff")
    fake_docker, log = _fake_runtime(tmp_path, mode="missing_image", backend="docker")
    with pytest.raises(runner.RunnerError, match="inspect the immutable"):
        runner.run_isolated(
            image=LOCAL_IMAGE_ID,
            input_root=handoff,
            output=tmp_path / "evidence.jsonl",
            profile=runner.PROFILE_PILOT_DOCKER,
            acknowledge_rootful_runtime=True,
            runtime=str(fake_docker),
        )
    assert not any(item["argv"][:1] == ["run"] for item in json.loads(log.read_text()))


def test_pull_policy_is_not_a_user_overridable_argument():
    parser = runner._build_parser(require_profile=True)
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--profile",
                runner.PROFILE_PILOT_DOCKER,
                "--acknowledge-rootful-runtime",
                "--image",
                LOCAL_IMAGE_ID,
                "--input",
                "/input",
                "--output",
                "/output/evidence.jsonl",
                "--pull",
                "always",
            ]
        )


@pytest.mark.parametrize(
    ("profile", "backend", "acknowledge"),
    [
        (runner.PROFILE_PRODUCTION_ROOTLESS, "podman", False),
        (runner.PROFILE_PILOT_DOCKER, "docker", True),
    ],
)
def test_input_mutation_is_rejected_for_both_profiles(
    tmp_path: Path, profile: str, backend: str, acknowledge: bool
):
    handoff = _make_handoff(tmp_path / "handoff")
    fake_runtime, _ = _fake_runtime(tmp_path, mode="mutate", backend=backend)
    with pytest.raises(runner.RunnerError, match="input manifest changed"):
        runner.run_isolated(
            image=IMAGE,
            input_root=handoff,
            output=tmp_path / "evidence.jsonl",
            profile=profile,
            acknowledge_rootful_runtime=acknowledge,
            runtime=str(fake_runtime),
        )
    assert not (tmp_path / "evidence.jsonl").exists()


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
    "simulation_reason", ["tool_unavailable", "compile_failure"]
)
def test_simulation_evidence_cannot_replace_a_failed_compile_leaf(
    tmp_path: Path, simulation_reason: str
):
    row = copy.deepcopy(_evidence_for_category("passed"))
    row["accepted"] = False
    row["failure_category"] = "compile_failure"
    row["checks"]["compile"]["candidate"] = {
        "attempted": True,
        "passed": True,
        "reason": None,
    }
    row["checks"]["simulation"]["candidate_passes"] = {
        "attempted": False,
        "passed": None,
        "reason": simulation_reason,
    }
    row["mismatch_summary"].update(
        reported_counts=[], reported_sample_counts=[], maximum_count=None
    )
    path = tmp_path / f"simulation-only-{simulation_reason}.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(runner.RunnerError):
        runner.validate_candidate_evidence_file(path, max_bytes=1_000_000)


@pytest.mark.parametrize(
    "simulation_reason",
    [
        "functional_mismatch",
        "simulation_result_missing",
        "simulation_failure",
        "timeout",
    ],
)
def test_simulation_derived_evidence_requires_successful_compile(
    tmp_path: Path, simulation_reason: str
):
    row = copy.deepcopy(_evidence_for_category("passed"))
    row["accepted"] = False
    row["failure_category"] = (
        "timeout" if simulation_reason == "timeout" else simulation_reason
    )
    row["checks"]["compile"]["candidate"] = {
        "attempted": True,
        "passed": False,
        "reason": "compile_failure",
    }
    row["checks"]["simulation"]["candidate_passes"] = {
        "attempted": True,
        "passed": False,
        "reason": simulation_reason,
    }
    row["mismatch_summary"].update(
        reported_counts=[1] if simulation_reason == "functional_mismatch" else [],
        reported_sample_counts=[1] if simulation_reason == "functional_mismatch" else [],
        maximum_count=1 if simulation_reason == "functional_mismatch" else None,
        timeout_reported=simulation_reason == "timeout",
    )
    path = tmp_path / f"compile-failed-{simulation_reason}.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(runner.RunnerError):
        runner.validate_candidate_evidence_file(path, max_bytes=1_000_000)


def test_compile_failure_with_successful_elaboration_is_rejected_if_mismatched(
    tmp_path: Path,
):
    row = copy.deepcopy(_evidence_for_category("passed"))
    row.update(accepted=False, failure_category="compile_failure")
    row["checks"]["simulation"]["candidate_passes"] = {
        "attempted": True,
        "passed": False,
        "reason": "compile_failure",
    }
    row["mismatch_summary"].update(
        reported_counts=[1], reported_sample_counts=[1], maximum_count=1
    )
    path = tmp_path / "elaboration-compile-mismatch.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(runner.RunnerError):
        runner.validate_candidate_evidence_file(path, max_bytes=1_000_000)


def test_non_compile_failure_cannot_be_rebranded_as_compile_failure(
    tmp_path: Path,
):
    row = copy.deepcopy(_evidence_for_category("passed"))
    row.update(accepted=False, failure_category="compile_failure")
    row["checks"]["compile"]["candidate"] = {
        "attempted": True,
        "passed": False,
        "reason": "timeout",
    }
    row["checks"]["simulation"]["candidate_passes"] = {
        "attempted": True,
        "passed": False,
        "reason": "compile_failure",
    }
    row["mismatch_summary"].update(
        reported_counts=[], reported_sample_counts=[], maximum_count=None
    )
    path = tmp_path / "wrong-compile-stage.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(runner.RunnerError):
        runner.validate_candidate_evidence_file(path, max_bytes=1_000_000)


def test_standalone_compile_timeout_requires_unattempted_simulation(
    tmp_path: Path,
):
    row = copy.deepcopy(_evidence_for_category("passed"))
    row.update(accepted=False, failure_category="timeout")
    row["checks"]["compile"]["candidate"] = {
        "attempted": True,
        "passed": False,
        "reason": "timeout",
    }
    row["checks"]["simulation"]["candidate_passes"] = {
        "attempted": False,
        "passed": None,
        "reason": "compile_failure",
    }
    row["mismatch_summary"].update(
        reported_counts=[], reported_sample_counts=[], maximum_count=None,
        timeout_reported=False,
    )
    path = tmp_path / "compile-timeout.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert runner.validate_candidate_evidence_file(path, max_bytes=1_000_000)


@pytest.mark.parametrize("timeout_reported", [False, True])
def test_simulation_timeout_requires_successful_compile(
    tmp_path: Path, timeout_reported: bool
):
    row = copy.deepcopy(_evidence_for_category("passed"))
    row.update(accepted=False, failure_category="timeout")
    row["checks"]["simulation"]["candidate_passes"] = {
        "attempted": True,
        "passed": False,
        "reason": "timeout",
    }
    row["mismatch_summary"]["timeout_reported"] = timeout_reported
    path = tmp_path / f"simulation-timeout-{timeout_reported}.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert runner.validate_candidate_evidence_file(path, max_bytes=1_000_000)


def test_both_timeout_stages_in_one_row_are_rejected(tmp_path: Path):
    row = copy.deepcopy(_evidence_for_category("passed"))
    row.update(accepted=False, failure_category="timeout")
    row["checks"]["compile"]["candidate"] = {
        "attempted": True,
        "passed": False,
        "reason": "timeout",
    }
    row["checks"]["simulation"]["candidate_passes"] = {
        "attempted": True,
        "passed": False,
        "reason": "timeout",
    }
    row["mismatch_summary"].update(
        reported_counts=[], reported_sample_counts=[], maximum_count=None,
        timeout_reported=False,
    )
    path = tmp_path / "both-timeouts.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(runner.RunnerError):
        runner.validate_candidate_evidence_file(path, max_bytes=1_000_000)


def test_verifier_generated_simulation_elaboration_failure_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    row = verification.load_manifest(
        Path(__file__).parent / "fixtures" / "candidate_verification" / "manifest.jsonl",
        Path(__file__).parent / "fixtures" / "candidate_verification",
    )[0]
    toolchain = {
        name: verification.ToolInfo(f"/fake/{name}", f"fake {name} 1.0")
        for name in ("iverilog", "vvp", "verilator", "yosys")
    }

    def fake_run(command, cwd, timeout, max_output_bytes, environment):
        stage_top = command[command.index("-s") + 1]
        if stage_top == row.top_module:
            return verification.CommandResult(0, "")
        assert stage_top == row.testbench_top
        return verification.CommandResult(1, "testbench elaboration failed")

    monkeypatch.setattr(verification, "_run", fake_run)
    evidence = verification.verify_row(
        row,
        input_hashes=verification.hash_inputs(row),
        toolchain=toolchain,
        work_dir=tmp_path / "work",
        timeout=0.5,
        workspace_root=Path(__file__).parent / "fixtures" / "candidate_verification",
        manifest_path=Path(__file__).parent
        / "fixtures"
        / "candidate_verification"
        / "manifest.jsonl",
    )
    assert evidence["failure_category"] == "compile_failure"
    assert evidence["checks"]["compile"]["candidate"] == {
        "attempted": True,
        "passed": True,
        "reason": None,
    }
    assert evidence["checks"]["simulation"]["candidate_passes"] == {
        "attempted": True,
        "passed": False,
        "reason": "compile_failure",
    }
    path = tmp_path / "verifier-generated.jsonl"
    path.write_text(json.dumps(evidence) + "\n", encoding="utf-8")
    assert runner.validate_candidate_evidence_file(path, max_bytes=1_000_000)


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
    assert "'exec /usr/local/bin/python -m rtlbench.cli \"$@\"'" in dockerfile
    assert "'exec python -m rtlbench.cli \"$@\"'" not in dockerfile
    assert "ENTRYPOINT [\"rtlbench\"]" in dockerfile
    assert "COPY src /opt/rtlbench/src" in dockerfile
    assert "test \"$(sed -n '2p' /usr/local/bin/rtlbench)\"" in dockerfile
    assert "env -i \\\n        HOME=/tmp \\\n        TMPDIR=/tmp \\\n        LANG=C \\\n        LC_ALL=C \\\n        PYTHONPATH=/opt/rtlbench/src \\\n        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \\\n        /usr/local/bin/rtlbench --help >/dev/null" in dockerfile
    assert "/usr/local/bin/python -c \\\n        'import rtlbench.cli; assert rtlbench.cli.__file__.startswith(\"/opt/rtlbench/src/rtlbench/\")'" in dockerfile


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
        "5.006",
        "--yosys-package-version",
        "0.23-6",
        "--yosys-version",
        "0.23",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "does not match the checked-out source HEAD" in result.stderr


def test_docker_builder_does_not_require_podman(tmp_path: Path):
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True
    ).strip()
    bin_root, docker_log, podman_log = _fake_builder_tools(tmp_path, commit)
    environment = os.environ.copy()
    environment["PATH"] = f"{bin_root}:/usr/bin:/bin"
    result = subprocess.run(
        [
            "bash",
            "runner/build_isolated_image.sh",
            "--builder",
            "docker",
            *_builder_arguments(commit),
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "built local image ID: sha256:" + "d" * 64 in result.stdout
    assert not podman_log.exists()
    build_rows = json.loads(docker_log.read_text())
    build_argv = next(row for row in build_rows if row[:1] == ["build"])
    assert "--pull=false" in build_argv
    assert "--pull=never" not in build_argv
    assert "--file" in build_argv
    assert build_argv[build_argv.index("--file") + 1] == "runner/Dockerfile"


def test_podman_builder_rejects_rootful_podman(tmp_path: Path):
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True
    ).strip()
    bin_root, docker_log, podman_log = _fake_builder_tools(tmp_path, commit)
    environment = os.environ.copy()
    environment["PATH"] = f"{bin_root}:/usr/bin:/bin"
    result = subprocess.run(
        [
            "bash",
            "runner/build_isolated_image.sh",
            "--builder",
            "podman-rootless",
            *_builder_arguments(commit),
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "rootless=true" in result.stderr
    assert podman_log.exists()
    assert not docker_log.exists()


def test_build_uses_podman_policy_pull_flag(tmp_path: Path):
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True
    ).strip()
    bin_root, docker_log, podman_log = _fake_builder_tools(
        tmp_path, commit, podman_rootless=True
    )
    environment = os.environ.copy()
    environment["PATH"] = f"{bin_root}:/usr/bin:/bin"
    result = subprocess.run(
        [
            "bash",
            "runner/build_isolated_image.sh",
            "--builder",
            "podman-rootless",
            *_builder_arguments(commit),
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not docker_log.exists()
    build_argv = next(
        row for row in json.loads(podman_log.read_text()) if row[:1] == ["build"]
    )
    assert "--pull=never" in build_argv
    assert "--pull=false" not in build_argv


def test_builder_requires_explicit_backend_and_wrapper_forces_podman():
    script = Path(__file__).parents[1] / "runner" / "build_isolated_image.sh"
    result = subprocess.run(
        ["bash", str(script), "--base-image", "invalid"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "usage:" in result.stderr
    wrapper = subprocess.run(
        [
            "bash",
            "runner/build_rootless_image.sh",
            "--builder",
            "docker",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert wrapper.returncode != 0
    assert "always selects --builder podman-rootless" in wrapper.stderr


def test_build_rejects_false_tool_version_labels():
    expected = build_identity.ExpectedIdentity(
        python_version="3.11.9",
        iverilog_package_version="11.0-1.1+b1",
        iverilog_version="11.0",
        vvp_version="11.0",
        verilator_package_version="5.006-3",
        verilator_version="5.006",
        yosys_package_version="0.23-6",
        yosys_version="0.23",
    )
    packages = {
        "iverilog": "11.0-1.1+b1",
        "verilator": "5.006-3",
        "yosys": "0.23-6",
    }
    tools = {
        "iverilog": "Icarus Verilog version 11.0",
        "vvp": "vvp version 11.0",
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


@pytest.mark.parametrize(
    ("parser", "output", "expected"),
    [
        (
            build_identity.parse_iverilog_version,
            "warning: surrounding text\nIcarus Verilog version 11.0 (stable)\n",
            "11.0",
        ),
        (
            build_identity.parse_vvp_version,
            "vvp.tgt version 11.0 (stable) (v11_0)\n",
            "11.0",
        ),
        (
            build_identity.parse_verilator_version,
            "Verilator 5.006 2023-01-01 rev v5.006\n",
            "5.006",
        ),
        (
            build_identity.parse_yosys_version,
            "Yosys 0.23 (git sha1 abcdef)\n",
            "0.23",
        ),
    ],
)
def test_tool_version_parsers_extract_exact_normalized_runtime_versions(
    parser, output: str, expected: str
):
    assert parser(output) == expected


def test_exact_tool_version_matching_rejects_substrings_and_bad_output():
    with pytest.raises(build_identity.BuildIdentityError, match="runtime version mismatch"):
        build_identity.validate_identity(
            build_identity.ExpectedIdentity(
                python_version="3.11.9",
                iverilog_package_version="11.0-1.1+b1",
                iverilog_version="1.0",
                vvp_version="11.0",
                verilator_package_version="5.006-3",
                verilator_version="5.006",
                yosys_package_version="0.23-6",
                yosys_version="0.23",
            ),
            python_version="3.11.9",
            package_versions={
                "iverilog": "11.0-1.1+b1",
                "verilator": "5.006-3",
                "yosys": "0.23-6",
            },
            tool_outputs={
                "iverilog": "Icarus Verilog version 11.0 (stable)",
                "vvp": "vvp.tgt version 11.0 (stable)",
                "verilator": "Verilator 5.006",
                "yosys": "Yosys 0.23",
            },
        )

    malformed = {
        "iverilog": "Icarus Verilog release unknown",
        "vvp": "vvp.tgt release unknown",
        "verilator": "Verilator release unknown",
        "yosys": "Yosys release unknown",
    }
    for name, output in malformed.items():
        parser = getattr(build_identity, f"parse_{name}_version")
        with pytest.raises(build_identity.BuildIdentityError):
            parser(output)

    with pytest.raises(build_identity.BuildIdentityError, match="conflicting"):
        build_identity.parse_yosys_version(
            "Yosys 0.23 (git sha1 abc)\nYosys 0.24 (git sha1 def)\n"
        )


def test_exact_tool_version_matching_accepts_all_normal_outputs():
    expected = build_identity.ExpectedIdentity(
        python_version="3.11.9",
        iverilog_package_version="11.0-1.1+b1",
        iverilog_version="11.0",
        vvp_version="11.0",
        verilator_package_version="5.006-3",
        verilator_version="5.006",
        yosys_package_version="0.23-6",
        yosys_version="0.23",
    )
    build_identity.validate_identity(
        expected,
        python_version="3.11.9",
        package_versions={
            "iverilog": "11.0-1.1+b1",
            "verilator": "5.006-3",
            "yosys": "0.23-6",
        },
        tool_outputs={
            "iverilog": "Icarus Verilog version 11.0 (stable)",
            "vvp": "vvp.tgt version 11.0 (stable)",
            "verilator": "Verilator 5.006 2023-01-01",
            "yosys": "Yosys 0.23 (git sha1 abc)",
        },
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
    assert "--pull=never" in command
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


def test_runtime_stderr_is_surfaced_for_no_evidence_failure(tmp_path: Path):
    handoff = _make_handoff(tmp_path / "handoff")
    fake_docker, _ = _fake_docker(tmp_path, mode="diagnostic")
    with pytest.raises(runner.RunnerError) as error:
        runner.run_isolated(
            image=IMAGE,
            input_root=handoff,
            output=tmp_path / "evidence.jsonl",
            profile=runner.PROFILE_PILOT_DOCKER,
            acknowledge_rootful_runtime=True,
            runtime=str(fake_docker),
        )
    message = str(error.value)
    assert "status 1" in message
    assert "synthetic stdout marker" in message
    assert "synthetic container traceback" in message


def test_runtime_stdout_and_stderr_are_combined(tmp_path: Path):
    execution = _direct_fake_execution(tmp_path, mode="diagnostic")
    assert execution.returncode == 1
    assert execution.timed_out is False
    assert "synthetic stdout marker" in execution.diagnostic_output
    assert "synthetic container traceback" in execution.diagnostic_output


def test_runtime_diagnostic_is_bounded_and_retains_tail(tmp_path: Path):
    execution = _direct_fake_execution(tmp_path, mode="large_diagnostic")
    assert execution.returncode == 1
    assert execution.diagnostic_truncated is True
    assert (
        len(execution.diagnostic_output.encode("utf-8"))
        <= runner.MAX_RUNTIME_DIAGNOSTIC_BYTES
    )
    assert "final failure marker" in execution.diagnostic_output

    handoff = _make_handoff(tmp_path / "formatted-handoff")
    formatted_root = tmp_path / "formatted"
    formatted_root.mkdir()
    fake_docker, _ = _fake_docker(formatted_root, mode="large_diagnostic")
    with pytest.raises(runner.RunnerError) as error:
        runner.run_isolated(
            image=IMAGE,
            input_root=handoff,
            output=tmp_path / "formatted-evidence.jsonl",
            profile=runner.PROFILE_PILOT_DOCKER,
            acknowledge_rootful_runtime=True,
            runtime=str(fake_docker),
        )
    assert runner.RUNTIME_DIAGNOSTIC_TRUNCATION_MARKER in str(error.value)


def test_timeout_runtime_diagnostic_is_bounded_and_surfaced(tmp_path: Path):
    execution = _direct_fake_execution(
        tmp_path / "direct", mode="timeout_diagnostic", timeout=0.2
    )
    assert execution.returncode == 124
    assert execution.timed_out is True
    assert "synthetic timeout marker" in execution.diagnostic_output
    assert (
        len(execution.diagnostic_output.encode("utf-8"))
        <= runner.MAX_RUNTIME_DIAGNOSTIC_BYTES
    )

    handoff = _make_handoff(tmp_path / "formatted-handoff")
    formatted_root = tmp_path / "formatted"
    formatted_root.mkdir()
    fake_docker, _ = _fake_docker(formatted_root, mode="timeout_diagnostic")
    with pytest.raises(runner.RunnerError) as error:
        runner.run_isolated(
            image=IMAGE,
            input_root=handoff,
            output=tmp_path / "formatted-evidence.jsonl",
            profile=runner.PROFILE_PILOT_DOCKER,
            acknowledge_rootful_runtime=True,
            runtime=str(fake_docker),
            wall_timeout=0.2,
        )
    message = str(error.value)
    assert "runner wall timeout expired without preserved evidence" in message
    assert "synthetic timeout marker" in message


def test_runtime_diagnostic_is_sanitized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    handoff = _make_handoff(tmp_path / "handoff")
    fake_docker, _ = _fake_docker(tmp_path, mode="sanitize_diagnostic")
    output = tmp_path / "published" / "evidence.jsonl"
    output.parent.mkdir()
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(runner.tempfile, "tempdir", str(temp_root))
    monkeypatch.setenv("FAKE_HOST_SECRET", "must-not-cross-boundary")

    with pytest.raises(runner.RunnerError) as error:
        runner.run_isolated(
            image=IMAGE,
            input_root=handoff,
            output=output,
            profile=runner.PROFILE_PILOT_DOCKER,
            acknowledge_rootful_runtime=True,
            runtime=str(fake_docker),
        )
    message = str(error.value)
    assert str(handoff.resolve()) not in message
    assert str(output.resolve()) not in message
    assert "<input>" in message
    assert "<staging>" in message
    assert "<runtime-environment>" in message
    assert "<temporary>" in message
    assert "must-not-cross-boundary" not in message
    assert "\x00" not in message
    assert "\x1b" not in message


def test_successful_evidence_does_not_publish_runtime_diagnostic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    handoff = _make_handoff(tmp_path / "handoff")
    fake_docker, _ = _fake_docker(tmp_path, mode="passing_diagnostic")
    output = tmp_path / "evidence.jsonl"
    runner.run_isolated(
        image=IMAGE,
        input_root=handoff,
        output=output,
        profile=runner.PROFILE_PILOT_DOCKER,
        acknowledge_rootful_runtime=True,
        runtime=str(fake_docker),
    )
    sidecar_text = Path(str(output) + ".runner.json").read_text(encoding="utf-8")
    evidence_text = output.read_text(encoding="utf-8")
    assert "success diagnostic should not be surfaced" not in evidence_text
    assert "success diagnostic should not be surfaced" not in sidecar_text
    assert "diagnostic_output" not in sidecar_text
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


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
    assert "partial runtime diagnostic" not in result.partial_output.read_text(
        encoding="utf-8"
    )
    assert "partial runtime diagnostic" not in result.identity_output.read_text(
        encoding="utf-8"
    )


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


def _require_rootless_podman_for_live_matrix() -> None:
    if shutil.which("podman") is None:
        raise RuntimeError("Podman is required when RTLBench_RUNNER_IMAGE is set")
    rootless_result = subprocess.run(
        ["podman", "info", "--format", "{{.Host.Security.Rootless}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if rootless_result.returncode != 0:
        raise RuntimeError("Podman rootless probe failed when RTLBench_RUNNER_IMAGE is set")
    if rootless_result.stdout.strip() != "true":
        raise RuntimeError(
            "Podman must report rootless=true when RTLBench_RUNNER_IMAGE is set"
        )


def test_selected_rootless_gate_requires_rootless_podman(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="Podman is required"):
        _require_rootless_podman_for_live_matrix()


@pytest.mark.integration
def test_rootless_image_acceptance_matrix(tmp_path: Path):
    """Run the real synthetic matrix when a digest-qualified image is supplied."""

    image = os.environ.get("RTLBench_RUNNER_IMAGE")
    if not image:
        pytest.skip("set RTLBench_RUNNER_IMAGE to run the immutable-image acceptance matrix")
    try:
        _require_rootless_podman_for_live_matrix()
    except RuntimeError as exc:
        pytest.fail(str(exc))

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
    assert "--pull=never" in runner.build_run_command(
        "podman", image, handoff, tmp_path, runner.load_config()
    )
    output = tmp_path / "candidate_evidence.jsonl"
    result = runner.run_isolated(image=image, input_root=handoff, output=output)
    assert result.returncode == 0
    evidence = runner.validate_candidate_evidence_file(output, max_bytes=1_000_000)
    assert {row["failure_category"] for row in evidence} == {
        expected for _, expected in directories
    }
    assert _tree_hashes(handoff) == before
    assert not any(path.name == "reference.sv" for path in handoff.rglob("*"))


@pytest.mark.integration
def test_docker_pilot_image_acceptance_matrix(tmp_path: Path):
    """Run the real sequential Docker pilot matrix only when explicitly selected."""

    image = os.environ.get("RTLBench_DOCKER_RUNNER_IMAGE")
    if not image:
        pytest.skip(
            "set RTLBench_DOCKER_RUNNER_IMAGE to run the Docker pilot acceptance matrix"
        )
    if shutil.which("docker") is None:
        pytest.fail("Docker is required when RTLBench_DOCKER_RUNNER_IMAGE is set")
    docker_result = subprocess.run(
        ["docker", "version", "--format", "{{json .Server.Version}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if docker_result.returncode != 0:
        pytest.fail("Docker is unavailable when RTLBench_DOCKER_RUNNER_IMAGE is set")

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
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    before = _tree_hashes(handoff)
    output = tmp_path / "isolated-output" / "candidate_evidence.jsonl"
    output.parent.mkdir()

    config = runner.load_config(profile=runner.PROFILE_PILOT_DOCKER)
    create_command = runner.build_run_command(
        "docker", image, handoff, output.parent, config
    )
    create_command[1] = "create"
    created = subprocess.run(
        create_command,
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode != 0:
        pytest.fail(f"Docker policy inspection container could not be created: {created.stderr}")
    container_id = created.stdout.strip()
    try:
        inspected = subprocess.run(
            ["docker", "inspect", container_id],
            capture_output=True,
            text=True,
            check=False,
        )
        if inspected.returncode != 0:
            pytest.fail(f"Docker policy inspection failed: {inspected.stderr}")
        container = json.loads(inspected.stdout)[0]
        host_config = container["HostConfig"]
        assert container["Config"]["User"] == "65532:65532"
        container_environment = container["Config"]["Env"]
        assert "PYTHONPATH=/opt/rtlbench/src" in container_environment
        assert all(
            value == "PYTHONPATH=/opt/rtlbench/src"
            for value in container_environment
            if value.startswith("PYTHONPATH=")
        )
        assert host_config["NetworkMode"] == "none"
        assert host_config["ReadonlyRootfs"] is True
        assert "ALL" in host_config["CapDrop"]
        assert any(
            "no-new-privileges" in option
            for option in host_config["SecurityOpt"]
        )
        mounts = {mount["Destination"]: mount for mount in container["Mounts"]}
        assert mounts["/input"]["RW"] is False
        assert mounts["/output"]["RW"] is True
        assert host_config["Memory"] == 536870912
        assert host_config["MemorySwap"] == 536870912
        assert host_config["NanoCpus"] == 1_000_000_000
        assert host_config["PidsLimit"] == 64
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_id],
            capture_output=True,
            text=True,
            check=False,
        )

    result = runner.run_isolated(
        image=image,
        input_root=handoff,
        output=output,
        profile=runner.PROFILE_PILOT_DOCKER,
        acknowledge_rootful_runtime=True,
    )
    assert result.returncode == 0
    evidence = runner.validate_candidate_evidence_file(output, max_bytes=1_000_000)
    assert {row["failure_category"] for row in evidence} == {
        expected for _, expected in directories
    }
    sidecar = json.loads(Path(str(output) + ".runner.json").read_text())
    assert sidecar["profile"] == runner.PROFILE_PILOT_DOCKER
    assert sidecar["runtime"] == "docker"
    assert sidecar["runtime_mode"] == "rootful-daemon"
    assert sidecar["rootless"] is False
    assert sidecar["schema_version"] == "rtlbench_runner_identity_v0.2"
    assert sidecar["image_identity_kind"] in {
        "local-image-id",
        "repository-digest",
    }
    assert sidecar["network_policy"] == "none"
    assert sidecar["manifest_sha256"] == hashlib.sha256(
        (handoff / "candidate_manifest.jsonl").read_bytes()
    ).hexdigest()
    assert sidecar["workspace_tree_sha256"] == runner.sha256_workspace_tree(
        handoff / "workspace"
    )
    assert sidecar["evidence_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert _tree_hashes(handoff) == before
    assert not any(path.name == "reference.sv" for path in handoff.rglob("*"))
