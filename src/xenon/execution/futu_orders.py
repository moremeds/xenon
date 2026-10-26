"""Futu order adapter (X3c): the only module that sends Futu SDK trade mutations.

Pure functions over an injected `OpenSecTradeContext` (or a fake with the same
methods); no threading, no Postgres, no IB. The Futu worker (X3c task 2) owns
the context and calls these functions; dispatch (task 3) owns the rows.

SDK facts (futu-api 10.03.6308, `.venv/lib/python3.13/site-packages/futu/`):
- `place_order(price, qty, code, trd_side, order_type, adjust_limit, trd_env,
  acc_id, acc_index, remark, time_in_force, fill_outside_rth, aux_price,
  trail_type, trail_value, trail_spread, session, ...)` (trade/open_trade_context.py:526-529).
  `remark` longer than 64 UTF-8 bytes is refused (:545-550). The reply is a
  one-row frame; its order fields come from a NON-refresh order query (:592-599).
  `price` is positional and always sent (`float(price)`, :568); `aux_price` /
  `trail_*` are sent only when not None (trade/trade_query.py:460-471).
- `modify_order(modify_order_op, order_id, qty, price, adjust_limit, trd_env,
  acc_id, acc_index, aux_price, trail_type, trail_value, trail_spread)`
  (:617-619): no `session`, no `order_type`; the reply is only
  `trd_env, order_id` (:661-664).
- `order_list_query(order_id, status_filter_list, code, start, end, trd_env,
  acc_id, acc_index, refresh_cache, order_market)` (:445); its rows carry
  `order_type`, `time_in_force`, `session`, `aux_price`, `trail_type`,
  `trail_value`, `trail_spread` (:466-467; 'N/A' when unset, trade/trade_query.py:392-398);
  `history_order_list_query(status_filter_list, code, start, end, trd_env,
  acc_id, acc_index, order_market)` (:739), window at most 90 days (:757).
- `unlock_trade(password=None, password_md5=None, is_unlock=True)` (:115).
- Return shape `(ret, data)`, `RET_OK = 0` / `RET_ERROR = -1`
  (common/constant.py:184-185). A RET_ERROR can be a broker reply, a send
  failure, a packet timeout or a disconnect (common/open_context_base.py:259-311);
  the caller cannot tell them apart, so after a possible send every non-OK
  result is uncertain.
- `OrderStatus` (common/constant.py:1251-1306), `ModifyOrderOp` NORMAL / CANCEL
  (:1339-1371), `TrdSide` BUY / SELL (:1374), `OrderType` (:1167-1199;
  NORMAL = US limit order, MARKET, STOP, STOP_LIMIT, MARKET_IF_TOUCHED,
  LIMIT_IF_TOUCHED, TRAILING_STOP, TRAILING_STOP_LIMIT at :1187-1199),
  `TrailType` RATIO / AMOUNT (:1240-1241), `TimeInForce` DAY / GTC
  (:2820-2822), `Session` RTH / ETH / ALL / OVERNIGHT (:2834-2837; default
  `Session.NONE`), `TrdEnv` REAL / SIMULATE (:1064).
- The SDK has no price-precision rule: the price is sent as `float(price)` and
  `adjust_limit=0` sets `adjustPrice=False` (trade/trade_query.py:445-447), so
  the server never moves it. This adapter never rounds a price either; it
  refuses one outside the US minimum increment (`_price_increment`).

Fills: this module never writes `order_fills` (plan §10, Fable F10); only the
observation pass (`orders_store.mirror_futu_order`) records Futu fills.
"""

from __future__ import annotations

import dataclasses
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

