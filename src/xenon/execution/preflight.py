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
    """Preflight / submit-gate block reasons. UI copy maps these in F6.

    F5 adds IB_CONNECTION / OWNERSHIP; F6 adds MODIFY_STALE.
    """

    # F2 — preflight (PR-A)
    UNIVERSE_UNKNOWN = "UNIVERSE_UNKNOWN"
    INDEX_HAS_NO_STOCK = "INDEX_HAS_NO_STOCK"
    INSUFFICIENT_SHARES = "INSUFFICIENT_SHARES"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    INDEX_CALL_UNCOVERED = "INDEX_CALL_UNCOVERED"
    ETF_CALL_UNCOVERED = "ETF_CALL_UNCOVERED"
    INVALID_ORDER_BODY = "INVALID_ORDER_BODY"
    # F3 — quote gate (PR-B)
    STALE_QUOTE = "STALE_QUOTE"
    OPTION_MARKET_CLOSED = "OPTION_MARKET_CLOSED"
    QUOTE_CONTRACT_MISMATCH = "QUOTE_CONTRACT_MISMATCH"
    QUOTE_UNAVAILABLE = "QUOTE_UNAVAILABLE"
    LIMIT_OUT_OF_BAND = "LIMIT_OUT_OF_BAND"
    LIMIT_OFF_TICK = "LIMIT_OFF_TICK"
    # F4 — idempotency (PR-B)
    ATTEMPT_ID_TERMINAL = "ATTEMPT_ID_TERMINAL"
    # F5 — cancel/modify failure classification (PR-C)
    IB_CONNECTION = "IB_CONNECTION"
    OWNERSHIP = "OWNERSHIP"
    IB_REJECT = "IB_REJECT"
    MODIFY_STALE = "MODIFY_STALE"
    MODIFY_SEQUENCE_REQUIRED = "MODIFY_SEQUENCE_REQUIRED"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    ORDER_IDENTIFIER_REQUIRED = "ORDER_IDENTIFIER_REQUIRED"
    PORTFOLIO_SNAPSHOT_REQUIRED = "PORTFOLIO_SNAPSHOT_REQUIRED"
    PORTFOLIO_SNAPSHOT_STALE = "PORTFOLIO_SNAPSHOT_STALE"
    READ_ONLY_BROKER = "READ_ONLY_BROKER"
    # F7 — pending timeout (PR-D)
    PENDING_TIMEOUT = "PENDING_TIMEOUT"
    # B5 — hard subprocess failure on /orders/place (non-2xx from runner).
    SUBPROCESS_ERROR = "SUBPROCESS_ERROR"


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
    # Multi-currency (Japan/Korea). A non-USD cash equity is out of the US-only
    # V1 universe by definition — gated on currency, not the symbol whitelist.
    currency: str = "USD"
    exchange: str | None = None


class ComboPreflightLeg(BaseModel):
    expiry: str | None = None
    strike: Decimal | None = None
    right: Literal["C", "P"]
    action: Literal["BUY", "SELL"]
    ratio: int = Field(gt=0)


class ComboPreflightRequest(BaseModel):
    ticker: str
    action: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    multiplier: int = 100
    legs: list[ComboPreflightLeg] = Field(min_length=1)


class PortfolioLeg(BaseModel):
    direction: Literal["LONG", "SHORT"]
    type: Literal["Stock", "Call", "Put"]
    contracts: int
    strike: Decimal = Decimal("0")


class PortfolioPosition(BaseModel):
    ticker: str
    structure_type: str
    # The portfolio.json producer emits LONG / SHORT for single legs and
    # DEBIT / CREDIT / COMBO for spreads and multi-leg structures. preflight
    # never reads position-level direction (all counting walks `legs`), so
    # we accept any string here — a Literal restriction would cause
    # ValidationError on real snapshots and fail-open the gate entirely.
    direction: str = "LONG"
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


