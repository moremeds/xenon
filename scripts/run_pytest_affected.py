#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/infra/dev/run_pytest_affected.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""

import sys

from infra.dev.run_pytest_affected import main

if __name__ == "__main__":
    sys.exit(main())
