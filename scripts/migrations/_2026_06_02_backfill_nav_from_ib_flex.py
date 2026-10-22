"""Backfill ``xenon.nav_history`` + ``xenon.ib_cash_flow`` from an IB Flex CSV export.

``--from-csv <path>`` parses a multi-section CSV downloaded from IB Account
Management (Reports → Flex Queries → a *Custom Date Range* query covering
inception-to-date → Run → save CSV). Each section is classified by its
header row:

* ``EquitySummaryInBase`` (``ReportDate`` + ``Total`` columns) → upserted
  into ``xenon.nav_history`` via ``upsert_nav_sync(..., source='close')`` —
  the single NAV writer (post-``2026_06_03_nav_src_pk`` the PK includes
  ``source``, so a hand-rolled ``ON CONFLICT`` no longer matches).
* ``CashTransactions`` (``TransactionID`` column) → parsed and persisted
  by the shared ``_parse_ib_cash_transactions_section`` /
  ``_persist_ib_cash_transactions`` helpers from ``xenon.jobs.ib_nav_flex``
  — the same code the daily ``xenon-nav-flex-refresh`` job runs. The saved
  daily query is a rolling ~2-week window and can never backfill history,
  which is why this one-shot exists.
* Anything else (Transfers, …) is counted as skipped.

``--dry-run`` parses the file and prints counts (NAV rows, cash rows, NAV
date range) with zero DB writes. ``--trades-csv <path>`` prints a
trade-history summary only — no PG write surface exists for historical
Flex trades today. No flags prints usage and exits 2. Writes refuse under
``XENON_READ_ONLY=1`` (exit 3), mirroring ``xenon-nav-flex-refresh``.
"""

from __future__ import annotations

import argparse
import csv
import io
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


def _parse_flex_csv(*, path: Path, scope: Any) -> dict[str, Any]:
    """Split the multi-section Flex CSV and classify each section by header.

    Returns NAV rows ready for ``upsert_nav_sync``, cash rows ready for
    ``_persist_ib_cash_transactions``, and skip counters. Sections whose
    header matches neither EquitySummaryInBase nor CashTransactions are
    counted in ``sections_skipped``; a CashTransactions-shaped header that
    fails the shared required-column validation lands in
    ``invalid_cash_sections`` and the caller refuses before any write.
    """
    from xenon.jobs.ib_nav_flex import (
        _cash_section_missing_columns,
        _parse_ib_cash_transactions_section,
        _split_ib_flex_csv_sections,
    )

    # utf-8-sig strips the BOM IB sometimes prepends to downloaded CSVs —
    # otherwise the first line's header check against "ClientAccountID" misses.
    sections = _split_ib_flex_csv_sections(path.read_text(encoding="utf-8-sig"))
    nav_rows: list[dict[str, Any]] = []
    cash_rows: list[dict[str, Any]] = []
    sections_skipped = 0
    skipped_mismatch = 0
    invalid_cash_sections: list[dict[str, Any]] = []
    for section_lines in sections:
        reader = csv.DictReader(io.StringIO("\n".join(section_lines)))
        field_names = reader.fieldnames or []
        if "ReportDate" in field_names and "Total" in field_names:
            for csv_row in reader:
                if csv_row.get("ClientAccountID", "").strip() != scope.broker_account:
                    skipped_mismatch += 1
                    continue
                total = _dec(csv_row["Total"])
                nav_rows.append(
                    {
                        "date": _parse_yyyymmdd(csv_row["ReportDate"]),
                        # nav column mirrors total for IB Flex (same convention
                        # as fetch_ib_nav_series — single authoritative source).
                        "nav": total,
                        "total": total,
                        "cash": _dec(csv_row["Cash"]),
                        "stock_value": _dec(csv_row["Stock"]),
                        "options_value": _dec(csv_row["Options"]),
                    }
                )
            continue
        missing = _cash_section_missing_columns(field_names)
        if missing is None:
            sections_skipped += 1
        elif missing:
            invalid_cash_sections.append(
                {"missing_columns": missing, "header": section_lines[0].strip()[:200]}
            )
        else:
            cash_rows.extend(
                _parse_ib_cash_transactions_section(
                    section_lines, account_filter=scope.broker_account
                )
            )
    return {
        "nav_rows": nav_rows,
        "cash_rows": cash_rows,
        "sections_skipped": sections_skipped,
        "skipped_account_mismatch": skipped_mismatch,
        "invalid_cash_sections": invalid_cash_sections,
    }