def combo_uncovered_short_call_ratio(req: ComboPreflightRequest) -> int:
    """Per-contract count of uncovered short calls implied by this combo.

    Counts leg-level actions only — the BAG envelope (`req.action`) is
    untrusted because IB's open-vs-close convention can be inverted by a
    buggy or malicious client. A combo where short calls outnumber long
    calls leaves naked exposure regardless of whether the user labels the
    envelope BUY or SELL.
    """
    sell_call_ratio = sum(leg.ratio for leg in req.legs if leg.action == "SELL" and leg.right == "C")
    buy_call_ratio = sum(leg.ratio for leg in req.legs if leg.action == "BUY" and leg.right == "C")
    return max(sell_call_ratio - buy_call_ratio, 0)


def combo_close_covered_by_portfolio(
    req: ComboPreflightRequest,
    portfolio: PortfolioView,
) -> bool:
    """True iff every leg of `req` has an opposite-direction inverse in the
    portfolio with sufficient contracts.

    Used by the regime gate to decide whether a combo SELL/BUY is genuinely
    closing existing exposure (and therefore exempt from new-exposure tier
    blocks). Conservative: aggregates supply across all matching positions
    but requires the entire combo to be 100% covered. Only same-expiry
    combos are recognised — calendar spreads fall through to the gate.
    """
    if not req.legs:
        return False
    leg_expiries = {leg.expiry for leg in req.legs}
    if len(leg_expiries) != 1:
        return False
    wanted_expiry = _normalize_expiry(next(iter(leg_expiries)))
    if wanted_expiry is None:
        return False

    needs: dict[tuple[str, Decimal, str], int] = {}
    for leg in req.legs:
        if leg.strike is None:
            return False
        want_dir = "SHORT" if leg.action == "BUY" else "LONG"
        want_type = "Call" if leg.right == "C" else "Put"
        key = (want_type, leg.strike, want_dir)
        needs[key] = needs.get(key, 0) + leg.ratio * req.quantity

    supply: dict[tuple[str, Decimal, str], int] = {}
    for pos in portfolio.positions:
        if pos.ticker.upper() != req.ticker.upper():
            continue
        if _normalize_expiry(pos.expiry) != wanted_expiry:
            continue
        for leg in pos.legs:
            if leg.type == "Stock":
                continue
            key = (leg.type, leg.strike, leg.direction)
            supply[key] = supply.get(key, 0) + int(leg.contracts)

    return all(supply.get(key, 0) >= want for key, want in needs.items())


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
            if leg.direction == "LONG" and leg.type == expected and leg.strike == strike:
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


def evaluate_combo(
    req: ComboPreflightRequest,
    portfolio: PortfolioView,
    reservations: WorkingReservations | None = None,
    cover_ratio: float = 1.0,
) -> Verdict:
    """Evaluate Gate 4 for IB BAG combos.

    The BAG envelope action stays BUY for opens; leg actions define the
    structure. Closing combo envelopes (SELL) reduce exposure and are allowed
    after universe validation.

    `cover_ratio` tightens the share-cover requirement per short call.
    Default 1.0 = standard Gate 4 (1 contract = `multiplier` shares).
    `RegimeGate` passes 1.25 on TIER_2 to require 125 shares per call —
    this is the only path that should change the ratio.
    """
    reservations = reservations or WorkingReservations()

    if not is_known(req.ticker):
        return Verdict(
            accept=False,
            reason_code=ReasonCode.UNIVERSE_UNKNOWN,
            reason_detail=f"{req.ticker} not in V1 universe",
        )

    uncovered_ratio = combo_uncovered_short_call_ratio(req)
    if uncovered_ratio <= 0:
        return Verdict(accept=True)

    new_uncovered_calls = uncovered_ratio * req.quantity
    if is_index(req.ticker):
        return Verdict(
            accept=False,
            reason_code=ReasonCode.INDEX_CALL_UNCOVERED,
            reason_detail=(
                f"Combo opens {new_uncovered_calls} uncovered {req.ticker} short call(s); "
                "index options require long-call cover in the combo"
            ),
        )

    existing_short = _count_existing_short_calls(portfolio.positions, req.ticker)
    shares = _count_long_shares(portfolio.positions, req.ticker)
    threshold = _share_cover_threshold(req.multiplier, cover_ratio)
    share_cover_units = max(shares - reservations.stock_sell_qty, 0) // threshold
    total_short_after = existing_short + reservations.short_call_qty + new_uncovered_calls
    if total_short_after > share_cover_units:
        return Verdict(
            accept=False,
            reason_code=ReasonCode.ETF_CALL_UNCOVERED,
            reason_detail=(
                f"Combo opens {new_uncovered_calls} uncovered {req.ticker} short call(s); "
                f"existing short calls {existing_short}, reserved {reservations.short_call_qty}, "
                f"share-cover units {share_cover_units} "
                f"(threshold={threshold} shares/call, cover_ratio={cover_ratio})"
            ),
        )

    return Verdict(accept=True)


