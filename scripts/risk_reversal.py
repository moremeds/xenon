#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/reports/risk_reversal.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from reports.risk_reversal import *  # noqa: F401,F403
from reports.risk_reversal import main

if __name__ == "__main__":
    main()
