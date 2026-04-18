#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/reports/evaluate.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from reports.evaluate import *  # noqa: F401,F403
from reports.evaluate import main

if __name__ == "__main__":
    main()
