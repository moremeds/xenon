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
    fetch_corp_actions_for_tickers,
    fetch_fills,
    insert_cash_flows,
    insert_corporate_actions,
    insert_fills,
    null_realized_for_ticker,
    update_realized_pnl,
    write_recon_anomaly,
)
from xenon.execution.account_scope import AccountScope
from xenon.execution.futu_fifo_matcher import (
    CorpActionInput,
    FillInput,
    match_fills,
)
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

    Three cases trigger rematch:

    1. **Never matched.** Ticker has fills but no row has realized_pnl.
       Happens on a first sync or after a manual NULL-out.

    2. **Late-arrival backfill.** A row exists whose `id` is newer than
       the newest matched-SELL's `id` (so it was inserted AFTER the FIFO
       pass) AND whose `filled_at` precedes that SELL's `filled_at` (so
       it would re-order the lot stack).

    3. **New corporate action.** A corporate_actions row exists for the
       ticker with effective_date >= the trade-date of the newest
       realized_pnl row. A freshly-pulled split would re-scale earlier
       lots, so realized_pnl on rows past the split date is stale (C28).

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
                    SELECT t.ticker,
                           MAX(t.id) FILTER (WHERE realized_pnl IS NOT NULL)
                                                       AS max_pnl_id,
                           MAX(t.filled_at)
                             FILTER (WHERE realized_pnl IS NOT NULL)
                                                       AS max_pnl_at,
                           (SELECT MAX(ca.effective_date)
                              FROM xenon.corporate_actions ca
                             WHERE ca.ticker = t.ticker)
                                                       AS last_corp_at
                      FROM xenon.futu_trades t
                     WHERE t.broker = :broker
                       AND t.account_env = :env
                       AND t.broker_account = :acc
                     GROUP BY t.ticker
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
                 OR (p.last_corp_at IS NOT NULL
                     AND (p.max_pnl_at IS NULL
                          OR p.last_corp_at >= (p.max_pnl_at AT TIME ZONE 'America/New_York')::date))
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

        # Step 4 prerequisite (C28): pull corp actions BEFORE rematch so
        # rematch sees split-aware lot accounting for any newly-pulled
        # corp action.
        pull_corporate_actions(scope, quote_ctx)

        # Step 5: recompute realized P&L for tickers needing rematch.
        # --rematch wipes _LOT_HISTORY_CACHE for the scope (C25 from Task 14).
        if rematch:
            for key in [
                k for k in _LOT_HISTORY_CACHE if k[:3] == (scope.broker, scope.account_env, scope.broker_account)
            ]:
                _LOT_HISTORY_CACHE.pop(key, None)
            with get_sync_engine().connect() as conn:
                tickers = {
                    r[0]
                    for r in conn.execute(
                        text(
                            "SELECT DISTINCT ticker FROM xenon.futu_trades "
                            "WHERE broker=:b AND account_env=:e AND broker_account=:a"
                        ),
                        {
                            "b": scope.broker,
                            "e": scope.account_env,
                            "a": scope.broker_account,
                        },
                    ).fetchall()
                }
        else:
            tickers = detect_tickers_needing_rematch(scope)
        for t in tickers:
            _rematch_ticker(scope, t)

        # Step 6: snapshot current account.
        snapshot_account_state(scope, trade_ctx)

        # Step 7 (NAV backwalk / daily forward UPSERT) — filled by Task 14.
        warnings.append("step_7_nav_walk_not_yet_implemented")

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


# C19: module-level cache of per-ticker FIFO lot history, used by the
# backward NAV walk in Task 14 to recover state when a ticker was closed
# and reopened (qty=0 mid-walk). Keyed by
# (broker, account_env, broker_account, ticker). Cleared by --rematch (C25).
_LOT_HISTORY_CACHE: dict[tuple[str, str, str, str], list[dict]] = {}


def _rematch_ticker(scope: AccountScope, ticker: str) -> None:
    """Re-run FIFO over the full history for this ticker.

    Writes realized_pnl back to each SELL row, AND populates
    _LOT_HISTORY_CACHE for the backward NAV walk to consume
    (spec § Failure modes: ticker-reopen).
    """
    null_realized_for_ticker(scope, ticker)
    fills_raw = [r for r in fetch_fills(scope, MIN_HISTORICAL_DATE, date(2099, 12, 31)) if r["ticker"] == ticker]
    fills = [
        FillInput(
            futu_deal_id=r["futu_deal_id"],
            action=r["action"],
            quantity=int(r["quantity"]),
            price=Decimal(str(r["price"])),
            fees=Decimal(str(r["fees"])),
            filled_at=r["filled_at"],
        )
        for r in fills_raw
    ]
    corp = [
        CorpActionInput(
            id=f"{ca['ticker']}-{ca['effective_date']}",
            action_type=ca["action_type"],
            effective_date=ca["effective_date"],
            ratio_numerator=Decimal(str(ca["ratio_numerator"] or 1)),
            ratio_denominator=Decimal(str(ca["ratio_denominator"] or 1)),
        )
        for ca in fetch_corp_actions_for_tickers([ticker])
    ]
    # Request the side-channel lot_history — Task 14's backwalk consumes it.
    result = match_fills(fills, corp_actions=corp, return_lot_history=True)
    _LOT_HISTORY_CACHE[(scope.broker, scope.account_env, scope.broker_account, ticker)] = result.lot_history
    for f in result.fills:
        if f.action == "SELL":
            update_realized_pnl(scope, f.futu_deal_id, f.realized_pnl)


