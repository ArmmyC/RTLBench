#!/usr/bin/env python3
"""CLI wrapper kept separate so the launcher can be run from a checkout."""

from rootless_runner import PROFILE_PRODUCTION_ROOTLESS, main


if __name__ == "__main__":
    raise SystemExit(
        main(default_profile=PROFILE_PRODUCTION_ROOTLESS, allow_profile=False)
    )
