#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/execution/ib_option_chain.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from execution.ib_option_chain import *  # noqa: F401,F403
from execution.ib_option_chain import main

if __name__ == "__main__":
    main()
