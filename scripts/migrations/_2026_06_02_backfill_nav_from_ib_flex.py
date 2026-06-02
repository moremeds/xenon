"""Backfill xenon.nav_history from IB Flex EquitySummaryByReportDateInBase.

Two ingest paths:

* ``--from-csv <path>`` — parse a CSV downloaded from IB Account Management
  (Reports → Flex Queries → Run → save CSV). Use for inception-to-date pulls
  that are too large for the Flex Web Service polling timeout.
* default (no flag) — call ``fetch_ib_nav_series`` which polls Flex Web Service
  and writes the same rows. Suitable for incremental refreshes.

Writes ``source='close'`` because EquitySummaryByReportDateInBase rows are
post-close. Raw SQL upsert avoids extending ``upsert_nav_sync`` (which only
writes ``source='intraday'`` today).

Optional ``--trades-csv <path>`` parses the trade-history CSV for summary only —
no PG write surface exists for historical Flex trades today.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import date as _date
from decimal import Decimal
from pathlib import Path
from typing import Any


def _load_env() -> None:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def _ensure_broker_account_env() -> None:
    if os.environ.get("XENON_BROKER_ACCOUNT"):
        return
    mode = os.environ.get("XENON_TRADING_MODE", "").strip().lower()
    env_key = {"live": "XENON_LIVE_ACCOUNT", "paper": "XENON_PAPER_ACCOUNT"}.get(mode)
    if env_key and os.environ.get(env_key):
        os.environ["XENON_BROKER_ACCOUNT"] = os.environ[env_key]


def _parse_yyyymmdd(s: str) -> _date:
    s = s.strip()
    if "-" in s:
        return _date.fromisoformat(s)
    return _date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def _dec(value: str) -> Decimal:
    return Decimal(value.strip() or "0")


def ingest_nav_csv(*, path: Path, scope: Any) -> dict[str, Any]:
    """Read CSV, validate account, upsert nav_history with source='close'."""
    from sqlalchemy import text

    from xenon.db.engine import get_sync_engine

    rows: list[dict[str, Any]] = []
    skipped_mismatch = 0
    extra_sections = 0  # IB Flex CSV may concat (NAV, CashTransactions, Transfers)
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for csv_row in reader:
            account = csv_row.get("ClientAccountID", "").strip()
            # Second/third header row marks the start of another section
            # (Cash Transactions, Transfers). csv.DictReader keeps the first
            # section's column names so reading further yields garbage.
            if account == "ClientAccountID":
                extra_sections += 1
                break
            if account != scope.broker_account:
                skipped_mismatch += 1
                continue
            day = _parse_yyyymmdd(csv_row["ReportDate"])
            total = _dec(csv_row["Total"])
            rows.append(
                {
                    "broker": scope.broker,
                    "account_env": scope.account_env,
                    "broker_account": scope.broker_account,
                    "date": day,
                    "nav": total,
                    "total": total,
                    "cash": _dec(csv_row["Cash"]),
                    "stock_value": _dec(csv_row["Stock"]),
                    "options_value": _dec(csv_row["Options"]),
                    "source": "close",
                }
            )

    engine = get_sync_engine()

    # Preflight: nav_history_one_env_per_day unique index excludes account_env.
    # A pre-existing row with a different env for the same (broker, account, date)
    # would block the upsert with an IntegrityError on the secondary index, not
    # the primary key. Catch it before we issue 265 individual upserts.
    conflicting: list[tuple[_date, str]] = []
    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                SELECT date, account_env FROM xenon.nav_history
                 WHERE broker = :broker
                   AND broker_account = :broker_account
                   AND account_env != :account_env
                   AND date = ANY(:dates)
                """
            ),
            {
                "broker": scope.broker,
                "broker_account": scope.broker_account,
                "account_env": scope.account_env,
                "dates": [r["date"] for r in rows],
            },
        )
        conflicting = [(row.date, row.account_env) for row in result]

    if conflicting:
        return {
            "error": "ENV_INDEX_CONFLICT",
            "conflicting_dates": [{"date": d.isoformat(), "existing_env": e} for d, e in conflicting[:10]],
            "conflict_count": len(conflicting),
        }

    inserted = 0
    updated = 0
    upsert_sql = text(
        """
        INSERT INTO xenon.nav_history
            (broker, account_env, broker_account, date,
             nav, total, cash, stock_value, options_value, source)
        VALUES
            (:broker, :account_env, :broker_account, :date,
             :nav, :total, :cash, :stock_value, :options_value, :source)
        ON CONFLICT (broker, account_env, broker_account, date)
        DO UPDATE SET
            nav           = EXCLUDED.nav,
            total         = EXCLUDED.total,
            cash          = EXCLUDED.cash,
            stock_value   = EXCLUDED.stock_value,
            options_value = EXCLUDED.options_value,
            source        = EXCLUDED.source
        RETURNING (xmax = 0) AS was_inserted
        """
    )
    with engine.begin() as conn:
        for r in rows:
            res = conn.execute(upsert_sql, r).first()
            if res and res.was_inserted:
                inserted += 1
            else:
                updated += 1

    nonzero = [r for r in rows if r["total"] != 0]
    return {
        "inserted": inserted,
        "updated": updated,
        "skipped_account_mismatch": skipped_mismatch,
        "additional_sections_skipped": extra_sections,
        "total_processed": len(rows),
        "earliest_date": rows[0]["date"].isoformat() if rows else None,
        "latest_date": rows[-1]["date"].isoformat() if rows else None,
        "earliest_funded_date": nonzero[0]["date"].isoformat() if nonzero else None,
        "latest_funded_total": str(nonzero[-1]["total"]) if nonzero else None,
    }


