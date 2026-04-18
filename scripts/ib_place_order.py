#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/execution/ib_place_order.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from execution.ib_place_order import *  # noqa: F401,F403
from execution.ib_place_order import main

if __name__ == "__main__":
    main()
