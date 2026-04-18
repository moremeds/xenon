#!/usr/bin/env python3
"""Futu one-shot sync CLI.

Connects to Futu OpenD, pulls positions + account info, writes a
single combined JSON to `data/futu_portfolio.json` via atomic_save.

Intended uses:
  - Smoke-testing the FutuClient against a real OpenD without booting
    the whole Xenon stack.
  - A launchd / cron fallback if the FastAPI singleton path is
    unavailable.

The FastAPI route (`POST /futu/sync` in a later phase) reuses the same
FutuClient as a long-lived singleton, so this CLI is for manual and
smoke-test use — NOT the hot path.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Ensure the repo root is on sys.path when run as a script.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.clients.futu_client import FutuClient  # noqa: E402
from scripts.clients.futu_exceptions import FutuError  # noqa: E402
from scripts.utils.atomic_io import atomic_save  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Futu positions → JSON")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11111)
    parser.add_argument("--firm", default="FUTUSECURITIES")
    parser.add_argument("--env", default="REAL", choices=["REAL", "SIMULATE"])
    parser.add_argument("--market", default="US", help="Market filter (US, HK, CN, ...)")
    parser.add_argument(
        "--output",
        default=str(_REPO_ROOT / "data" / "futu_portfolio.json"),
        help="Destination JSON path (default: data/futu_portfolio.json)",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--print",
        dest="print_json",
        action="store_true",
        help="Also print the result JSON to stdout",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    client = FutuClient(
        host=args.host,
        port=args.port,
        security_firm=args.firm,
        trd_env=args.env,
        filter_trading_market=args.market,
    )

    try:
        client.connect()
        result = client.fetch_portfolio(force=True)
    except FutuError as exc:
        print(f"FUTU ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — CLI wants to report everything
        print(f"UNEXPECTED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    finally:
        try:
            client.disconnect()
        except Exception:
            pass

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_save(str(out_path), result)

    n = result.get("count", 0)
    acct = result.get("account_summary", {})
    print(
        f"Synced {n} Futu position(s) → {out_path} "
        f"(account={result.get('account_id')}, "
        f"net_liq=${acct.get('net_liquidation', 0):,.2f}, "
        f"unrealized=${acct.get('unrealized_pnl', 0):,.2f})",
        file=sys.stderr,
    )
    if args.print_json:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
