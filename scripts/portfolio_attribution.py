#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/reports/portfolio_attribution.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from reports.portfolio_attribution import *  # noqa: F401,F403
from reports.portfolio_attribution import main

if __name__ == "__main__":
    main()
