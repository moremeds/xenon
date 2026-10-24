"""Coherent Futu observation pass (X3b): read, persist, then publish.

Modelled on the IB pass (`ib_sync.py` `_persist_pass_activity` /
`_observation_stamp`). One pass for the configured Futu scope:

1. `pass_seq` / `pass_cluster` (`gate4.next_pass_seq`) and `as_of` (UTC) are
   taken before the first broker read.
2. The scope is verified against `get_acc_list()` (`futu_scope.verify_live`).
   Then, with `refresh_cache=True` and the explicit acc_id / trd_env:
   positions, open orders (`order_list_query`) and history orders over a
   window that covers `as_of - FILL_LAG_S` across the ET date boundary.
   Deals are never read (SIMULATE does not support them; fills are order
   grain everywhere).
3. Every observed order is mirrored into `order_submissions`, and cumulative
   `dealt_qty` advances become `order_fills` rows (replay-safe high-water;
   `orders_store.mirror_futu_order`).
4. Only then is `account_snapshots` (broker FUTU) published, with
   `observation{as_of, complete, account, pass_seq, pass_cluster}`, through
   the stale-publication fence.

Only US rows are used (the scope trades US); rows of other markets cannot
cover or consume US cover and are left out. `complete` is true only when
every read succeeded and every observed row was keyed, parsed and persisted
(a multi-leg order, a fractional order quantity or a fill delta without a
price is not persisted faithfully, so it makes the pass incomplete). An
unverified account, a failed position read or a missing pass_seq publishes
nothing and invalidates the Gate-4 view instead, so an older stamp is never
renewed. Fractional holdings and option legs without
IB-qualified metadata are not defaulted: Gate 4 fails their symbol closed.
This module never sends anything to Futu.

The lifespan runs one pass every 60 s (`start_observation_loop` /
`ObservationLoop`) on its own worker thread; a published `complete:true`
pass also runs the shadow naked-short audit (it never cancels).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import threading
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, insert

from xenon.db.engine import get_sync_engine
from xenon.db.schema import account_snapshots
from xenon.execution import coverage, futu_scope, gate4, naked_short_audit, orders_store
from xenon.execution.account_scope import AccountScope
from xenon.utils.symbol_norm import futu_to_ib

log = logging.getLogger(__name__)

# futu.common.constant.RET_OK (SDK 10.3).
RET_OK = 0
_ET = ZoneInfo("America/New_York")
_TRD_ENV = {"paper": "SIMULATE", "live": "REAL"}

# Futu OrderStatus (SDK 10.3) → order_submissions.state. Pending, timed-out
# and unknown statuses stay non-terminal (UNKNOWN): they keep their demand.
FUTU_STATUS_TO_STATE: dict[str, str] = {
    "SUBMITTED": "WORKING",
    "CANCELLING_ALL": "WORKING",
    "FILLED_PART": "PARTIALLY_FILLED",
    "CANCELLING_PART": "PARTIALLY_FILLED",
    "FILLED_ALL": "FILLED",
    "CANCELLED_ALL": "CANCELLED",
    "CANCELLED_PART": "CANCELLED",
    "DISABLED": "CANCELLED",
    "DELETED": "CANCELLED",
    "FAILED": "REJECTED",
    "SUBMIT_FAILED": "REJECTED",
}
_SIDE = {"BUY": "BUY", "BUY_BACK": "BUY", "SELL": "SELL", "SELL_SHORT": "SELL"}

MetaResolver = Callable[[list[dict]], list[dict | None]]


def futu_time_to_utc(value: Any) -> datetime | None:
    """Futu US trade time (America/New_York wall time) → aware UTC.

    DST-aware. An ambiguous fall-back wall time maps to the later instant
    (EST), so a fill near the observation boundary counts as after it
    (over-reserves). Unparseable or missing → None.
    """
    if not isinstance(value, str) or not value.strip() or value.strip() == "N/A":
        return None
    try:
        naive = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if naive.tzinfo is not None:
        return naive.astimezone(timezone.utc)
    early, late = naive.replace(tzinfo=_ET, fold=0), naive.replace(tzinfo=_ET, fold=1)
    chosen = late if early.utcoffset() != late.utcoffset() else early
    return chosen.astimezone(timezone.utc)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _text(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and value != value):
        return None
    s = str(value).strip()
    return s if s and s != "N/A" else None


def _whole(value: Any) -> int | None:
    f = _num(value)
    return int(f) if f is not None and f == int(f) else None


def _decimal(value: Any) -> Decimal | None:
    f = _num(value)
    if f is None or f <= 0:
        return None
    try:
        return Decimal(str(f))
    except InvalidOperation:
        return None


def _rows(data: Any) -> list[dict]:
    if data is None:
        return []
    if hasattr(data, "to_dict"):
        return data.to_dict("records")
    return list(data)


def _read(name: str, fn: Callable[[], Any]) -> list[dict] | None:
    """One broker read; None when it failed (exception or ret != RET_OK)."""
    try:
        ret, data = fn()
    except Exception as exc:  # noqa: BLE001 — any failure makes the pass incomplete
        log.warning("futu observation: %s read failed (%s)", name, exc)
        return None
    if ret != RET_OK:
        log.warning("futu observation: %s read returned %s (%s)", name, ret, data)
        return None
    return _rows(data)


def _history_window(as_of: datetime) -> tuple[str, str]:
    """ET wall-clock window covering `as_of - FILL_LAG_S` across the date boundary."""
    start = (as_of - timedelta(seconds=gate4.FILL_LAG_S)).astimezone(_ET).date() - timedelta(days=1)
    end = as_of.astimezone(_ET).date() + timedelta(days=1)
    return f"{start:%Y-%m-%d} 00:00:00", f"{end:%Y-%m-%d} 23:59:59"


class _MetaCache:
    """IB-qualified option metadata per (sym, right, exp, strike) for one pass.

    A resolver result is used only when it is an OPT contract that matches
    the requested symbol / right / expiry / strike; anything else is None
    (unresolved, never defaulted).
    """

    def __init__(self, resolver: MetaResolver | None) -> None:
        self._resolver = resolver
        self._cache: dict[tuple, dict | None] = {}

    def prefetch(self, specs: Iterable[dict]) -> None:
        todo = []
        for spec in specs:
            key = (spec["sym"], spec["right"], spec["exp"], spec["strike"])
            if key not in self._cache and key not in {(s["sym"], s["right"], s["exp"], s["strike"]) for s in todo}:
                todo.append(spec)
        if not todo:
            return
        results: list[dict | None] = [None] * len(todo)
        if self._resolver is not None:
            try:
                got = list(self._resolver([dict(s, con_id=None) for s in todo]))
                results = got if len(got) == len(todo) else results
            except Exception as exc:  # noqa: BLE001 — unresolved, fail closed
                log.warning("futu observation: contract metadata lookup failed (%s)", exc)
        for spec, meta in zip(todo, results):
            ok = (
                isinstance(meta, dict)
                and meta.get("sec") == "OPT"
                and meta.get("sym") == spec["sym"]
                and meta.get("right") == spec["right"]
                and meta.get("exp") == spec["exp"]
                and meta.get("strike") is not None
                and abs(float(meta["strike"]) - spec["strike"]) <= 1e-6
            )
            self._cache[(spec["sym"], spec["right"], spec["exp"], spec["strike"])] = meta if ok else None

    def get(self, spec: dict) -> dict | None:
        return self._cache.get((spec["sym"], spec["right"], spec["exp"], spec["strike"]))


def _instrument(code: Any) -> dict | None:
    """Futu code → {'sec', 'sym', and for options 'right','exp','strike'};
    None when it is not a parseable US stock/option code."""
    norm = futu_to_ib(code if isinstance(code, str) else "")
    if norm["kind"] == "STK" and norm.get("market") == "US":
        return {"sec": "STK", "sym": norm["symbol"]}
    if norm["kind"] == "OPT":
        return {
            "sec": "OPT",
            "sym": norm["symbol"],
            "right": norm["right"],
            "exp": norm["expiry"],
            "strike": norm["strike"],
        }
    return None


# Futu code prefixes outside the US market (SDK 10.3 `Market`), and the
# market labels Futu reports in `position_market` / `order_market`.
_FOREIGN_CODE_PREFIXES = frozenset({"HK", "SH", "SZ", "SG", "JP", "AU", "MY", "CA", "FX"})


def _market_class(row: dict, market_field: str) -> str | None:
    """'US', 'FOREIGN', or None when the row cannot be classified.

    Only a row positively outside the US is FOREIGN: a known non-US code
    prefix whose market field (when present) does not say US. A missing or
    unknown code, or a code that contradicts the market field, is None."""
    code = _text(row.get("code"))
    prefix = code.split(".", 1)[0].upper() if code and "." in code else None
    market = _text(row.get(market_field))
    market = market.upper() if market else None
    if prefix == "US":
        return "US" if market in (None, "US") else None
    if prefix in _FOREIGN_CODE_PREFIXES:
        return "FOREIGN" if market != "US" else None
    return None


def _split_markets(rows: list[dict], market_field: str, *, keep_unkeyed: bool) -> tuple[list[dict], bool]:
    """(US rows, every row classified). FOREIGN rows are left out. With
    `keep_unkeyed`, a row without order_id is kept so keying fails closed."""
    kept, ok = [], True
    for row in rows:
        cls = _market_class(row, market_field)
        if cls == "US" or (keep_unkeyed and _text(row.get("order_id")) is None):
            kept.append(row)
        elif cls is None:
            ok = False
    return kept, ok


def _merge_orders(open_rows: list[dict], history_rows: list[dict]) -> tuple[dict[str, dict], bool]:
    """Rows by order_id (the most advanced observation wins: larger dealt_qty,
    then later updated_time). Returns (by_id, ok); ok is False when a row has
    no order_id or one order_id spans several codes (a multi-leg order, which
    this pass cannot model)."""
    by_id: dict[str, dict] = {}
    codes: dict[str, set] = {}
    ok = True
    for row in [*open_rows, *history_rows]:
        oid = _text(row.get("order_id"))
        if oid is None:
            ok = False
            continue
        codes.setdefault(oid, set()).add(_text(row.get("code")))
        prev = by_id.get(oid)
        if prev is None or _order_rank(row) > _order_rank(prev):
            by_id[oid] = row
    if any(len(c) > 1 for c in codes.values()):
        ok = False
    return by_id, ok


def _order_rank(row: dict) -> tuple:
    return (
        _num(row.get("dealt_qty")) or 0.0,
        futu_time_to_utc(row.get("updated_time")) or datetime.min.replace(tzinfo=timezone.utc),
    )


def _order_observation(
    row: dict, scope: AccountScope, metas: _MetaCache, orders_read_at: datetime
) -> orders_store.FutuOrderObservation | None:
    """Parse one US order row; None when it cannot be persisted faithfully."""
    inst = _instrument(row.get("code"))
    action = _SIDE.get(_text(row.get("trd_side")) or "")
    qty = _whole(row.get("qty"))
    dealt = _whole(row.get("dealt_qty") or 0)
    if inst is None or action is None or qty is None or qty < 1 or dealt is None or dealt < 0:
        return None
    status = _text(row.get("order_status")) or ""
    if inst["sec"] == "OPT":
        meta = metas.get(inst)
        leg = coverage.Leg(
            action="BUY",
            sym=inst["sym"],
            sec="OPT",
            ratio=1,
            right=inst["right"],
            exp=inst["exp"],
            mult=meta["mult"] if meta else None,
            tc=meta["tc"] if meta else None,
            strike=inst["strike"],
            con_id=meta["con_id"] if meta else None,
            und_sec=meta["und_sec"] if meta else None,
        )
        legs = gate4.legs_payload_for(legs=[leg] if meta else None, guaranteed=None, account=scope.broker_account)
    else:
        leg = coverage.Leg(action="BUY", sym=inst["sym"], sec="STK", ratio=1)
        legs = gate4.legs_payload_for(legs=[leg], guaranteed=None, account=scope.broker_account)
        meta = None
    return orders_store.FutuOrderObservation(
        order_id=_text(row.get("order_id")),
        remark=_text(row.get("remark")),
        ticker=inst["sym"],
        security_type=inst["sec"],
        action=action,
        quantity=qty,
        limit_price=_decimal(row.get("price")),
        state=FUTU_STATUS_TO_STATE.get(status, "UNKNOWN"),
        legs=legs,
        dealt_qty=dealt,
        dealt_avg_price=_decimal(row.get("dealt_avg_price")),
        # Order-observation timing, not an execution timestamp. A missing
        # time falls back to the order read instant (after as_of: counts as
        # post-snapshot demand).
        updated_at=futu_time_to_utc(row.get("updated_time")) or orders_read_at,
        strike=inst.get("strike"),
        right=inst.get("right"),
        expiry=inst.get("exp"),
        con_id=meta["con_id"] if meta else None,
        order_type=_text(row.get("order_type")),
        tif=_text(row.get("time_in_force")),
        aux_price=_decimal(row.get("aux_price")),
        fill_metadata={
            "futu_code": _text(row.get("code")),
            "futu_order_id": _text(row.get("order_id")),
            "futu_status": status,
            "right": inst.get("right"),
            "strike": inst.get("strike"),
            "expiry": inst.get("exp"),
        },
    )


def _position_entry(row: dict, scope: AccountScope, metas: _MetaCache) -> dict | None:
    """One US position row → payload position (one leg); None when the row
    cannot be classified (unparseable code, missing qty, side contradicts sign)."""
    inst = _instrument(row.get("code"))
    qty = _num(row.get("qty"))
    if inst is None or qty is None:
        return None
    side = _text(row.get("position_side"))
    if (side == "LONG" and qty < 0) or (side == "SHORT" and qty > 0):
        return None
    magnitude = abs(qty)
    # Fractional quantities stay floats: Gate 4 rejects a non-int holding and
    # fails only that symbol closed (existing unresolved-holding mechanism).
    contracts: int | float = int(magnitude) if magnitude == int(magnitude) else magnitude
    direction = "LONG" if qty > 0 else "SHORT"
    leg = {
        "type": "Stock",
        "direction": direction,
        "contracts": contracts,
        "account": scope.broker_account,
        "futu_code": _text(row.get("code")),
        "avg_cost": _num(row.get("cost_price")),
        "market_price": _num(row.get("nominal_price")),
        "market_value": _num(row.get("market_val")),
        "currency": (_text(row.get("currency")) or "USD").upper(),
    }
    expiry = None
    if inst["sec"] == "OPT":
        meta = metas.get(inst)
        exp = inst["exp"]
        expiry = f"{exp[:4]}-{exp[4:6]}-{exp[6:]}"
        leg.update(
            type="Call" if inst["right"] == "C" else "Put",
            strike=inst["strike"],
            conId=meta["con_id"] if meta else None,
            mult=meta["mult"] if meta else None,
            tc=meta["tc"] if meta else None,
            und_sec=meta["und_sec"] if meta else None,
        )
    return {
        "ticker": inst["sym"],
        "structure": leg["type"] if inst["sec"] == "STK" else f"{direction.title()} {leg['type']}",
        "expiry": expiry or "N/A",
        "contracts": contracts,
        "direction": direction,
        "legs": [leg],
    }


def _option_specs(rows: Iterable[dict]) -> list[dict]:
    out = []
    for row in rows:
        inst = _instrument(row.get("code"))
        if inst and inst["sec"] == "OPT":
            out.append({"sym": inst["sym"], "right": inst["right"], "exp": inst["exp"], "strike": inst["strike"]})
    return out


def _publish(scope: AccountScope, payload: dict, may_publish: Callable[[], bool] | None = None) -> bool:
    """Insert the FUTU snapshot unless a newer pass already published or the
    pass was retired (`may_publish()` false, checked under the scope lock)."""
    orders_store.ensure_writable()
    scope_d = scope.as_dict()
    with get_sync_engine().begin() as conn:
        if gate4.is_stale_publication(conn, scope_d, payload["observation"]):
            log.info("futu observation: stale pass %s not published", payload["observation"].get("pass_seq"))
            return False
        if may_publish is not None and not may_publish():
            log.warning("futu observation: retired pass %s not published", payload["observation"].get("pass_seq"))
            return False
        conn.execute(
            insert(account_snapshots).values(
                account=scope.broker_account,
                # NOT NULL column; this pass reads no account balances (0 = not observed).
                bankroll=Decimal(0),
                net_liquidation=None,
                payload=payload,
                broker=scope.broker,
                account_env=scope.account_env,
                broker_account=scope.broker_account,
                snapshot_at=func.clock_timestamp(),
            )
        )
    return True


def _invalidate(scope: AccountScope, reason: str) -> None:
    try:
        gate4.invalidate_view(scope.as_dict(), reason)
    except Exception as exc:  # noqa: BLE001 — nothing else can be done here
        log.error("futu observation: view invalidation failed (%s): %s", reason, exc)


_TERMINAL_STATES = frozenset({"FILLED", "CANCELLED", "REJECTED"})


def _audit_open_orders(parsed: dict, scope: AccountScope) -> list[dict]:
    """Live observed orders in the IB open-order shape the shadow audit reads
    (`gate4.legs_payload_from_snapshot_order`). Option metadata is the
    IB-qualified leg metadata; an unresolved leg carries none, so the audit
    skips that symbol unless the order is a pure BUY."""
    out = []
    for obs in parsed.values():
        if obs is None or obs.state in _TERMINAL_STATES:
            continue
        leg = ((obs.legs or {}).get("legs") or [{}])[0]
        out.append(
            {
                "clientId": "futu",
                "orderId": obs.order_id,
                "permId": None,
                "account": scope.broker_account,
                "action": obs.action,
                "totalQuantity": obs.quantity,
                "filled": obs.dealt_qty,
                "status": obs.state,
                "contract": {
                    "secType": obs.security_type,
                    "symbol": obs.ticker,
                    "right": obs.right,
                    "expiry": obs.expiry,
                    "strike": obs.strike,
                    "conId": obs.con_id,
                    "tradingClass": leg.get("tc"),
                    "multiplier": leg.get("mult"),
                    "underSecType": leg.get("und_sec"),
                },
            }
        )
    return out


def run_pass(
    scope: AccountScope,
    ctx: Any,
    *,
    resolve_meta: MetaResolver | None = None,
    audit: bool = True,
    may_publish: Callable[[], bool] | None = None,
) -> dict:
    """One observation pass for `scope` on the Futu trade context `ctx`.

    `resolve_meta(specs)` returns IB-qualified option metadata per spec
    (`contract_meta` shape); without it every option leg stays unresolved.
    With `audit`, a published `complete:true` pass also runs the shadow
    naked-short audit (log + service_health only; it never cancels).
    `may_publish()` false (a pass the loop retired after a timeout or stop)
    means nothing is published.
    Returns {"published", "complete", "reads", "observation", "orders", "fills", "audit"}.
    """
    result: dict = {
        "published": False,
        "complete": False,
        "reads": {},
        "observation": None,
        "orders": 0,
        "fills": 0,
        "audit": None,
    }
    if os.environ.get("XENON_READ_ONLY") == "1":
        log.info("futu observation: XENON_READ_ONLY=1 — pass skipped")
        return result
    trd_env = _TRD_ENV.get(scope.account_env)
    if scope.broker != "FUTU" or trd_env is None:
        raise ValueError(f"not a Futu trade scope: {scope!r}")
    acc_id = int(scope.broker_account)

    as_of = datetime.now(timezone.utc)
    try:
        pass_seq, pass_cluster = gate4.next_pass_seq()
    except Exception as exc:  # noqa: BLE001 — no seq: the pass cannot be published
        log.warning("futu observation: pass_seq unavailable (%s)", exc)
        pass_seq = pass_cluster = None
    reads = result["reads"]

    acc_rows = _read("get_acc_list", ctx.get_acc_list)
    verified, reason = futu_scope.verify_live(scope, acc_rows) if acc_rows is not None else (False, "read_failed")
    reads["account"] = verified
    if not verified:
        _invalidate(scope, f"futu_account_unverified:{reason}")
        return result

    positions = _read("positions", lambda: ctx.position_list_query(trd_env=trd_env, acc_id=acc_id, refresh_cache=True))
    reads["positions"] = positions is not None
    orders_read_at = datetime.now(timezone.utc)
    open_rows = _read("open_orders", lambda: ctx.order_list_query(trd_env=trd_env, acc_id=acc_id, refresh_cache=True))
    reads["open_orders"] = open_rows is not None
    start, end = _history_window(as_of)
    history_rows = _read(
        "history_orders",
        lambda: ctx.history_order_list_query(trd_env=trd_env, acc_id=acc_id, start=start, end=end),
    )
    reads["history_orders"] = history_rows is not None

    us_positions, pos_ok = _split_markets(positions or [], "position_market", keep_unkeyed=False)
    us_open, open_ok = _split_markets(open_rows or [], "order_market", keep_unkeyed=True)
    us_history, hist_ok = _split_markets(history_rows or [], "order_market", keep_unkeyed=True)
    reads["rows_classified"] = pos_ok and open_ok and hist_ok
    metas = _MetaCache(resolve_meta)
    metas.prefetch(_option_specs([*us_positions, *us_open, *us_history]))

    by_id, reads["orders_keyed"] = _merge_orders(us_open, us_history)
    parsed = {oid: _order_observation(row, scope, metas, orders_read_at) for oid, row in by_id.items()}
    reads["orders_parsed"] = all(obs is not None for obs in parsed.values())
    persisted = True
    for obs in parsed.values():
        if obs is None:
            continue
        try:
            out = orders_store.mirror_futu_order(
                obs, broker_account=scope.broker_account, account_env=scope.account_env, observed_at=orders_read_at
            )
        except Exception as exc:  # noqa: BLE001 — any failure degrades the stamp
            log.warning("futu observation: persist of order %s failed (%s)", obs.order_id, exc, exc_info=True)
            persisted = False
            continue
        result["orders"] += 1
        result["fills"] += 1 if out.get("fill") else 0
        if out.get("fill_skipped"):
            log.warning("futu observation: order %s fill not recorded (%s)", obs.order_id, out["fill_skipped"])
            persisted = False
        if out.get("identity_mismatch"):
            log.warning(
                "futu observation: order %s not bound to %s (%s)",
                obs.order_id,
                out.get("submission_id"),
                out["identity_mismatch"],
            )
            persisted = False
    reads["persisted"] = persisted

    if positions is None or pass_seq is None:
        _invalidate(scope, "futu_positions_read_failed" if positions is None else "futu_pass_seq_unavailable")
        return result
    entries = [_position_entry(r, scope, metas) for r in us_positions if _num(r.get("qty")) != 0]
    reads["positions_parsed"] = all(e is not None for e in entries)
    complete = all(reads.values())
    observation = {
        "as_of": as_of.isoformat(),
        "complete": complete,
        "account": scope.broker_account,
        "reads": dict(reads),
        "pass_seq": str(pass_seq) if pass_seq is not None else None,
        "pass_cluster": pass_cluster,
        "source": "futu_observation",
    }
    payload = {
        "source": "futu",
        "broker": "FUTU",
        "account": scope.broker_account,
        "positions": [e for e in entries if e is not None],
        "account_summary": None,
        "observation": observation,
    }
    result["observation"] = observation
    try:
        result["published"] = _publish(scope, payload, may_publish)
    except Exception as exc:  # noqa: BLE001 — never leave an older stamp valid
        log.warning("futu observation: publish failed (%s)", exc, exc_info=True)
        _invalidate(scope, "futu_publish_failed")
    result["complete"] = complete and result["published"]
    if result["complete"] and audit:
        audited = naked_short_audit.run_shadow_audit(
            payload,
            _audit_open_orders(parsed, scope),
            scope.broker_account,
            observation=observation,
            scope=scope.as_dict(),
        )
        result["audit"] = audited.summary()
    return result


# ---------------------------------------------------------------------------
# Lifespan loop: one pass every OBSERVATION_INTERVAL_S on its own thread
# ---------------------------------------------------------------------------

OBSERVATION_INTERVAL_S = 60.0
# A pass that runs longer is abandoned by the loop (its thread finishes on its
# own); no new pass starts while it is still running.
PASS_TIMEOUT_S = 50.0
META_TIMEOUT_S = 20.0

Connector = Callable[[], tuple[Any, Callable[[], None]]]


def make_ib_meta_resolver(loop: asyncio.AbstractEventLoop, get_pool: Callable[[], Any]) -> MetaResolver:
    """`run_pass` resolver, called on the observation thread: qualifies option
    specs through the IB pool's `data` role on the API event loop, with the
    place gate's `_resolve_leg_meta_with_client`. No pool, a timeout or any
    error → every spec None (unresolved, fail closed)."""

    def resolve(specs: list[dict]) -> list[dict | None]:
        pool = get_pool()
        if pool is None or not specs:
            return [None] * len(specs)
        from xenon.api.services.order_entry import _resolve_leg_meta_with_client

        async def _qualify() -> list[dict | None]:
            async with pool.acquire("data") as client:
                return await pool.run_sync("data", _resolve_leg_meta_with_client, client, specs)

        fut = asyncio.run_coroutine_threadsafe(_qualify(), loop)
        try:
            return list(fut.result(timeout=META_TIMEOUT_S))
        except Exception as exc:  # noqa: BLE001 — fail closed
            fut.cancel()
            log.warning("futu observation: IB contract qualification failed (%s)", exc)
            return [None] * len(specs)

    return resolve


def futu_client_connector(scope: AccountScope) -> Connector:
    """Opens a dedicated read-only trade context for the loop (not the
    /futu/sync singleton, so neither blocks the other). `FutuClient.connect`
    probes the OpenD port first, so a down OpenD fails fast instead of the
    SDK retrying forever. The account is verified per pass by `run_pass`."""

    def connect() -> tuple[Any, Callable[[], None]]:
        from xenon.clients.futu_client import FutuClient

        client = FutuClient(
            host=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"),
            port=int(os.environ.get("FUTU_OPEND_PORT", "11111")),
            security_firm=os.environ.get("FUTU_SECURITY_FIRM", "FUTUSECURITIES"),
            trd_env=_TRD_ENV[scope.account_env],
            filter_trading_market="US",
        )
        client.connect()
        return client._trd_ctx, client.disconnect

    return connect


class ObservationLoop:
    """Runs `run_pass` for one scope on a single-worker executor.

    Reads only. The worker thread owns the trade context: it opens it
    lazily, reopens it after a failed pass, and closes it when its pass was
    retired or the loop stopped. A pass is retired when its wait times out
    or the loop stops: the scope's Gate-4 view is invalidated at once, and
    the retired pass can no longer publish (`may_publish` is checked under
    the scope lock) nor keep a late connection. An OpenD that cannot be
    reached also invalidates the view; the read routes keep serving the
    persisted rows.
    """

    def __init__(
        self,
        scope: AccountScope,
        *,
        connect: Connector,
        resolve_meta: MetaResolver | None = None,
        run: Callable[..., dict] = run_pass,
        interval_s: float = OBSERVATION_INTERVAL_S,
        pass_timeout_s: float = PASS_TIMEOUT_S,
    ) -> None:
        self.scope = scope
        self._connect = connect
        self._resolve_meta = resolve_meta
        self._run = run
        self._interval_s = interval_s
        self._pass_timeout_s = pass_timeout_s
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="xenon-futu-observe")
        # Worker-thread state: only `_pass_blocking` / `_retire_ctx` touch it.
        self._ctx: Any = None
        self._close: Callable[[], None] | None = None
        # Pass generation: a pass may publish only while its generation is
        # current and the loop is not stopping.
        self._gen = 0
        self._gen_lock = threading.Lock()
        self._stopping = threading.Event()
        self._inflight: concurrent.futures.Future | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.last_result: dict | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        self._task = asyncio.create_task(self._main(), name="futu-observation-loop")

    async def _main(self) -> None:
        while not self._stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(self._stop.wait(), self._interval_s)
            except TimeoutError:
                pass

    def _current(self, gen: int) -> bool:
        with self._gen_lock:
            return gen == self._gen and not self._stopping.is_set()

    def _retire_pass(self) -> None:
        with self._gen_lock:
            self._gen += 1

    async def tick(self) -> dict | None:
        """One pass on the worker thread; None when skipped, stopped or timed out."""
        if self._stopping.is_set():
            return None
        if self._inflight is not None and not self._inflight.done():
            log.warning("futu observation: previous pass still running — tick skipped")
            return None
        with self._gen_lock:
            gen = self._gen
        fut = self._executor.submit(self._pass_blocking, gen)
        self._inflight = fut
        try:
            result = await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(fut)), self._pass_timeout_s)
        except TimeoutError:
            log.warning("futu observation: pass exceeded %.0fs — retired, view invalidated", self._pass_timeout_s)
            self._retire_pass()
            await asyncio.to_thread(_invalidate, self.scope, "futu_observation_timeout")
            return None
        self.last_result = result
        return result

    async def refresh(self) -> dict | None:
        """Run one pass now, or join the pass already running (manual refresh)."""
        fut = self._inflight
        if fut is not None and not fut.done():
            try:
                return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(fut)), self._pass_timeout_s)
            except TimeoutError:
                return None
        return await self.tick()

    def _pass_blocking(self, gen: int) -> dict:
        if not self._current(gen):
            return {"published": False, "complete": False, "error": "retired"}
        if self._ctx is None:
            try:
                ctx, close = self._connect()
            except Exception as exc:  # noqa: BLE001 — OpenD down: keep serving persisted rows
                log.warning("futu observation: OpenD connect failed (%s)", exc)
                _invalidate(self.scope, "futu_opend_unreachable")
                return {"published": False, "complete": False, "error": "connect_failed"}
            self._ctx, self._close = ctx, close
            if not self._current(gen):  # stopped or timed out during connect
                self._retire_ctx()
                return {"published": False, "complete": False, "error": "retired"}
        try:
            result = self._run(
                self.scope, self._ctx, resolve_meta=self._resolve_meta, may_publish=lambda: self._current(gen)
            )
        except Exception as exc:  # noqa: BLE001 — the loop must survive any pass bug
            log.warning("futu observation: pass failed (%s)", exc, exc_info=True)
            self._retire_ctx()
            _invalidate(self.scope, "futu_observation_failed")
            return {"published": False, "complete": False, "error": type(exc).__name__}
        if not self._current(gen) or (result.get("reads") or {}).get("account") is not True:
            self._retire_ctx()  # retired, stopping, or reconnect on the next tick
        return result

    def _retire_ctx(self) -> None:
        """Close the worker's context (worker thread only)."""
        close, self._ctx, self._close = self._close, None, None
        if close is not None:
            try:
                close()
            except Exception as exc:  # noqa: BLE001
                log.warning("futu observation: context close failed (%s)", exc)

    async def stop(self, wait_s: float = 0.5) -> None:
        """Stop the loop. A running pass is retired (it cannot publish); the
        worker closes the context after it, waited for at most `wait_s`."""
        self._stopping.set()
        self._retire_pass()
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except BaseException:  # noqa: BLE001 — CancelledError or a late pass error
                pass
        try:
            cleanup = self._executor.submit(self._retire_ctx)
        except RuntimeError:  # already shut down
            cleanup = None
        self._executor.shutdown(wait=False)
        if cleanup is not None:
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(cleanup)), wait_s)
            except TimeoutError:
                log.warning("futu observation: pass still running at stop; it closes its context when it ends")


def start_observation_loop(
    *, resolve_meta: MetaResolver | None = None, connector: Callable[[AccountScope], Connector] = futu_client_connector
) -> ObservationLoop | None:
    """Start the loop for the configured Futu trade scope (`FUTU_TRADE_ACC_ID`
    + `FUTU_TRADE_ENV`). Not gated on `XENON_FUTU_TRADING`. Returns None (no
    loop) under `XENON_READ_ONLY=1` or without a configured scope."""
    if os.environ.get("XENON_READ_ONLY") == "1":
        log.info("futu observation loop skipped: XENON_READ_ONLY=1")
        return None
    scope, reason = futu_scope.configured_scope()
    if scope is None:
        log.info("futu observation loop skipped: no configured Futu scope (%s)", reason)
        return None
    loop = ObservationLoop(scope, connect=connector(scope), resolve_meta=resolve_meta)
    loop.start()
    log.info("futu observation loop started (%s, every %.0fs)", scope.account_env, OBSERVATION_INTERVAL_S)
    return loop
