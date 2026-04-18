#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/reports/blotter.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
import sys
from reports.blotter import *  # noqa: F401,F403
from reports.blotter import main

if __name__ == "__main__":
    sys.exit(main())
