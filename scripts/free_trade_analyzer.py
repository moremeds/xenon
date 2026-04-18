#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/reports/free_trade_analyzer.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from reports.free_trade_analyzer import *  # noqa: F401,F403
from reports.free_trade_analyzer import main

if __name__ == "__main__":
    main()
