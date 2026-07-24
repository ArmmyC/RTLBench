from pathlib import Path

from rtlbench.cli import build_parser, build_verify_mutations_parser


def test_legacy_parser_still_accepts_config():
    args = build_parser().parse_args(["--config", "configs/verilogeval.yaml"])
    assert args.config == Path("configs/verilogeval.yaml")


def test_verify_mutations_parser():
    args = build_verify_mutations_parser().parse_args(
        ["--manifest", "m.jsonl", "--output", "e.jsonl", "--workspace-root", "corpus", "--work-dir", "work", "--force"]
    )
    assert args.manifest == Path("m.jsonl")
    assert args.force is True
