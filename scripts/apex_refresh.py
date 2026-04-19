#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/fetchers/fetch_apex_data.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""

import runpy

runpy.run_module("fetchers.fetch_apex_data", run_name="__main__")
