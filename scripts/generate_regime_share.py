#!/usr/bin/env python3
"""Compatibility shim. Real home: scripts/shares/generate_regime_share.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""

from shares.generate_regime_share import *  # noqa: F401,F403
from shares.generate_regime_share import main

if __name__ == "__main__":
    main()
