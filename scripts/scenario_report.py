#!/usr/bin/env python3
"""Compatibility shim. Real home: src/xenon/reports/scenario_report.py.

Phase 1 preserves old invocation paths. Removed in Phase 2 PR 4."""

import runpy

if __name__ == "__main__":
    runpy.run_module("xenon.reports.scenario_report", run_name="__main__")
