#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/services/exit_order_service.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from services.exit_order_service import *  # noqa: F401,F403
from services.exit_order_service import main

if __name__ == "__main__":
    main()
