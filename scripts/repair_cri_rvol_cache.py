#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/scanners/repair_cri_rvol_cache.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from scanners.repair_cri_rvol_cache import *  # noqa: F401,F403
from scanners.repair_cri_rvol_cache import main

if __name__ == "__main__":
    main()
