"""Daily IB Flex NAV refresh — invoked by launchd at 17:30 ET.

Polls IB Flex Web Service for EquitySummaryByReportDateInBase rows and
upserts them into ``xenon.nav_history`` with ``source='close'``. The
underlying ``fetch_ib_nav_series`` handles the two-step SendRequest +
GetStatement polling and the upsert.

Exit codes:
  0 — fetched and persisted N>0 rows
  1 — fetch returned None or empty (token rejected, poll timeout, no rows)
  2 — FLEX_NOT_CONFIGURED (missing IB_FLEX_TOKEN or IB_FLEX_NAV_QUERY_ID)
  3 — READ_ONLY (XENON_READ_ONLY=1) — refusing to write (Pass-1)

Per the [[flex-is-reconciliation-not-history]] architecture, the saved
Flex query should be a ~2-week rolling window — this CLI is the
reconciliation path, not a historical backfill.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    except ImportError:
        pass


def _ensure_broker_account_env() -> None:
    """Derive XENON_BROKER_ACCOUNT from XENON_TRADING_MODE (mirror of dev.sh)."""
    if os.environ.get("XENON_BROKER_ACCOUNT"):
        return
    mode = os.environ.get("XENON_TRADING_MODE", "").strip().lower()
    env_key = {"live": "XENON_LIVE_ACCOUNT", "paper": "XENON_PAPER_ACCOUNT"}.get(mode)
    if env_key and os.environ.get(env_key):
        os.environ["XENON_BROKER_ACCOUNT"] = os.environ[env_key]


# Re-export so tests can monkeypatch via the local module namespace.
from xenon.reports.portfolio_performance import fetch_ib_nav_series  # noqa: E402


def main() -> int:
    _load_env()

    # Surface xenon.* INFO logs (e.g. "ingested N IB CashTransactions row(s)")
    # to stderr so the launchd-captured log includes the cash-flow ingest count.
    # idempotent — basicConfig is a no-op once root logger has handlers.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")

    # Pass-1 / Pass-2 T8: refuse to write under XENON_READ_ONLY=1. A MacBook
    # `dev.sh live` session sets the flag so this CLI fired against the live
    # IB connection would otherwise pollute `core_test` with live close NAVs.
    # Real live trading writes via the macmini Docker stack, which does NOT
    # set the flag.
    if os.environ.get("XENON_READ_ONLY") == "1":
        print(
            "READ_ONLY: XENON_READ_ONLY=1 — refusing to ingest NAV rows. "
            "Unset the flag (or run on the macmini prod stack) to enable.",
            file=sys.stderr,
        )
        return 3

    _ensure_broker_account_env()

    if not os.environ.get("IB_FLEX_TOKEN") or not os.environ.get("IB_FLEX_NAV_QUERY_ID"):
        print(
            "FLEX_NOT_CONFIGURED: set IB_FLEX_TOKEN and IB_FLEX_NAV_QUERY_ID",
            file=sys.stderr,
        )
        return 2

    print(
        f"xenon-nav-flex-refresh: mode={os.environ.get('XENON_TRADING_MODE')} "
        f"account={os.environ.get('XENON_BROKER_ACCOUNT')}"
    )
    print("polling IB Flex Web Service (last-N-days query, ~30-90s)...")

    entries = fetch_ib_nav_series()
    if entries is None:
        print(
            "fetch_ib_nav_series returned None — token rejected, poll timeout, or no rows",
            file=sys.stderr,
        )
        return 1
    if not entries:
        print("fetch_ib_nav_series returned 0 rows", file=sys.stderr)
        return 1

    plural = "s" if len(entries) != 1 else ""
    print(f"fetched {len(entries)} NAV row{plural} (source='close' persisted via upsert_nav_sync)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
