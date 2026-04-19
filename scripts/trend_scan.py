#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/scanners/trend/cli.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from xenon.scanners.trend.cli import *  # noqa: F401,F403
from xenon.scanners.trend.cli import main

if __name__ == "__main__":
    main()
