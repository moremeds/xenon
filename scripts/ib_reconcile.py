#!/usr/bin/env python3
"""Compatibility shim. Real home: src/xenon/execution/ib_reconcile.py.

Phase 1 preserves old invocation paths. Removed in Phase 2 PR 4."""

import runpy

if __name__ == "__main__":
    runpy.run_module("xenon.execution.ib_reconcile", run_name="__main__")