def ingest_nav_csv(*, path: Path, scope: Any, dry_run: bool = False) -> dict[str, Any]:
    """Ingest a Flex CSV export into nav_history (source='close') + ib_cash_flow.

    ``dry_run=True`` parses and reports counts only — zero DB writes. NAV
    writes go through ``upsert_nav_sync``; cash writes through the shared
    ``_persist_ib_cash_transactions`` (idempotent on the
    ``transaction_id`` primary key). A ``NavAccountEnvConflict`` from the
    env guard surfaces as ``{"error": "ENV_INDEX_CONFLICT", ...}`` like the
    old pre-flight did.
    """
    from xenon.api.services.futu_nav_persistence import NavAccountEnvConflict
    from xenon.jobs.ib_nav_flex import _persist_ib_cash_transactions
    from xenon.utils.portfolio_loader import upsert_nav_sync

    parsed = _parse_flex_csv(path=path, scope=scope)
    nav_rows = parsed["nav_rows"]
    cash_rows = parsed["cash_rows"]

    # min/max, not first/last — a descending or multi-section export must
    # still report the true coverage bounds the runbook verifies against.
    nav_dates = [r["date"] for r in nav_rows]
    result: dict[str, Any] = {
        "dry_run": dry_run,
        "nav_rows": len(nav_rows),
        "cash_flow_rows": len(cash_rows),
        "skipped_account_mismatch": parsed["skipped_account_mismatch"],
        "sections_skipped": parsed["sections_skipped"],
        "earliest_date": min(nav_dates).isoformat() if nav_dates else None,
        "latest_date": max(nav_dates).isoformat() if nav_dates else None,
    }
    if cash_rows:
        result["cash_earliest"] = min(r["occurred_at"] for r in cash_rows).isoformat()
        result["cash_latest"] = max(r["occurred_at"] for r in cash_rows).isoformat()

    # Refuse before ANY write (NAV or cash) when a CashTransactions-shaped
    # section is missing required columns — e.g. an export with Amount
    # deselected would upsert stored flows to zero on transaction_id replay.
    if parsed["invalid_cash_sections"]:
        return {
            **result,
            "error": "INVALID_CASH_SECTION",
            "invalid_cash_sections": parsed["invalid_cash_sections"],
        }
    if dry_run:
        return result

    nav_written = 0
    try:
        for r in nav_rows:
            upsert_nav_sync(
                scope=scope,
                day=r["date"],
                nav=r["nav"],
                total=r["total"],
                cash=r["cash"],
                stock_value=r["stock_value"],
                options_value=r["options_value"],
                source="close",
            )
            nav_written += 1
    except NavAccountEnvConflict as exc:
        return {
            **result,
            "error": "ENV_INDEX_CONFLICT",
            "detail": str(exc),
            "nav_rows_written_before_error": nav_written,
        }

    cash_inserted, cash_updated = _persist_ib_cash_transactions(cash_rows)
    return {
        **result,
        "nav_rows_written": nav_written,
        "cash_inserted": cash_inserted,
        "cash_updated": cash_updated,
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-csv", type=Path, help="Path to Flex CSV export (NAV + CashTransactions)")
    parser.add_argument("--trades-csv", type=Path, help="Path to trades CSV (summary only, no PG write)")
    parser.add_argument("--dry-run", action="store_true", help="Parse and report counts only — no DB writes")
    args = parser.parse_args()

    if not args.from_csv and not args.trades_csv:
        # The old no-flag path polled the Flex Web Service — a duplicate of
        # `xenon-nav-flex-refresh`. Removed; this script is CSV-only.
        parser.print_usage(sys.stderr)
        return 2

    _load_env()
    _ensure_broker_account_env()

    from xenon.execution.account_scope import resolve_from_env

    scope = resolve_from_env()
    print(f"scope: broker={scope.broker} env={scope.account_env} account={scope.broker_account}")

    if args.trades_csv:
        print(f"\n[trades] reading {args.trades_csv}")
        summary = summarize_trades_csv(path=args.trades_csv, scope=scope)
        for k, v in summary.items():
            print(f"  {k}: {v}")

    if args.from_csv:
        if not args.dry_run and os.environ.get("XENON_READ_ONLY") == "1":
            print(
                "READ_ONLY: XENON_READ_ONLY=1 — refusing to ingest NAV/cash rows. "
                "Use --dry-run to preview, or unset the flag to write.",
                file=sys.stderr,
            )
            return 3
        print(f"\n[nav] ingesting {args.from_csv}{' (dry-run)' if args.dry_run else ''}")
        result = ingest_nav_csv(path=args.from_csv, scope=scope, dry_run=args.dry_run)
        for k, v in result.items():
            print(f"  {k}: {v}")
        if "error" in result:
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
