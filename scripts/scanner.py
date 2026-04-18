#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/scanners/scanner.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from scanners.scanner import *  # noqa: F401,F403
from scanners.scanner import main

if __name__ == "__main__":
    main()
