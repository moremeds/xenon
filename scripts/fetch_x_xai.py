#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/fetchers/fetch_x_xai.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from fetchers.fetch_x_xai import *  # noqa: F401,F403
from fetchers.fetch_x_xai import main

if __name__ == "__main__":
    main()
