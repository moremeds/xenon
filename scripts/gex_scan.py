#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/scanners/gex.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from xenon.scanners.gex import *  # noqa: F401,F403
from xenon.scanners.gex import main

if __name__ == "__main__":
    main()
