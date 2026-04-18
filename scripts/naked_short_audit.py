#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/execution/naked_short_audit.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from execution.naked_short_audit import *  # noqa: F401,F403
from execution.naked_short_audit import main

if __name__ == "__main__":
    main()