# Futu `OrderType` (common/constant.py:1187-1199) ↔ Xenon label (wire
# `orderType`); the table lives in orders_store (binder and read path use it).
FUTU_TO_ORDER_TYPE = dict(orders_store.FUTU_ORDER_LABELS)
ORDER_TYPE_TO_FUTU = {v: k for k, v in FUTU_TO_ORDER_TYPE.items()}
LIMIT_TYPES = orders_store.FUTU_LIMIT_LABELS  # a fixed user limit (`limitPrice`)
AUX_TYPES = orders_store.FUTU_AUX_LABELS  # a trigger (`auxPrice`)
TRAIL_TYPES = orders_store.FUTU_TRAIL_LABELS  # `trailType` + `trailValue`
SPREAD_TYPES = orders_store.FUTU_SPREAD_LABELS  # `trailSpread`
TRAIL_KINDS = ("RATIO", "AMOUNT")  # TrailType (common/constant.py:1240-1241)
SEC_TYPES = ("STK", "OPT")
TIFS = tuple(_TIF)
SESSIONS = ("RTH", "ETH", "ALL", "OVERNIGHT")  # Session (common/constant.py:2834-2837)
DEFAULT_SESSION = "RTH"
# `price` is a required SDK argument; types without a fixed limit send this
# placeholder (Futu ignores `price` for them). Never persisted or shown.
PLACEHOLDER_PRICE = 0.0
RATIO_MAX = Decimal(20)  # trail ratio, percent


def _product(secs, types, tifs, sessions) -> set[tuple[str, str, str, str]]:
    return {(s, t, f, n) for s in secs for t in types for f in tifs for n in sessions}


# Every (security, order type, TIF, session) Futu supports for the env (plan § 2);
# an operator allowlist can only narrow it. Paper: Futu SIMULATE takes limit and
# market orders, DAY, RTH. Live: stock LMT in any session; other stock types
# RTH only (other sessions unverified); option MKT / STP / MIT / TRAIL /
# TRAIL LIMIT never (unbounded fill on a wide spread).
STATIC_TUPLES: dict[str, frozenset[tuple[str, str, str, str]]] = {
    "paper": frozenset(_product(SEC_TYPES, ["LMT"], ["DAY"], ["RTH"]) | _product(["STK"], ["MKT"], ["DAY"], ["RTH"])),
    "live": frozenset(
        _product(["STK"], ["LMT"], TIFS, SESSIONS)
        | _product(["STK"], ["MKT"], ["DAY"], ["RTH"])
        | _product(["STK"], ["STP", "STP LMT", "MIT", "LIT", "TRAIL", "TRAIL LIMIT"], TIFS, ["RTH"])
        | _product(["OPT"], ["LMT", "STP LMT", "LIT"], TIFS, ["RTH"])
    ),
}
TUPLES_ENV = {"live": "XENON_FUTU_TUPLES_LIVE", "paper": "XENON_FUTU_TUPLES_PAPER"}
# Unset allowlists keep the v0.16 code surface: live DAY + GTC LMT; paper DAY
# LMT plus stock MKT (paper GTC withdrawn: SIMULATE is DAY only).
DEFAULT_TUPLES = {
    "live": "STK:LMT:DAY:RTH,STK:LMT:GTC:RTH,OPT:LMT:DAY:RTH,OPT:LMT:GTC:RTH",
    "paper": "STK:LMT:DAY:RTH,OPT:LMT:DAY:RTH,STK:MKT:DAY:RTH",
}
MODIFY_TYPES_ENV = "XENON_FUTU_MODIFY_TYPES"
DEFAULT_MODIFY_TYPES = "LMT"

# Status table: the observation pass's (`futu_observation.FUTU_STATUS_TO_STATE`),
# one table for both paths. Statuses it does not map — N/A, UNSUBMITTED,
# WAITING_SUBMIT, SUBMITTING, TIMEOUT, FILL_CANCELLED (common/constant.py:
# 1288-1305) and any unknown value — are UNKNOWN (non-terminal, demand kept).
TERMINAL_STATES = frozenset({"FILLED", "CANCELLED", "REJECTED"})

