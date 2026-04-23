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

_MAX_AGE_RTH_MS = 500


class QuoteVerdict(BaseModel):
    accept: bool
    reason_code: ReasonCode | None = None
    reason_detail: str | None = None


def _on_tick(price: Decimal, min_tick: Decimal) -> bool:
    return (price % min_tick) == Decimal("0")


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
    if security_type == "OPT" and not is_opt_tradeable(now):
        return QuoteVerdict(
            accept=False,
            reason_code=ReasonCode.STALE_QUOTE,
            reason_detail="equity-option market closed (09:30-16:00 ET weekdays)",
        )

    max_age = _MAX_AGE_RTH_MS
    try:
        payload = quote_tokens.verify(token, token_secret, max_age_ms=max_age)
    except quote_tokens.QuoteTokenExpired as exc:
        return QuoteVerdict(accept=False, reason_code=ReasonCode.STALE_QUOTE, reason_detail=str(exc))
    except quote_tokens.QuoteTokenInvalid as exc:
        return QuoteVerdict(
            accept=False,
            reason_code=ReasonCode.STALE_QUOTE,
            reason_detail=f"token invalid: {exc}",
        )

    if payload.ticker.upper() != ticker.upper() or payload.con_id != con_id:
        return QuoteVerdict(
            accept=False,
            reason_code=ReasonCode.STALE_QUOTE,
            reason_detail="token contract mismatch",
        )

    if payload.bid > payload.ask or payload.bid_size <= 0 or payload.ask_size <= 0:
        return QuoteVerdict(
            accept=False,
            reason_code=ReasonCode.STALE_QUOTE,
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


class CheckComboLeg(BaseModel):
    token: str
    con_id: int
    ticker: str
    action: Literal["BUY", "SELL"]
    right: Literal["C", "P", "STK"]
    ratio: int = 1


def _compute_execution_net(
    leg_payloads: list[tuple["quote_tokens.QuotePayload", str, int]],
    envelope_action: str,
) -> Decimal:
    """Return the signed net price IB would execute this combo at.

    IB semantics (per web/CLAUDE.md "IB Combo (BAG) Order Leg Convention"):
      - envelope=BUY executes legs as-labeled (BUY leg pays ask,
        SELL leg receives bid).
      - envelope=SELL reverses leg actions (BUY leg receives bid,
        SELL leg pays ask).

    Returned sign convention: positive = user pays (debit), negative =
    user receives (credit). This matches "pay ask on effective-BUY legs,
    receive bid on effective-SELL legs".

    For LONG debit spread open (BUY env on [BUY,SELL]) → positive debit.
    For LONG debit spread close (SELL env on [BUY,SELL]) → negative credit.
    For SHORT credit spread open (SELL env on [SELL,BUY]) → positive debit
      of the SHORT structure as-reversed, i.e. the debit user pays when
      IB reverses the legs (equivalent to the debit to CLOSE, not open) —
      except here envelope SELL means the user sold the (as-labeled) combo
      at a net they'd accept.
    Callers should take abs() of this value and compare to abs(limit_price)
    when banding, since user-entered limits are conventionally positive.
    """
    exec_net = Decimal("0")
    for payload, leg_action, ratio in leg_payloads:
        r = Decimal(ratio)
        # Effective direction after envelope: match = pay ask, mismatch = receive bid.
        if envelope_action == leg_action:
            exec_net += payload.ask * r
        else:
            exec_net -= payload.bid * r
    return exec_net


def check_combo(
    *,
    legs: list[CheckComboLeg],
    envelope_action: Literal["BUY", "SELL"],
    limit_price: Decimal,
    token_secret: str,
    now: datetime,
) -> QuoteVerdict:
    if not legs:
        return QuoteVerdict(
            accept=False,
            reason_code=ReasonCode.STALE_QUOTE,
            reason_detail="no legs",
        )
    if any(leg.right in ("C", "P") for leg in legs) and not is_opt_tradeable(now):
        return QuoteVerdict(
            accept=False,
            reason_code=ReasonCode.STALE_QUOTE,
            reason_detail="equity-option market closed (09:30-16:00 ET weekdays)",
        )

    max_age = _MAX_AGE_RTH_MS
    leg_payloads: list[tuple[quote_tokens.QuotePayload, str, int]] = []
    for leg in legs:
        try:
            payload = quote_tokens.verify(leg.token, token_secret, max_age_ms=max_age)
        except quote_tokens.QuoteTokenExpired as exc:
            return QuoteVerdict(
                accept=False,
                reason_code=ReasonCode.STALE_QUOTE,
                reason_detail=f"leg {leg.con_id}: {exc}",
            )
        except quote_tokens.QuoteTokenInvalid as exc:
            return QuoteVerdict(
                accept=False,
                reason_code=ReasonCode.STALE_QUOTE,
                reason_detail=f"leg {leg.con_id}: token invalid: {exc}",
            )
        if payload.ticker.upper() != leg.ticker.upper() or payload.con_id != leg.con_id:
            return QuoteVerdict(
                accept=False,
                reason_code=ReasonCode.STALE_QUOTE,
                reason_detail=f"leg {leg.con_id}: token contract mismatch",
            )
        if payload.bid > payload.ask or payload.bid_size <= 0 or payload.ask_size <= 0:
            return QuoteVerdict(
                accept=False,
                reason_code=ReasonCode.STALE_QUOTE,
                reason_detail=f"leg {leg.con_id}: crossed or zero-size quote",
            )
        leg_payloads.append((payload, leg.action, leg.ratio))

    exec_net = _compute_execution_net(leg_payloads, envelope_action)

    # Which side of the band protects the user depends on the SIGN of
    # exec_net (debit vs credit), NOT envelope_action directly.
    #   exec_net > 0  → trade is a net debit; user pays.
    #                    Cap limit at +5% to block fat-finger "paying too much".
    #   exec_net < 0  → trade is a net credit; user receives.
    #                    Floor limit at −5% to block fat-finger "accepting too little credit".
    # The previous implementation keyed on envelope_action alone, which
    # produced asymmetric holes: a short-credit close (envelope=SELL but
    # exec_net>0 debit) got floor-checked instead of capped, letting a
    # +27 fat-finger through on a ~2.70 debit close.
    abs_net = abs(exec_net)
    abs_limit = abs(limit_price)
    tolerance = abs_net * Decimal("0.05")

    if exec_net > 0:
        cap = abs_net + tolerance
        if abs_limit > cap:
            return QuoteVerdict(
                accept=False,
                reason_code=ReasonCode.LIMIT_OUT_OF_BAND,
                reason_detail=f"debit limit |{limit_price}| > cap {cap} (|exec_net|={abs_net})",
            )
    elif exec_net < 0:
        floor = abs_net - tolerance
        if abs_limit < floor:
            return QuoteVerdict(
                accept=False,
                reason_code=ReasonCode.LIMIT_OUT_OF_BAND,
                reason_detail=f"credit limit |{limit_price}| < floor {floor} (|exec_net|={abs_net})",
            )
    # exec_net == 0 (crossed quotes net to zero): any non-tiny limit would
    # be suspect. Cap at an absolute tolerance of 0.05 to reject spikes.
    elif abs_limit > Decimal("0.05"):
        return QuoteVerdict(
            accept=False,
            reason_code=ReasonCode.LIMIT_OUT_OF_BAND,
            reason_detail=f"exec_net=0 but limit |{limit_price}| is non-trivial",
        )

    return QuoteVerdict(accept=True)
