#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/reports/portfolio_report.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from reports.portfolio_report import *  # noqa: F401,F403
from reports.portfolio_report import main

if __name__ == "__main__":
    main()
