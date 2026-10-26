"""Quote-payload tick-grid + limit-band + market-hours gate (token verify lives in quote_tokens).

Spec: docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md §7.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from threading import Lock
from typing import Callable, Literal

from pydantic import BaseModel

from xenon.execution import quote_tokens
from xenon.execution.preflight import ReasonCode
from xenon.utils import market_calendar

logger = logging.getLogger(__name__)

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
    # Equity options trade the regular session only. A year missing from the
    # holiday file degrades to the weekday rule (logged): IB stays the final gate.
    if security_type == "OPT" and not market_calendar.is_open(now, strict=False):
        return QuoteVerdict(
            accept=False,
            reason_code=ReasonCode.OPTION_MARKET_CLOSED,
            reason_detail="equity-option market closed (09:30-16:00 ET on US trading days)",
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


# Per-type IB quote rules of a FUTU order (X5 plan § 4; contract § Place).
# LMT keeps `check_payload`; every other type needs the same sized,
# non-crossed quote (also in ETH / OVERNIGHT), then its own rule.
STOP_TYPES = ("STP", "STP LMT")  # fires when the market trades through the trigger
TOUCH_TYPES = ("MIT", "LIT")  # fires when the market touches the trigger
TRIGGER_LIMIT_TYPES = ("STP LMT", "LIT")  # the limit bounds the fill: trigger rule overridable
TRAIL_TYPES = ("TRAIL", "TRAIL LIMIT")
MKT_SPREAD_MAX = Decimal("0.01")  # of mid
TRAIL_AMOUNT_MAX = Decimal("0.20")  # of the IB reference price (mid)
TRAIL_SPREAD_MAX = Decimal("0.05")  # of the IB reference price (mid)
MKT_NOTIONAL_CAP_ENV = "XENON_FUTU_MKT_NOTIONAL_CAP"
MKT_NOTIONAL_CAP_DEFAULT = Decimal("25000")  # USD


def mkt_notional_cap(environ: Mapping[str, str] | None = None) -> Decimal | None:
    """`XENON_FUTU_MKT_NOTIONAL_CAP` (USD, default 25 000). None for a value
    that is not a positive finite number: every MKT order is then refused
    (fail closed, never a guessed cap)."""
    raw = (os.environ if environ is None else environ).get(MKT_NOTIONAL_CAP_ENV)
    if raw is None or raw.strip() == "":
        return MKT_NOTIONAL_CAP_DEFAULT
    try:
        cap = Decimal(raw.strip())
    except InvalidOperation:
        cap = None
    if cap is None or not cap.is_finite() or cap <= 0:
        logger.error("%s=%r is not a positive number; market orders are refused", MKT_NOTIONAL_CAP_ENV, raw)
        return None
    return cap


def _refuse(code: ReasonCode, detail: str) -> QuoteVerdict:
    return QuoteVerdict(accept=False, reason_code=code, reason_detail=detail)


def overridable(order_type: str, verdict: QuoteVerdict) -> bool:
    """`acknowledge_limit_override` may accept this refusal of a place: the
    limit band of any limit type, and the trigger rule of STP LMT / LIT. A
    would-fire STP / MIT (an immediate market order), the MKT rules and the
    trail bounds are never overridable; modify takes no override at all."""
    return verdict.reason_code == ReasonCode.LIMIT_OUT_OF_BAND or (
        verdict.reason_code == ReasonCode.TRIGGER_WOULD_FIRE and order_type in TRIGGER_LIMIT_TYPES
    )


def check_terms(
    *,
    payload: quote_tokens.QuotePayload,
    con_id: int,
    ticker: str,
    security_type: Literal["STK", "OPT"],
    action: Literal["BUY", "SELL"],
    order_type: str,
    now: datetime,
    tick_rule_lookup: Callable[[int], Decimal],
    mkt_notional_cap: Decimal | None,
    quantity: int | None = None,
    limit_price: Decimal | None = None,
    aux_price: Decimal | None = None,
    trail_type: str | None = None,
    trail_value: Decimal | None = None,
    trail_spread: Decimal | None = None,
) -> list[QuoteVerdict]:
    """The IB quote refusals of one order's full terms, [] = pass. A
    non-overridable refusal comes alone; otherwise every overridable one
    (`overridable`) is listed, so an override accepts them all knowingly.
    The terms already passed the Futu field / precision checks."""
    if order_type == "LMT":
        v = check_payload(
            payload=payload,
            con_id=con_id,
            ticker=ticker,
            security_type=security_type,
            action=action,
            limit_price=limit_price if limit_price is not None else Decimal(0),
            now=now,
            tick_rule_lookup=tick_rule_lookup,
        )
        return [] if v.accept else [v]
    market = check_market_hours(security_type=security_type, now=now)
    if not market.accept:
        return [market]
    if payload.ticker.upper() != ticker.upper() or payload.con_id != con_id:
        return [_refuse(ReasonCode.QUOTE_CONTRACT_MISMATCH, "token contract mismatch")]
    bid, ask = payload.bid, payload.ask
    if bid > ask or bid <= 0 or payload.bid_size <= 0 or payload.ask_size <= 0:
        return [_refuse(ReasonCode.QUOTE_UNAVAILABLE, "crossed, zero-price or zero-size quote")]
    mid = (bid + ask) / 2
    min_tick = tick_rule_lookup(con_id)
    for name, price in (("limit", limit_price), ("trigger", aux_price)):
        if price is not None and not _on_tick(price, min_tick):
            return [_refuse(ReasonCode.LIMIT_OFF_TICK, f"{name} {price} not on tick grid {min_tick}")]

    if order_type == "MKT":
        try:
            open_now = market_calendar.is_open(now)
        except market_calendar.CalendarYearMissing:
            open_now = False  # fail closed: no holiday data, no market order
        if not open_now:
            return [
                _refuse(
                    ReasonCode.FUTU_SESSION_UNSUPPORTED,
                    "market orders need the US regular session (09:30-16:00 ET on trading days)",
                )
            ]
        if ask - bid > mid * MKT_SPREAD_MAX:
            return [_refuse(ReasonCode.MKT_SPREAD_TOO_WIDE, f"spread {ask - bid} > 1% of mid {mid}")]
        notional = Decimal(quantity or 0) * ask
        if mkt_notional_cap is None or notional > mkt_notional_cap:
            return [
                _refuse(
                    ReasonCode.MKT_NOTIONAL_CAP,
                    f"quantity x ask {notional} > {MKT_NOTIONAL_CAP_ENV} {mkt_notional_cap}",
                )
            ]
        return []

    failures: list[QuoteVerdict] = []
    if order_type in STOP_TYPES + TOUCH_TYPES:
        if aux_price is None:
            return [_refuse(ReasonCode.FUTU_INVALID_ORDER, f"{order_type} orders need a trigger price")]
        stop = order_type in STOP_TYPES
        safe = {
            ("BUY", True): aux_price > ask,
            ("SELL", True): aux_price < bid,
            ("BUY", False): aux_price < bid,
            ("SELL", False): aux_price > ask,
        }[(action, stop)]
        if not safe:
            side = {
                ("BUY", True): "above the ask",
                ("SELL", True): "below the bid",
                ("BUY", False): "below the bid",
                ("SELL", False): "above the ask",
            }[(action, stop)]
            v = _refuse(
                ReasonCode.TRIGGER_WOULD_FIRE,
                f"{action} {order_type} trigger {aux_price} must be {side} (bid {bid}, ask {ask}); it would fire at once",
            )
            if order_type not in TRIGGER_LIMIT_TYPES:
                return [v]
            failures.append(v)
    if order_type in TRIGGER_LIMIT_TYPES:
        if limit_price is None:
            return [_refuse(ReasonCode.FUTU_INVALID_ORDER, f"{order_type} orders need a limit price")]
        two_ticks = min_tick * 2
        if action == "BUY":
            low, high = aux_price, min(aux_price * Decimal("1.05"), aux_price + two_ticks)
        else:
            low, high = max(aux_price * Decimal("0.95"), aux_price - two_ticks), aux_price
        if not low <= limit_price <= high:
            failures.append(
                _refuse(
                    ReasonCode.LIMIT_OUT_OF_BAND,
                    f"{action} {order_type} limit {limit_price} outside [{low}, {high}] around trigger {aux_price}",
                )
            )
        return failures
    if order_type in STOP_TYPES + TOUCH_TYPES:
        return failures
    if order_type in TRAIL_TYPES:
        if trail_type == "AMOUNT" and (trail_value is None or trail_value > mid * TRAIL_AMOUNT_MAX):
            return [_refuse(ReasonCode.FUTU_INVALID_ORDER, f"trail amount {trail_value} > 20% of the IB mid {mid}")]
        if trail_spread is not None and trail_spread > mid * TRAIL_SPREAD_MAX:
            return [_refuse(ReasonCode.FUTU_INVALID_ORDER, f"trail spread {trail_spread} > 5% of the IB mid {mid}")]
        return []
    return [_refuse(ReasonCode.FUTU_ORDER_TYPE_UNSUPPORTED, f"no IB quote rule for {order_type} orders")]
