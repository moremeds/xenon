"""Server-side Gate 4 preflight evaluation.

Spec: docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md §5

This is a pure function over an injected PortfolioView so it's trivially
testable. Wiring into FastAPI /orders/place is in src/xenon/api/server.py.

Working-order reservations are stubbed empty in F2 (see WorkingReservations
below). Phase F4 will replace the stub with a duckdb-backed read from
orders_submissions per spec §12.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from xenon.execution.universe import UNIVERSE, is_index, is_known


class ReasonCode(StrEnum):
    """Preflight block reasons. UI copy maps these in F6.

    Only codes relevant to F2 are defined here. F3 adds STALE_QUOTE /
    LIMIT_OUT_OF_BAND / LIMIT_OFF_TICK; F4 adds ATTEMPT_ID_TERMINAL;
    F5 adds IB_CONNECTION / OWNERSHIP; F6 adds MODIFY_STALE.
    """

    UNIVERSE_UNKNOWN = "UNIVERSE_UNKNOWN"
    INDEX_HAS_NO_STOCK = "INDEX_HAS_NO_STOCK"
    INSUFFICIENT_SHARES = "INSUFFICIENT_SHARES"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    INDEX_CALL_UNCOVERED = "INDEX_CALL_UNCOVERED"
    ETF_CALL_UNCOVERED = "ETF_CALL_UNCOVERED"


class PreflightRequest(BaseModel):
    """Server-side input to evaluate(). Constructed from the /orders/place body."""

    ticker: str
    security_type: Literal["STK", "OPT"]
    action: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    right: Literal["C", "P"] | None = None
    expiry: str | None = None
    strike: Decimal | None = None
    multiplier: int = 100
    limit_price: Decimal


class PortfolioLeg(BaseModel):
    direction: Literal["LONG", "SHORT"]
    type: Literal["Stock", "Call", "Put"]
    contracts: int
    strike: float = 0.0


class PortfolioPosition(BaseModel):
    ticker: str
    structure_type: str
    direction: Literal["LONG", "SHORT"] = "LONG"
    contracts: int
    expiry: str | None = None
    legs: list[PortfolioLeg]


class PortfolioView(BaseModel):
    """Snapshot injected into evaluate(). Matches data/portfolio.json shape.

    F5 migrates the source to live IB pool; for F2 the callsite
    (server.py) loads from portfolio.json for parity with the TS guard.
    """

    positions: list[PortfolioPosition] = Field(default_factory=list)
    available_funds: Decimal = Decimal("0")


class WorkingReservations(BaseModel):
    """Placeholder for F4. Always empty in F2."""

    stock_sell_qty: int = 0
    short_call_qty: int = 0
    short_put_cash_required: Decimal = Decimal("0")
    long_call_close_qty_same_exp: int = 0


class Verdict(BaseModel):
    accept: bool
    reason_code: ReasonCode | None = None
    reason_detail: str | None = None


def _normalize_expiry(expiry: str | None) -> str | None:
    if not expiry:
        return None
    clean = expiry.replace("-", "")
    return clean if len(clean) == 8 and clean.isdigit() else None


def _count_long_shares(positions: list[PortfolioPosition], ticker: str) -> int:
    total = 0
    for pos in positions:
        if pos.ticker.upper() != ticker.upper():
            continue
        for leg in pos.legs:
            if leg.type == "Stock" and leg.direction == "LONG":
                total += leg.contracts
    return total


def _count_long_calls_at_expiry(positions: list[PortfolioPosition], ticker: str, expiry: str | None) -> int:
    normalized = _normalize_expiry(expiry)
    if normalized is None:
        return 0
    total = 0
    for pos in positions:
        if pos.ticker.upper() != ticker.upper():
            continue
        if _normalize_expiry(pos.expiry) != normalized:
            continue
        for leg in pos.legs:
            if leg.direction == "LONG" and leg.type == "Call":
                total += leg.contracts
    return total


def _count_matching_long_options(
    positions: list[PortfolioPosition],
    ticker: str,
    expiry: str | None,
    strike: Decimal | None,
    right: Literal["C", "P"],
) -> int:
    normalized = _normalize_expiry(expiry)
    if normalized is None or strike is None:
        return 0
    expected = "Call" if right == "C" else "Put"
    total = 0
    for pos in positions:
        if pos.ticker.upper() != ticker.upper():
            continue
        if _normalize_expiry(pos.expiry) != normalized:
            continue
        for leg in pos.legs:
            if leg.direction == "LONG" and leg.type == expected and Decimal(str(leg.strike)) == strike:
                total += leg.contracts
    return total


def _count_existing_short_calls(positions: list[PortfolioPosition], ticker: str) -> int:
    total = 0
    for pos in positions:
        if pos.ticker.upper() != ticker.upper():
            continue
        for leg in pos.legs:
            if leg.type == "Call" and leg.direction == "SHORT":
                total += leg.contracts
    return total


def evaluate(
    req: PreflightRequest,
    portfolio: PortfolioView,
    reservations: WorkingReservations | None = None,
) -> Verdict:
    """Evaluate Gate 4 server-side. Pure function.

    F2: universe + Gate 4 using `portfolio` (live-like view) and empty-by-default
    `reservations` (F4 replaces the stub with duckdb reads).
    """
    reservations = reservations or WorkingReservations()

    # ① Universe
    if not is_known(req.ticker):
        return Verdict(
            accept=False,
            reason_code=ReasonCode.UNIVERSE_UNKNOWN,
            reason_detail=f"{req.ticker} not in V1 universe",
        )

    if req.security_type == "STK" and is_index(req.ticker):
        return Verdict(
            accept=False,
            reason_code=ReasonCode.INDEX_HAS_NO_STOCK,
            reason_detail=f"{req.ticker} is an index — no stock leg exists",
        )

    # ② BUY never creates short exposure
    if req.action == "BUY":
        return Verdict(accept=True)

    # ③ Stock SELL — must be covered by shares (minus working sells)
    if req.security_type == "STK":
        held = _count_long_shares(portfolio.positions, req.ticker)
        available = held - reservations.stock_sell_qty
        if req.quantity > available:
            return Verdict(
                accept=False,
                reason_code=ReasonCode.INSUFFICIENT_SHARES,
                reason_detail=(
                    f"SELL {req.quantity} shares of {req.ticker} exceeds "
                    f"{available} available ({held} held, "
                    f"{reservations.stock_sell_qty} reserved)"
                ),
            )
        return Verdict(accept=True)

    # ④ Option SELL
    # SELL put — cash-secured; F2 accepts unconditionally (F4 will enforce funds)
    if req.right == "P":
        return Verdict(accept=True)

    # SELL call — Gate 4
    if req.right == "C":
        # Sell-to-close exact match
        closing = _count_matching_long_options(portfolio.positions, req.ticker, req.expiry, req.strike, "C")
        remaining_after_close = max(req.quantity - closing, 0)
        if remaining_after_close == 0:
            return Verdict(accept=True)

        # Vertical spread cover at same expiry
        long_at_expiry = _count_long_calls_at_expiry(portfolio.positions, req.ticker, req.expiry)
        working_closes = reservations.long_call_close_qty_same_exp
        long_cover_available = max(long_at_expiry - closing - working_closes, 0)
        remaining_after_spread = max(remaining_after_close - long_cover_available, 0)
        if remaining_after_spread == 0:
            return Verdict(accept=True)

        # Index: stock cover impossible
        if is_index(req.ticker):
            return Verdict(
                accept=False,
                reason_code=ReasonCode.INDEX_CALL_UNCOVERED,
                reason_detail=(
                    f"SELL {req.quantity} {req.ticker} call(s) at expiry {req.expiry}: "
                    f"index options require long-call cover (same expiry); "
                    f"{long_cover_available} contracts available"
                ),
            )

        # ETF: fall back to stock cover
        existing_short = _count_existing_short_calls(portfolio.positions, req.ticker)
        shares = _count_long_shares(portfolio.positions, req.ticker)
        share_cover_units = max(shares - reservations.stock_sell_qty, 0) // req.multiplier

        total_cover = share_cover_units + long_cover_available
        total_short_after = existing_short + reservations.short_call_qty + remaining_after_spread
        if total_cover < total_short_after:
            return Verdict(
                accept=False,
                reason_code=ReasonCode.ETF_CALL_UNCOVERED,
                reason_detail=(
                    f"SELL {req.quantity} {req.ticker} call(s): total short after fill "
                    f"({total_short_after}) exceeds cover ({total_cover}) — "
                    f"{share_cover_units} from shares + {long_cover_available} from long calls"
                ),
            )
        return Verdict(accept=True)

    # Option SELL with no right shouldn't reach here thanks to pydantic validation,
    # but return a safe reject:
    return Verdict(
        accept=False,
        reason_code=ReasonCode.UNIVERSE_UNKNOWN,
        reason_detail="option SELL without right (C/P) is not permitted",
    )
