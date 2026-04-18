#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/reports/performance_explainer_report.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from reports.performance_explainer_report import *  # noqa: F401,F403
from reports.performance_explainer_report import main

if __name__ == "__main__":
    main()
