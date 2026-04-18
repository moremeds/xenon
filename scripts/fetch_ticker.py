#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/fetchers/fetch_ticker.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from fetchers.fetch_ticker import *  # noqa: F401,F403
from fetchers.fetch_ticker import main

if __name__ == "__main__":
    main()
