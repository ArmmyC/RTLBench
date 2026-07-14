from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rtlbench.mutation_verification import (
    VerificationInterrupted,
    VerificationPreflightError,
    verify_mutations,
)


def build_parser() -> argparse.ArgumentParser:
    from rtlbench.adapters import ADAPTERS

    parser = argparse.ArgumentParser(description="Run RTL generation benchmarks")
    parser.add_argument("--config", type=Path, default=Path("configs/verilogeval.yaml"))
    parser.add_argument("--benchmark", choices=sorted(ADAPTERS))
    parser.add_argument("--benchmark-root", dest="root")
    parser.add_argument("--split")
    parser.add_argument("--model-preset")
    parser.add_argument("--model", dest="name")
    parser.add_argument("--prompt-profile")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--samples-per-task", type=int)
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--top-p", type=float)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--request-timeout", type=float)
    parser.add_argument("--evaluation-timeout", type=float)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--output-dir")
    parser.add_argument("--notes", default="")
    parser.add_argument("--overwrite", action="store_true")
    return parser


def build_verify_mutations_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rtlbench verify-mutations",
        description="Verify original, mutated, and repaired RTL from a JSONL manifest",
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "verify-mutations":
        args = build_verify_mutations_parser().parse_args(arguments[1:])
        try:
            summary = verify_mutations(
                manifest=args.manifest,
                output=args.output,
                workspace_root=args.workspace_root,
                work_dir=args.work_dir,
                timeout=args.timeout,
                force=args.force,
            )
        except VerificationInterrupted as exc:
            print(str(exc), file=sys.stderr)
            return 4
        except VerificationPreflightError as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"Wrote {summary['rows']} evidence row(s) to {args.output}")
        return 0

    args = build_parser().parse_args(arguments)
    from rtlbench.config import load_config
    from rtlbench.runner import run_benchmark

    overrides = {
        key: value
        for key, value in vars(args).items()
        if key not in {"config", "overwrite", "notes"}
    }
    config = load_config(args.config, overrides)
    output = run_benchmark(config, overwrite=args.overwrite, notes=args.notes)
    print(f"Results written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