def _share_cover_threshold(multiplier: int, cover_ratio: float) -> int:
    """Shares required to cover one short call at the given ratio.

    Conservatively rounds up — if multiplier × ratio is non-integer, the
    desk needs more shares, not fewer. Example: multiplier=100,
    cover_ratio=1.25 → 125 shares. cover_ratio=1.0 (default) → 100.
    """
    import math as _math

    return _math.ceil(multiplier * cover_ratio)


def evaluate(
    req: PreflightRequest,
    portfolio: PortfolioView,
    reservations: WorkingReservations | None = None,
    cover_ratio: float = 1.0,
) -> Verdict:
    """Evaluate Gate 4 server-side. Pure function.

    F2: universe + Gate 4 using `portfolio` (live-like view) and empty-by-default
    `reservations` (F4 replaces the stub with duckdb reads).

    `cover_ratio` tightens the share-cover requirement per short call.
    Default 1.0. `RegimeGate` passes 1.25 on TIER_2 throttle.
    """
    reservations = reservations or WorkingReservations()

    # ① Universe — V1 universe is US-only. Foreign cash equities (non-USD STK)
    # are out of that universe by definition; gate them on currency instead, so
    # BUY-accept (②) and SELL-coverage (③) still apply. USD tickers unchanged.
    is_foreign_equity = req.security_type == "STK" and (req.currency or "USD").upper() != "USD"
    if not is_foreign_equity and not is_known(req.ticker):
        return Verdict(
            accept=False,
            reason_code=ReasonCode.UNIVERSE_UNKNOWN,
            reason_detail=f"{req.ticker} not in V1 universe",
        )

    # is_index() raises KeyError for non-universe tickers, so it must only run
    # for known USD tickers — a foreign equity is never an index.
    if req.security_type == "STK" and not is_foreign_equity and is_index(req.ticker):
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

        # ETF: fall back to stock cover. Long calls at this expiry were ALREADY
        # consumed when computing remaining_after_spread above — do not count
        # them a second time in total_cover (matches web/lib/nakedShortGuard.ts
        # lines 273-283). `remaining_after_spread` is the uncovered-tail count
        # that still needs share cover.
        existing_short = _count_existing_short_calls(portfolio.positions, req.ticker)
        shares = _count_long_shares(portfolio.positions, req.ticker)
        threshold = _share_cover_threshold(req.multiplier, cover_ratio)
        share_cover_units = max(shares - reservations.stock_sell_qty, 0) // threshold

        total_short_after = existing_short + reservations.short_call_qty + remaining_after_spread
        if share_cover_units < total_short_after:
            return Verdict(
                accept=False,
                reason_code=ReasonCode.ETF_CALL_UNCOVERED,
                reason_detail=(
                    f"SELL {req.quantity} {req.ticker} call(s): uncovered tail after "
                    f"spread accounting is {remaining_after_spread}, existing short "
                    f"calls {existing_short} — total {total_short_after} exceeds "
                    f"{share_cover_units} share-cover units"
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
