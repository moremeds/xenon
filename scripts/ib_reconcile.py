#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/execution/ib_reconcile.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from execution.ib_reconcile import *  # noqa: F401,F403
from execution.ib_reconcile import main

if __name__ == "__main__":
    main()
