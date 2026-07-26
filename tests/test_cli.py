from pathlib import Path

import pytest

import rtlbench.cli as cli
from rtlbench.cli import (
    build_parser,
    build_verify_candidates_parser,
    build_verify_mutations_parser,
    main,
)


def test_legacy_parser_still_accepts_config():
    args = build_parser().parse_args(["--config", "configs/verilogeval.yaml"])
    assert args.config == Path("configs/verilogeval.yaml")


def test_verify_mutations_parser():
    args = build_verify_mutations_parser().parse_args(
        ["--manifest", "m.jsonl", "--output", "e.jsonl", "--workspace-root", "corpus", "--work-dir", "work", "--force"]
    )
    assert args.manifest == Path("m.jsonl")
    assert args.force is True


def test_verify_candidates_parser():
    args = build_verify_candidates_parser().parse_args(
        [
            "--manifest", "m.jsonl",
            "--output", "e.jsonl",
            "--workspace-root", "corpus",
            "--work-dir", "work",
            "--force",
        ]
    )
    assert args.manifest == Path("m.jsonl")
    assert args.timeout == 30.0
    assert args.max_output_bytes == 65_536
    assert args.max_artifact_bytes == 8 * 1024 * 1024
    assert args.max_row_input_bytes == 32 * 1024 * 1024
    assert args.max_run_input_bytes == 256 * 1024 * 1024
    assert args.force is True


def test_verify_candidates_parser_accepts_output_limit():
    args = build_verify_candidates_parser().parse_args(
        [
            "--manifest", "m.jsonl",
            "--output", "e.jsonl",
            "--workspace-root", "corpus",
            "--work-dir", "work",
            "--max-output-bytes", "1024",
        ]
    )
    assert args.max_output_bytes == 1024


def test_verify_candidates_parser_accepts_input_limits():
    args = build_verify_candidates_parser().parse_args(
        [
            "--manifest", "m.jsonl",
            "--output", "e.jsonl",
            "--workspace-root", "corpus",
            "--work-dir", "work",
            "--max-artifact-bytes", "100",
            "--max-row-input-bytes", "200",
            "--max-run-input-bytes", "300",
        ]
    )
    assert (args.max_artifact_bytes, args.max_row_input_bytes, args.max_run_input_bytes) == (100, 200, 300)


def test_verify_candidates_rejects_invalid_output_limit(tmp_path: Path):
    fixture = Path(__file__).parent / "fixtures" / "candidate_verification"
    assert cli.main(
        [
            "verify-candidates",
            "--manifest", str(fixture / "manifest.jsonl"),
            "--output", str(tmp_path / "evidence.jsonl"),
            "--workspace-root", str(fixture),
            "--work-dir", str(tmp_path / "work"),
            "--max-output-bytes", "0",
        ]
    ) == 2


@pytest.mark.parametrize(
    "option,value",
    [
        ("--max-artifact-bytes", "0"),
        ("--max-row-input-bytes", "0"),
        ("--max-run-input-bytes", "0"),
        ("--max-artifact-bytes", str(32 * 1024 * 1024 + 1)),
    ],
)
def test_verify_candidates_rejects_invalid_input_limits(
    tmp_path: Path, option: str, value: str
):
    fixture = Path(__file__).parent / "fixtures" / "candidate_verification"
    assert cli.main(
        [
            "verify-candidates",
            "--manifest", str(fixture / "manifest.jsonl"),
            "--output", str(tmp_path / f"{option[2:]}.jsonl"),
            "--workspace-root", str(fixture),
            "--work-dir", str(tmp_path / "work"),
            option, value,
        ]
    ) == 2


def test_verify_candidates_help():
    with pytest.raises(SystemExit) as excinfo:
        main(["verify-candidates", "--help"])
    assert excinfo.value.code == 0


def test_candidate_manifest_error_exit_2(tmp_path: Path):
    manifest = tmp_path / "bad.jsonl"
    manifest.write_text("not json\n", encoding="utf-8")
    assert cli.main(
        [
            "verify-candidates",
            "--manifest", str(manifest),
            "--output", str(tmp_path / "evidence.jsonl"),
            "--workspace-root", str(Path(__file__).parent / "fixtures" / "candidate_verification"),
            "--work-dir", str(tmp_path / "work"),
        ]
    ) == 2


def test_candidate_filesystem_preflight_error_exit_3(tmp_path: Path):
    output_dir = tmp_path / "output-dir"
    output_dir.mkdir()
    fixture = Path(__file__).parent / "fixtures" / "candidate_verification"
    assert cli.main(
        [
            "verify-candidates",
            "--manifest", str(fixture / "manifest.jsonl"),
            "--output", str(output_dir),
            "--workspace-root", str(fixture),
            "--work-dir", str(tmp_path / "work"),
        ]
    ) == 3


def test_candidate_level_failures_still_exit_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fixture = Path(__file__).parent / "fixtures" / "candidate_verification"
    output = tmp_path / "evidence.jsonl"
    monkeypatch.setattr(cli, "verify_candidates", lambda **kwargs: {"rows": 1, "passed": 0, "failed": 1})
    assert cli.main(
        [
            "verify-candidates",
            "--manifest", str(fixture / "manifest.jsonl"),
            "--output", str(output),
            "--workspace-root", str(fixture),
            "--work-dir", str(tmp_path / "work"),
        ]
    ) == 0


def test_candidate_interruption_exit_4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fixture = Path(__file__).parent / "fixtures" / "candidate_verification"
    monkeypatch.setattr(
        cli,
        "verify_candidates",
        lambda **kwargs: (_ for _ in ()).throw(cli.CandidateVerificationInterrupted("interrupted")),
    )
    assert cli.main(
        [
            "verify-candidates",
            "--manifest", str(fixture / "manifest.jsonl"),
            "--output", str(tmp_path / "evidence.jsonl"),
            "--workspace-root", str(fixture),
            "--work-dir", str(tmp_path / "work"),
        ]
    ) == 4
