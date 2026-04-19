#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/scanners/vcg.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from xenon.scanners.vcg import *  # noqa: F401,F403
from xenon.scanners.vcg import main

if __name__ == "__main__":
    main()
