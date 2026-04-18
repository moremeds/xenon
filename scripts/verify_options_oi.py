#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/reports/verify_options_oi.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from reports.verify_options_oi import *  # noqa: F401,F403
from reports.verify_options_oi import main

if __name__ == "__main__":
    main()
