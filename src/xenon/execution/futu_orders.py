"""Futu order adapter (X3c): the only module that sends Futu SDK trade mutations.

Pure functions over an injected `OpenSecTradeContext` (or a fake with the same
methods); no threading, no Postgres, no IB. The Futu worker (X3c task 2) owns
the context and calls these functions; dispatch (task 3) owns the rows.

SDK facts (futu-api 10.03.6308, `.venv/lib/python3.13/site-packages/futu/`):
- `place_order(price, qty, code, trd_side, order_type, adjust_limit, trd_env,
  acc_id, acc_index, remark, time_in_force, ...)` (trade/open_trade_context.py:526).
  `remark` longer than 64 UTF-8 bytes is refused (:545-550). The reply is a
  one-row frame; its order fields come from a NON-refresh order query (:592-599).
- `modify_order(modify_order_op, order_id, qty, price, adjust_limit, trd_env,
  acc_id, ...)` (:617); the reply is only `trd_env, order_id` (:661-664).
- `order_list_query(order_id, status_filter_list, code, start, end, trd_env,
  acc_id, acc_index, refresh_cache, order_market)` (:445);
  `history_order_list_query(status_filter_list, code, start, end, trd_env,
  acc_id, acc_index, order_market)` (:739), window at most 90 days (:757).
- `unlock_trade(password=None, password_md5=None, is_unlock=True)` (:115).
- Return shape `(ret, data)`, `RET_OK = 0` / `RET_ERROR = -1`
  (common/constant.py:184-185). A RET_ERROR can be a broker reply, a send
  failure, a packet timeout or a disconnect (common/open_context_base.py:259-311);
  the caller cannot tell them apart, so after a possible send every non-OK
  result is uncertain.
- `OrderStatus` (common/constant.py:1251-1306), `ModifyOrderOp` NORMAL / CANCEL
  (:1339-1371), `TrdSide` BUY / SELL (:1374), `OrderType.NORMAL` = US limit
  order (:1167-1176), `TimeInForce` DAY / GTC (:2820), `TrdEnv` REAL /
  SIMULATE (:1064).
- The SDK has no price-precision rule: the price is sent as `float(price)` and
  `adjust_limit=0` sets `adjustPrice=False` (trade/trade_query.py:445-447), so
  the server never moves it. This adapter never rounds a price either; it
  refuses one outside the US minimum increment (`_price_increment`).

Fills: this module never writes `order_fills` (plan §10, Fable F10); only the
observation pass (`orders_store.mirror_futu_order`) records Futu fills.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from xenon.api.services.futu_observation import FUTU_STATUS_TO_STATE
from xenon.execution import orders_store
from xenon.utils.symbol_norm import futu_to_ib, ib_to_futu

log = logging.getLogger(__name__)

RET_OK = 0  # futu.common.constant.RET_OK (common/constant.py:184)
REMARK_MAX_BYTES = 64  # trade/open_trade_context.py:545-550

MODIFY_NORMAL = "NORMAL"  # ModifyOrderOp.NORMAL (common/constant.py:1366)
MODIFY_CANCEL = "CANCEL"  # ModifyOrderOp.CANCEL (common/constant.py:1367)
ORDER_TYPE_LIMIT = "NORMAL"  # OrderType.NORMAL = US limit order (common/constant.py:1174)
_TIF = {"DAY": "DAY", "GTC": "GTC"}  # TimeInForce (common/constant.py:2820-2822)
_TRD_ENV = {"paper": "SIMULATE", "live": "REAL"}  # TrdEnv (common/constant.py:1073-1074)

# Status table: the observation pass's (`futu_observation.FUTU_STATUS_TO_STATE`),
# one table for both paths. Statuses it does not map — N/A, UNSUBMITTED,
# WAITING_SUBMIT, SUBMITTING, TIMEOUT, FILL_CANCELLED (common/constant.py:
# 1288-1305) and any unknown value — are UNKNOWN (non-terminal, demand kept).
TERMINAL_STATES = frozenset({"FILLED", "CANCELLED", "REJECTED"})

# Reason codes (top-level `reason_code` in API responses).
UNSUPPORTED_ORDER_TYPE = "FUTU_ORDER_TYPE_UNSUPPORTED"
PRICE_PRECISION = "FUTU_PRICE_PRECISION"
INVALID_ORDER = "FUTU_INVALID_ORDER"
REMARK_TOO_LONG = "FUTU_REMARK_TOO_LONG"
UNLOCK_SECRET_MISSING = "FUTU_UNLOCK_SECRET_MISSING"
UNLOCK_SECRET_INVALID = "FUTU_UNLOCK_SECRET_INVALID"
UNLOCK_FAILED = "FUTU_UNLOCK_FAILED"
SEND_UNCERTAIN = "FUTU_SEND_UNCERTAIN"

PWD_MD5_ENV = "FUTU_TRADE_PWD_MD5"
_MD5_RE = re.compile(r"[0-9a-fA-F]{32}")
_OPT_UNDERLYING_RE = re.compile(r"[A-Z][A-Z0-9]*")


def state_for(futu_status: Any) -> str:
    """Futu `order_status` → order_submissions.state; unknown values → UNKNOWN."""
    return FUTU_STATUS_TO_STATE.get(str(futu_status) if futu_status is not None else "", "UNKNOWN")


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


class FutuOrderRefused(Exception):
    """Refused before any SDK mutation (nothing sent)."""

    def __init__(self, reason_code: str, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.http_status = http_status


@dataclass(frozen=True)
class FutuOrderSpec:
    """One single-leg US limit order, in Xenon (IB) conventions."""

    sec_type: str  # "STK" | "OPT"
    symbol: str  # IB symbol ("BRK B", "SPY")
    action: str  # "BUY" | "SELL"
    quantity: int
    limit_price: Decimal
    order_type: str = "LMT"
    tif: str = "DAY"
    expiry: str | None = None  # YYYYMMDD (OPT)
    right: str | None = None  # "C" | "P" (OPT)
    strike: Decimal | None = None  # OPT


def remark_for(submission_id: str) -> str:
    """`xo:<submission_id>` (the observation binds own rows by it), at most
    64 UTF-8 bytes (the SDK limit)."""
    remark = orders_store.order_ref_for(submission_id)
    if len(remark.encode("utf-8")) > REMARK_MAX_BYTES:
        raise FutuOrderRefused(REMARK_TOO_LONG, f"remark is longer than {REMARK_MAX_BYTES} bytes")
    return remark


def futu_code_for(spec: FutuOrderSpec) -> str:
    """Futu code of the instrument. Stock: `ib_to_futu` (`BRK B` → `US.BRK.B`).
    Option: `US.<UNDERLYING><YYMMDD><C|P><strike×1000>` without zero padding,
    the form OpenD reports (SDK example `US.AAPL210115C185000`,
    quote/open_quote_context.py:214). The code must parse back to the same
    symbol / expiry / right / strike through `futu_to_ib`."""
    if spec.sec_type == "STK":
        return ib_to_futu({"kind": "STK", "symbol": spec.symbol, "market": "US"})
    if spec.sec_type != "OPT":
        raise FutuOrderRefused(INVALID_ORDER, f"unsupported security type {spec.sec_type!r}")
    if not (spec.expiry and len(spec.expiry) == 8 and spec.expiry.isdigit()) or spec.right not in ("C", "P"):
        raise FutuOrderRefused(INVALID_ORDER, "option needs expiry YYYYMMDD and right C/P")
    if not _OPT_UNDERLYING_RE.fullmatch(spec.symbol or ""):
        # e.g. "BRK B": the Futu option code of a class share is not verified.
        raise FutuOrderRefused(INVALID_ORDER, f"option underlying {spec.symbol!r} has no verified Futu code")
    strike = _decimal(spec.strike)
    milli = strike * 1000 if strike is not None else None
    if milli is None or milli <= 0 or milli != milli.to_integral_value():
        raise FutuOrderRefused(INVALID_ORDER, "option strike must be positive with at most 3 decimals")
    code = f"US.{spec.symbol}{spec.expiry[2:]}{spec.right}{int(milli)}"
    back = futu_to_ib(code)
    if (
        back.get("kind") != "OPT"
        or back.get("symbol") != spec.symbol
        or back.get("expiry") != spec.expiry
        or back.get("right") != spec.right
        or Decimal(str(back.get("strike"))) != strike
    ):
        raise FutuOrderRefused(INVALID_ORDER, "option code does not round-trip")
    return code


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _price_increment(spec: FutuOrderSpec, price: Decimal) -> Decimal:
    """Minimum US price increment: $0.01, and $0.0001 for a stock below $1
    (Reg NMS Rule 612). Option tick tables (0.05 / 0.01) are checked by the
    IB quote gate in dispatch; this only refuses sub-penny option prices."""
    if spec.sec_type == "STK" and price < 1:
        return Decimal("0.0001")
    return Decimal("0.01")


def checked_price(spec: FutuOrderSpec, price: Any) -> Decimal:
    """The limit price, unchanged. Refused (never moved) when it is not a
    positive finite number on the minimum increment."""
    p = _decimal(price)
    if p is None or p <= 0:
        raise FutuOrderRefused(INVALID_ORDER, "limit price must be a positive number")
    if p % _price_increment(spec, p) != 0:
        raise FutuOrderRefused(PRICE_PRECISION, f"limit price {p} is not on the {_price_increment(spec, p)} increment")
    return p


def tick_price(spec: FutuOrderSpec, price: Any) -> Decimal | None:
    """`price` rounded to the spec's minimum increment (half-even), None when
    it is not a positive finite number. Turns a broker float echo into its
    grid value; two different valid ticks stay different (modify
    confirmation compares these, never an absolute tolerance)."""
    p = _decimal(price)
    if p is None or p <= 0:
        return None
    return p.quantize(_price_increment(spec, p))


def _checked_spec(spec: FutuOrderSpec) -> tuple[str, Decimal]:
    if spec.order_type != "LMT":
        raise FutuOrderRefused(UNSUPPORTED_ORDER_TYPE, f"Futu orders are limit only (got {spec.order_type!r})")
    if spec.action not in ("BUY", "SELL"):
        raise FutuOrderRefused(INVALID_ORDER, f"action must be BUY or SELL (got {spec.action!r})")
    if not isinstance(spec.quantity, int) or isinstance(spec.quantity, bool) or spec.quantity < 1:
        raise FutuOrderRefused(INVALID_ORDER, "quantity must be a whole number >= 1")
    if spec.tif not in _TIF:
        raise FutuOrderRefused(INVALID_ORDER, f"time in force must be DAY or GTC (got {spec.tif!r})")
    return futu_code_for(spec), checked_price(spec, spec.limit_price)


def _trd_env(account_env: str) -> str:
    try:
        return _TRD_ENV[account_env]
    except KeyError:
        raise FutuOrderRefused(INVALID_ORDER, f"not a Futu account env: {account_env!r}") from None


# ---------------------------------------------------------------------------
# Unlock (REAL only)
# ---------------------------------------------------------------------------


def trade_secret(environ: Mapping[str, str]) -> str:
    """The REAL unlock secret (`FUTU_TRADE_PWD_MD5`), format-checked only:
    missing or not 32 hex characters → `FutuOrderRefused` (503). Also the
    `/health` `futu.trading.live` check (which keeps only pass / fail)."""
    secret = environ.get(PWD_MD5_ENV, "")
    if not secret:
        raise FutuOrderRefused(UNLOCK_SECRET_MISSING, f"{PWD_MD5_ENV} is not set", 503)
    if not _MD5_RE.fullmatch(secret):
        raise FutuOrderRefused(UNLOCK_SECRET_INVALID, f"{PWD_MD5_ENV} is not 32 hex characters", 503)
    return secret


def _redact(text: Any, secret: str | None) -> str:
    """Broker text without the unlock secret (or its lower/upper-case forms)."""
    s = str(text) if text is not None else ""
    if secret:
        for form in {secret, secret.lower(), secret.upper()}:
            s = s.replace(form, "[redacted]")
    return s


def unlock(ctx: Any, account_env: str, *, environ: Mapping[str, str] | None = None) -> None:
    """Unlock trading for a REAL mutation; SIMULATE needs none (the SDK skips
    it, trade/open_trade_context.py:126-129). Refuses (403/503) on a missing
    or malformed secret or a non-OK reply; the secret never leaves this
    function (not logged, not in the exception)."""
    if _trd_env(account_env) != "REAL":
        return
    secret = trade_secret(os.environ if environ is None else environ)
    raised = None
    try:
        ret, data = ctx.unlock_trade(password_md5=secret)
    except Exception as exc:  # noqa: BLE001
        raised = type(exc).__name__  # keep only the type: the text may echo the secret
    if raised is not None:
        # Raised outside the except block so `__context__` does not keep the original exception.
        raise FutuOrderRefused(UNLOCK_FAILED, f"unlock_trade raised {raised}", 503)
    if ret != RET_OK:
        log.warning("futu orders: unlock_trade refused (%s)", _redact(data, secret))
        raise FutuOrderRefused(UNLOCK_FAILED, f"unlock_trade failed: {_redact(data, secret)}", 403)


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FutuSendResult:
    """Outcome of one mutation that reached the send step.

    `outcome`: "accepted" (RET_OK with an order id, place only), "sent"
    (modify / cancel RET_OK: NOT a confirmation) or "unknown" (exception or
    non-OK reply after the possible-send mark). Confirmation needs a fresh
    account-scoped query (`query_order`)."""

    outcome: str
    order_id: str | None = None
    futu_code: str | None = None
    futu_status: str | None = None
    state: str = "UNKNOWN"
    message: str | None = None
    reason_code: str | None = None


def _rows(data: Any) -> list[dict]:
    if data is None:
        return []
    if hasattr(data, "to_dict"):
        return data.to_dict("records")
    return list(data) if isinstance(data, list) else []


def _text(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and value != value):
        return None
    s = str(value).strip()
    return s if s and s != "N/A" else None


def _send(
    name: str, mark_sent: Callable[[], None] | None, call: Callable[[], tuple[Any, Any]]
) -> tuple[Any, Any] | FutuSendResult:
    """Run one SDK mutation after the possible-send mark. Any exception is
    UNKNOWN (the request may have left), never "not sent"."""
    if mark_sent is not None:
        mark_sent()
    try:
        return call()
    except Exception as exc:  # noqa: BLE001
        log.warning("futu orders: %s raised after send mark (%s)", name, type(exc).__name__)
        return FutuSendResult("unknown", message=f"{name} raised {type(exc).__name__}", reason_code=SEND_UNCERTAIN)


def place_single(
    ctx: Any,
    spec: FutuOrderSpec,
    *,
    submission_id: str,
    account_env: str,
    acc_id: str,
    mark_sent: Callable[[], None] | None = None,
    environ: Mapping[str, str] | None = None,
) -> FutuSendResult:
    """Place one limit order with remark `xo:<submission_id>`.

    Every check (order type, quantity, price increment, code, remark, unlock)
    runs before `mark_sent`; a refusal raises `FutuOrderRefused` with nothing
    sent. After the mark: RET_OK with an order id → "accepted" (state from the
    reply's non-refresh row, never terminal: a terminal status there is
    UNKNOWN until a fresh query); anything else → "unknown"."""
    code, price = _checked_spec(spec)
    remark = remark_for(submission_id)
    trd_env = _trd_env(account_env)
    unlock(ctx, account_env, environ=environ)
    got = _send(
        "place_order",
        mark_sent,
        lambda: ctx.place_order(
            price=float(price),
            qty=spec.quantity,
            code=code,
            trd_side=spec.action,
            order_type=ORDER_TYPE_LIMIT,
            adjust_limit=0,
            trd_env=trd_env,
            acc_id=int(acc_id),
            remark=remark,
            time_in_force=_TIF[spec.tif],
        ),
    )
    if isinstance(got, FutuSendResult):
        return got
    ret, data = got
    if ret != RET_OK:
        return FutuSendResult("unknown", futu_code=code, message=str(data), reason_code=SEND_UNCERTAIN)
    rows = _rows(data)
    row = rows[0] if rows else {}
    order_id = _text(row.get("order_id"))
    if order_id is None:
        return FutuSendResult(
            "unknown", futu_code=code, message="place reply without order id", reason_code=SEND_UNCERTAIN
        )
    status = _text(row.get("order_status"))
    state = state_for(status)
    return FutuSendResult(
        "accepted",
        order_id=order_id,
        # The code OpenD reports for the order is the stored identity.
        futu_code=_text(row.get("code")) or code,
        futu_status=status,
        state="UNKNOWN" if state in TERMINAL_STATES else state,
    )


def _modify(
    ctx: Any,
    op: str,
    *,
    order_id: str,
    qty: int,
    price: Decimal,
    account_env: str,
    acc_id: str,
    mark_sent: Callable[[], None] | None,
    environ: Mapping[str, str] | None,
) -> FutuSendResult:
    trd_env = _trd_env(account_env)
    if not order_id:
        raise FutuOrderRefused(INVALID_ORDER, "order id required")
    unlock(ctx, account_env, environ=environ)
    got = _send(
        f"modify_order {op}",
        mark_sent,
        lambda: ctx.modify_order(
            modify_order_op=op,
            order_id=str(order_id),
            qty=qty,
            price=float(price),
            adjust_limit=0,
            trd_env=trd_env,
            acc_id=int(acc_id),
        ),
    )
    if isinstance(got, FutuSendResult):
        return got
    ret, data = got
    if ret != RET_OK:
        return FutuSendResult("unknown", order_id=str(order_id), message=str(data), reason_code=SEND_UNCERTAIN)
    return FutuSendResult("sent", order_id=str(order_id))


def modify(
    ctx: Any,
    spec: FutuOrderSpec,
    *,
    order_id: str,
    account_env: str,
    acc_id: str,
    mark_sent: Callable[[], None] | None = None,
    environ: Mapping[str, str] | None = None,
) -> FutuSendResult:
    """`modify_order(NORMAL)` to the spec's quantity / limit price. A RET_OK
    is "sent", never a confirmation."""
    _, price = _checked_spec(spec)
    return _modify(
        ctx,
        MODIFY_NORMAL,
        order_id=order_id,
        qty=spec.quantity,
        price=price,
        account_env=account_env,
        acc_id=acc_id,
        mark_sent=mark_sent,
        environ=environ,
    )


def cancel(
    ctx: Any,
    *,
    order_id: str,
    account_env: str,
    acc_id: str,
    mark_sent: Callable[[], None] | None = None,
    environ: Mapping[str, str] | None = None,
) -> FutuSendResult:
    """`modify_order(CANCEL)`; qty / price are required by the signature and
    ignored for a cancel (sent as 0). A RET_OK is "sent", never a
    confirmation: the demand stays until a fresh query shows a terminal state."""
    return _modify(
        ctx,
        MODIFY_CANCEL,
        order_id=order_id,
        qty=0,
        price=Decimal(0),
        account_env=account_env,
        acc_id=acc_id,
        mark_sent=mark_sent,
        environ=environ,
    )


# ---------------------------------------------------------------------------
# Fresh evidence (reads)
# ---------------------------------------------------------------------------


class FutuReadFailed(Exception):
    """A Futu read failed: no evidence, the row keeps its state."""


@dataclass(frozen=True)
class FutuOrderEvidence:
    """One order row from a fresh, account-scoped query."""

    order_id: str
    futu_code: str | None
    futu_status: str | None
    state: str
    qty: Decimal | None
    price: Decimal | None
    dealt_qty: Decimal | None
    remark: str | None
    row: dict


def _evidence(row: dict) -> FutuOrderEvidence | None:
    oid = _text(row.get("order_id"))
    if oid is None:
        return None
    status = _text(row.get("order_status"))
    return FutuOrderEvidence(
        order_id=oid,
        futu_code=_text(row.get("code")),
        futu_status=status,
        state=state_for(status),
        qty=_decimal(row.get("qty")),
        price=_decimal(row.get("price")),
        dealt_qty=_decimal(row.get("dealt_qty")),
        remark=_text(row.get("remark")),
        row=dict(row),
    )


def query_order(ctx: Any, *, order_id: str, account_env: str, acc_id: str) -> FutuOrderEvidence | None:
    """Fresh evidence for one order: `order_list_query(order_id=…,
    refresh_cache=True)` with explicit acc_id / env. None when absent;
    raises `FutuReadFailed` when the read failed (no evidence either way)."""
    ret, data = _read(
        "order_list_query",
        lambda: ctx.order_list_query(
            order_id=str(order_id), trd_env=_trd_env(account_env), acc_id=int(acc_id), refresh_cache=True
        ),
    )
    found = [e for e in map(_evidence, _rows(data)) if e is not None and e.order_id == str(order_id)]
    return found[0] if found else None


def _read(name: str, call: Callable[[], tuple[Any, Any]]) -> tuple[Any, Any]:
    try:
        ret, data = call()
    except Exception as exc:  # noqa: BLE001
        raise FutuReadFailed(f"{name} raised {type(exc).__name__}") from None
    if ret != RET_OK:
        raise FutuReadFailed(f"{name} returned {ret}: {data}")
    return ret, data


@dataclass(frozen=True)
class RemarkSearch:
    """`find_by_remark` result: "found" (exactly one order), "absent" or
    "ambiguous" (several orders carry the remark: never pick one)."""

    status: str
    orders: tuple[FutuOrderEvidence, ...] = ()

    @property
    def order(self) -> FutuOrderEvidence | None:
        return self.orders[0] if self.status == "found" else None


def find_by_remark(
    ctx: Any,
    *,
    remark: str,
    futu_code: str,
    account_env: str,
    acc_id: str,
    history_start: str | None = None,
    history_end: str | None = None,
) -> RemarkSearch:
    """Recovery after an uncertain place: orders whose remark is exactly
    `remark`, searched by `code=` with explicit acc_id / env —
    `order_list_query(refresh_cache=True)`, plus `history_order_list_query`
    over [history_start, history_end] (ET "YYYY-MM-DD HH:MM:SS", ≤ 90 days)
    when given (an order that may be older than 24 h). Any failed read raises
    `FutuReadFailed`. No match → "absent": the caller keeps the row UNKNOWN
    and never re-sends."""
    trd_env, acc = _trd_env(account_env), int(acc_id)
    _, today = _read(
        "order_list_query",
        lambda: ctx.order_list_query(code=futu_code, trd_env=trd_env, acc_id=acc, refresh_cache=True),
    )
    rows = _rows(today)
    if history_start is not None:
        _, hist = _read(
            "history_order_list_query",
            lambda: ctx.history_order_list_query(
                code=futu_code, start=history_start, end=history_end or "", trd_env=trd_env, acc_id=acc
            ),
        )
        rows += _rows(hist)
    by_id: dict[str, FutuOrderEvidence] = {}
    for ev in map(_evidence, rows):
        if ev is not None and ev.remark == remark and ev.order_id not in by_id:
            by_id[ev.order_id] = ev
    if not by_id:
        return RemarkSearch("absent")
    return RemarkSearch("found" if len(by_id) == 1 else "ambiguous", tuple(by_id.values()))
