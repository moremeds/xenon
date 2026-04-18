#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/reports/scenario_report.py.

Phase 1 preserves old invocation paths. Removed in Phase 2.
Uses runpy because scenario_report is a top-level script (no main() function)."""
import runpy
from pathlib import Path

_target = Path(__file__).resolve().parent / "reports" / "scenario_report.py"
runpy.run_path(str(_target), run_name="__main__")
