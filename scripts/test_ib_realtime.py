#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/infra/ib_realtime/test_ib_realtime.py.

Phase 1 preserves old invocation paths. Removed in Phase 2.
Uses runpy because test_ib_realtime is a smoke harness, not an importable module.
Guarded with __main__ check so pytest collection doesn't execute it."""

import runpy
from pathlib import Path

if __name__ == "__main__":
    _target = Path(__file__).resolve().parent / "infra" / "ib_realtime" / "test_ib_realtime.py"
    runpy.run_path(str(_target), run_name="__main__")
