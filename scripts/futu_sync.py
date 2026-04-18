#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/execution/futu_sync.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from execution.futu_sync import *  # noqa: F401,F403
from execution.futu_sync import main

if __name__ == "__main__":
    main()