# Reason codes (top-level `reason_code` in API responses).
UNSUPPORTED_ORDER_TYPE = "FUTU_ORDER_TYPE_UNSUPPORTED"
TIF_UNSUPPORTED = "FUTU_TIF_UNSUPPORTED"
SESSION_UNSUPPORTED = "FUTU_SESSION_UNSUPPORTED"
NOT_MODIFIABLE = "FUTU_ORDER_NOT_MODIFIABLE"
PRICE_PRECISION = "FUTU_PRICE_PRECISION"
INVALID_ORDER = "FUTU_INVALID_ORDER"
REMARK_TOO_LONG = "FUTU_REMARK_TOO_LONG"
UNLOCK_SECRET_MISSING = "FUTU_UNLOCK_SECRET_MISSING"
UNLOCK_SECRET_INVALID = "FUTU_UNLOCK_SECRET_INVALID"
UNLOCK_FAILED = "FUTU_UNLOCK_FAILED"
SEND_UNCERTAIN = "FUTU_SEND_UNCERTAIN"
ORDER_UNVERIFIED = "FUTU_ORDER_UNVERIFIED"  # modify: the fresh read lacks the order or a term
INVALID_ORDER_BODY = "INVALID_ORDER_BODY"  # preflight.ReasonCode.INVALID_ORDER_BODY

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
    """One single-leg US order, in Xenon (IB) conventions. `order_type` is a
    Xenon label (`ORDER_TYPE_TO_FUTU`); `limit_price` is None for a type
    without a fixed limit; `aux_price` / `trail_*` only for the types that
    have them (`AUX_TYPES`, `TRAIL_TYPES`, `SPREAD_TYPES`)."""

    sec_type: str  # "STK" | "OPT"
    symbol: str  # IB symbol ("BRK B", "SPY")
    action: str  # "BUY" | "SELL"
    quantity: int
    limit_price: Decimal | None
    order_type: str = "LMT"
    tif: str = "DAY"
    expiry: str | None = None  # YYYYMMDD (OPT)
    right: str | None = None  # "C" | "P" (OPT)
    strike: Decimal | None = None  # OPT
    session: str = DEFAULT_SESSION
    aux_price: Decimal | None = None  # trigger
    trail_type: str | None = None  # "RATIO" (percent) | "AMOUNT" (price distance)
    trail_value: Decimal | None = None
    trail_spread: Decimal | None = None


# ---------------------------------------------------------------------------
# Allowed tuples (plan § 2)
# ---------------------------------------------------------------------------

_bad_config_logged: set[tuple[str, str]] = set()


def _log_bad_config(name: str, raw: str, why: str) -> None:
    if (name, raw) not in _bad_config_logged:
        _bad_config_logged.add((name, raw))
        log.error("%s=%r %s", name, raw, why)


def _label(text: str) -> str:
    return " ".join(text.split()).upper()


def _parse_tuple(token: str) -> tuple[str, str, str, str] | None:
    parts = [_label(p) for p in token.split(":")]
    if len(parts) != 4:
        return None
    sec, order_type, tif, session = parts
    if sec not in SEC_TYPES or order_type not in ORDER_TYPE_TO_FUTU or tif not in TIFS or session not in SESSIONS:
        return None
    return sec, order_type, tif, session


def allowed_tuples(account_env: str, environ: Mapping[str, str] | None = None) -> frozenset[tuple[str, str, str, str]]:
    """The env's tuples `(sec, order_type, tif, session)`: the operator
    allowlist (`XENON_FUTU_TUPLES_LIVE` / `_PAPER`, exact `SEC:TYPE:TIF:SESSION`
    entries; unset or blank → `DEFAULT_TUPLES`) intersected with
    `STATIC_TUPLES`. An entry that does not parse turns the whole list off
    (fail closed, one ERROR log); a valid entry outside the static set is
    dropped (one ERROR log)."""
    static = STATIC_TUPLES.get(account_env)
    if static is None:
        return frozenset()
    env = os.environ if environ is None else environ
    name = TUPLES_ENV[account_env]
    raw = env.get(name) or ""
    if not raw.strip():
        raw = DEFAULT_TUPLES[account_env]
    parsed = [_parse_tuple(t) for t in raw.split(",") if t.strip()]
    if any(p is None for p in parsed):
        _log_bad_config(name, raw, "has an entry that is not SEC:TYPE:TIF:SESSION: no Futu tuple is allowed")
        return frozenset()
    outside = [p for p in parsed if p not in static]
    if outside:
        _log_bad_config(name, raw, f"names tuples Futu does not support for {account_env}, ignored: {outside}")
    return frozenset(p for p in parsed if p in static)


