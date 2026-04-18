#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/scanners/garch.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from scanners.garch import *  # noqa: F401,F403
from scanners.garch import main

if __name__ == "__main__":
    main()
