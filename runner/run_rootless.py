#!/usr/bin/env python3
"""CLI wrapper kept separate so the launcher can be run from a checkout."""

from rootless_runner import main


if __name__ == "__main__":
    raise SystemExit(main())
