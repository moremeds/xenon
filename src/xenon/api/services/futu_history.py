"""Futu-history sync orchestrator (Tasks 10-14).

One idempotent function — futu_history_sync(scope, since) — drives both
first-run backfill and daily-forward UPSERT. Invocation paths:
  - CLI: scripts/futu_history_sync.py (M6)
  - FastAPI: POST /futu/history/sync (M7)
  - launchd: 16:30 ET daily (M6)

Singleflight via pg_try_advisory_lock(LOCK_KEY_FUTU_HISTORY_SYNC = 7343001).
A second concurrent invocation returns status='in_progress' instead of
contending — only one worker actually pulls.

Idempotency model:
  - Each insert uses ON CONFLICT DO NOTHING. Re-running the same window
    is a no-op at the row level.
  - Each insert commits in its own transaction (no single wrapping txn),
    so a crash mid-batch leaves prior batches durable; the next run
    finishes the tail without duplicating earlier rows.

Step order (spec § 5.3):
  1. compute [start, today] from cursors (with 1-day overlap)
  1.5. ensure FX coverage in fx_daily for [start, today]  (C27)
  2. pull fills (deal_list_query)
  3. pull cash flow (acc_cash_flow) — needs FX from 1.5
  4. pull corporate actions (get_rehab) — Task 13, before rematch (C28)
  5. rematch tickers (FIFO) — Task 12, consumes corp actions
  6. snapshot account state — Task 12
  7. NAV backwalk (first run) or write_today_reported_nav (incremental) — Task 14
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from xenon.api.services.advisory_lock import (
    LOCK_KEY_FUTU_HISTORY_SYNC,
    try_advisory_lock,
)
from xenon.clients.futu_history_fetcher import FutuHistoryFetcher
from xenon.db.engine import get_sync_engine
from xenon.db.queries.futu_history import (
    cursor_for_cash_flows,
    cursor_for_corp_actions,
    cursor_for_fills,
    insert_cash_flows,
    insert_fills,
    write_recon_anomaly,
)
from xenon.execution.account_scope import AccountScope
from xenon.utils.futu_fx import (
    FxBackfillFailed,
    FxRateMissing,
    backfill_fx_series,
    convert_to_usd,
)

log = logging.getLogger(__name__)

MIN_HISTORICAL_DATE = date(2024, 5, 1)


@dataclass
class SinceUnparseable(Exception):
    reason_code: str = "since_unparseable"
    bad_value: str = ""


@dataclass
class SinceOutOfRange(Exception):
    reason_code: str = "since_out_of_range"
    bad_value: str = ""


def parse_since(since: str | None) -> date | None:
    """Return the start date or None for 'auto'. Raises structured errors.

    Upper bound uses America/New_York date — UTC would let `since=tomorrow_ET`
    slip through between 19:00 ET and 04:00 ET on the boundary day.
    """
    from zoneinfo import ZoneInfo

    if since is None or since == "auto":
        return None
    try:
        d = date.fromisoformat(since)
    except ValueError as exc:
        raise SinceUnparseable(bad_value=since) from exc
    today_et = datetime.now(ZoneInfo("America/New_York")).date()
    if d < MIN_HISTORICAL_DATE or d > today_et:
        raise SinceOutOfRange(bad_value=since)
    return d


# Futu cashflow_type -> canonical bucket. Real OpenD has emitted at least
# 'IPO Subscription Return' (verified 2026-06-01 C23 probe), which doesn't
# match any enum key. The classifier is therefore liberal: exact upper-case
# match first, then substring fallback. Anything that doesn't match falls
# through to 'other' — which is permitted by ck_futu_cash_flow_type.
_FLOW_TAXONOMY = {
    "FUND_IN": "deposit",
    "FUND_OUT": "withdraw",
    "DIVIDEND": "dividend",
    "INTEREST": "interest",
    "COMMISSION": "fee",
    "PLATFORM_FEE": "fee",
    "CLEARING_FEE": "fee",
}

_FLOW_SUBSTRINGS: tuple[tuple[str, str], ...] = (
    ("dividend", "dividend"),
    ("interest", "interest"),
    ("commission", "fee"),
    ("clearing", "fee"),
    ("platform fee", "fee"),
    ("deposit", "deposit"),
    ("withdraw", "withdraw"),
    ("fund in", "deposit"),
    ("fund out", "withdraw"),
)


def _normalize_flow_type(raw: str) -> str:
    """Canonical bucket name for a Futu cashflow_type string.

    Liberal classifier — see _FLOW_TAXONOMY docstring.
    """
    if not raw:
        return "other"
    upper = raw.strip().upper()
    if upper in _FLOW_TAXONOMY:
        return _FLOW_TAXONOMY[upper]
    lower = raw.strip().lower()
    for needle, bucket in _FLOW_SUBSTRINGS:
        if needle in lower:
            return bucket
    return "other"


def _build_trade_ctx():  # pragma: no cover — exercised only against live OpenD
    import os

    from futu import OpenSecTradeContext, SecurityFirm, TrdMarket

    return OpenSecTradeContext(
        filter_trdmarket=TrdMarket.US,
        host=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"),
        port=int(os.environ.get("FUTU_OPEND_PORT", "11111")),
        security_firm=SecurityFirm.FUTUSECURITIES,
    )


def _build_quote_ctx():  # pragma: no cover
    import os

    from futu import OpenQuoteContext

    return OpenQuoteContext(
        host=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"),
        port=int(os.environ.get("FUTU_OPEND_PORT", "11111")),
    )


def convert_cash_flow_amount(scope: AccountScope, row: dict) -> tuple[Decimal | None, Decimal | None]:
    """Return (amount_usd, fx_rate_used).

    USD passthrough returns the original amount and a None rate (no
    conversion applied). For HKD/CNY/etc., looks up fx_daily; on miss,
    writes an anomaly row to futu_reconciliation_log and returns (None, None)
    so the caller stores the cash-flow row with amount=NULL.
    """
    currency = row["original_currency"]
    if currency == "USD":
        return row["amount_original"], None
    flow_date = row["flow_at"].date()
    try:
        amount = convert_to_usd(row["amount_original"], currency, flow_date)
    except FxRateMissing:
        write_recon_anomaly(
            scope,
            flow_date,
            notes={
                "anomaly_type": "missing_fx",
                "currency": currency,
                "flow_id": row["futu_flow_id"],
            },
        )
        return None, None
    # Effective rate = original / converted. Skip pathological zero so a
    # malformed flow doesn't blow up the whole pull with DivisionByZero.
    if amount and amount != 0:
        fx_rate = (row["amount_original"] / amount).quantize(Decimal("0.000001"))
    else:
        fx_rate = None
    return amount, fx_rate


def _pull_fills(scope: AccountScope, trade_ctx, start: date, end: date) -> int:
    fetcher = FutuHistoryFetcher(trade_ctx=trade_ctx, trd_env="REAL")
    rows: list[dict] = []
    for fill in fetcher.fetch_deals(start, end, acc_id=scope.broker_account):
        rows.append(
            {
                "futu_deal_id": fill.futu_deal_id,
                "futu_order_id": fill.futu_order_id,
                "ticker": fill.ticker,
                "futu_code": fill.futu_code,
                "market": fill.market,
                "action": fill.action,
                "quantity": fill.quantity,
                "price": fill.price,
                "fees": fill.fees,
                "filled_at": fill.filled_at,
                "raw": fill.raw,
            }
        )
    if not rows:
        return 0
    return insert_fills(scope, rows)


def _pull_cash_flow(scope: AccountScope, trade_ctx, start: date, end: date) -> int:
    fetcher = FutuHistoryFetcher(trade_ctx=trade_ctx, trd_env="REAL")
    rows: list[dict] = []
    for cf in fetcher.fetch_cash_flow(start, end, acc_id=scope.broker_account):
        normalized = {
            "futu_flow_id": cf.futu_flow_id,
            "futu_type_raw": cf.futu_type_raw,
            "amount_original": cf.amount_original,
            "original_currency": cf.original_currency,
            "flow_at": cf.flow_at,
            "ticker": cf.ticker,
            "description": cf.description,
            "raw": cf.raw,
        }
        amount, fx_rate = convert_cash_flow_amount(scope, normalized)
        rows.append(
            {
                **normalized,
                "amount": amount,
                "fx_rate_used": fx_rate,
                "flow_type": _normalize_flow_type(cf.futu_type_raw),
            }
        )
    if not rows:
        return 0
    return insert_cash_flows(scope, rows)


def sum_cash_flow_for_date(scope: AccountScope, d: date) -> Decimal:
    """Sum cash-flow amounts for an ET trade date, EXCLUDING fee rows whose
    raw.deal_id resolves to an existing futu_trades.futu_deal_id.

    The exclusion runs at query time, not ingest time, so dedup is
    order-of-arrival independent. Used by the NAV reconstruction math —
    fees on a fill row are already captured in futu_trades.fees, so we
    must not double-count them via the cash-flow stream.
    """
    with get_sync_engine().connect() as conn:
        result = conn.execute(
            text(
                """
                SELECT COALESCE(SUM(cf.amount), 0)
                  FROM xenon.futu_cash_flow cf
                 WHERE cf.broker = :broker
                   AND cf.account_env = :env
                   AND cf.broker_account = :acc
                   AND (cf.flow_at AT TIME ZONE 'America/New_York')::date = :d
                   AND NOT EXISTS (
                       SELECT 1
                         FROM xenon.futu_trades t
                        WHERE t.broker = cf.broker
                          AND t.account_env = cf.account_env
                          AND t.broker_account = cf.broker_account
                          AND t.futu_deal_id = (cf.raw->>'deal_id')
                   )
                """
            ),
            {
                "broker": scope.broker,
                "env": scope.account_env,
                "acc": scope.broker_account,
                "d": d,
            },
        ).scalar()
    return Decimal(str(result or 0))


def detect_tickers_needing_rematch(scope: AccountScope) -> set[str]:
    """Return the set of tickers whose realized_pnl is stale.

    Two cases trigger rematch:

    1. **Never matched.** Ticker has fills but no row has realized_pnl.
       Happens on a first sync or after a manual NULL-out.

    2. **Late-arrival backfill.** A row exists whose `id` is newer than
       the newest matched-SELL's `id` (so it was inserted AFTER the FIFO
       pass) AND whose `filled_at` precedes that SELL's `filled_at` (so
       it would re-order the lot stack).

    The schema has no `created_at` column, so the auto-increment `id`
    serves as a proxy for insertion order. New BUYs always get higher
    ids; a late-arriving older BUY shows up with id > max_matched_id
    but filled_at < max_matched_filled_at.
    """
    with get_sync_engine().connect() as conn:
        rows = conn.execute(
            text(
                """
                WITH per_ticker AS (
                    SELECT ticker,
                           MAX(id) FILTER (WHERE realized_pnl IS NOT NULL)
                                                       AS max_pnl_id,
                           MAX(filled_at)
                             FILTER (WHERE realized_pnl IS NOT NULL)
                                                       AS max_pnl_at
                      FROM xenon.futu_trades
                     WHERE broker = :broker
                       AND account_env = :env
                       AND broker_account = :acc
                     GROUP BY ticker
                )
                SELECT DISTINCT p.ticker
                  FROM per_ticker p
                 WHERE EXISTS (
                    SELECT 1 FROM xenon.futu_trades t
                     WHERE t.broker = :broker
                       AND t.account_env = :env
                       AND t.broker_account = :acc
                       AND t.ticker = p.ticker
                       AND (
                           p.max_pnl_id IS NULL
                           OR (t.id > p.max_pnl_id
                               AND t.filled_at < p.max_pnl_at)
                       )
                 )
                """
            ),
            {
                "broker": scope.broker,
                "env": scope.account_env,
                "acc": scope.broker_account,
            },
        ).fetchall()
    return {r[0] for r in rows}


def futu_history_sync(
    scope: AccountScope,
    since: date | None,
    rematch: bool = False,
    until_date: date | None = None,
) -> dict[str, Any]:
    """Idempotent orchestrator. Returns a JSON-serializable result dict.

    On concurrent invocation, the second caller returns
    status='in_progress' without contending for OpenD.
    """
    eng = get_sync_engine()
    with eng.connect() as conn:
        with try_advisory_lock(conn, LOCK_KEY_FUTU_HISTORY_SYNC) as locked:
            if not locked:
                log.info("futu_history_sync: another run holds the lock; skipping")
                return {
                    "status": "in_progress",
                    "fills_added": 0,
                    "cash_flows_added": 0,
                    "nav_rows_added": 0,
                    "warnings": [],
                }
            return _run_sync_under_lock(
                scope=scope,
                since=since,
                rematch=rematch,
                until_date=until_date,
            )


def _resolve_window(scope: AccountScope, since: date | None, until_date: date | None) -> tuple[date, date]:
    """Resolve [start, today] from cursors with a 1-day overlap (C31).

    Filter `None` cursors so a missing-corp-actions cursor doesn't silently
    force a full replay every day. The `-1` overlap re-pulls the previous
    boundary to catch same-window late arrivals around settlement.
    """
    today = until_date or datetime.now(timezone.utc).date()
    if since is not None:
        return since, today
    cursor_fills = cursor_for_fills(scope)
    cursor_flows = cursor_for_cash_flows(scope)
    cursor_corp = cursor_for_corp_actions(scope)
    cursors = [c for c in (cursor_fills, cursor_flows, cursor_corp) if c is not None]
    if cursors:
        start = max(MIN_HISTORICAL_DATE, min(cursors) - timedelta(days=1))
    else:
        start = MIN_HISTORICAL_DATE
    return start, today


def _run_sync_under_lock(
    scope: AccountScope,
    since: date | None,
    rematch: bool,
    until_date: date | None,
) -> dict[str, Any]:
    """Orchestrate the full sync under the advisory lock.

    Contexts are explicitly closed in finally to avoid leaking TCP
    sockets to Futu OpenD across launchd invocations (C17).
    """
    start, today = _resolve_window(scope, since, until_date)

    trade_ctx = _build_trade_ctx()
    quote_ctx = _build_quote_ctx()
    warnings: list[str] = []
    try:
        # Step 1.5: ensure FX coverage for the window BEFORE pulling cash
        # flow. On a fresh DB this populates fx_daily for HKD/CNY across
        # [start, today] in one FRED hit. Idempotent: ON CONFLICT DO UPDATE
        # inside backfill_fx_series.
        try:
            for currency in ("HKD", "CNY"):
                backfill_fx_series(currency, start, today)
        except FxBackfillFailed as exc:
            return {
                "status": "error",
                "reason_code": exc.reason_code,
                "fills_added": 0,
                "cash_flows_added": 0,
                "nav_rows_added": 0,
                "warnings": [str(exc)],
            }

        # Step 2: fills
        fills_added = _pull_fills(scope, trade_ctx, start, today)
        # Step 3: cash flow (FX-aware)
        cash_flows_added = _pull_cash_flow(scope, trade_ctx, start, today)

        # Steps 4-7 (corp actions, rematch, snapshot, NAV walk) live in
        # Tasks 12, 13, 14. Their orchestration MUST: pull corp actions
        # BEFORE rematch (C28); --rematch wipes historical reconstructed
        # rows + _LOT_HISTORY_CACHE before walking (C25); first sync writes
        # the cost-basis anchor as a recon-log row, not into nav_history
        # (C24).
        warnings.append("steps_4_to_7_not_yet_implemented")

        return {
            "status": "ok",
            "fills_added": fills_added,
            "cash_flows_added": cash_flows_added,
            "nav_rows_added": 0,  # filled in Task 14
            "warnings": warnings,
            "started_at": start.isoformat(),
            "completed_at": today.isoformat(),
        }
    finally:
        # C17: close OpenD contexts so launchd doesn't leak TCP sockets.
        try:
            trade_ctx.close()
        except Exception:  # noqa: BLE001
            log.warning("trade_ctx.close failed", exc_info=True)
        try:
            quote_ctx.close()
        except Exception:  # noqa: BLE001
            log.warning("quote_ctx.close failed", exc_info=True)