def modify_types(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    """Order types whose modify is on (`XENON_FUTU_MODIFY_TYPES`, labels;
    unset or blank → `LMT`). An unknown label turns modify off for every type
    (fail closed, one ERROR log). Cancel never depends on it."""
    env = os.environ if environ is None else environ
    raw = env.get(MODIFY_TYPES_ENV) or ""
    if not raw.strip():
        raw = DEFAULT_MODIFY_TYPES
    labels = [_label(t) for t in raw.split(",") if t.strip()]
    if any(t not in ORDER_TYPE_TO_FUTU for t in labels):
        _log_bad_config(MODIFY_TYPES_ENV, raw, "has an unknown order type: Futu modify is off")
        return frozenset()
    return frozenset(labels)


def allowed(
    account_env: str,
    sec_type: str,
    order_type: str,
    tif: str,
    session: str,
    *,
    op: str = "place",
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str] | None:
    """None when the tuple may be placed (`op="place"`) or modified
    (`op="modify"`: the tuple is allowed AND its type is in `modify_types`)
    in `account_env`; else `(reason_code, detail)`, the detail naming the
    failing part. One function for the server gate and `/health`."""
    where = f"Futu {account_env}"
    if order_type not in ORDER_TYPE_TO_FUTU:
        return UNSUPPORTED_ORDER_TYPE, f"order type {order_type!r} is not a Futu order type"
    if tif not in TIFS:
        return TIF_UNSUPPORTED, f"time in force {tif!r} is not DAY or GTC"
    if session not in SESSIONS:
        return SESSION_UNSUPPORTED, f"session {session!r} is not one of {', '.join(SESSIONS)}"
    if sec_type not in SEC_TYPES:
        return INVALID_ORDER, f"security type {sec_type!r} is not STK or OPT"
    tuples = allowed_tuples(account_env, environ)
    if (sec_type, order_type, tif, session) not in tuples:
        if not any(t[:2] == (sec_type, order_type) for t in tuples):
            return UNSUPPORTED_ORDER_TYPE, f"{where} does not allow {order_type} {sec_type} orders"
        if not any(t[:3] == (sec_type, order_type, tif) for t in tuples):
            return TIF_UNSUPPORTED, f"{where} does not allow {tif} for {order_type} {sec_type} orders"
        return SESSION_UNSUPPORTED, f"{where} does not allow session {session} for {order_type} {sec_type} {tif} orders"
    if op == "modify" and order_type not in modify_types(environ):
        return NOT_MODIFIABLE, f"{where} modify is off for {order_type} orders; cancel instead"
    return None


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
    """Minimum US price increment (`orders_store.futu_price_increment`).
    Option tick tables (0.05 / 0.01) are checked by the IB quote gate in
    dispatch; this only refuses sub-penny option prices."""
    return orders_store.futu_price_increment(spec.sec_type, price)


def checked_price(spec: FutuOrderSpec, price: Any, name: str = "limit price") -> Decimal:
    """The limit (or trigger) price, unchanged. Refused (never moved) when it
    is not a positive finite number on the minimum increment."""
    p = _decimal(price)
    if p is None or p <= 0:
        raise FutuOrderRefused(INVALID_ORDER, f"{name} must be a positive number")
    if p % _price_increment(spec, p) != 0:
        raise FutuOrderRefused(PRICE_PRECISION, f"{name} {p} is not on the {_price_increment(spec, p)} increment")
    return p


def _checked_dp(value: Decimal, dp: int, name: str) -> Decimal:
    try:
        exact = value.quantize(Decimal(1).scaleb(-dp)) == value
    except InvalidOperation:
        exact = False
    if not exact:
        raise FutuOrderRefused(PRICE_PRECISION, f"{name} {value} has more than {dp} decimals (never rounded)")
    return value


def trail_value_dp(spec: FutuOrderSpec) -> int:
    """Decimals Futu keeps for `trail_value`: RATIO 2 (percent); AMOUNT 4 for a
    stock, 2 for an option (plan § 1)."""
    return orders_store.futu_trail_value_dp(spec.sec_type, spec.trail_type)


def checked_trail_value(spec: FutuOrderSpec, value: Any) -> Decimal:
    """`trail_value`, unchanged: finite, > 0, on its decimals (`trail_value_dp`),
    RATIO at most 20 (percent). The AMOUNT upper bound (20 % of the IB
    reference price) belongs to the quote gate."""
    if spec.trail_type not in TRAIL_KINDS:
        raise FutuOrderRefused(INVALID_ORDER, f"trail type must be RATIO or AMOUNT (got {spec.trail_type!r})")
    v = _decimal(value)
    if v is None or v <= 0:
        raise FutuOrderRefused(INVALID_ORDER, "trail value must be a positive number")
    _checked_dp(v, trail_value_dp(spec), "trail value")
    if spec.trail_type == "RATIO" and v > RATIO_MAX:
        raise FutuOrderRefused(INVALID_ORDER, f"trail ratio must be at most {RATIO_MAX} (percent)")
    return v


def checked_trail_spread(value: Any) -> Decimal:
    """`trail_spread`, unchanged: finite, >= 0 (0 = limit at the trigger), at
    most 3 decimals. The upper bound (5 % of the IB reference price) belongs
    to the quote gate."""
    v = _decimal(value)
    if v is None or v < 0:
        raise FutuOrderRefused(INVALID_ORDER, "trail spread must be a number >= 0")
    return _checked_dp(v, orders_store.FUTU_TRAIL_SPREAD_DP, "trail spread")


def tick_price(spec: FutuOrderSpec, price: Any) -> Decimal | None:
    """`price` rounded to the spec's minimum increment (half-even), None when
    it is not a positive finite number. Turns a broker float echo into its
    grid value; two different valid ticks stay different (modify
    confirmation compares these, never an absolute tolerance)."""
    p = _decimal(price)
    if p is None or p <= 0:
        return None
    return p.quantize(_price_increment(spec, p))


def field_error(spec: FutuOrderSpec) -> str | None:
    """The first field the spec's order type requires but lacks, or has but
    must not have (plan § 1; TRAIL LIMIT has no fixed limit), as a message;
    None when the fields fit the type. A type that is not a Futu label is
    `allowed()`'s refusal, not this one."""
    t = spec.order_type
    for name, value, required in (
        ("a limit price", spec.limit_price, t in LIMIT_TYPES),
        ("a trigger price", spec.aux_price, t in AUX_TYPES),
        ("a trail type", spec.trail_type, t in TRAIL_TYPES),
        ("a trail value", spec.trail_value, t in TRAIL_TYPES),
        ("a trail spread", spec.trail_spread, t in SPREAD_TYPES),
    ):
        if required and value is None:
            return f"{t} orders need {name}"
        if not required and value is not None:
            return f"{t} orders take no {name}"
    return None


def intent_for(spec: FutuOrderSpec) -> dict:
    """The frozen placement fingerprint `legs.futu_intent` of a checked spec
    (X5 § 3): order type, trigger, trail type / value / spread, session, as
    canonical strings (None when the type has no such term). Written with the
    reservation; the gate-4 request identity and the remark bind compare it."""
    dec = orders_store.canonical_decimal
    return {
        "order_type": spec.order_type,
        "aux_price": dec(spec.aux_price),
        "trail_type": spec.trail_type,
        "trail_value": dec(spec.trail_value),
        "trail_spread": dec(spec.trail_spread),
        "session": spec.session,
    }


def terms_for(intent: Mapping[str, Any]) -> dict:
    """`legs.futu_terms` at placement: the amendable terms of the intent."""
    return {k: intent.get(k) for k in orders_store.FUTU_TERM_KEYS}


@dataclass(frozen=True)
class CheckedOrder:
    """A spec that passed every pre-send check: the Futu code, the SDK `price`
    (the limit, or `PLACEHOLDER_PRICE` for a type without one) and the
    type-specific SDK keywords (`aux_price`, `trail_*`) shared by place and
    modify. Session is place-only (`place_single`)."""

    code: str
    price: float
    terms: dict


def check_spec(
    spec: FutuOrderSpec, account_env: str, *, op: str = "place", environ: Mapping[str, str] | None = None
) -> CheckedOrder:
    """Every pre-send check of a place (`op="place"`) or modify
    (`op="modify"`): action, quantity, the allowed tuple (`allowed`), the
    fields of the type (plan § 1: required or absent) and their precision
    (refused, never rounded), the Futu code. Raises `FutuOrderRefused`; sends
    nothing."""
    _trd_env(account_env)
    if spec.action not in ("BUY", "SELL"):
        raise FutuOrderRefused(INVALID_ORDER, f"action must be BUY or SELL (got {spec.action!r})")
    if not isinstance(spec.quantity, int) or isinstance(spec.quantity, bool) or spec.quantity < 1:
        raise FutuOrderRefused(INVALID_ORDER, "quantity must be a whole number >= 1")
    refusal = allowed(account_env, spec.sec_type, spec.order_type, spec.tif, spec.session, op=op, environ=environ)
    if refusal is not None:
        raise FutuOrderRefused(*refusal)
    t = spec.order_type
    missing_or_extra = field_error(spec)
    if missing_or_extra is not None:
        raise FutuOrderRefused(INVALID_ORDER, missing_or_extra)
    price = float(checked_price(spec, spec.limit_price)) if t in LIMIT_TYPES else PLACEHOLDER_PRICE
    terms: dict = {}
    if t in AUX_TYPES:
        terms["aux_price"] = float(checked_price(spec, spec.aux_price, "trigger price"))
    if t in TRAIL_TYPES:
        terms["trail_value"] = float(checked_trail_value(spec, spec.trail_value))
        terms["trail_type"] = spec.trail_type
    if t in SPREAD_TYPES:
        terms["trail_spread"] = float(checked_trail_spread(spec.trail_spread))
    return CheckedOrder(code=futu_code_for(spec), price=price, terms=terms)


def _on_grid(value: Decimal | None, dp: int) -> Decimal | None:
    return None if value is None else value.quantize(Decimal(1).scaleb(-dp))


def modify_spec(
    base: FutuOrderSpec,
    ev: FutuOrderEvidence,
    *,
    intent: Mapping[str, Any] | None,
    quantity: int | None,
    limit_price: Decimal | None = None,
    aux_price: Decimal | None = None,
    trail_value: Decimal | None = None,
    trail_spread: Decimal | None = None,
) -> FutuOrderSpec:
    """The full outgoing terms of a modify (X5 § 3, R2-2), from a fresh read.

    Type, trail type and session (immutable) come from the placement
    `intent` (a Xenon order, incl. the legacy LMT intent); an order made
    outside Xenon (`intent` None) takes them from the read, never a guess.
    Quantity, limit, trigger and trail terms come from the request
    (`checked_amend`), else the read (an omitted field keeps Futu's current
    value, also one changed in the Futu app), each on its own grid. Raises
    `FutuOrderRefused` (nothing sent):
    `NOT_MODIFIABLE` when the read contradicts the intent's type, or an
    outside order's type / session / a term its type needs cannot be read;
    `FUTU_ORDER_UNVERIFIED` when a Xenon order's read lacks a term its type
    needs; `INVALID_ORDER_BODY` for a new field the type does not have."""
    label = ev.order_type
    if intent is not None:
        order_type = str(intent.get("order_type") or "LMT")
        if ev.futu_order_type is not None and label != order_type:
            raise FutuOrderRefused(
                NOT_MODIFIABLE, f"Futu reports a {ev.futu_order_type} order; Xenon placed {order_type}; cancel instead"
            )
        session = str(intent.get("session") or DEFAULT_SESSION)
        trail_type = intent.get("trail_type")
        if order_type in TRAIL_TYPES and ev.trail_type != trail_type:
            # The omitted trail value is read in the broker's unit: a changed
            # or unreadable unit would resend it in the placed unit (PR #244 F1).
            raise FutuOrderRefused(
                NOT_MODIFIABLE,
                f"Futu reports trail type {ev.trail_type or 'unknown'}; Xenon placed {trail_type}; cancel instead",
            )
        missing = ORDER_UNVERIFIED, 503
    else:
        if label is None:
            raise FutuOrderRefused(
                NOT_MODIFIABLE, f"Futu order type {ev.futu_order_type!r} of this order cannot be read; cancel instead"
            )
        order_type = label
        session = ev.session
        if session is None:
            # N/A (SDK `Session.NONE`): RTH only when the legacy flag proves the
            # order cannot fill outside RTH; True or unreported is unknown
            # (PR #244 F2). Modify sends no session, so a guess would widen it.
            if ev.fill_outside_rth is not False:
                raise FutuOrderRefused(
                    NOT_MODIFIABLE, "the session of this order cannot be read from Futu; cancel instead"
                )
            session = DEFAULT_SESSION
        if session not in SESSIONS:
            raise FutuOrderRefused(NOT_MODIFIABLE, f"Futu session {session!r} of this order cannot be read")
        trail_type = ev.trail_type
        missing = NOT_MODIFIABLE, 400
    spec = dataclasses.replace(base, order_type=order_type, session=session)
    if order_type in TRAIL_TYPES:
        spec = dataclasses.replace(spec, trail_type=trail_type)
    new = {"limit_price": limit_price, "aux_price": aux_price, "trail_value": trail_value, "trail_spread": trail_spread}
    read: dict[str, Any] = {
        "limit_price": tick_price(spec, ev.price) if order_type in LIMIT_TYPES else None,
        "aux_price": tick_price(spec, ev.aux_price) if order_type in AUX_TYPES else None,
        "trail_value": _on_grid(ev.trail_value, trail_value_dp(spec)) if order_type in TRAIL_TYPES else None,
        "trail_spread": _on_grid(ev.trail_spread, orders_store.FUTU_TRAIL_SPREAD_DP)
        if order_type in SPREAD_TYPES
        else None,
    }
    lacking = (["trail_type"] if order_type in TRAIL_TYPES and trail_type not in TRAIL_KINDS else []) + [
        k for k in read if new[k] is None and read[k] is None and _needs(order_type, k)
    ]
    if lacking:
        code, status = missing
        raise FutuOrderRefused(
            code, f"Futu did not report the {', '.join(lacking)} of this {order_type} order; nothing was sent", status
        )
    checked = checked_amend(spec, **new)
    terms = {k: checked[k] if checked[k] is not None else read[k] for k in read}
    if ev.qty is None or ev.qty != ev.qty.to_integral_value():
        raise FutuOrderRefused(ORDER_UNVERIFIED, "Futu did not report this order's quantity", 503)
    return dataclasses.replace(spec, quantity=quantity if quantity is not None else int(ev.qty), **terms)


_TERM_NAMES = {
    "limit_price": "limit price",
    "aux_price": "trigger price",
    "trail_value": "trail value",
    "trail_spread": "trail spread",
}


def checked_amend(spec: FutuOrderSpec, **new: Any) -> dict[str, Decimal | None]:
    """The new terms of a modify body (`limit_price`, `aux_price`,
    `trail_value`, `trail_spread`; None = keep Futu's value), unchanged, each
    on its own grid. Raises `FutuOrderRefused`: `INVALID_ORDER_BODY` for a
    field the spec's type does not have; the place precision refusals
    otherwise (never rounded)."""
    checks = {
        "limit_price": lambda v: checked_price(spec, v),
        "aux_price": lambda v: checked_price(spec, v, "trigger price"),
        "trail_value": lambda v: checked_trail_value(spec, v),
        "trail_spread": checked_trail_spread,
    }
    out: dict[str, Decimal | None] = {}
    for key, value in new.items():
        if value is not None and not _needs(spec.order_type, key):
            raise FutuOrderRefused(INVALID_ORDER_BODY, f"{spec.order_type} orders have no {_TERM_NAMES[key]}")
        out[key] = None if value is None else checks[key](value)
    return out


def amend_confirmed(spec: FutuOrderSpec, ev: FutuOrderEvidence | None) -> bool:
    """A fresh read shows every sent field of `spec` (X5 § 5), each on its
    own grid: quantity; limit tick (types with a fixed limit); trigger tick;
    trail value at its decimals; spread at 3 decimals. A derived broker
    price (the moving stop of a trailing order) is never compared."""
    if ev is None or ev.qty is None or ev.qty != spec.quantity:
        return False
    t = spec.order_type
    if ev.order_type != t or (t in TRAIL_TYPES and ev.trail_type != spec.trail_type):
        return False  # the values of another type or unit are other terms (PR #244 F3)
    return all(
        (
            t not in LIMIT_TYPES or tick_price(spec, ev.price) == spec.limit_price,
            t not in AUX_TYPES or tick_price(spec, ev.aux_price) == spec.aux_price,
            t not in TRAIL_TYPES or _on_grid(ev.trail_value, trail_value_dp(spec)) == spec.trail_value,
            t not in SPREAD_TYPES or _on_grid(ev.trail_spread, orders_store.FUTU_TRAIL_SPREAD_DP) == spec.trail_spread,
        )
    )


def _needs(order_type: str, term: str) -> bool:
    return (
        order_type
        in {
            "limit_price": LIMIT_TYPES,
            "aux_price": AUX_TYPES,
            "trail_value": TRAIL_TYPES,
            "trail_spread": SPREAD_TYPES,
        }[term]
    )


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
    """Place one order with remark `xo:<submission_id>`.

    Every check (`check_spec`: allowed tuple, fields, precision, code; then
    remark, unlock) runs before `mark_sent`; a refusal raises
    `FutuOrderRefused` with nothing sent. The SDK gets `order_type` from the
    label map, the limit or `PLACEHOLDER_PRICE`, the type's `aux_price` /
    `trail_*`, and `session` only when it is not RTH (RTH keeps the SDK
    default `Session.NONE`, the v0.16 wire). After the mark: RET_OK with an
    order id → "accepted" (state from the reply's non-refresh row, never
    terminal: a terminal status there is UNKNOWN until a fresh query);
    anything else → "unknown"."""
    checked = check_spec(spec, account_env, environ=environ)
    code = checked.code
    remark = remark_for(submission_id)
    trd_env = _trd_env(account_env)
    extra = dict(checked.terms)
    if spec.session != DEFAULT_SESSION:
        extra["session"] = spec.session
    unlock(ctx, account_env, environ=environ)
    got = _send(
        "place_order",
        mark_sent,
        lambda: ctx.place_order(
            price=checked.price,
            qty=spec.quantity,
            code=code,
            trd_side=spec.action,
            order_type=ORDER_TYPE_TO_FUTU[spec.order_type],
            adjust_limit=0,
            trd_env=trd_env,
            acc_id=int(acc_id),
            remark=remark,
            time_in_force=_TIF[spec.tif],
            **extra,
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
    price: float,
    account_env: str,
    acc_id: str,
    mark_sent: Callable[[], None] | None,
    environ: Mapping[str, str] | None,
    terms: Mapping[str, Any] | None = None,
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
            **(terms or {}),
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
    """`modify_order(NORMAL)` to the spec's full terms: quantity, the limit
    (or `PLACEHOLDER_PRICE`), and the type's `aux_price` / `trail_*`. Type,
    trail type and session are the order's own (`check_spec(op="modify")`
    checks the tuple and the modify allowlist); `session` is never sent
    (`modify_order` has none). A RET_OK is "sent", never a confirmation."""
    checked = check_spec(spec, account_env, op="modify", environ=environ)
    return _modify(
        ctx,
        MODIFY_NORMAL,
        order_id=order_id,
        qty=spec.quantity,
        price=checked.price,
        account_env=account_env,
        acc_id=acc_id,
        mark_sent=mark_sent,
        environ=environ,
        terms=checked.terms,
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
        price=0.0,
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
    futu_order_type: str | None = None  # raw Futu `OrderType`
    order_type: str | None = None  # Xenon label; None when absent or not a mapped type
    aux_price: Decimal | None = None
    trail_type: str | None = None
    trail_value: Decimal | None = None
    trail_spread: Decimal | None = None
    tif: str | None = None
    session: str | None = None
    fill_outside_rth: bool | None = None  # legacy flag; None when not reported


def _evidence(row: dict) -> FutuOrderEvidence | None:
    oid = _text(row.get("order_id"))
    if oid is None:
        return None
    status = _text(row.get("order_status"))
    futu_type = _text(row.get("order_type"))
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
        futu_order_type=futu_type,
        order_type=FUTU_TO_ORDER_TYPE.get(futu_type or ""),
        aux_price=_decimal(row.get("aux_price")),
        trail_type=_text(row.get("trail_type")),
        trail_value=_decimal(row.get("trail_value")),
        trail_spread=_decimal(row.get("trail_spread")),
        tif=_text(row.get("time_in_force")),
        session=_text(row.get("session")),
        fill_outside_rth=_flag(row.get("fill_outside_rth")),
    )


def _flag(value: Any) -> bool | None:
    """A reported boolean (Python or numpy bool), else None ('N/A', absent).
    SDK: trade/trade_query.py:393 (`fill_outside_rth`, 'N/A' when unset)."""
    return bool(value) if type(value).__name__ in ("bool", "bool_") else None


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
