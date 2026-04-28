"""Quote-token verify + tick-grid + limit-band + market-hours gate.

Spec: docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md §7.
"""

from __future__ import annotations

import time
from datetime import datetime
from decimal import Decimal
from threading import Lock
from typing import Callable, Literal

from pydantic import BaseModel

from xenon.execution import quote_tokens
from xenon.execution.market_hours import is_opt_tradeable
from xenon.execution.preflight import ReasonCode

MAX_AGE_RTH_MS = 500


class QuoteVerdict(BaseModel):
    accept: bool
    reason_code: ReasonCode | None = None
    reason_detail: str | None = None


def check_market_hours(
    *,
    security_type: Literal["STK", "OPT"],
    now: datetime,
) -> QuoteVerdict:
    if security_type == "OPT" and not is_opt_tradeable(now):
        return QuoteVerdict(
            accept=False,
            reason_code=ReasonCode.OPTION_MARKET_CLOSED,
            reason_detail="equity-option market closed (09:30-16:00 ET weekdays)",
        )
    return QuoteVerdict(accept=True)


def _on_tick(price: Decimal, min_tick: Decimal) -> bool:
    return (price % min_tick) == Decimal("0")


def check_payload(
    *,
    payload: quote_tokens.QuotePayload,
    con_id: int,
    ticker: str,
    security_type: Literal["STK", "OPT"],
    action: Literal["BUY", "SELL"],
    limit_price: Decimal,
    now: datetime,
    tick_rule_lookup: Callable[[int], Decimal],
) -> QuoteVerdict:
    market = check_market_hours(security_type=security_type, now=now)
    if not market.accept:
        return market

    if payload.ticker.upper() != ticker.upper() or payload.con_id != con_id:
        return QuoteVerdict(
            accept=False,
            reason_code=ReasonCode.QUOTE_CONTRACT_MISMATCH,
            reason_detail="token contract mismatch",
        )

    if payload.bid > payload.ask or payload.bid_size <= 0 or payload.ask_size <= 0:
        return QuoteVerdict(
            accept=False,
            reason_code=ReasonCode.QUOTE_UNAVAILABLE,
            reason_detail="crossed or zero-size quote",
        )

    min_tick = tick_rule_lookup(con_id)
    if not _on_tick(limit_price, min_tick):
        return QuoteVerdict(
            accept=False,
            reason_code=ReasonCode.LIMIT_OFF_TICK,
            reason_detail=f"limit {limit_price} not on tick grid {min_tick}",
        )

    two_ticks = min_tick * Decimal("2")
    if action == "BUY":
        cap = min(payload.ask * Decimal("1.05"), payload.ask + two_ticks)
        if limit_price > cap:
            return QuoteVerdict(
                accept=False,
                reason_code=ReasonCode.LIMIT_OUT_OF_BAND,
                reason_detail=f"BUY limit {limit_price} > cap {cap} (ask {payload.ask})",
            )
    else:
        floor = max(payload.bid * Decimal("0.95"), payload.bid - two_ticks)
        if limit_price < floor:
            return QuoteVerdict(
                accept=False,
                reason_code=ReasonCode.LIMIT_OUT_OF_BAND,
                reason_detail=f"SELL limit {limit_price} < floor {floor} (bid {payload.bid})",
            )

    return QuoteVerdict(accept=True)


def check(
    *,
    token: str,
    token_secret: str,
    con_id: int,
    ticker: str,
    security_type: Literal["STK", "OPT"],
    action: Literal["BUY", "SELL"],
    limit_price: Decimal,
    now: datetime,
    tick_rule_lookup: Callable[[int], Decimal],
) -> QuoteVerdict:
    try:
        payload = quote_tokens.verify(token, token_secret, max_age_ms=MAX_AGE_RTH_MS)
    except quote_tokens.QuoteTokenExpired as exc:
        return QuoteVerdict(accept=False, reason_code=ReasonCode.STALE_QUOTE, reason_detail=str(exc))
    except quote_tokens.QuoteTokenInvalid as exc:
        return QuoteVerdict(
            accept=False,
            reason_code=ReasonCode.STALE_QUOTE,
            reason_detail=f"token invalid: {exc}",
        )

    return check_payload(
        payload=payload,
        con_id=con_id,
        ticker=ticker,
        security_type=security_type,
        action=action,
        limit_price=limit_price,
        now=now,
        tick_rule_lookup=tick_rule_lookup,
    )


class TickRuleCache:
    """Per-con_id minTick cache with TTL (default 24h per SL §7)."""

    def __init__(self, source: Callable[[int], Decimal], ttl_seconds: int = 24 * 3600):
        self._source = source
        self._ttl = ttl_seconds
        self._lock = Lock()
        self._cache: dict[int, tuple[float, Decimal]] = {}

    def get(self, con_id: int) -> Decimal:
        with self._lock:
            entry = self._cache.get(con_id)
            now = time.monotonic()
            if entry is not None and (now - entry[0]) < self._ttl:
                return entry[1]
            value = self._source(con_id)
            self._cache[con_id] = (now, value)
            return value
