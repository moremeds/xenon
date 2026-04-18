#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/infra/ib_realtime/test_ib_realtime.py.

Phase 1 preserves old invocation paths. Removed in Phase 2.
Uses runpy because test_ib_realtime is a smoke harness, not an importable module."""

import runpy
from pathlib import Path

_target = Path(__file__).resolve().parent / "infra" / "ib_realtime" / "test_ib_realtime.py"
runpy.run_path(str(_target), run_name="__main__")
