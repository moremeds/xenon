"""Order entry: place / cancel / replace / modify cores behind the HTTP routes and the combo wizard.

Moved from server.py (PR-20). Every core starts with ensure_order_allowed(); orders run on the single
order owner (server.order_owner). Process-wide state (app, ib_pool, order_owner) stays in server.py and is
read at call time.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from ib_async import Contract, Option, Stock
from pydantic import ValidationError
from sqlalchemy import select

from xenon.api.guards import ensure_order_allowed
from xenon.api.order_owner import BROKER_DISCONNECTED, ORDERS_HALTED
from xenon.clients.ib_client import pool_client_id
from xenon.db.engine import get_sync_engine
from xenon.db.queries.portfolio import DISPLAY_SNAPSHOT
from xenon.db.schema import account_snapshots
from xenon.execution import gate4, ib_order_manage, ib_place_order, orders_store, preflight, quote_guard, quote_tokens
from xenon.execution.account_scope import resolve_from_app_state
from xenon.execution.preflight import PortfolioView, PreflightRequest, ReasonCode, Verdict
from xenon.execution.quote_tokens import QuotePayload
from xenon.utils import market_calendar

logger = logging.getLogger("xenon.api")


# ---------------------------------------------------------------------------
# Phase 3: IB order operations
# ---------------------------------------------------------------------------


def _load_portfolio_view_sync() -> tuple[PortfolioView | None, datetime | None]:
    """Load the latest scoped portfolio snapshot from Postgres for preflight."""
    scope = _resolve_scope_kwargs()
    try:
        engine = get_sync_engine()
        stmt = (
            select(account_snapshots.c.payload, account_snapshots.c.snapshot_at)
            .where(account_snapshots.c.broker == scope["broker"])
            .where(account_snapshots.c.account_env == scope["account_env"])
            .where(account_snapshots.c.broker_account == scope["broker_account"])
            .where(DISPLAY_SNAPSHOT)
            .order_by(account_snapshots.c.snapshot_at.desc())
            .limit(1)
        )
        with engine.connect() as con:
            row = con.execute(stmt).first()
        if row is None or not row.payload:
            return None, None
        return PortfolioView.model_validate(dict(row.payload)), row.snapshot_at
    except (ValueError, ValidationError) as exc:
        logger.warning("[preflight] Could not validate Postgres portfolio snapshot: %s", exc)
        return None, None
    except Exception as exc:
        logger.warning("[preflight] Could not load Postgres portfolio snapshot: %s", exc)
        return None, None


async def _load_portfolio_view() -> tuple[PortfolioView | None, datetime | None]:
    return await asyncio.to_thread(_load_portfolio_view_sync)


def _unpack_portfolio_load(loaded) -> tuple[PortfolioView | None, datetime | None]:
    if isinstance(loaded, tuple) and len(loaded) == 2:
        return loaded
    return loaded, None


def _portfolio_snapshot_stale_response(snapshot_at: datetime | None) -> Verdict | None:
    if snapshot_at is None:
        return None
    if snapshot_at.tzinfo is None:
        snapshot_at = snapshot_at.replace(tzinfo=timezone.utc)
    market_open = market_calendar.is_open(
        market_calendar.now_et(), strict=False, close_inclusive_minute=True
    )
    threshold_env = "XENON_PORTFOLIO_SNAPSHOT_STALE_S" if market_open else "XENON_PORTFOLIO_SNAPSHOT_STALE_CLOSED_S"
    default_seconds = "300" if market_open else "1800"
    try:
        threshold_seconds = int(os.environ.get(threshold_env, default_seconds))
    except ValueError:
        threshold_seconds = int(default_seconds)
    age_seconds = (datetime.now(timezone.utc) - snapshot_at).total_seconds()
    if age_seconds <= threshold_seconds:
        return None
    return Verdict(
        accept=False,
        reason_code=ReasonCode.PORTFOLIO_SNAPSHOT_STALE,
        reason_detail=(
            f"Portfolio snapshot is stale ({int(age_seconds)}s old; "
            f"threshold {threshold_seconds}s). Sync portfolio before submitting SELL exposure."
        ),
    )


def _body_to_preflight_request(body: dict) -> PreflightRequest:
    """Translate non-combo /orders/place bodies to PreflightRequest."""
    from xenon.execution.universe import UNIVERSE, get_multiplier

    sec_type = "STK" if body.get("type") == "stock" else "OPT"
    right_raw = (body.get("right") or "").upper()
    right = right_raw if right_raw in ("C", "P") else None
    limit = body.get("limitPrice")
    ticker = str(body.get("symbol", "")).upper()
    # SECURITY: multiplier MUST come from server-side universe metadata, never
    # from request body. A client posting `multiplier: 1` would otherwise
    # inflate share-cover units 100x and bypass Gate 4. Unknown tickers fall
    # back to 100 so the universe check can produce UNIVERSE_UNKNOWN rather
    # than a KeyError.
    multiplier = get_multiplier(ticker) if ticker in UNIVERSE else 100
    return PreflightRequest(
        ticker=ticker,
        security_type=sec_type,
        action=str(body.get("action", "")).upper(),
        quantity=int(body.get("quantity", 0)),
        right=right,
        expiry=body.get("expiry"),
        strike=Decimal(str(body["strike"])) if body.get("strike") is not None else None,
        multiplier=multiplier,
        limit_price=Decimal(str(limit)) if limit is not None else Decimal("0"),
        currency=str(body.get("currency") or "USD").upper(),
        exchange=body.get("exchange"),
    )


def _body_to_combo_preflight_request(body: dict) -> preflight.ComboPreflightRequest:
    from xenon.execution.universe import UNIVERSE, get_multiplier

    ticker = str(body.get("symbol", "")).upper()
    multiplier = get_multiplier(ticker) if ticker in UNIVERSE else 100
    legs = []
    for leg in body.get("legs") or []:
        right_raw = str(leg.get("right") or "").upper()
        if right_raw not in ("C", "P"):
            raise ValueError(f"combo leg right must be C or P, got {right_raw!r}")
        legs.append(
            preflight.ComboPreflightLeg(
                expiry=leg.get("expiry"),
                strike=Decimal(str(leg["strike"])) if leg.get("strike") is not None else None,
                right=right_raw,
                action=str(leg.get("action", "")).upper(),
                ratio=int(leg.get("ratio") or 1),
            )
        )
    return preflight.ComboPreflightRequest(
        ticker=ticker,
        action=str(body.get("action", "")).upper(),
        quantity=int(body.get("quantity", 0)),
        multiplier=multiplier,
        legs=legs,
    )


def _normalize_gate_expiry(expiry: Any) -> str | None:
    if not expiry:
        return None
    clean = str(expiry).replace("-", "")
    return clean if len(clean) == 8 and clean.isdigit() else None


def _portfolio_has_matching_long_option(
    portfolio: PortfolioView,
    *,
    ticker: str,
    expiry: str | None,
    strike: Decimal | None,
    right: str | None,
    quantity: int,
) -> bool:
    if strike is None or right not in {"C", "P"}:
        return False
    expected_type = "Call" if right == "C" else "Put"
    wanted_expiry = _normalize_gate_expiry(expiry)
    if wanted_expiry is None:
        return False
    held = 0
    for pos in portfolio.positions:
        if pos.ticker.upper() != ticker.upper():
            continue
        if _normalize_gate_expiry(pos.expiry) != wanted_expiry:
            continue
        for leg in pos.legs:
            if leg.direction == "LONG" and leg.type == expected_type and leg.strike == strike:
                held += int(leg.contracts)
    return held >= quantity


# TIFs the place flow supports end to end (web OrderTif / placeOrderBodySchema;
# the place CLI passes the value to IB LimitOrder unchanged).
_PLACE_TIFS = frozenset({"DAY", "GTC"})


def _canonical_tif(raw) -> str:
    """Strip + upper; None / "" → DAY (the CLI default). Raises ValueError
    for anything outside _PLACE_TIFS."""
    if raw is None:
        return "DAY"
    if not isinstance(raw, str):
        raise ValueError(f"tif must be one of {sorted(_PLACE_TIFS)}")
    tif = raw.strip().upper() or "DAY"
    if tif not in _PLACE_TIFS:
        raise ValueError(f"tif must be one of {sorted(_PLACE_TIFS)}, got {raw!r}")
    return tif


def _invalid_order_body_response(exc: Exception) -> Verdict:
    return Verdict(
        accept=False,
        reason_code=ReasonCode.INVALID_ORDER_BODY,
        reason_detail=str(exc),
    )


def _portfolio_required_response(body: dict) -> Verdict | None:
    order_type = body.get("type")
    action = str(body.get("action", "")).upper()
    if order_type == "combo":
        req = _body_to_combo_preflight_request(body)
        if preflight.combo_uncovered_short_call_ratio(req) > 0:
            return Verdict(
                accept=False,
                reason_code=ReasonCode.PORTFOLIO_SNAPSHOT_REQUIRED,
                reason_detail="Portfolio snapshot required to verify combo short-call coverage",
            )
        return None
    if action != "SELL":
        return None
    if order_type == "option" and str(body.get("right", "")).upper() == "P":
        return None
    return Verdict(
        accept=False,
        reason_code=ReasonCode.PORTFOLIO_SNAPSHOT_REQUIRED,
        reason_detail="Portfolio snapshot required to verify short exposure",
    )


def _universe_precheck(body: dict) -> Verdict | None:
    """Pure-metadata Gate-4 checks that must precede the reservation.

    Universe/index rules are request-shape validation — no portfolio data
    needed — so they run before the advisory-locked coverage transaction.
    """
    from xenon.execution.universe import is_index, is_known

    ticker = str(body.get("symbol", "")).upper()
    currency = str(body.get("currency") or "USD").upper()
    # Non-USD cash equities sit outside the V1 universe by definition; they
    # gate on currency instead (same carve-out as the legacy preflight).
    is_foreign_equity = body.get("type") == "stock" and currency != "USD"
    if not is_foreign_equity and not is_known(ticker):
        return Verdict(
            accept=False,
            reason_code=ReasonCode.UNIVERSE_UNKNOWN,
            reason_detail=f"{ticker} not in V1 universe",
        )
    # is_index() raises KeyError for non-universe tickers — the is_known
    # early return above guarantees this only runs for known USD tickers.
    if body.get("type") == "stock" and not is_foreign_equity and is_index(ticker):
        return Verdict(
            accept=False,
            reason_code=ReasonCode.INDEX_HAS_NO_STOCK,
            reason_detail=f"{ticker} is an index — no stock leg exists",
        )
    return None


def _preflight_block_response(verdict: Verdict) -> JSONResponse:
    code = verdict.reason_code.value if verdict.reason_code else None
    # `detail` is the field web/lib/xenonApi.ts:39 reads for human-readable
    # error copy; the reason_code fields drive structured UI toast mapping.
    return JSONResponse(
        status_code=400,
        content={
            "detail": verdict.reason_detail or code or "Preflight blocked",
            "reason_code": code,
            "reason_detail": verdict.reason_detail,
        },
    )


# How long a cancel/modify request waits for the order owner's worker. After
# it the request answers (unsent if the job never started, else unconfirmed)
# and a started job keeps running and settles Gate 4 itself. Gate 4 expiry
# does not depend on it: the send_by fence is checked right before the send.
_ORDER_ACTION_TIMEOUT_S = 15


def _gate4_reject_verdict(
    reasons: list[str],
    uncovered: dict | None,
    short_stock: dict | None,
    split_required: bool = False,
) -> Verdict:
    """Map a coverage rejection to the route's reason-code contract.

    Infrastructure failures (view/scope) take precedence — a classifiable
    coverage breach computed on unprovable data is still reported, but the
    integrity problem is the headline. `split_required` marks a guaranteed
    BAG that only fails leg-by-leg — the operator sends the legs as
    separate orders, BUY leg first.
    """
    from xenon.execution.universe import is_index

    if "view_incomplete" in reasons:
        return Verdict(
            accept=False,
            reason_code=ReasonCode.PORTFOLIO_SNAPSHOT_REQUIRED,
            reason_detail="Portfolio snapshot required to verify short exposure",
        )
    if "view_stale" in reasons:
        return Verdict(
            accept=False,
            reason_code=ReasonCode.PORTFOLIO_SNAPSHOT_STALE,
            reason_detail="Portfolio snapshot is stale; sync before submitting SELL exposure",
        )
    if any(r.startswith("candidate_unknown_metadata") and r.rsplit(":", 1)[-1] in ("mult", "tc") for r in reasons):
        # The candidate's contract could not be qualified at IB (conId /
        # tradingClass / multiplier / underlying type) — never guessed.
        # Other leg problems (right/action/exp/…) are body defects and fall
        # through to INVALID_ORDER_BODY below.
        return Verdict(
            accept=False,
            reason_code=ReasonCode.GATE4_UNRESOLVED,
            reason_detail="Contract metadata unavailable from IB; cannot verify cover. " + "; ".join(reasons),
        )
    if split_required:
        return Verdict(
            accept=False,
            reason_code=ReasonCode.COMBO_SPLIT_REQUIRED,
            reason_detail="Combo legs are checked one by one while netting is off. Send the BUY leg as its own order first; send the SELL leg after the BUY fills and the portfolio syncs.",
        )
    if short_stock:
        sym = sorted(short_stock)[0]
        return Verdict(
            accept=False,
            reason_code=ReasonCode.INSUFFICIENT_SHARES,
            reason_detail=f"Insufficient shares to cover stock sell in {sym}",
        )
    if uncovered:
        key = sorted(uncovered, key=str)[0]
        sym = key[0] if isinstance(key, tuple) else str(key)
        is_idx = False
        try:
            is_idx = bool(is_index(sym))
        except KeyError:
            is_idx = False
        code = ReasonCode.INDEX_CALL_UNCOVERED if is_idx else ReasonCode.ETF_CALL_UNCOVERED
        return Verdict(
            accept=False,
            reason_code=code,
            reason_detail=f"Uncovered short call on {sym}",
        )
    if any(r.startswith("candidate_") for r in reasons):
        return Verdict(
            accept=False,
            reason_code=ReasonCode.INVALID_ORDER_BODY,
            reason_detail="; ".join(reasons),
        )
    return Verdict(
        accept=False,
        reason_code=ReasonCode.GATE4_UNRESOLVED,
        reason_detail="; ".join(reasons) or "Gate-4 could not prove coverage",
    )


async def _run_preflight(body: dict, user_id: str = "local", cover_ratio: float = 1.0) -> Verdict:
    """Run server-side Gate 4. `cover_ratio` is plumbed from RegimeGate
    when binding_tier is TIER_2 (1.25 → 125 shares per short call)."""
    if body.get("type") == "combo":
        try:
            req = _body_to_combo_preflight_request(body)
        except (ValueError, ValidationError) as exc:
            return _invalid_order_body_response(exc)
        if preflight.combo_uncovered_short_call_ratio(req) <= 0 or req.action == "SELL":
            return preflight.evaluate_combo(req, PortfolioView(), cover_ratio=cover_ratio)
        portfolio, snapshot_at = _unpack_portfolio_load(await _load_portfolio_view())
        if portfolio is None:
            try:
                missing = _portfolio_required_response(body)
            except (ValueError, ValidationError) as exc:
                return _invalid_order_body_response(exc)
            if missing is not None:
                return missing
            portfolio = PortfolioView()
        stale = _portfolio_snapshot_stale_response(snapshot_at)
        if stale is not None:
            return stale
        reservations = orders_store.working_reservations_for(user_id, req.ticker, **_resolve_scope_kwargs())
        return preflight.evaluate_combo(req, portfolio, reservations=reservations, cover_ratio=cover_ratio)

    try:
        req = _body_to_preflight_request(body)
    except (ValueError, ValidationError) as exc:
        return _invalid_order_body_response(exc)
    if req.action == "BUY":
        return preflight.evaluate(req, PortfolioView(), cover_ratio=cover_ratio)

    portfolio, snapshot_at = _unpack_portfolio_load(await _load_portfolio_view())
    if portfolio is None:
        missing = _portfolio_required_response(body)
        if missing is not None:
            return missing
        portfolio = PortfolioView()
    stale = _portfolio_snapshot_stale_response(snapshot_at)
    if stale is not None:
        return stale
    reservations = orders_store.working_reservations_for(user_id, req.ticker, **_resolve_scope_kwargs())
    return preflight.evaluate(req, portfolio, reservations=reservations, cover_ratio=cover_ratio)


def _lookup_min_tick_via_pool(con_id: int) -> Decimal:
    """Front-of-house minTick approximation. Returns 0.01 for every contract.

    **By design.** Replicating IB's tick-rule table locally is fragile:
    Reg NMS Rule 612 sub-penny carve-outs for stocks under $1, OPRA penny
    pilot rosters for options, and exchange-specific market rules for
    futures all change without notice. Instead of reaching into IB on the
    sync ``quote_guard.check`` path (which would require ``ib_async``'s
    ``reqContractDetailsAsync`` and a wider sync→async refactor), we keep
    the cheap 0.01 approximation **and rely on IB to reject off-tick
    prices server-side**.

    When IB rejects with error code 110 ("price does not conform to
    minimum price variation"), ``place`` re-maps the
    response from generic ``IB_REJECT`` to ``LIMIT_OFF_TICK`` so the UI
    surfaces a clean message and the IB code+message land in the
    ``orders_events`` audit row for later analysis.

    Most US equities and most options at the prices we trade have $0.01
    ticks, so this approximation covers the common case correctly. Edge
    instruments (sub-$1 stocks, foreign equities, futures, index option
    strike bands) will hit IB's rejection — that's acceptable: the
    user-visible error is actionable and we have a structured log to
    measure how often it happens.

    If you ever need real tick lookups (e.g. for fractional-share / sub-$1
    stock support), the right path is to plumb ``reqContractDetailsAsync``
    through an async ``quote_guard``, not to patch this stub.
    """
    return Decimal("0.01")


_tick_rule_cache = quote_guard.TickRuleCache(
    source=_lookup_min_tick_via_pool,
    ttl_seconds=24 * 3600,
)


def _now() -> datetime:
    """Test seam: override via monkeypatch to inject a fixed RTH timestamp."""
    return datetime.now(tz=timezone.utc)


def _ensure_thread_event_loop() -> None:
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _ticker_to_quote_snapshot(ticker: str, con_id: int, tk: Any) -> dict:
    import math as _math

    bid = getattr(tk, "bid", None)
    ask = getattr(tk, "ask", None)
    bid_size = getattr(tk, "bidSize", 0) or 0
    ask_size = getattr(tk, "askSize", 0) or 0

    if (
        bid is None
        or ask is None
        or (isinstance(bid, float) and _math.isnan(bid))
        or (isinstance(ask, float) and _math.isnan(ask))
    ):
        raise HTTPException(status_code=503, detail=f"No quote available for {ticker}/{con_id}")
    return {
        "bid": Decimal(str(bid)),
        "ask": Decimal(str(ask)),
        "bid_size": int(bid_size),
        "ask_size": int(ask_size),
    }


def _fetch_quote_snapshot_with_client(client: Any, ticker: str, con_id: int) -> dict:
    """Run blocking ib_async quote work in a worker thread that owns a loop."""
    _ensure_thread_event_loop()
    contract = Contract(conId=int(con_id), exchange="SMART")
    qualified = client.qualify_contract(contract)
    tk = client.get_quote(qualified, snapshot=True)
    return _ticker_to_quote_snapshot(ticker, con_id, tk)


def _contract_from_order_body(body: dict) -> Contract:
    symbol = str(body.get("symbol", "")).upper()
    if body.get("type") == "option":
        return Option(
            symbol=symbol,
            lastTradeDateOrContractMonth=str(body.get("expiry") or ""),
            strike=float(body.get("strike") or 0),
            right=str(body.get("right") or "").upper(),
            exchange="SMART",
            currency="USD",
        )
    # Stock quote/qualify path: honor body exchange/currency so a foreign cash
    # equity (5016 TSEJ JPY, 000660 KRX KRW) quotes from its native venue.
    # Option branch stays SMART/USD (US/index options only — no foreign options).
    return Stock(
        symbol,
        str(body.get("exchange") or "SMART").upper(),
        str(body.get("currency") or "USD").upper(),
    )


def _fetch_order_quote_snapshot_with_client(client: Any, body: dict) -> tuple[int, dict]:
    """Qualify a stock/option order body and fetch its snapshot quote."""
    _ensure_thread_event_loop()
    ticker = str(body.get("symbol", "")).upper()
    contract = _contract_from_order_body(body)
    qualified = client.qualify_contract(contract)
    con_id = int(getattr(qualified, "conId", 0) or 0)
    if con_id <= 0:
        raise HTTPException(status_code=503, detail=f"Could not qualify contract for {ticker}")
    tk = client.get_quote(qualified, snapshot=True)
    return con_id, _ticker_to_quote_snapshot(ticker, con_id, tk)


def _qualify_order_con_id_with_client(client: Any, body: dict) -> int:
    """Qualify a stock/option order body and return its IB conId."""
    _ensure_thread_event_loop()
    ticker = str(body.get("symbol", "")).upper()
    contract = _contract_from_order_body(body)
    qualified = client.qualify_contract(contract)
    con_id = int(getattr(qualified, "conId", 0) or 0)
    if con_id <= 0:
        raise HTTPException(status_code=503, detail=f"Could not qualify contract for {ticker}")
    return con_id


async def _fetch_quote_snapshot(ticker: str, con_id: int) -> dict:
    """Fetch a bid/ask snapshot from the serialized ib_pool 'data' role."""
    from xenon.api import server

    pool = server.ib_pool
    if pool is None:
        raise HTTPException(status_code=503, detail="IB data role unavailable")

    try:
        async with pool.acquire("data") as client:
            return await pool.run_sync(
                "data",
                _fetch_quote_snapshot_with_client,
                client,
                ticker,
                con_id,
            )
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def _fetch_order_quote_snapshot(body: dict) -> tuple[int, dict]:
    from xenon.api import server

    ticker = str(body.get("symbol", "")).upper()
    con_id = int(body.get("con_id") or 0)
    if con_id > 0:
        return con_id, await _fetch_quote_snapshot(ticker, con_id)

    pool = server.ib_pool
    if pool is None:
        raise HTTPException(status_code=503, detail="IB data role unavailable")

    try:
        async with pool.acquire("data") as client:
            return await pool.run_sync(
                "data",
                _fetch_order_quote_snapshot_with_client,
                client,
                dict(body),
            )
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def _qualify_order_con_id(body: dict) -> int:
    from xenon.api import server

    con_id = int(body.get("con_id") or 0)
    if con_id > 0:
        return con_id

    pool = server.ib_pool
    if pool is None:
        raise HTTPException(status_code=503, detail="IB data role unavailable")

    try:
        async with pool.acquire("data") as client:
            return await pool.run_sync(
                "data",
                _qualify_order_con_id_with_client,
                client,
                dict(body),
            )
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _quote_payload_from_snapshot(ticker: str, con_id: int, snap: dict) -> QuotePayload:
    return QuotePayload(
        con_id=int(con_id),
        ticker=ticker.upper(),
        bid=Decimal(str(snap["bid"])),
        ask=Decimal(str(snap["ask"])),
        bid_size=int(snap["bid_size"]),
        ask_size=int(snap["ask_size"]),
        ts_server_ms=int(time.time() * 1000),
    )


async def _validate_non_combo_quote(body: dict) -> tuple[quote_guard.QuoteVerdict, int]:
    ticker = str(body.get("symbol", "")).upper()
    security_type = "STK" if body.get("type") == "stock" else "OPT"
    action = str(body.get("action", "")).upper()
    limit_price = Decimal(str(body.get("limitPrice", "0")))
    now = _now()

    market = quote_guard.check_market_hours(security_type=security_type, now=now)
    if not market.accept:
        return market, 400

    payload: QuotePayload | None = None
    token = body.get("quote_token")
    if token:
        try:
            payload = quote_tokens.verify(
                token,
                os.environ.get("XENON_QUOTE_TOKEN_SECRET", ""),
                max_age_ms=quote_guard.MAX_AGE_RTH_MS,
            )
        except quote_tokens.QuoteTokenExpired:
            payload = None
        except quote_tokens.QuoteTokenInvalid as exc:
            return (
                quote_guard.QuoteVerdict(
                    accept=False,
                    reason_code=ReasonCode.STALE_QUOTE,
                    reason_detail=f"token invalid: {exc}",
                ),
                400,
            )

    con_id = int(body.get("con_id") or 0)
    if payload is not None and body.get("type") == "option" and con_id <= 0:
        try:
            con_id = await _qualify_order_con_id(body)
        except HTTPException as exc:
            return (
                quote_guard.QuoteVerdict(
                    accept=False,
                    reason_code=ReasonCode.QUOTE_UNAVAILABLE,
                    reason_detail=str(exc.detail),
                ),
                exc.status_code if exc.status_code >= 500 else 400,
            )

    if payload is None:
        try:
            con_id, snap = await _fetch_order_quote_snapshot(body)
        except HTTPException as exc:
            return (
                quote_guard.QuoteVerdict(
                    accept=False,
                    reason_code=ReasonCode.QUOTE_UNAVAILABLE,
                    reason_detail=str(exc.detail),
                ),
                exc.status_code if exc.status_code >= 500 else 400,
            )
        payload = _quote_payload_from_snapshot(ticker, con_id, snap)

    expected_con_id = con_id or payload.con_id
    return (
        quote_guard.check_payload(
            payload=payload,
            con_id=expected_con_id,
            ticker=ticker,
            security_type=security_type,
            action=action,
            limit_price=limit_price,
            now=now,
            tick_rule_lookup=_tick_rule_cache.get,
        ),
        400,
    )


def _gate4_leg_specs(body: dict) -> list[dict]:
    """One spec per option leg the gate must qualify (combo legs in body
    order; a single option body is one leg)."""
    sym = str(body.get("symbol", "")).upper()
    raw_legs = body.get("legs") or [] if body.get("type") == "combo" else [body]
    specs = []
    for leg in raw_legs:
        con_id = leg.get("con_id") or leg.get("conId")
        try:
            strike = float(leg.get("strike")) if leg.get("strike") is not None else None
        except (TypeError, ValueError):
            strike = None
        specs.append(
            {
                "sym": sym,
                "right": str(leg.get("right") or "").upper() or None,
                "exp": str(leg.get("expiry") or "").replace("-", "") or None,
                "strike": strike,
                "con_id": int(con_id) if str(con_id or "").isdigit() and int(con_id) > 0 else None,
            }
        )
    return specs


def _resolve_leg_meta_with_client(client: Any, specs: list[dict]) -> list[dict | None]:
    """Qualify each leg via reqContractDetails; a leg whose qualified contract
    does not match the body (symbol/right/expiry/strike) resolves to None."""
    from xenon.execution import contract_meta

    _ensure_thread_event_loop()
    out: list[dict | None] = []
    for spec in specs:
        if spec["con_id"] is not None:
            contract = Contract(conId=spec["con_id"])
        elif spec["right"] and spec["exp"] and spec["strike"] is not None:
            contract = Option(
                symbol=spec["sym"],
                lastTradeDateOrContractMonth=spec["exp"],
                strike=spec["strike"],
                right=spec["right"],
                exchange="SMART",
                currency="USD",
            )
        else:
            out.append(None)
            continue
        meta = contract_meta.fetch_meta(client, contract)
        if (
            meta is None
            or meta["sec"] != "OPT"
            or meta["sym"] != spec["sym"]
            or meta["right"] != spec["right"]
            or meta["exp"] != spec["exp"]
            or spec["strike"] is None
            or meta["strike"] is None
            or abs(meta["strike"] - spec["strike"]) > 1e-6
        ):
            out.append(None)
            continue
        out.append(meta)
    return out


async def _resolve_gate4_leg_meta(body: dict) -> list[dict | None]:
    """IB-qualified option metadata for the Gate-4 candidate (spec §2.3 cover
    unit). Any failure leaves the leg unqualified → the gate fails closed."""
    from xenon.api import server

    specs = _gate4_leg_specs(body)
    pool = server.ib_pool
    if pool is None or not specs:
        return [None] * len(specs)
    try:
        async with pool.acquire("data") as client:
            return await pool.run_sync("data", _resolve_leg_meta_with_client, client, specs)
    except Exception as exc:  # noqa: BLE001 — fail closed, never guess metadata
        logger.warning("gate4 contract qualification failed: %s", exc)
        return [None] * len(specs)


def _resolve_scope_kwargs() -> dict[str, str]:
    """Resolve broker account scope from app.state with safe fallback.

    Lifespan populates `app.state.trading_mode` and `app.state.account`.
    Tests that bypass lifespan get `legacy_unknown` defaults — this matches
    the column server_default so existing test rows still pass CHECK.
    """
    from xenon.api import server

    try:
        return resolve_from_app_state(server.app.state).as_dict()
    except ValueError:
        pass
    return {"broker": "IB", "account_env": "legacy_unknown", "broker_account": "legacy_unknown"}


async def place(body: dict, *, broker: str | None = None, account_env: str | None = None):
    from xenon.api import server

    if _order_broker(broker) != "IB":
        return await _futu_place(body, _order_broker(broker), account_env)
    denial = ensure_order_allowed("order placement", server.app.state)
    if denial is not None:
        return denial
    prep = await _prepare_place(body)
    if isinstance(prep, JSONResponse):
        return prep
    cid, req_row, candidate, legs_payload, scope = prep
    outcome = gate4.reserve_with_coverage(
        "local",
        cid,
        req_row,
        candidate=candidate,
        legs_payload=legs_payload,
        scope=scope,
        override_audit=None,
    )
    refusal = _reservation_refusal(outcome, cid, replacement=False)
    if refusal is not None:
        return refusal
    return await _place_reserved(body, outcome.submission_id, scope)


async def _prepare_place(body: dict, scope: dict | None = None):
    """Pre-reservation place checks → JSONResponse refusal, or
    (cid, request row, Gate-4 candidate, legs payload, scope). `scope`: the
    FUTU request scope; None → the IB scope from app.state."""
    # RegimeGate (VCG-CRI signal layer) was removed in the pure-portfolio
    # pivot (#104). Order entry no longer gates on regime tiers and there is
    # no override audit.
    # Gate 4 (PR-03b): universe/index precheck — pure request-shape rules.
    verdict = _universe_precheck(body)
    if verdict is not None:
        return _preflight_block_response(verdict)

    # Gate 4 (PR-03b): coverage.evaluate + reservation insert in ONE
    # transaction serialized by an account-scope pg advisory lock. The SELL
    # short-circuit is gone — every non-BUY candidate (and every combo,
    # either envelope) is evaluated against the durable view. A single-leg
    # BUY still skips evaluation (it creates no demand) but still serializes
    # its reservation under the same lock.
    cid = body.get("client_attempt_id")
    if not cid:
        return JSONResponse(
            status_code=400,
            content={"detail": "client_attempt_id is required"},
        )
    scope = scope or _resolve_scope_kwargs()
    security_type = "STK" if body.get("type") == "stock" else "BAG" if body.get("type") == "combo" else "OPT"
    ticker = str(body.get("symbol", "")).upper()
    try:
        # One canonical TIF for the reservation, the stored column and the
        # place CLI (PR-03b round 7). Unknown values fail before reserving.
        tif = _canonical_tif(body.get("tif"))
        body["tif"] = tif
        req_row = orders_store.RequestRow(
            ticker=ticker,
            security_type=security_type,
            action=str(body.get("action", "")).upper(),
            quantity=int(body.get("quantity", 0)),
            expiry=body.get("expiry"),
            strike=Decimal(str(body["strike"])) if body.get("strike") is not None else None,
            right=(body.get("right") or "").upper() or None,
            # Multiplier is server-authoritative — a client-provided value
            # must never reach the reservation row (spoofed mult inflates
            # share-cover math).
            multiplier=gate4.server_multiplier(ticker),
            con_id=int(body.get("con_id") or 0) or None,
            limit_price=Decimal(str(body.get("limitPrice", "0"))),
            tif=tif,
        )
    except (ValueError, TypeError, ValidationError) as exc:
        return _preflight_block_response(_invalid_order_body_response(exc))
    leg_meta = await _resolve_gate4_leg_meta(body) if gate4.needs_contract_meta(body) else None
    candidate, legs_payload = gate4.candidate_from_place_body(body, scope["broker_account"], leg_meta)
    return cid, req_row, candidate, legs_payload, scope


def _reservation_refusal(outcome, cid: str, *, replacement: bool) -> JSONResponse | dict | None:
    """Map a non-winner reservation outcome to its response (None = winner).

    `replacement`: a claim for /orders/replace — any existing attempt id is a
    refusal (nothing cancelled), never a 200 the client reads as replaced."""
    if outcome.status == "target_unresolved":
        return JSONResponse(
            status_code=409,
            content={
                "reason_code": ReasonCode.ORDER_TARGET_AMBIGUOUS.value,
                "detail": f"{outcome.detail} — nothing was cancelled",
            },
        )
    if outcome.status == "in_progress":
        return JSONResponse(
            status_code=409,
            content={
                "reason_code": ReasonCode.REPLACEMENT_IN_PROGRESS.value,
                "detail": "Another replacement of this order is in progress or done — nothing was cancelled. Check working orders.",
                "state": outcome.state,
                "submission_id": outcome.submission_id,
            },
        )
    if outcome.status == "rejected":
        return _preflight_block_response(
            _gate4_reject_verdict(
                outcome.reasons, outcome.uncovered, outcome.short_stock, outcome.split_required
            )
        )
    if outcome.status == "body_mismatch" and outcome.reason_code == ReasonCode.ATTEMPT_ALREADY_PLACED.value:
        # Same id, different body, and that attempt may be at IB: the new
        # terms were not placed. Never invite a rebuild/resubmit (PR-08).
        return JSONResponse(
            status_code=409,
            content={
                "reason_code": ReasonCode.ATTEMPT_ALREADY_PLACED.value,
                "detail": f"attempt {cid} belongs to an existing order; the requested terms are not confirmed",
                "state": outcome.state,
                "submission_id": outcome.submission_id,
            },
        )
    if outcome.status == "body_mismatch":
        return JSONResponse(
            status_code=409,
            content={
                "reason_code": ReasonCode.ATTEMPT_BODY_MISMATCH.value,
                "detail": f"attempt {cid} already reserved with a different request body",
                "submission_id": outcome.submission_id,
            },
        )
    if outcome.status == "terminal":
        return JSONResponse(
            status_code=409,
            content={
                "detail": f"attempt {cid} already in terminal state {outcome.state}",
                "reason_code": ReasonCode.ATTEMPT_ID_TERMINAL.value,
                "state": outcome.state,
                "prior_reason_code": outcome.reason_code,
            },
        )
    if outcome.status == "duplicate" and outcome.state in ("UNKNOWN", "PENDING"):
        # Same attempt id while the first send is unresolved / in flight:
        # never a 200 the client reads as success (it would mint a new id).
        return JSONResponse(
            status_code=502,
            content={
                **_PLACE_UNKNOWN_BODY,
                "state": outcome.state,
                "submission_id": outcome.submission_id,
                "duplicate_of": outcome.duplicate_of,
            },
        )
    if outcome.status == "duplicate" and replacement:
        return JSONResponse(
            status_code=409,
            content={
                "reason_code": ReasonCode.ATTEMPT_BODY_MISMATCH.value,
                "detail": f"attempt {cid} already used (state {outcome.state}) — nothing was cancelled",
                "state": outcome.state,
                "submission_id": outcome.submission_id,
            },
        )
    if outcome.status == "duplicate":
        return JSONResponse(
            status_code=200,
            content={
                "duplicate_of": outcome.duplicate_of,
                "state": outcome.state,
                "submission_id": outcome.submission_id,
            },
        )
    return None


async def _quote_gate(body: dict, submission_id: str) -> JSONResponse | None:
    """F3: quote-token + tick-grid + limit-band + market-hours (IB quotes,
    for IB and FUTU orders alike). Runs AFTER the reservation so a Gate-4
    coverage reason always wins over a quote failure; a quote rejection marks
    the row REJECTED so the reservation releases its cover instead of
    leaking a PENDING row. None = pass."""
    if body.get("type") != "combo":
        qv, quote_status = await _validate_non_combo_quote(body)
        _override_detail = None
        if not qv.accept:
            if qv.reason_code == ReasonCode.LIMIT_OUT_OF_BAND and body.get("acknowledge_limit_override") is True:
                _override_detail = {
                    "reason_detail": qv.reason_detail,
                    "limit_price": body.get("limitPrice"),
                }
            else:
                orders_store.mark_terminal(
                    submission_id=submission_id,
                    state="REJECTED",
                    reason_code=qv.reason_code.value if qv.reason_code else "QUOTE_REJECT",
                    filled_qty=0,
                    avg_fill_price=None,
                    expected_states=("PENDING",),
                )
                return JSONResponse(
                    status_code=quote_status,
                    content={
                        "detail": qv.reason_detail,
                        "reason_code": qv.reason_code.value if qv.reason_code else None,
                        "reason_detail": qv.reason_detail,
                    },
                )
    else:
        _override_detail = None

    if _override_detail is not None:
        orders_store.record_event(submission_id, "PREFLIGHT_ACK_LIMIT", _override_detail)
    return None


async def _place_reserved(body: dict, submission_id: str, scope: dict):
    """Quote gate → broker place → verdict for a row already reserved PENDING."""
    from xenon.api import server

    refusal = await _quote_gate(body, submission_id)
    if refusal is not None:
        return refusal

    if server._is_test_mode():
        order_id, perm_id = server._next_test_order_ids()
        orders_store.mark_submitted(
            submission_id=submission_id,
            ib_order_id=str(order_id),
            perm_id=str(perm_id),
            placing_client_id=pool_client_id("orders"),
            expected_states=("PENDING",),
        )
        return {
            "status": "ok",
            "orderId": order_id,
            "permId": perm_id,
            "initialStatus": "Submitted",
            "message": "Order accepted in test mode",
            "echo": body,
            "submission_id": submission_id,
        }

    # Place on the account the reservation row was stamped with — never on an
    # account named by the request body, never on IB's default. The core
    # fails closed if it is legacy_unknown or not managed. submission_id
    # becomes orderRef "xo:<submission_id>" (recovery key); a body-supplied
    # orderRef is dropped.
    params = {
        **{k: v for k, v in body.items() if k != "orderRef"},
        "account": scope["broker_account"],
        "submission_id": submission_id,
    }
    if server.order_owner is None:
        return _finish_place(submission_id, _owner_unavailable_reply())
    settled: dict = {}

    def settle(reply: dict) -> None:
        # Runs exactly once: on the orders worker for a started job (even
        # when this request is gone), else here for a job that never started.
        settled["response"] = _finish_place(submission_id, reply)

    await server.order_owner.run(
        lambda client: ib_place_order.place_order_on_client(client, params),
        timeout=_PLACE_TIMEOUT_S,
        settle=settle,
    )
    if "response" in settled:
        return settled["response"]
    # Deadline after the job started: IB may hold the order. The worker
    # still owns the final write (PENDING / UNKNOWN → WORKING / REJECTED).
    logger.warning("place: deadline after start, outcome pending on the worker: submission=%s", submission_id)
    return _place_outcome_unknown(submission_id, {})


# Owner classifications that prove nothing was sent → reason code on the
# FAILED row and the 503 body (a known 5xx code is terminal in the web).
_PLACE_NOT_SENT_REASONS = {
    "connection": ReasonCode.IB_CONNECTION,
    BROKER_DISCONNECTED: ReasonCode.IB_BROKER_DISCONNECTED,
    ORDERS_HALTED: ReasonCode.ORDERS_HALTED,
}


def _owner_unavailable_reply() -> dict:
    return {
        "status": "error",
        "message": "Order owner is not running; nothing was sent",
        "classification": BROKER_DISCONNECTED,
        "amend_outcome": "not_sent",
    }


def _finish_place(submission_id: str, reply: dict) -> Any:
    """Persist one place outcome and build its response (A3). Runs on the
    orders worker for a started job, so it never depends on the request.

    - owner refusal / abandoned before start (``amend_outcome: not_sent``)
      → FAILED (from PENDING or UNKNOWN), 503 with IB_CONNECTION /
      IB_BROKER_DISCONNECTED / ORDERS_HALTED: nothing was sent;
    - possibly sent, not confirmed (``amend_outcome: unconfirmed``) →
      UNKNOWN with the ids the core learned (PENDING or UNKNOWN), 502;
    - otherwise the core's result through the verdict. Final writes accept
      PENDING or UNKNOWN (the request may have marked UNKNOWN at its
      deadline) and never overwrite a state the poller set from broker
      evidence. The placing clientId stored is always the owner's.
    """
    owner_id = pool_client_id("orders")
    outcome = reply.get("amend_outcome")
    if outcome == "not_sent" and reply.get("classification") in _PLACE_NOT_SENT_REASONS:
        reason = _PLACE_NOT_SENT_REASONS[reply["classification"]].value
        # PENDING or UNKNOWN: the request may have marked UNKNOWN at its
        # deadline before the worker proved nothing was sent. Never a state
        # the poller set from broker evidence.
        applied = orders_store.mark_terminal(
            submission_id=submission_id,
            state="FAILED",
            reason_code=reason,
            filled_qty=0,
            avg_fill_price=None,
            expected_states=_PLACE_FINAL_FROM,
        )
        if not applied:
            # Stronger evidence won the row: do not claim FAILED.
            logger.warning(
                "place: proven unsent (%s) but submission %s is no longer PENDING/UNKNOWN; row left as is",
                reason,
                submission_id,
            )
            return JSONResponse(status_code=502, content={**_PLACE_UNKNOWN_BODY, "submission_id": submission_id})
        return JSONResponse(status_code=503, content={"detail": reply.get("message"), "reason_code": reason})
    if outcome == "unconfirmed":
        logger.warning("place: possibly sent, unconfirmed: submission=%s (%s)", submission_id, reply.get("message"))
        return _place_outcome_unknown(submission_id, {**reply, "clientId": owner_id}, from_states=_PLACE_FINAL_FROM)
    return _place_result(submission_id, reply, owner_id)


# States a worker's final place write may leave: PENDING, or UNKNOWN set by
# the request at its deadline. Never a broker-observed or terminal state.
_PLACE_FINAL_FROM = ("PENDING", "UNKNOWN")


def _place_result(submission_id: str, data: dict, owner_id: int) -> Any:
    """Verdict + persistence for a place-core result."""
    final_states = _PLACE_FINAL_FROM
    verdict = _place_verdict(data)
    if verdict == "unknown":
        return _place_outcome_unknown(submission_id, {**data, "clientId": owner_id}, from_states=final_states)
    if verdict == "rejected":
        # B6 — write a reason_code the UI toast map can resolve. Most IB
        # rejections collapse to IB_REJECT, but tick-rule rejections
        # (code 110) re-map to LIMIT_OFF_TICK so the user sees a clean
        # actionable message instead of raw IB error text. The raw IB
        # numeric code + message always go into the orders_events audit
        # row so we don't lose the info.
        ib_code = data.get("code")
        ib_message = data.get("message", "Order failed")
        core_reason = data.get("reason_code")
        if core_reason in (
            ReasonCode.ACCOUNT_UNRESOLVED.value,
            ReasonCode.ACCOUNT_NOT_MANAGED.value,
            ReasonCode.INVALID_ORDER_BODY.value,
        ):
            reason_code = core_reason
        elif str(ib_code) == "110":
            reason_code = ReasonCode.LIMIT_OFF_TICK.value
            logger.warning(
                "IB rejected order with tick-rule violation: submission=%s ib_code=110 ib_message=%s",
                submission_id,
                ib_message,
            )
        else:
            reason_code = ReasonCode.IB_REJECT.value
        # Conditional: a poller that already saw this order at IB wins, and
        # then the core's "rejected" is contradicted by broker evidence.
        # Ids the core learned are kept: a reject after the send means the
        # order reached IB (gate4._may_be_at_ib and wizard protect read them).
        if not orders_store.mark_terminal(
            submission_id=submission_id,
            state="REJECTED",
            reason_code=reason_code,
            filled_qty=0,
            avg_fill_price=None,
            expected_states=final_states,
            ib_order_id=str(data.get("orderId") or "") or None,
            perm_id=str(data.get("permId") or "") or None,
        ):
            logger.warning("place: core rejected but row %s already moved on; not rejecting", submission_id)
            return JSONResponse(status_code=502, content={**_PLACE_UNKNOWN_BODY, "submission_id": submission_id})
        try:
            orders_store.record_event(
                submission_id,
                "IB_REJECT",
                {"ib_code": ib_code, "ib_message": ib_message},
            )
        except Exception:  # pragma: no cover — event writes are best-effort
            logger.warning(
                "Failed to record IB_REJECT event for submission %s",
                submission_id,
                exc_info=True,
            )
        # Top-level reason_code so the toast helper renders the mapped copy.
        return JSONResponse(
            status_code=502,
            content={"detail": ib_message, "reason_code": reason_code},
        )
    if data.get("clientId") not in (None, owner_id):
        logger.warning("place: core reported clientId %s, owner is %s", data.get("clientId"), owner_id)
    data = {**data, "clientId": owner_id}
    try:
        # PENDING / UNKNOWN only: the activity poller may already have bound
        # (WORKING) or finished (FILLED) this row from broker evidence;
        # never demote it.
        orders_store.mark_submitted(
            submission_id=submission_id,
            ib_order_id=str(data.get("orderId") or ""),
            perm_id=str(data.get("permId") or ""),
            placing_client_id=owner_id,
            expected_states=final_states,
        )
    except Exception as exc:
        # IB accepted the order: report it (never a 5xx that invites a
        # resend). The row stays PENDING; orderRef recovery repairs it.
        logger.error("place: IB accepted but persist failed: submission=%s", submission_id, exc_info=True)
        _record_place_event(submission_id, {"persist_error": str(exc)})
        return {
            **data,
            "submission_id": submission_id,
            "persist_warning": "Order accepted by IB but not saved; it will be reconciled from IB.",
        }
    if data.get("ibNotices"):
        _record_place_event(submission_id, {"notices": data["ibNotices"]})
    return data


def _place_verdict(data: dict | None) -> str:
    """``accepted`` | ``rejected`` | ``unknown`` for a place-core result.

    The core's explicit ``outcome`` (from the ordered ib_async Trade.log) is
    trusted: ``accepted`` / ``unconfirmed`` → accepted (stored WORKING),
    ``rejected`` → rejected; ``status: uncertain`` → unknown. Every result
    after ``placeOrder`` carries an outcome or ``uncertain``, so a
    ``status: error`` without one is a pre-send error → rejected.

    A WORKING row is then updated by the activity poller / fill mirror, but
    the activity-mirror sweep (``sweep_disappeared_orders``) skips an empty
    open-order snapshot and needs two consecutive misses, and boot rehydrate
    without a positions baseline leaves the row as is — a per-row grace /
    unknown-order reconciliation is a follow-up.
    """
    if not data:
        return "unknown"
    outcome = data.get("outcome")
    if data.get("status") == "uncertain" or outcome == "uncertain":
        return "unknown"
    if outcome == "rejected":
        return "rejected"
    if outcome in ("accepted", "unconfirmed") or data.get("status") != "error":
        return "accepted"
    return "rejected"


_PLACE_UNKNOWN_BODY = {
    "detail": "Order outcome unknown — check working orders before placing it again. Do not resubmit.",
    "reason_code": ReasonCode.PLACE_OUTCOME_UNKNOWN.value,
}


def _place_outcome_unknown(
    submission_id: str, data: dict, from_states: tuple[str, ...] = ("PENDING",)
) -> JSONResponse:
    """IB may or may not hold the order. Never leave the row PENDING (it
    would age into a rehydrate decision) and never call it rejected. UNKNOWN
    is the uncertain state /health counts; it keeps the Gate-4 reservation.
    Any IB ids the CLI returned are stored in the same conditional write
    (PENDING → UNKNOWN only: a row the poller already bound or filled from
    broker evidence is left as is). The activity poller resolves the row by
    orderRef "xo:<submission_id>". A DB failure still returns the uncertain
    502 (the row then stays PENDING, which keeps the reservation too).

    The request's deadline write is PENDING-only and carries no ids. The
    worker's late uncertain write passes ``from_states`` PENDING/UNKNOWN: it
    enriches the row with the ids it learned and the owner id (non-empty
    values only, so neither write ever clears the other's evidence)."""
    try:
        applied = orders_store.mark_place_unknown(
            submission_id=submission_id,
            ib_order_id=str(data.get("orderId") or "") or None,
            perm_id=str(data.get("permId") or "") or None,
            placing_client_id=int(data["clientId"]) if data.get("clientId") is not None else None,
            reason_code=ReasonCode.PLACE_OUTCOME_UNKNOWN.value,
            expected_states=from_states,
        )
        if applied and (data.get("orderId") or data.get("permId") or data.get("code")):
            _record_place_event(
                submission_id,
                {
                    "ib_code": data.get("code"),
                    "ib_message": data.get("message"),
                    "initial_status": data.get("initialStatus"),
                },
            )
    except Exception:
        logger.error("place: failed to persist UNKNOWN for submission %s", submission_id, exc_info=True)
    return JSONResponse(
        status_code=502,
        content={**_PLACE_UNKNOWN_BODY, "submission_id": submission_id},
    )


def _record_place_event(submission_id: str, detail: dict) -> None:
    """Best-effort IB_NOTICE audit row; a failed write never fails the place."""
    try:
        orders_store.record_event(submission_id, "IB_NOTICE", detail)
    except Exception:  # pragma: no cover — event writes are best-effort
        logger.warning("Failed to record IB_NOTICE event for submission %s", submission_id, exc_info=True)


# F5.4 — cancel/modify failure classification
# ib_reject codes that mean "order vanished" map to 404 so the UI can
# distinguish from a hard reject. All other ib_reject codes are 400.
_IB_REJECT_NOT_FOUND_CODES = {"10147", "10148"}


def _map_ib_reject_status(upstream_code: Any) -> int:
    code = str(upstream_code) if upstream_code is not None else ""
    return 404 if code in _IB_REJECT_NOT_FOUND_CODES else 400


def _classify_to_http(data: dict) -> tuple[int, str]:
    """Map an order-action reply classification → (http_status, reason_code)."""
    classification = data.get("classification")
    if classification in ("connection", "send_window_closed", "unconfirmed"):
        # "unconfirmed": the action reached IB without an ack (amend_outcome
        # carries it); the routes treat it as uncertain, never as a reject.
        return 503, ReasonCode.IB_CONNECTION.value
    if classification == BROKER_DISCONNECTED:
        return 503, ReasonCode.IB_BROKER_DISCONNECTED.value
    if classification == ORDERS_HALTED:
        return 503, ReasonCode.ORDERS_HALTED.value
    if classification == "ownership":
        return 409, ReasonCode.ORDER_NOT_OWNED.value
    if classification == "ib_reject":
        upstream = data.get("upstream") or {}
        return _map_ib_reject_status(upstream.get("code")), ReasonCode.IB_REJECT.value
    # Unknown/missing classification — fall back to 502 with a generic code.
    return 502, ReasonCode.IB_REJECT.value


def order_error(status: int, reason_code: str, detail: dict, **extra) -> JSONResponse:
    """Single error contract for the cancel/modify routes.

    Emits ``reason_code`` at the TOP level of the body — that is what the
    web toast helpers read via ``readReasonCode`` (``HTTPException(
    detail=dict)`` only ever produced ``body.detail.reason_code``). The
    structured payload stays under ``detail`` for the readers that consume
    it (``detail.applied`` on MODIFY_STALE, ``upstream``, ``applied_
    sequence``, ``http_status``); ``detail.reason_code`` is kept too so
    nested readers keep working. ``**extra`` lands at the top level.
    """
    return JSONResponse(
        status_code=status,
        content={
            "reason_code": reason_code,
            "detail": {**detail, "reason_code": reason_code},
            **extra,
        },
    )


def _record_manage_event(
    ib_order_id: str,
    kind: str,
    detail: dict,
    perm_id: str = "",
    scope: dict[str, str] | None = None,
) -> None:
    """Write orders_events row for a cancel/modify attempt.

    Resolves the submission canonically (`load_submission_for_modify`:
    permId first, an orderId alone only when it names exactly one row —
    orderIds are unique per clientId only). If no submission exists (order placed pre-F4, or before a
    reserve_attempt row was created), the event is skipped — orders_events
    .submission_id is NOT NULL and has no synthetic parent row available.
    """
    try:
        scope = scope or _resolve_scope_kwargs()
        row = orders_store.load_submission_for_modify(order_id=ib_order_id, perm_id=perm_id, **scope)
        if row:
            orders_store.record_event(row["submission_id"], kind, detail)
    except Exception:  # pragma: no cover — event writes are best-effort
        logger.warning(
            "Failed to record %s event for order=%s perm=%s",
            kind,
            ib_order_id,
            perm_id,
            exc_info=True,
        )


async def cancel(body: dict, *, broker: str | None = None, account_env: str | None = None):
    from xenon.api import server

    if _order_broker(broker) != "IB":
        return await _futu_cancel(body, _order_broker(broker), account_env)
    denial = ensure_order_allowed("order cancellation", server.app.state)
    if denial is not None:
        return denial
    _kind, response = await _cancel_order(body)
    return response


def _int_id(value: Any) -> int:
    """orderId/permId from a request body (0 when absent); ValueError if not an integer."""
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise ValueError(f"not an order id: {value!r}")
    return int(value)


_NOT_OWNED_HINT = "Cancel or modify it in TWS / IBKR Mobile."


def _not_owned_refusal(placing_client_id: Any, order_id: Any, **extra) -> JSONResponse | None:
    """Positive routing (A1): only an order placed by this environment's
    ``orders`` connection takes the owner path. A recorded placing clientId
    that is anything else (legacy subprocess 20–49 / 26, clientId 0 / TWS /
    mobile, relay, another environment's base) → 409 ORDER_NOT_OWNED with no
    broker work at all. No recorded clientId → the owner path, whose fresh
    scoped open-order snapshot refuses a foreign owner before any send."""
    if placing_client_id is None:
        return None
    owner = pool_client_id("orders")
    try:
        placing = int(placing_client_id)
    except (TypeError, ValueError):
        placing = None
    if placing == owner:
        return None
    return order_error(
        409,
        ReasonCode.ORDER_NOT_OWNED.value,
        {
            "classification": "ownership",
            "message": (
                f"Order {order_id} was placed by clientId {placing_client_id}, not this API's order "
                f"connection (clientId {owner}). Nothing was sent. {_NOT_OWNED_HINT}"
            ),
            "originalClientId": placing,
            "http_status": 409,
            **extra,
        },
    )


def _owner_unavailable(**extra) -> JSONResponse:
    return order_error(
        503,
        ReasonCode.IB_BROKER_DISCONNECTED.value,
        {"message": "Order owner is not running; nothing was sent", "http_status": 503, **extra},
    )


def _owner_error_detail(data: dict, **extra) -> tuple[int, str, dict]:
    """(http_status, reason_code, detail) for an error reply from the owner."""
    http_status, reason_code = _classify_to_http(data)
    message = data.get("message")
    if reason_code == ReasonCode.ORDER_NOT_OWNED.value:
        message = f"{message}. {_NOT_OWNED_HINT}"
    detail = {
        "reason_code": reason_code,
        "classification": data.get("classification"),
        "message": message,
        "upstream": data.get("upstream"),
        "amend_outcome": data.get("amend_outcome"),
        "http_status": http_status,
        **extra,
    }
    return http_status, reason_code, detail


async def _cancel_order(body: dict, row: dict | None = None, *, reason_code: str = "USER_CANCEL") -> tuple[str, Any]:
    """Cancel one order through the order owner → (kind, response). kind:
    "ok"; "failed" (nothing was sent, or IB said no and the order is still
    working); "uncertain" (the cancel may have reached IB unconfirmed).

    `row` = the target's submission row already resolved by the caller
    ({submission_id, placing_client_id}); else it is resolved canonically
    here. Only that row is marked CANCELLED (with `reason_code`)."""
    from xenon.api import server

    if server._is_test_mode():
        return "ok", {
            "status": "ok",
            "message": "Cancel accepted in test mode",
            "echo": body,
        }

    order_id = body.get("orderId", 0)
    perm_id = body.get("permId", 0)

    if not order_id and not perm_id:
        return "failed", order_error(
            400,
            ReasonCode.ORDER_IDENTIFIER_REQUIRED.value,
            {
                "message": "Cancel request must include orderId or permId.",
                "http_status": 400,
            },
        )
    try:
        oid, pid = _int_id(order_id), _int_id(perm_id)
    except (TypeError, ValueError):
        return "failed", order_error(
            400,
            ReasonCode.ORDER_IDENTIFIER_REQUIRED.value,
            {"message": "orderId and permId must be integers.", "http_status": 400},
        )

    scope = _resolve_scope_kwargs()

    # PR-05 identity tuple: the core asserts the scope account plus the
    # recorded placing clientId (when a submission row carries one), so a
    # cancel can never land on a different account's/client's order. The
    # account is asserted only when the scope resolved a real account — the
    # legacy_unknown fallback would otherwise fail every identity check.
    _broker_account = scope.get("broker_account") or ""
    account = _broker_account if _broker_account and _broker_account != "legacy_unknown" else None
    if row is None:
        if not perm_id and (
            orders_store.count_submissions_for_order_id(str(order_id), client_id=body.get("clientId"), **scope) > 1
        ):
            # Several rows share this orderId (different clients): the core
            # could cancel the wrong one. No row at all (TWS order) → owner
            # path, which refuses an order it does not own.
            return "failed", order_error(
                409,
                ReasonCode.ORDER_TARGET_AMBIGUOUS.value,
                {
                    "message": f"orderId {order_id} names several orders; send its permId. Nothing was cancelled.",
                    "http_status": 409,
                },
            )
        row = orders_store.load_submission_for_modify(
            order_id=str(order_id or ""), perm_id=str(perm_id or ""), client_id=body.get("clientId"), **scope
        )
    placing = row.get("placing_client_id") if row else None

    refusal = _not_owned_refusal(placing, order_id or perm_id)
    if refusal is None and server.order_owner is None:
        refusal = _owner_unavailable()
    if refusal is not None:
        _record_manage_event(
            str(order_id or ""), "CANCEL", _json_body(refusal)["detail"], perm_id=str(perm_id or ""), scope=scope
        )
        return "failed", refusal

    client_id = int(placing) if placing is not None else None
    data = await server.order_owner.run(
        lambda client: ib_order_manage.cancel_order_on_client(client, oid, pid, account=account, client_id=client_id),
        timeout=_ORDER_ACTION_TIMEOUT_S,
    )
    if data.get("status") == "error":
        http_status, reason_code, detail = _owner_error_detail(data)
        _record_manage_event(str(order_id or ""), "CANCEL", detail, perm_id=str(perm_id or ""), scope=scope)
        kind = "uncertain" if data.get("amend_outcome") == "unconfirmed" else "failed"
        return kind, order_error(http_status, reason_code, detail)

    _record_manage_event(
        str(order_id or ""),
        "CANCEL",
        {"status": data.get("status"), "message": data.get("message"), "http_status": 200},
        perm_id=str(perm_id or ""),
    )
    _mark_submission_cancelled((row or {}).get("submission_id"), reason_code=reason_code)
    return "ok", data


def _replace_targets(body: dict) -> list[dict]:
    """Deduplicated cancel targets: body.cancelOrders, else orderId/permId
    (+ optional clientId, the placing client — needed for an orderId-only
    target when the scope holds more than one row with that orderId)."""
    raw = body.get("cancelOrders") or [
        {"orderId": body.get("orderId"), "permId": body.get("permId"), "clientId": body.get("clientId")}
    ]
    seen: set[str] = set()
    targets: list[dict] = []
    for t in raw if isinstance(raw, list) else []:
        try:
            oid = int((t or {}).get("orderId") or 0)
            perm = int((t or {}).get("permId") or 0)
            client = (t or {}).get("clientId")
            client = int(client) if client is not None else None
        except (TypeError, ValueError, AttributeError):
            continue
        if oid <= 0 and perm <= 0:
            continue
        key = f"perm:{perm}" if perm > 0 else f"oid:{client}:{oid}"
        if key in seen:
            continue
        seen.add(key)
        targets.append({"orderId": oid, "permId": perm, "clientId": client})
    return targets


def _json_body(resp: Any) -> dict:
    if isinstance(resp, JSONResponse):
        return json.loads(bytes(resp.body))
    return dict(resp or {})


_REPLACE_FAILED_COPY = "CRITICAL: Original order cancelled, replacement FAILED. Place a new order manually."
_REPLACE_UNKNOWN_COPY = (
    "Original order cancelled; replacement submitted, status unknown — check working orders. Do not resubmit."
)


async def replace(body: dict, *, broker: str | None = None, account_env: str | None = None):
    from xenon.api import server

    if _order_broker(broker) != "IB":
        # Never dispatched for FUTU (capability false): refused, never sent to IB.
        return ensure_order_allowed(
            "order replacement", SimpleNamespace(broker=_order_broker(broker)), account_env=account_env
        ) or _futu_not_dispatched("order replacement")
    denial = ensure_order_allowed("order replacement", server.app.state)
    if denial is not None:
        return denial
    replacement = body.get("replaceOrder")
    if not isinstance(replacement, dict):
        return JSONResponse(
            status_code=400,
            content={"detail": "replaceOrder is required", "reason_code": ReasonCode.INVALID_ORDER_BODY.value},
        )
    targets = _replace_targets(body)
    if not targets:
        return JSONResponse(
            status_code=400,
            content={
                "detail": "At least one order to cancel is required",
                "reason_code": ReasonCode.ORDER_IDENTIFIER_REQUIRED.value,
            },
        )
    prep = await _prepare_place(replacement)
    if isinstance(prep, JSONResponse):
        return prep
    cid, req_row, candidate, legs_payload, scope = prep

    # 1. Claim (one txn, scope advisory lock): refuse a second replacement
    #    of any target and any reused attempt id; else the successor's row
    #    is reserved PENDING through the Gate-4 insert path.
    outcome = gate4.claim_replacement(
        "local",
        cid,
        req_row,
        candidate=candidate,
        legs_payload=legs_payload,
        scope=scope,
        targets=[
            {"perm_id": str(t["permId"] or ""), "ib_order_id": str(t["orderId"] or ""), "client_id": t["clientId"]}
            for t in targets
        ],
    )
    refusal = _reservation_refusal(outcome, cid, replacement=True)
    if refusal is not None:
        return refusal
    submission_id = outcome.submission_id

    # 2. Cancel every target by its resolved identity (permId when known)
    #    and persist the cancel on exactly its row. Nothing is placed unless
    #    all are confirmed.
    cancelled: list[dict] = []
    for ident in outcome.targets:
        target = {"orderId": int(ident["ib_order_id"] or 0), "permId": int(ident["perm_id"] or 0)}
        row = {"submission_id": ident["submission_id"], "placing_client_id": ident["client_id"]}
        kind, resp = await _cancel_order({k: v for k, v in target.items() if v}, row=row)
        if kind == "failed":
            orders_store.mark_terminal(
                submission_id=submission_id,
                state="FAILED",
                reason_code="REPLACE_CANCEL_FAILED",
                filled_qty=0,
                avg_fill_price=None,
                expected_states=("PENDING",),
            )
            status = resp.status_code if isinstance(resp, JSONResponse) else 502
            return JSONResponse(
                status_code=status,
                content={**_json_body(resp), "replacement_placed": False, "cancelled": cancelled},
            )
        if kind == "uncertain":
            orders_store.mark_place_unknown(
                submission_id=submission_id,
                ib_order_id=None,
                perm_id=None,
                placing_client_id=None,
                reason_code="REPLACE_CANCEL_UNCERTAIN",
                expected_states=("PENDING",),
            )
            return JSONResponse(
                status_code=502,
                content={
                    **_PLACE_UNKNOWN_BODY,
                    "error": "Cancel of the original order is unconfirmed; the replacement was NOT placed. "
                    "Check working orders. Do not resubmit.",
                    "replacement_placed": False,
                    "submission_id": submission_id,
                },
            )
        cancelled.append(target)

    # 3. Gate 4 again on the post-cancel view, then the normal place path
    #    for the claimed row (never the PENDING-duplicate branch).
    if candidate is not None:
        rejected = gate4.recheck_claim(submission_id, candidate, scope)
        if rejected is not None:
            verdict = _gate4_reject_verdict(
                rejected.reasons, rejected.uncovered, rejected.short_stock, rejected.split_required
            )
            orders_store.mark_terminal(
                submission_id=submission_id,
                state="REJECTED",
                reason_code=verdict.reason_code.value if verdict.reason_code else "GATE4_REJECT",
                filled_qty=0,
                avg_fill_price=None,
                expected_states=("PENDING",),
            )
            resp = _preflight_block_response(verdict)
            return JSONResponse(
                status_code=resp.status_code, content={**_json_body(resp), "error": _REPLACE_FAILED_COPY}
            )
    resp = await _place_reserved(replacement, submission_id, scope)
    if not isinstance(resp, JSONResponse) or resp.status_code < 400:
        return resp
    content = _json_body(resp)
    unknown = content.get("reason_code") == ReasonCode.PLACE_OUTCOME_UNKNOWN.value
    return JSONResponse(
        status_code=resp.status_code,
        content={**content, "error": _REPLACE_UNKNOWN_COPY if unknown else _REPLACE_FAILED_COPY},
    )


def _mark_submission_cancelled(submission_id: str | None, *, reason_code: str = "USER_CANCEL") -> None:
    """Transition exactly this order_submissions row to CANCELLED after a
    successful cancel. The activity poller cannot do this on its own — naive
    disappearance-detection misclassifies fills as cancels — so the cancel
    path is the only authoritative trigger. The row comes from a canonical
    resolve (permId, or a unique clientId+orderId), never an orderId-first
    lookup: orderIds repeat across clients, and marking the wrong live order
    terminal would drop its Gate-4 demand. No row → nothing is marked.
    """
    if not submission_id:
        return
    try:
        # filled_qty=None keeps executed quantity: a cancel releases only
        # the unfilled remainder (spec §2.3), even before the fill row lands.
        orders_store.mark_terminal(
            submission_id=submission_id,
            state="CANCELLED",
            reason_code=reason_code,
            filled_qty=None,
            avg_fill_price=None,
            # Never over FILLED or an earlier terminal state (a fill or a
            # TWS-cancel mirror that landed first wins).
            expected_states=orders_store.NON_TERMINAL_STATES,
        )
    except Exception:  # pragma: no cover — best-effort
        logger.warning("Failed to mark submission %s CANCELLED", submission_id, exc_info=True)


def _confirmed_int(confirmed, requested) -> int | None:
    """Broker-confirmed quantity when the CLI reports one, else the request
    (a quantity amend confirms only on exact equality)."""
    for value in (confirmed, requested):
        if value is None or isinstance(value, bool):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


async def modify(body: dict, *, broker: str | None = None, account_env: str | None = None):
    from xenon.api import server

    if _order_broker(broker) != "IB":
        return await _futu_modify(body, _order_broker(broker), account_env)
    denial = ensure_order_allowed("order modification", server.app.state)
    if denial is not None:
        return denial

    if server._is_test_mode():
        return {
            "status": "ok",
            "message": "Modify accepted in test mode",
            "echo": body,
        }

    order_id = body.get("orderId", 0)
    perm_id = body.get("permId", 0)
    new_price = body.get("newPrice")
    new_quantity = body.get("newQuantity")
    outside_rth = body.get("outsideRth")
    modify_sequence = body.get("modifySequence")

    if modify_sequence is None:
        return order_error(
            400,
            ReasonCode.MODIFY_SEQUENCE_REQUIRED.value,
            {
                "message": "modifySequence is required",
                "http_status": 400,
            },
        )
    try:
        modify_sequence = int(modify_sequence)
    except (TypeError, ValueError):
        return order_error(
            400,
            ReasonCode.MODIFY_SEQUENCE_REQUIRED.value,
            {
                "message": "modifySequence must be an integer",
                "http_status": 400,
            },
        )

    # Apply sequence gate BEFORE the owner is called. If only permId is
    # supplied (UI-initiated modifies often have orderId=0), route through the
    # perm_id variant so we can still find the submissions row.
    if not order_id and not perm_id:
        return order_error(
            400,
            ReasonCode.ORDER_IDENTIFIER_REQUIRED.value,
            {
                "message": "Modify request must include orderId or permId.",
                "http_status": 400,
            },
        )
    _scope = _resolve_scope_kwargs()

    if not order_id and perm_id:
        seq_outcome = orders_store.apply_modify_by_perm_id(str(perm_id), modify_sequence, **_scope)
    else:
        seq_outcome = orders_store.apply_modify(str(order_id), modify_sequence, **_scope)
    if not seq_outcome["applied"]:
        current = seq_outcome["current_sequence"]
        if current == -1:
            return order_error(
                404,
                ReasonCode.ORDER_NOT_FOUND.value,
                {
                    "message": f"Order {order_id} not found",
                    "http_status": 404,
                },
            )
        return order_error(
            409,
            ReasonCode.MODIFY_STALE.value,
            {
                "message": f"modifySequence {modify_sequence} is stale; current is {current}",
                "applied": current,
                "http_status": 409,
            },
        )

    try:
        oid, pid = _int_id(order_id), _int_id(perm_id)
        price = float(new_price) if new_price is not None else None
        quantity = int(new_quantity) if new_quantity is not None else None
    except (TypeError, ValueError):
        return order_error(
            400,
            ReasonCode.INVALID_ORDER_BODY.value,
            {
                "message": "orderId/permId/newQuantity must be integers and newPrice a number",
                "applied_sequence": modify_sequence,
                "http_status": 400,
            },
        )

    # PR-05 identity tuple: the core asserts the scope account plus the
    # recorded placing clientId (when a submission row carries one), so a
    # modify can never land on a different account's/client's order. The
    # account is asserted only when the scope resolved a real account.
    _broker_account = _scope.get("broker_account") or ""
    account = _broker_account if _broker_account and _broker_account != "legacy_unknown" else None
    _row = orders_store.load_submission_for_modify(
        order_id=str(order_id or ""), perm_id=str(perm_id or ""), **_scope
    )
    placing = _row.get("placing_client_id") if _row else None

    # Positive routing (A1) before any amend opens: an order this API's
    # orders connection does not own is refused with no broker work.
    refusal = _not_owned_refusal(placing, order_id or perm_id, applied_sequence=modify_sequence)
    if refusal is None and server.order_owner is None:
        refusal = _owner_unavailable(applied_sequence=modify_sequence)
    if refusal is not None:
        _record_manage_event(
            str(order_id or ""), "MODIFY", _json_body(refusal)["detail"], perm_id=str(perm_id or ""), scope=_scope
        )
        return refusal

    # BAG guarantee mode is durable order state, never client-mutable. The
    # broker always receives it explicitly — a resubmitted BAG without
    # NonGuaranteed raises "Missing or invalid NonGuaranteed value", and a
    # default could silently flip a guaranteed combo to non-guaranteed.
    # Checked BEFORE the amend opens so a refused mode change never leaves
    # a pending amend behind.
    effective_ng: int | None = None
    if _row and _row.get("security_type") == "BAG":
        stored_guaranteed = gate4.persisted_guarantee(_row)
        effective_ng = 0 if stored_guaranteed else 1
        requested_ng = body.get("nonGuaranteed")
        if requested_ng is not None:
            try:
                req_ng = int(requested_ng)
            except (TypeError, ValueError):
                req_ng = -1
            if req_ng != effective_ng:
                return order_error(
                    400,
                    ReasonCode.MODIFY_GUARANTEE_MODE.value,
                    {
                        "message": "Guarantee mode is fixed at placement; cannot change NonGuaranteed on modify",
                        "http_status": 400,
                    },
                )

    # Gate 4 (PR-03b): every modify of a known row opens one amend generation
    # under the account-scope advisory lock. A row with an unresolved amend
    # refuses the new modify (MODIFY_PENDING). A quantity modify evaluates
    # the same coverage model as placement and persists pending_amend_qty, so
    # an acknowledgement lost in flight keeps reserving max(old, new) - filled.
    _submission_id = _row.get("submission_id") if _row else None
    _amend_id: str | None = None
    _send_by = None
    if new_quantity is not None and _submission_id is None:
        return order_error(
            404,
            ReasonCode.ORDER_NOT_FOUND.value,
            {
                "message": "Quantity modify requires a resolvable submission row",
                "http_status": 404,
            },
        )
    if _submission_id is not None:
        mod_outcome = gate4.begin_modify(_submission_id, new_quantity, _scope)
        if not mod_outcome.accept:
            if gate4.MODIFY_PENDING_REASON in mod_outcome.reasons:
                return order_error(
                    409,
                    ReasonCode.MODIFY_PENDING.value,
                    {
                        "message": (
                            "An earlier change to this order is not confirmed by IB yet. "
                            "Retry in about two minutes, after IB reports the order again, or cancel the order."
                        ),
                        "http_status": 409,
                    },
                )
            v = _gate4_reject_verdict(
                mod_outcome.reasons, mod_outcome.uncovered, mod_outcome.short_stock, mod_outcome.split_required
            )
            return order_error(
                400,
                v.reason_code.value if v.reason_code else ReasonCode.GATE4_UNRESOLVED.value,
                {
                    "message": v.reason_detail or "Modify blocked by coverage gate",
                    "http_status": 400,
                },
            )
        _amend_id = mod_outcome.amend_id
        # Send-by fence (round 5, item 2): the core re-checks it right
        # before placeOrder, so a job that starts late sends nothing.
        _send_by = mod_outcome.send_by

    def _settle_amend(reply: dict) -> None:
        """Gate 4 settlement with the captured amend id — runs exactly once,
        on the worker for a started job (even after this request timed out),
        never "uncertain" from the request path for a job the worker owns.
        Only an amend proven unapplied (never sent, or an explicit IB
        rejection) releases the reservation; anything else is uncertain."""
        if _amend_id is None:
            return
        if reply.get("status") == "ok":
            gate4.settle_modify(
                _submission_id,
                _amend_id,
                "confirmed",
                _scope,
                # The broker's confirmed values, never the request:
                # confirmation tolerates a <0.001 price difference.
                new_qty=_confirmed_int(reply.get("confirmedQuantity"), new_quantity),
                new_limit_price=reply.get("confirmedPrice"),
            )
            return
        outcome = "rejected" if reply.get("amend_outcome") in ("rejected", "not_sent") else "uncertain"
        gate4.settle_modify(_submission_id, _amend_id, outcome, _scope)

    client_id = int(placing) if placing is not None else None
    data = await server.order_owner.run(
        lambda client: ib_order_manage.modify_order_on_client(
            client,
            oid,
            pid,
            price,
            outside_rth=outside_rth if isinstance(outside_rth, bool) else None,
            new_quantity=quantity,
            account=account,
            client_id=client_id,
            non_guaranteed=effective_ng,
            send_by=_send_by,
        ),
        timeout=_ORDER_ACTION_TIMEOUT_S,
        settle=_settle_amend,
    )

    if data.get("status") == "error":
        http_status, reason_code, detail = _owner_error_detail(data, applied_sequence=modify_sequence)
        _record_manage_event(str(order_id or ""), "MODIFY", detail, perm_id=str(perm_id or ""), scope=_scope)
        return order_error(http_status, reason_code, detail)

    _record_manage_event(
        str(order_id or ""),
        "MODIFY",
        {
            "status": data.get("status"),
            "message": data.get("message"),
            "applied_sequence": modify_sequence,
            "http_status": 200,
        },
        perm_id=str(perm_id or ""),
        scope=_scope,
    )
    # Echo the applied sequence so the UI can anchor its per-order counter.
    return {**data, "applied_sequence": modify_sequence}


# The order owner's wait for one place: readiness round trip + qualify + ack
# wait (5 s combo) + queueing behind another order action.
_PLACE_TIMEOUT_S = 30


# ---------------------------------------------------------------------------
# FUTU dispatch (X3c): place_single / cancel / modify on the Futu order worker
# ---------------------------------------------------------------------------
#
# Order of checks, as for IB: read-only → capability (env enabled + account
# configured) → scope-change veto (all in `ensure_order_allowed`) → Gate 4
# (naked-short / coverage, FUTU request scope) → IB quote gate (place) → the
# worker, which verifies the live OpenD account (`verify_live`) before every
# core. A FUTU request never falls back to IB. The adapter never writes
# order_fills: every row change after a send comes from fresh account-scoped
# evidence through the observation mirror (`orders_store.mirror_futu_order`).
# Uncertain send = UNKNOWN; there is no automatic re-send.

FUTU_NOT_SENT = "FUTU_NOT_SENT"
FUTU_SEND_WINDOW_CLOSED = "FUTU_SEND_WINDOW_CLOSED"
FUTU_WORKER_MISSING = "FUTU_WORKER_STOPPED"


def _order_broker(broker: str | None) -> str:
    """The request's broker: the `broker` query value, else app.state.broker."""
    from xenon.api import server

    return str(broker or getattr(server.app.state, "broker", "IB") or "IB").upper()


def _futu_not_dispatched(kind: str) -> JSONResponse:
    message = f"FUTU is read-only for {kind}"
    return JSONResponse(
        status_code=403,
        content={"detail": message, "reason_code": ReasonCode.READ_ONLY_BROKER.value, "reason_detail": message},
    )


def _futu_guard(kind: str, broker: str, op: str | None, account_env: str | None):
    """→ (scope, None) or (None, refusal). Unknown brokers are refused here too."""
    from xenon.execution import futu_scope

    denial = ensure_order_allowed(kind, SimpleNamespace(broker=broker), op, account_env=account_env)
    if denial is not None:
        return None, denial
    scope, _ = futu_scope.configured_scope(account_env) if broker == "FUTU" else (None, None)
    if scope is None:  # pragma: no cover — the capability proved the account
        return None, _futu_not_dispatched(kind)
    return scope, None


def futu_trading_status() -> dict[str, bool]:
    """/health `futu.trading`: per env, would a Futu place pass the guard?
    True only when not read-only, the Futu order worker runs, the env is in
    XENON_FUTU_TRADING and has a configured account (`broker_capabilities.evaluate`), no
    FUTU_SCOPE_CHANGE_PENDING veto applies, and for live FUTU_TRADE_PWD_MD5
    is well-formed (format only, no unlock call). No account numbers."""
    from xenon.api import guards
    from xenon.execution import broker_capabilities, futu_orders, futu_scope

    out: dict[str, bool] = {}
    for env in futu_scope.ACCOUNT_ENVS:
        worker = _futu_worker()
        ok = (
            not guards.is_read_only()
            and worker is not None
            and not worker.stopped
            and broker_capabilities.evaluate("FUTU", "place_single", env)[0]
        )
        try:
            ok = ok and not guards.futu_scope_change_pending(env)
            if ok and env == "live":
                futu_orders.trade_secret(os.environ)
        except futu_orders.FutuOrderRefused:
            ok = False
        except Exception as exc:  # noqa: BLE001 — health renders without the DB; unknown = off
            logger.debug("futu trading status (%s): %s", env, type(exc).__name__)
            ok = False
        out[env] = bool(ok)
    return out


def _futu_worker():
    from xenon.api import server

    return getattr(server.app.state, "futu_order_worker", None)


def _futu_worker_missing() -> dict:
    return {
        "status": "error",
        "message": "Futu order worker is not running; nothing was sent",
        "classification": BROKER_DISCONNECTED,
        "amend_outcome": "not_sent",
        "reason_code": FUTU_WORKER_MISSING,
    }


def _futu_refused_reply(exc) -> dict:
    """Adapter refusal before any send → a not-sent reply."""
    return {
        "status": "error",
        "message": str(exc),
        "classification": "futu_refused",
        "amend_outcome": "not_sent",
        "reason_code": exc.reason_code,
        "http_status": exc.http_status,
    }


def _futu_uncertain_reply(message: str | None, reason_code: str) -> dict:
    return {
        "status": "error",
        "message": message or "Futu did not confirm the request",
        "classification": "connection",
        "amend_outcome": "unconfirmed",
        "reason_code": reason_code,
    }


def _futu_fresh(ctx, order_id: str, scope) -> tuple[datetime, Any]:
    """(read start, evidence | None): a fresh account-scoped query; a failed
    read is no evidence."""
    from xenon.execution import futu_orders

    read_at = datetime.now(timezone.utc)
    try:
        ev = futu_orders.query_order(ctx, order_id=order_id, account_env=scope.account_env, acc_id=scope.broker_account)
    except futu_orders.FutuReadFailed as exc:
        logger.warning("futu orders: evidence read for %s failed (%s)", order_id, exc)
        ev = None
    return read_at, ev


def _futu_mirror(scope, row: dict | None, read_at: datetime | None) -> dict | None:
    """Apply one fresh order row through the observation mirror (binds an own
    row by remark, state, fills). None when there is nothing to apply, the
    row cannot be parsed, or its identity contradicts the stored row."""
    from xenon.api.services import futu_observation

    if not row or read_at is None:
        return None
    obs = futu_observation.observation_from_row(row, scope, read_at)
    if obs is None:
        logger.warning("futu orders: evidence row for %s cannot be parsed", row.get("order_id"))
        return None
    try:
        out = orders_store.mirror_futu_order(
            obs, broker_account=scope.broker_account, account_env=scope.account_env, observed_at=read_at
        )
    except Exception:  # noqa: BLE001 — the row keeps its state; the observation pass retries
        logger.warning("futu orders: mirror of order %s failed", obs.order_id, exc_info=True)
        return None
    if out.get("identity_mismatch"):
        logger.warning("futu orders: order %s not bound (%s)", obs.order_id, out["identity_mismatch"])
        return None
    return out


def _futu_event(submission_id: str | None, kind: str, detail: dict) -> None:
    if not submission_id:
        return
    try:
        orders_store.record_event(submission_id, kind, detail)
    except Exception:  # pragma: no cover — event writes are best-effort
        logger.warning("Failed to record %s event for submission %s", kind, submission_id, exc_info=True)


def _futu_spec(body: dict):
    from xenon.execution import futu_orders

    sec = {"stock": "STK", "option": "OPT"}.get(body.get("type"))
    if sec is None:
        raise futu_orders.FutuOrderRefused(futu_orders.INVALID_ORDER, "Futu orders are single stock or option orders")
    quantity = body.get("quantity")
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise futu_orders.FutuOrderRefused(futu_orders.INVALID_ORDER, "quantity must be a whole number")
    return futu_orders.FutuOrderSpec(
        sec_type=sec,
        symbol=str(body.get("symbol", "")).upper(),
        action=str(body.get("action", "")).upper(),
        quantity=quantity,
        limit_price=Decimal(str(body.get("limitPrice"))),
        tif=body.get("tif") or "DAY",
        expiry=_normalize_gate_expiry(body.get("expiry")) if sec == "OPT" else None,
        right=((body.get("right") or "").upper() or None) if sec == "OPT" else None,
        strike=Decimal(str(body["strike"])) if sec == "OPT" and body.get("strike") is not None else None,
    )


def _futu_refusal_response(exc) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={"detail": str(exc), "reason_code": exc.reason_code, "reason_detail": str(exc)},
    )


async def _futu_place(body: dict, broker: str, account_env: str | None):
    from xenon.execution import futu_orders

    op = "combo" if body.get("type") == "combo" else "place_single"
    scope, refusal = _futu_guard("order placement", broker, op, account_env)
    if refusal is not None:
        return refusal
    prep = await _prepare_place(body, scope.as_dict())
    if isinstance(prep, JSONResponse):
        return prep
    cid, req_row, candidate, legs_payload, scope_d = prep
    # Refuse what the adapter would refuse before a row is reserved.
    try:
        spec = _futu_spec(body)
        code = futu_orders.futu_code_for(spec)
        futu_orders.checked_price(spec, spec.limit_price)
    except futu_orders.FutuOrderRefused as exc:
        return _futu_refusal_response(exc)
    except (ValueError, TypeError, InvalidOperation) as exc:
        return _preflight_block_response(_invalid_order_body_response(exc))
    outcome = gate4.reserve_with_coverage(
        "local",
        cid,
        req_row,
        candidate=candidate,
        legs_payload={**legs_payload, "futu_code": code},
        scope=scope_d,
        override_audit=None,
    )
    refusal = _reservation_refusal(outcome, cid, replacement=False)
    if refusal is not None:
        return refusal
    submission_id = outcome.submission_id
    refusal = await _quote_gate(body, submission_id)
    if refusal is not None:
        return refusal
    return await _futu_place_reserved(spec, code, submission_id, scope)


async def _futu_place_reserved(spec, code: str, submission_id: str, scope):
    from xenon.execution import futu_orders

    env, acc = scope.account_env, scope.broker_account

    def core(ctx, mark_sent) -> dict:
        try:
            res = futu_orders.place_single(
                ctx, spec, submission_id=submission_id, account_env=env, acc_id=acc, mark_sent=mark_sent
            )
        except futu_orders.FutuOrderRefused as exc:
            return _futu_refused_reply(exc)
        if res.outcome == "accepted":
            read_at, ev = _futu_fresh(ctx, res.order_id, scope)
            return {
                "status": "ok",
                "order_id": res.order_id,
                "futu_status": res.futu_status,
                "row": ev.row if ev is not None else None,
                "read_at": read_at,
            }
        # Possibly sent, not confirmed: look for the order by its exact
        # remark in this account; no match → UNKNOWN. Never re-send.
        read_at = datetime.now(timezone.utc)
        try:
            found = futu_orders.find_by_remark(
                ctx,
                remark=futu_orders.remark_for(submission_id),
                futu_code=res.futu_code or code,
                account_env=env,
                acc_id=acc,
            )
        except futu_orders.FutuReadFailed as exc:
            logger.warning("futu place %s: remark recovery read failed (%s)", submission_id, exc)
            found = None
        if found is not None and found.order is not None:
            ev = found.order
            return {
                "status": "ok",
                "order_id": ev.order_id,
                "futu_status": ev.futu_status,
                "row": ev.row,
                "read_at": read_at,
                "recovered": True,
            }
        reply = _futu_uncertain_reply(res.message, res.reason_code or futu_orders.SEND_UNCERTAIN)
        reply["recovery"] = found.status if found is not None else "read_failed"
        return reply

    worker = _futu_worker()
    if worker is None:
        return _futu_finish_place(submission_id, scope, _futu_worker_missing())
    settled: dict = {}

    def settle(reply: dict) -> None:
        settled["response"] = _futu_finish_place(submission_id, scope, reply)

    await worker.run(core, scope=scope, timeout=_PLACE_TIMEOUT_S, settle=settle)
    if "response" in settled:
        return settled["response"]
    logger.warning("futu place: deadline after start, outcome pending on the worker: submission=%s", submission_id)
    return _place_outcome_unknown(submission_id, {})


def _futu_finish_place(submission_id: str, scope, reply: dict) -> Any:
    """Persist one Futu place outcome (exactly once, on the worker for a
    started job): proven not sent → FAILED; possibly sent → UNKNOWN (non-OK
    after the mark is UNKNOWN, never a reject); accepted → the row is bound
    to the Futu order id from fresh evidence, else UNKNOWN until the
    observation pass binds it by remark."""
    if reply.get("amend_outcome") == "not_sent":
        reason = reply.get("reason_code") or FUTU_NOT_SENT
        applied = orders_store.mark_terminal(
            submission_id=submission_id,
            state="FAILED",
            reason_code=reason,
            filled_qty=0,
            avg_fill_price=None,
            expected_states=_PLACE_FINAL_FROM,
        )
        if not applied:
            return JSONResponse(status_code=502, content={**_PLACE_UNKNOWN_BODY, "submission_id": submission_id})
        return JSONResponse(
            status_code=int(reply.get("http_status") or 503),
            content={"detail": reply.get("message"), "reason_code": reason, "submission_id": submission_id},
        )
    if reply.get("status") != "ok":
        logger.warning(
            "futu place: possibly sent, unconfirmed: submission=%s (%s)", submission_id, reply.get("message")
        )
        _futu_event(
            submission_id, "FUTU_PLACE_UNCERTAIN", {"message": reply.get("message"), "recovery": reply.get("recovery")}
        )
        return _place_outcome_unknown(submission_id, {}, from_states=_PLACE_FINAL_FROM)
    out = _futu_mirror(scope, reply.get("row"), reply.get("read_at"))
    order_id = str(reply.get("order_id"))
    _futu_event(
        submission_id,
        "FUTU_PLACE_ACCEPTED",
        {
            "order_id": order_id,
            "recovered": bool(reply.get("recovered")),
            "bound": bool(out and out.get("submission_id") == submission_id),
        },
    )
    if out is None or out.get("submission_id") != submission_id:
        logger.warning("futu place: order %s accepted, not bound to %s yet", order_id, submission_id)
        bound = 0
        if not reply.get("row"):
            # No fresh read: keep Futu's own accepted id so the order is
            # manageable before the remark bind (conditional write).
            try:
                bound = orders_store.bind_futu_order_id(
                    submission_id, order_id, broker_account=scope.broker_account, account_env=scope.account_env
                )
            except Exception:  # noqa: BLE001 — the row stays UNKNOWN; observation binds by remark
                logger.warning("futu place: storing order id %s failed", order_id, exc_info=True)
        unknown = _place_outcome_unknown(submission_id, {}, from_states=_PLACE_FINAL_FROM)
        if not bound:
            return unknown
        return JSONResponse(status_code=502, content={**_json_body(unknown), "brokerOrderId": order_id})
    return {
        "status": "ok",
        "broker": "FUTU",
        "accountEnv": scope.account_env,
        "brokerOrderId": order_id,
        "orderId": 0,
        "permId": 0,
        "initialStatus": reply.get("futu_status"),
        "state": _futu_row_state(submission_id),
        "submission_id": submission_id,
        "message": "Order accepted by Futu",
    }


def _futu_row_state(submission_id: str) -> str | None:
    from xenon.db.schema import order_submissions

    with get_sync_engine().connect() as conn:
        return conn.execute(
            select(order_submissions.c.state).where(order_submissions.c.submission_id == submission_id)
        ).scalar()


def _futu_target(body: dict, scope) -> tuple[dict | None, JSONResponse | None]:
    """The non-terminal FUTU row named by `brokerOrderId` (the Futu order id,
    a string), looked up inside the request scope only."""
    from xenon.db.schema import order_submissions

    order_id = str(body.get("brokerOrderId") or "").strip()
    if not order_id.isdigit():
        return None, order_error(
            400,
            ReasonCode.ORDER_IDENTIFIER_REQUIRED.value,
            {"message": "A Futu cancel or modify needs brokerOrderId (the Futu order id).", "http_status": 400},
        )
    with get_sync_engine().connect() as conn:
        row = conn.execute(
            select(order_submissions).where(
                order_submissions.c.broker == "FUTU",
                order_submissions.c.account_env == scope.account_env,
                order_submissions.c.broker_account == scope.broker_account,
                order_submissions.c.broker_order_id == order_id,
                order_submissions.c.state.in_(orders_store.NON_TERMINAL_STATES),
            )
        ).first()
    if row is None:
        return None, order_error(
            404,
            ReasonCode.ORDER_NOT_FOUND.value,
            {"message": f"No working Futu order {order_id} in this {scope.account_env} account", "http_status": 404},
        )
    return dict(row._mapping), None


def _futu_manage_error(reply: dict, kind: str, **extra) -> JSONResponse:
    if reply.get("amend_outcome") == "not_sent":
        reason = reply.get("reason_code") or FUTU_NOT_SENT
        status = int(reply.get("http_status") or 503)
        message = reply.get("message")
    else:
        reason = reply.get("reason_code") or "FUTU_SEND_UNCERTAIN"
        status = 503
        message = (
            f"The {kind} may have reached Futu but is not confirmed. The order keeps its reservation "
            "until Futu reports it again. Do not resend."
        )
    return order_error(
        status,
        reason,
        {"message": message, "amend_outcome": reply.get("amend_outcome"), "http_status": status, **extra},
    )


async def _futu_cancel(body: dict, broker: str, account_env: str | None):
    from xenon.execution import futu_orders

    scope, refusal = _futu_guard("order cancellation", broker, "cancel", account_env)
    if refusal is not None:
        return refusal
    row, refusal = _futu_target(body, scope)
    if refusal is not None:
        return refusal
    order_id, submission_id = row["broker_order_id"], row["submission_id"]

    def core(ctx, mark_sent) -> dict:
        try:
            res = futu_orders.cancel(
                ctx,
                order_id=order_id,
                account_env=scope.account_env,
                acc_id=scope.broker_account,
                mark_sent=mark_sent,
            )
        except futu_orders.FutuOrderRefused as exc:
            return _futu_refused_reply(exc)
        if res.outcome != "sent":
            return _futu_uncertain_reply(res.message, res.reason_code or futu_orders.SEND_UNCERTAIN)
        read_at, ev = _futu_fresh(ctx, order_id, scope)
        return {"status": "ok", "row": ev.row if ev is not None else None, "read_at": read_at}

    def finish(reply: dict) -> Any:
        _futu_event(
            submission_id,
            "CANCEL",
            {
                "status": reply.get("status"),
                "message": reply.get("message"),
                "amend_outcome": reply.get("amend_outcome"),
            },
        )
        if reply.get("status") != "ok":
            return _futu_manage_error(reply, "cancel")
        # The demand stays until fresh evidence shows the order terminal.
        _futu_mirror(scope, reply.get("row"), reply.get("read_at"))
        state = _futu_row_state(submission_id)
        confirmed = state == "CANCELLED"
        if confirmed:
            message = "Cancel confirmed by Futu"
        elif state in futu_orders.TERMINAL_STATES:
            message = f"Futu reports the order {state}; it was not cancelled"
        else:
            message = "Cancel sent; Futu has not reported the order cancelled yet"
        return {"status": "ok", "brokerOrderId": order_id, "state": state, "confirmed": confirmed, "message": message}

    return await _futu_run(core, finish, scope, "cancel")


async def _futu_run(core, finish, scope, kind: str, **extra) -> Any:
    """Run one cancel / modify core on the worker; `finish` settles exactly once."""
    worker = _futu_worker()
    if worker is None:
        return finish(_futu_worker_missing())
    settled: dict = {}

    def settle(reply: dict) -> None:
        settled["response"] = finish(reply)

    await worker.run(core, scope=scope, timeout=_ORDER_ACTION_TIMEOUT_S, settle=settle)
    if "response" in settled:
        return settled["response"]
    return _futu_manage_error({"amend_outcome": "unconfirmed"}, kind, **extra)


def _futu_apply_sequence(submission_id: str, sequence: int) -> tuple[bool, int]:
    """modifySequence gate for one FUTU row (the IB gate keys on ib_order_id):
    advance only to a larger sequence. → (applied, current)."""
    from sqlalchemy import update

    from xenon.db.schema import order_submissions

    orders_store.ensure_writable()
    with get_sync_engine().begin() as conn:
        done = conn.execute(
            update(order_submissions)
            .where(order_submissions.c.submission_id == submission_id, order_submissions.c.modify_sequence < sequence)
            .values(modify_sequence=sequence, updated_at=orders_store.monotone_updated_at(datetime.now(timezone.utc)))
            .returning(order_submissions.c.modify_sequence)
        ).first()
        if done is not None:
            return True, sequence
        current = conn.execute(
            select(order_submissions.c.modify_sequence).where(order_submissions.c.submission_id == submission_id)
        ).scalar()
        return False, int(current or 0)


class _SendWindowClosed(Exception):
    pass


FUTU_ORDER_UNVERIFIED = "FUTU_ORDER_UNVERIFIED"
FUTU_ORDER_NOT_WORKING = "FUTU_ORDER_NOT_WORKING"


def _futu_unsent(message: str, reason_code: str, http_status: int, exc: Exception | None = None) -> dict:
    """A refusal on the worker before any send (nothing was sent)."""
    if exc is not None:
        logger.warning("futu modify: %s (%s)", message, exc)
    return {
        "status": "error",
        "message": message,
        "classification": "futu_refused",
        "amend_outcome": "not_sent",
        "reason_code": reason_code,
        "http_status": http_status,
    }


async def _futu_modify(body: dict, broker: str, account_env: str | None):
    from xenon.execution import futu_orders

    scope, refusal = _futu_guard("order modification", broker, "modify", account_env)
    if refusal is not None:
        return refusal
    try:
        sequence = int(body["modifySequence"])
    except (KeyError, TypeError, ValueError):
        return order_error(
            400,
            ReasonCode.MODIFY_SEQUENCE_REQUIRED.value,
            {"message": "modifySequence is required and must be an integer", "http_status": 400},
        )
    row, refusal = _futu_target(body, scope)
    if refusal is not None:
        return refusal
    order_id, submission_id = row["broker_order_id"], row["submission_id"]
    new_price, new_quantity = body.get("newPrice"), body.get("newQuantity")
    try:
        if new_price is None and new_quantity is None:
            raise ValueError("newPrice or newQuantity is required")
        if new_quantity is not None and (isinstance(new_quantity, bool) or int(new_quantity) != new_quantity):
            raise ValueError("newQuantity must be a whole number")
        if new_quantity is not None and int(new_quantity) < 1:
            raise ValueError("newQuantity must be at least 1")
        expiry = row.get("expiry")
        # Identity from the row; quantity / price are set on the worker from
        # the request, else from a fresh read of the order (never the row).
        base_spec = futu_orders.FutuOrderSpec(
            sec_type=row["security_type"],
            symbol=row["ticker"],
            action=row["action"],
            quantity=1,
            limit_price=Decimal(1),
            tif=row.get("tif") or "DAY",
            expiry=expiry.strftime("%Y%m%d") if expiry is not None else None,
            right=row.get("right"),
            strike=Decimal(str(row["strike"])) if row.get("strike") is not None else None,
        )
        req_price = futu_orders.checked_price(base_spec, Decimal(str(new_price))) if new_price is not None else None
    except futu_orders.FutuOrderRefused as exc:
        return order_error(exc.http_status, exc.reason_code, {"message": str(exc), "http_status": exc.http_status})
    except (ValueError, TypeError, InvalidOperation) as exc:
        return order_error(400, ReasonCode.INVALID_ORDER_BODY.value, {"message": str(exc), "http_status": 400})
    applied, current = _futu_apply_sequence(submission_id, sequence)
    if not applied:
        return order_error(
            409,
            ReasonCode.MODIFY_STALE.value,
            {
                "message": f"modifySequence {sequence} is stale; current is {current}",
                "applied": current,
                "http_status": 409,
            },
        )
    scope_d = scope.as_dict()
    qty_amend = int(new_quantity) if new_quantity is not None else None
    gen: dict = {}  # the amend generation this core opened (core and finish run on the worker)

    def core(ctx, mark_sent) -> dict:
        def mark_in_window() -> None:
            # Send-by fence right before the mutation: a late job sends nothing.
            if datetime.now(timezone.utc) >= gen["send_by"]:
                raise _SendWindowClosed()
            mark_sent()

        # Fresh read of this order before the send: Futu modify sends both
        # quantity and price, so an omitted field takes the broker's current
        # value (the user may have changed it in the Futu app). No usable
        # read → nothing is sent.
        try:
            cur = futu_orders.query_order(
                ctx, order_id=order_id, account_env=scope.account_env, acc_id=scope.broker_account
            )
        except futu_orders.FutuReadFailed as exc:
            return _futu_unsent("Futu order read failed; nothing was sent", FUTU_ORDER_UNVERIFIED, 503, exc)
        cur_price = futu_orders.tick_price(base_spec, cur.price) if cur is not None else None
        if cur is None or cur.qty is None or cur.qty != cur.qty.to_integral_value() or cur_price is None:
            return _futu_unsent("Futu did not report this order; nothing was sent", FUTU_ORDER_UNVERIFIED, 503)
        if cur.state in futu_orders.TERMINAL_STATES:
            return _futu_unsent(f"Futu reports the order {cur.state}; nothing was sent", FUTU_ORDER_NOT_WORKING, 409)
        quantity = qty_amend if qty_amend is not None else int(cur.qty)
        price = req_price if req_price is not None else cur_price
        # Gate 4 reserves the full outgoing quantity under the scope lock,
        # also for a price-only modify: pending_amend_qty pins max(old, new)
        # until settle, so an observation cannot lower the reservation
        # below what this send carries.
        opened = gate4.begin_modify(submission_id, quantity, scope_d)
        if not opened.accept:
            if gate4.MODIFY_PENDING_REASON in opened.reasons:
                return _futu_unsent(
                    "An earlier change to this order is not confirmed by Futu yet. Cancel the order, "
                    "or resolve the change first (see the Futu runbook).",
                    ReasonCode.MODIFY_PENDING.value,
                    409,
                )
            v = _gate4_reject_verdict(opened.reasons, opened.uncovered, opened.short_stock, opened.split_required)
            return _futu_unsent(
                v.reason_detail or "Modify blocked by coverage gate",
                v.reason_code.value if v.reason_code else ReasonCode.GATE4_UNRESOLVED.value,
                400,
            )
        gen.update(amend_id=opened.amend_id, send_by=opened.send_by)
        try:
            res = futu_orders.modify(
                ctx,
                dataclasses.replace(base_spec, quantity=quantity, limit_price=price),
                order_id=order_id,
                account_env=scope.account_env,
                acc_id=scope.broker_account,
                mark_sent=mark_in_window,
            )
        except futu_orders.FutuOrderRefused as exc:
            return _futu_refused_reply(exc)
        except _SendWindowClosed:
            return {
                "status": "error",
                "message": "Send window closed; nothing was sent",
                "http_status": 503,
                "classification": "send_window_closed",
                "amend_outcome": "not_sent",
                "reason_code": FUTU_SEND_WINDOW_CLOSED,
            }
        if res.outcome != "sent":
            return _futu_uncertain_reply(res.message, res.reason_code or futu_orders.SEND_UNCERTAIN)
        read_at, ev = _futu_fresh(ctx, order_id, scope)
        return {
            "status": "ok",
            "row": ev.row if ev is not None else None,
            "read_at": read_at,
            "qty": ev.qty if ev is not None else None,
            "price": ev.price if ev is not None else None,
            "sent_qty": quantity,
            "sent_price": price,
        }

    def finish(reply: dict) -> Any:
        _futu_event(
            submission_id,
            "MODIFY",
            {
                "status": reply.get("status"),
                "message": reply.get("message"),
                "amend_outcome": reply.get("amend_outcome"),
                "applied_sequence": sequence,
            },
        )
        if reply.get("status") != "ok":
            # Proven unsent releases the amend; anything else keeps the
            # larger reservation (gate4.settle_modify "uncertain").
            if gen.get("amend_id") is not None:
                gate4.settle_modify(
                    submission_id,
                    gen["amend_id"],
                    "rejected" if reply.get("amend_outcome") == "not_sent" else "uncertain",
                    scope_d,
                )
            return _futu_manage_error(reply, "modify", applied_sequence=sequence)
        got_qty, sent_qty, sent_price = reply.get("qty"), reply["sent_qty"], reply["sent_price"]
        # Same tick on the price grid (a sub-dollar stock tick is 0.0001), never a tolerance.
        confirmed = (
            got_qty is not None
            and got_qty == sent_qty
            and futu_orders.tick_price(base_spec, reply.get("price")) == sent_price
        )
        # Settle only on fresh evidence of the new terms; otherwise the
        # amend stays uncertain and keeps max(old, outgoing) until an
        # operator resolves it (runbook § 5).
        if confirmed:
            gate4.settle_modify(
                submission_id, gen.get("amend_id"), "confirmed", scope_d, new_qty=sent_qty, new_limit_price=sent_price
            )
        else:
            gate4.settle_modify(submission_id, gen.get("amend_id"), "uncertain", scope_d)
        _futu_mirror(scope, reply.get("row"), reply.get("read_at"))
        return {
            "status": "ok",
            "brokerOrderId": order_id,
            "confirmed": confirmed,
            "confirmedQuantity": int(sent_qty) if confirmed else None,
            "confirmedPrice": str(sent_price) if confirmed else None,
            "applied_sequence": sequence,
            "message": "Modify confirmed by Futu"
            if confirmed
            else "Modify sent; Futu has not reported the new terms yet",
        }

    return await _futu_run(core, finish, scope, "modify", applied_sequence=sequence)