def summarize_trades_csv(*, path: Path, scope: Any) -> dict[str, Any]:
    """Read the trades CSV — summary only, no PG write path exists today."""
    rows: list[dict[str, str]] = []
    skipped_mismatch = 0
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for csv_row in reader:
            if csv_row.get("ClientAccountID", "").strip() != scope.broker_account:
                skipped_mismatch += 1
                continue
            rows.append(csv_row)
    if not rows:
        return {"count": 0, "skipped_account_mismatch": skipped_mismatch}

    dates = sorted(r["DateTime"][:8] for r in rows if r.get("DateTime"))
    by_asset: dict[str, int] = {}
    for r in rows:
        ac = r.get("AssetClass", "").strip() or "?"
        by_asset[ac] = by_asset.get(ac, 0) + 1
    by_side: dict[str, int] = {}
    for r in rows:
        side = r.get("Buy/Sell", "").strip() or "?"
        by_side[side] = by_side.get(side, 0) + 1
    return {
        "count": len(rows),
        "skipped_account_mismatch": skipped_mismatch,
        "earliest": _parse_yyyymmdd(dates[0]).isoformat() if dates else None,
        "latest": _parse_yyyymmdd(dates[-1]).isoformat() if dates else None,
        "by_asset_class": by_asset,
        "by_side": by_side,
    }


def fetch_via_flex_api(scope: Any) -> int:
    """Original API path — calls fetch_ib_nav_series which polls Flex Web Service.

    Persists via upsert_nav_sync (writes source='intraday' — known gap until
    that helper accepts source). Kept for incremental refresh use; CSV path
    is preferred for inception backfills.
    """
    if not os.environ.get("IB_FLEX_TOKEN") or not os.environ.get("IB_FLEX_NAV_QUERY_ID"):
        print("FLEX_NOT_CONFIGURED: missing IB_FLEX_TOKEN or IB_FLEX_NAV_QUERY_ID", file=sys.stderr)
        return 2
    from xenon.reports.portfolio_performance import fetch_ib_nav_series

    print(f"scope: broker={scope.broker} env={scope.account_env} account={scope.broker_account}")
    print("polling IB Flex Web Service (up to 300s)...")
    entries = fetch_ib_nav_series()
    if entries is None:
        print("fetch_ib_nav_series returned None — token rejected, poll timeout, or no rows", file=sys.stderr)
        return 1
    print(f"fetched {len(entries)} NAV rows from IB Flex API")
    return 0


def main() -> int:
    _load_env()
    _ensure_broker_account_env()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-csv", type=Path, help="Path to NAV CSV (preferred for inception backfill)")
    parser.add_argument("--trades-csv", type=Path, help="Path to trades CSV (summary only, no PG write)")
    args = parser.parse_args()

    from xenon.execution.account_scope import resolve_from_env

    scope = resolve_from_env()
    print(f"scope: broker={scope.broker} env={scope.account_env} account={scope.broker_account}")

    if args.trades_csv:
        print(f"\n[trades] reading {args.trades_csv}")
        summary = summarize_trades_csv(path=args.trades_csv, scope=scope)
        for k, v in summary.items():
            print(f"  {k}: {v}")

    if args.from_csv:
        print(f"\n[nav] ingesting {args.from_csv}")
        result = ingest_nav_csv(path=args.from_csv, scope=scope)
        for k, v in result.items():
            print(f"  {k}: {v}")
        if "error" in result:
            return 1
        return 0

    if not args.trades_csv:
        return fetch_via_flex_api(scope)

    return 0


if __name__ == "__main__":
    sys.exit(main())