def snapshot_account_state(scope: AccountScope, trade_ctx) -> None:
    """Pull live accinfo + positions and write one row to xenon.account_snapshots.

    Futu's raw accinfo_query returns `total_assets` (not `net_liquidation` —
    C26). We map it to the canonical xenon column name `net_liquidation`
    and mirror into `bankroll` (used by sizing helpers).
    """
    import json

    code, accinfo_df = trade_ctx.accinfo_query(acc_id=scope.broker_account, trd_env="REAL")
    if code != 0:
        raise RuntimeError(f"accinfo_query failed: {accinfo_df!r}")
    code, pos_df = trade_ctx.position_list_query(acc_id=scope.broker_account, trd_env="REAL")
    if code != 0:
        raise RuntimeError(f"position_list_query failed: {pos_df!r}")

    summary = accinfo_df.to_dict(orient="records")[0]
    raw_total = summary.get("total_assets")
    if raw_total is None:
        raise RuntimeError(
            "Futu accinfo_query returned no `total_assets` column — "
            "verify SDK version against C26 (run the Task 5 Step 0 probe)"
        )
    nl = Decimal(str(raw_total))
    positions: list[dict] = []
    for r in pos_df.to_dict(orient="records"):
        positions.append(
            {
                "code": r.get("code"),
                "qty": float(r.get("qty", 0)),
                "cost_price": float(r.get("cost_price", 0)),
                "nominal_price": float(r.get("nominal_price", 0)),
                "market_value": float(r.get("market_val", 0)),
            }
        )
    payload = {"account_summary": summary, "positions": positions}

    with get_sync_engine().begin() as conn:
        prev_peak = conn.execute(
            text(
                "SELECT MAX(peak_value) FROM xenon.account_snapshots "
                "WHERE broker=:b AND account_env=:e AND broker_account=:a"
            ),
            {
                "b": scope.broker,
                "e": scope.account_env,
                "a": scope.broker_account,
            },
        ).scalar() or Decimal("0")
        peak = max(Decimal(str(prev_peak)), nl)
        # C3: account_snapshots.account is NOT NULL with no default.
        # Bind it to scope.broker_account (the same value as the broker_account
        # column — they coexist for cross-broker query compatibility).
        conn.execute(
            text(
                """
                INSERT INTO xenon.account_snapshots (
                    account, broker, account_env, broker_account,
                    net_liquidation, bankroll, peak_value, payload, snapshot_at)
                VALUES (:acc, :broker, :env, :acc, :nl, :nl, :peak,
                        CAST(:payload AS jsonb), now())
                """
            ),
            {
                "broker": scope.broker,
                "env": scope.account_env,
                "acc": scope.broker_account,
                "nl": nl,
                "peak": peak,
                "payload": json.dumps(payload, default=str),
            },
        )


def pull_corporate_actions(scope: AccountScope, quote_ctx) -> int:
    """For every ticker ever held in this scope, call get_rehab and persist.

    Per-ticker failures are logged + recorded as anomalies; they do NOT
    abort the sync — a single corrupt or rate-limited ticker should not
    block the whole window's worth of trade data.
    """
    from xenon.clients.futu_quote_client import FutuQuoteClient

    client = FutuQuoteClient(quote_ctx=quote_ctx)
    with get_sync_engine().connect() as conn:
        tickers = [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT DISTINCT ticker FROM xenon.futu_trades "
                    "WHERE broker=:b AND account_env=:e AND broker_account=:a"
                ),
                {
                    "b": scope.broker,
                    "e": scope.account_env,
                    "a": scope.broker_account,
                },
            ).fetchall()
        ]

    rows: list[dict] = []
    for ticker in tickers:
        try:
            actions = list(client.fetch_corporate_actions(f"US.{ticker}"))
        except Exception as exc:  # noqa: BLE001
            log.warning("get_rehab(%s) failed: %s", ticker, exc)
            write_recon_anomaly(
                scope,
                datetime.now(timezone.utc).date(),
                notes={
                    "anomaly_type": "missing_rehab",
                    "ticker": ticker,
                    "error": str(exc),
                },
            )
            continue
        for ca in actions:
            rows.append(
                {
                    "ticker": ca.ticker,
                    "action_type": ca.action_type,
                    "effective_date": ca.effective_date,
                    "ratio_numerator": ca.ratio_numerator,
                    "ratio_denominator": ca.ratio_denominator,
                    "new_ticker": ca.new_ticker,
                    "notes": {"raw": ca.raw},
                }
            )
    return insert_corporate_actions(rows)
