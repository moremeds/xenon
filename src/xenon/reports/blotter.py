#!/usr/bin/env python3
"""
Trade Blotter - Fetch and reconcile trades from Interactive Brokers.

Usage:
    xenon-blotter                 # Today's trades
    xenon-blotter --summary       # P&L summary only
    xenon-blotter --json          # JSON output
    xenon-blotter --verbose       # Show execution details
    xenon-blotter --port 7497     # Custom IB port

Integration tests:
    python3 src/xenon/trade_blotter/test_integration.py
"""

import sys

from xenon.trade_blotter.cli import main

if __name__ == "__main__":
    sys.exit(main())
