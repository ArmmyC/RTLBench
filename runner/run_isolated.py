#!/usr/bin/env python3
"""Primary CLI for the explicitly selected RTLBench isolation profiles."""

from rootless_runner import main


if __name__ == "__main__":
    raise SystemExit(main(require_profile=True))
