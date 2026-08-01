"""Build-time checks for the immutable RTLBench runner image."""

from __future__ import annotations

import argparse
import platform
import subprocess
from dataclasses import dataclass
from typing import Mapping, Sequence


class BuildIdentityError(ValueError):
    """Raised when an image identity claim does not match the build environment."""


@dataclass(frozen=True)
class ExpectedIdentity:
    python_version: str
    iverilog_package_version: str
    iverilog_version: str
    vvp_version: str
    verilator_package_version: str
    verilator_version: str
    yosys_package_version: str
    yosys_version: str


def validate_identity(
    expected: ExpectedIdentity,
    *,
    python_version: str,
    package_versions: Mapping[str, str],
    tool_outputs: Mapping[str, str],
) -> None:
    if python_version != expected.python_version:
        raise BuildIdentityError(
            f"Python version mismatch: expected {expected.python_version!r}, "
            f"got {python_version!r}"
        )
    package_expectations = {
        "iverilog": expected.iverilog_package_version,
        "verilator": expected.verilator_package_version,
        "yosys": expected.yosys_package_version,
    }
    for name, required in package_expectations.items():
        actual = package_versions.get(name)
        if actual != required:
            raise BuildIdentityError(
                f"{name} package version mismatch: expected {required!r}, got {actual!r}"
            )
    tool_expectations = {
        "iverilog": expected.iverilog_version,
        "vvp": expected.vvp_version,
        "verilator": expected.verilator_version,
        "yosys": expected.yosys_version,
    }
    for name, required in tool_expectations.items():
        output = tool_outputs.get(name)
        if not output or required not in output:
            raise BuildIdentityError(
                f"{name} runtime version mismatch: expected {required!r}"
            )


def collect_and_validate(expected: ExpectedIdentity) -> None:
    package_versions = {
        name: _package_version(name) for name in ("iverilog", "verilator", "yosys")
    }
    tool_outputs = {
        "iverilog": _tool_output(("iverilog", "-V")),
        "vvp": _tool_output(("vvp", "-V")),
        "verilator": _tool_output(("verilator", "--version")),
        "yosys": _tool_output(("yosys", "--version")),
    }
    validate_identity(
        expected,
        python_version=platform.python_version(),
        package_versions=package_versions,
        tool_outputs=tool_outputs,
    )


def _package_version(package: str) -> str:
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${Version}", package],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise BuildIdentityError(f"could not inspect installed package {package!r}")
    return result.stdout.strip()


def _tool_output(command: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise BuildIdentityError(f"could not execute {command[0]!r}: {exc}") from exc
    if result.returncode != 0:
        raise BuildIdentityError(f"{command[0]} version probe failed")
    return result.stdout + result.stderr


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate RTLBench runner image identity")
    for name in (
        "python-version",
        "iverilog-package-version",
        "iverilog-version",
        "vvp-version",
        "verilator-package-version",
        "verilator-version",
        "yosys-package-version",
        "yosys-version",
    ):
        parser.add_argument(f"--{name}", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    expected = ExpectedIdentity(
        python_version=args.python_version,
        iverilog_package_version=args.iverilog_package_version,
        iverilog_version=args.iverilog_version,
        vvp_version=args.vvp_version,
        verilator_package_version=args.verilator_package_version,
        verilator_version=args.verilator_version,
        yosys_package_version=args.yosys_package_version,
        yosys_version=args.yosys_version,
    )
    try:
        collect_and_validate(expected)
    except BuildIdentityError as exc:
        print(f"runner image identity: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
