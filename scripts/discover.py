#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/scanners/discover.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from scanners.discover import *  # noqa: F401,F403
from scanners.discover import main

if __name__ == "__main__":
    main()
