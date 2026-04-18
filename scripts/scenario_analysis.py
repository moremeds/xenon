#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/reports/scenario_analysis.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from reports.scenario_analysis import *  # noqa: F401,F403
from reports.scenario_analysis import main

if __name__ == "__main__":
    main()
