from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rtlbench.mutation_verification import (
    VerificationInterrupted,
    VerificationPreflightError,
    verify_mutations,
)
from rtlbench.candidate_manifest import (
    CandidateManifestValidationError,
    CandidateWorkspaceValidationError,
)
from rtlbench.candidate_verification import (
    DEFAULT_MAX_ARTIFACT_BYTES,
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_MAX_ROW_INPUT_BYTES,
    DEFAULT_MAX_RUN_INPUT_BYTES,
    MAX_ARTIFACT_BYTES_LIMIT,
    MAX_OUTPUT_BYTES_LIMIT,
    MAX_ROW_INPUT_BYTES_LIMIT,
    MAX_RUN_INPUT_BYTES_LIMIT,
    CandidateVerificationInternalError,
    CandidateVerificationInterrupted,
    CandidateVerificationPreflightError,
    verify_candidates,
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


def build_verify_candidates_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rtlbench verify-candidates",
        description="Verify generated RTL candidates from a JSONL manifest",
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-output-bytes", type=int, default=DEFAULT_MAX_OUTPUT_BYTES)
    parser.add_argument(
        "--max-artifact-bytes", type=int, default=DEFAULT_MAX_ARTIFACT_BYTES
    )
    parser.add_argument(
        "--max-row-input-bytes", type=int, default=DEFAULT_MAX_ROW_INPUT_BYTES
    )
    parser.add_argument(
        "--max-run-input-bytes", type=int, default=DEFAULT_MAX_RUN_INPUT_BYTES
    )
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

    if arguments and arguments[0] == "verify-candidates":
        args = build_verify_candidates_parser().parse_args(arguments[1:])
        if args.timeout <= 0:
            print("timeout must be greater than zero", file=sys.stderr)
            return 2
        if args.max_output_bytes <= 0 or args.max_output_bytes > MAX_OUTPUT_BYTES_LIMIT:
            print(
                f"max-output-bytes must be between 1 and {MAX_OUTPUT_BYTES_LIMIT}",
                file=sys.stderr,
            )
            return 2
        input_limits = (
            ("max-artifact-bytes", args.max_artifact_bytes, MAX_ARTIFACT_BYTES_LIMIT),
            ("max-row-input-bytes", args.max_row_input_bytes, MAX_ROW_INPUT_BYTES_LIMIT),
            ("max-run-input-bytes", args.max_run_input_bytes, MAX_RUN_INPUT_BYTES_LIMIT),
        )
        for name, value, hard_cap in input_limits:
            if value <= 0 or value > hard_cap:
                print(
                    f"{name} must be between 1 and {hard_cap}",
                    file=sys.stderr,
                )
                return 2
        try:
            summary = verify_candidates(
                manifest=args.manifest,
                output=args.output,
                workspace_root=args.workspace_root,
                work_dir=args.work_dir,
                timeout=args.timeout,
                force=args.force,
                max_output_bytes=args.max_output_bytes,
                max_artifact_bytes=args.max_artifact_bytes,
                max_row_input_bytes=args.max_row_input_bytes,
                max_run_input_bytes=args.max_run_input_bytes,
            )
        except CandidateVerificationInterrupted as exc:
            print(str(exc), file=sys.stderr)
            return 4
        except CandidateVerificationInternalError as exc:
            print(str(exc), file=sys.stderr)
            return 4
        except CandidateVerificationPreflightError as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except (CandidateManifestValidationError, CandidateWorkspaceValidationError) as exc:
            print(str(exc), file=sys.stderr)
            return 3 if isinstance(exc, CandidateWorkspaceValidationError) else 2
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
