"""PR #236 review round 2 (Fable F1-F11) regressions for the Futu
observation pass, resolver, read scope, display readers and ledger.

Real Postgres (`committed_db`), fake Futu trade context, fake IB pool /
metadata resolver. All ids, quantities and prices are made-up test values,
not market data.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, insert

from scripts.tests.test_futu_observation import (  # noqa: F401 — autouse fixture
    ACC,
    CALL_CODE,
    ORDER_COLS,
    SCOPE,
    SPY_META,
    FakeTradeCtx,
    _env,
    _fills,
    _frame,
    _gate,
    _order,
    _pos,
    _subs,
    resolver,
)
from scripts.tests.test_futu_observation_loop import _Pool
from xenon.api.services import futu_observation as fo
from xenon.db.engine import get_sync_engine
from xenon.db.schema import account_snapshots
from xenon.execution import orders_store
from xenon.execution.account_scope import AccountScope

pytestmark = pytest.mark.committed_db

ET = ZoneInfo("America/New_York")
OTHER = AccountScope(broker="FUTU", account_env="live", broker_account="111000111")


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("FUTU_TRADE_ACC_ID_SIMULATE", ACC)
    monkeypatch.setattr(fo, "_IB_META_CACHE", {}, raising=False)


class ByIdCtx(FakeTradeCtx):
    """Adds `order_list_query(order_id=...)` (the F3 by-id lookup)."""

    def __init__(self, *, by_id=None, **kw):
        super().__init__(**kw)
        self.by_id = dict(by_id or {})

    def order_list_query(self, **kw):
        if kw.get("order_id"):
            return self._after("order_by_id", kw, _frame(ORDER_COLS, self.by_id.get(kw["order_id"], [])))
        return super().order_list_query(**kw)


def _run(ctx):
    return fo.run_pass(SCOPE, ctx, resolve_meta=resolver())


SPEC = {"sym": "SPY", "right": "C", "exp": SPY_META["exp"], "strike": 500.0, "con_id": None}


# ---------------------------------------------------------------------------
# F1 — resolver: cross-pass cache, failures not cached, no reconnect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f1_known_contract_is_not_requalified():
    pool = _Pool()
    resolve = fo.make_ib_meta_resolver(asyncio.get_running_loop(), lambda: pool)

    first = await asyncio.to_thread(resolve, [SPEC])
    second = await asyncio.to_thread(resolve, [dict(SPEC)])

    assert first == second == [SPY_META]
    assert pool.calls == 1  # the second pass reuses the qualified meta (and con_id)


@pytest.mark.asyncio
async def test_f1_failed_lookup_is_not_cached():
    pool = _Pool(fail=True)
    resolve = fo.make_ib_meta_resolver(asyncio.get_running_loop(), lambda: pool)

    assert await asyncio.to_thread(resolve, [SPEC]) == [None]
    pool.fail = False
    assert await asyncio.to_thread(resolve, [SPEC]) == [SPY_META]
    assert pool.calls == 2


@pytest.mark.asyncio
async def test_f1_disconnected_data_role_fails_closed_without_acquire():
    pool = _Pool(connected=False)
    resolve = fo.make_ib_meta_resolver(asyncio.get_running_loop(), lambda: pool)

    assert await asyncio.to_thread(resolve, [SPEC]) == [None]
    assert pool.roles == [] and pool.calls == 0  # acquire() would reconnect


# ---------------------------------------------------------------------------
# F2 — an unpriced cumulative fill never makes the row terminal
# ---------------------------------------------------------------------------


def test_f2_terminal_status_without_price_keeps_the_row_working():
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order("7101")])
    assert _run(ctx)["complete"] is True

    ctx.open_orders = []
    ctx.history = [_order("7101", status="FILLED_ALL", dealt=1.0, avg="N/A")]
    result = _run(ctx)

    row = _subs()["7101"]
    assert result["complete"] is False and result["reads"]["persisted"] is False
    assert (row.state, row.filled_qty, row.reason_code) == ("WORKING", 0, orders_store.FUTU_FILL_UNPRICED)
    assert _fills() == []
    assert _gate().accept is False  # the SELL call's demand is still reserved


def test_f2_first_seen_unpriced_fill_is_unknown_then_priced_later():
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], history=[_order("7102", status="FILLED_ALL", dealt=1.0)])
    assert _run(ctx)["complete"] is False
    row = _subs()["7102"]
    assert (row.state, row.reason_code) == ("UNKNOWN", orders_store.FUTU_FILL_UNPRICED)

    ctx.history = [_order("7102", status="FILLED_ALL", dealt=1.0, avg=1.30)]
    assert _run(ctx)["complete"] is True
    row = _subs()["7102"]
    assert (row.state, row.filled_qty, row.reason_code) == ("FILLED", 1, None)
    assert [f.price for f in _fills()] == [Decimal("1.3000")]


# ---------------------------------------------------------------------------
# F3 — a vanished non-terminal order is looked up by id, then UNKNOWN
# ---------------------------------------------------------------------------


def test_f3_order_absent_from_two_full_passes_becomes_unknown(caplog):
    ctx = ByIdCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order("7201")])
    _run(ctx)
    ctx.open_orders = []

    assert _run(ctx)["complete"] is True
    row = _subs()["7201"]
    assert row.state == "WORKING" and row.legs["futu_missing"] == 1
    lookups = [kw for name, kw in ctx.calls if name == "order_by_id"]
    assert lookups and lookups[0]["order_id"] == "7201" and lookups[0]["refresh_cache"] is True

    with caplog.at_level(logging.WARNING, logger=fo.__name__):
        _run(ctx)
    row = _subs()["7201"]
    assert (row.state, row.reason_code) == ("UNKNOWN", orders_store.FUTU_NOT_OBSERVED)
    assert "absent from 2 full passes" in caplog.text
    assert _gate().accept is False  # UNKNOWN keeps the demand


def test_f3_partial_pass_never_counts_a_miss():
    ctx = ByIdCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order("7202")])
    _run(ctx)
    ctx.open_orders = []
    ctx.fail["history_orders"] = (-1, "history down")

    for _ in range(3):
        assert _run(ctx)["complete"] is False
    row = _subs()["7202"]
    assert row.state == "WORKING" and "futu_missing" not in row.legs
    assert not [n for n, _ in ctx.calls if n == "order_by_id"]


def test_f3_by_id_lookup_mirrors_a_found_order():
    ctx = ByIdCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order("7203")])
    _run(ctx)
    ctx.open_orders = []
    ctx.by_id["7203"] = [_order("7203", status="FILLED_ALL", dealt=1.0, avg=1.10)]

    assert _run(ctx)["complete"] is True
    row = _subs()["7203"]
    assert (row.state, row.filled_qty) == ("FILLED", 1) and "futu_missing" not in row.legs
    assert [f.price for f in _fills()] == [Decimal("1.1000")]


def test_f3_failed_lookup_makes_the_pass_incomplete_and_counts_nothing():
    ctx = ByIdCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order("7204")])
    _run(ctx)
    ctx.open_orders = []
    ctx.fail["order_by_id"] = (-1, "lookup down")

    assert _run(ctx)["complete"] is False
    assert "futu_missing" not in _subs()["7204"].legs


def test_f3_reobserved_order_clears_the_miss_marker():
    ctx = ByIdCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order("7205")])
    _run(ctx)
    held = ctx.open_orders
    ctx.open_orders = []
    _run(ctx)
    _run(ctx)
    assert _subs()["7205"].state == "UNKNOWN"

    ctx.open_orders = [_order("7205")]
    assert _run(ctx)["complete"] is True
    row = _subs()["7205"]
    assert (row.state, row.reason_code) == ("WORKING", None) and "futu_missing" not in row.legs
    assert held  # same order the whole time


# ---------------------------------------------------------------------------
# F4 — the history window never ends in the future
# ---------------------------------------------------------------------------


def test_f4_history_window_end_is_not_in_the_future():
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)])
    _run(ctx)
    after = datetime.now(ET).replace(tzinfo=None)

    (kw,) = [kw for name, kw in ctx.calls if name == "history_orders"]
    assert datetime.strptime(kw["end"], "%Y-%m-%d %H:%M:%S") <= after


# ---------------------------------------------------------------------------
# F5 — the configured trade scope is the FUTU read scope
# ---------------------------------------------------------------------------


def _offline_futu(monkeypatch, db_scope):
    from xenon.api import guards
    from xenon.api.routes import futu as futu_routes

    offline = type("Offline", (), {"is_connected": lambda self: False})()
    monkeypatch.setattr(futu_routes, "_get_futu_client", lambda *_a: offline)
    monkeypatch.setattr(guards, "_futu_scope_from_db", lambda: db_scope)
    return guards


@pytest.mark.parametrize("db_scope", [OTHER, None])
def test_f5_configured_scope_wins_without_opend(monkeypatch, db_scope):
    guards = _offline_futu(monkeypatch, db_scope)

    req = type("Req", (), {"query_params": {}})()
    assert guards.get_broker_scope(req, "FUTU") == SCOPE  # no 503, no other account


def test_f5_without_configured_scope_the_fallback_stays(monkeypatch):
    guards = _offline_futu(monkeypatch, OTHER)
    monkeypatch.delenv("FUTU_TRADE_ACC_ID_SIMULATE")

    req = type("Req", (), {"query_params": {"account_env": OTHER.account_env}})()
    assert guards.get_broker_scope(req, "FUTU") == OTHER


# ---------------------------------------------------------------------------
# F6 — display readers never take an observation row
# ---------------------------------------------------------------------------


def _store(scope: AccountScope, payload: dict):
    with get_sync_engine().begin() as conn:
        return conn.execute(
            insert(account_snapshots)
            .values(
                account=scope.broker_account,
                bankroll=Decimal(1),
                payload=payload,
                broker=scope.broker,
                account_env=scope.account_env,
                broker_account=scope.broker_account,
                snapshot_at=func.clock_timestamp(),
            )
            .returning(account_snapshots.c.snapshot_at)
        ).scalar_one()


def _display_then_observation():
    shown = _store(SCOPE, {"positions": [], "fetched_at": "display"})
    _store(SCOPE, {"positions": [], "observation": {"complete": True}, "fetched_at": "observation"})
    return shown


def test_f6_scoped_and_unscoped_loaders_take_display_rows():
    from xenon.utils.portfolio_loader import load_account_snapshots_history_sync, load_portfolio_payload_sync

    _display_then_observation()

    assert load_portfolio_payload_sync(scope=SCOPE)["fetched_at"] == "display"
    assert load_portfolio_payload_sync(scope=None)["fetched_at"] == "display"
    assert [r["payload"]["fetched_at"] for r in load_account_snapshots_history_sync(scope=SCOPE)] == ["display"]


def test_f6_preflight_loader_takes_the_display_row(monkeypatch):
    from xenon.api.services import order_entry

    shown = _display_then_observation()
    monkeypatch.setattr(order_entry, "_resolve_scope_kwargs", lambda: SCOPE.as_dict())

    view, at = order_entry._load_portfolio_view_sync()

    assert view is not None and at == shown


def test_f6_snapshotter_heartbeat_ignores_observation_rows():
    from xenon.api.routes.system import _snapshotter_health

    shown = _display_then_observation()

    assert _snapshotter_health()["last_write_at"] == shown.astimezone(timezone.utc).isoformat()


@pytest.mark.asyncio
async def test_f6_portfolio_query_takes_the_display_row():
    from sqlalchemy.ext.asyncio import create_async_engine

    from xenon._test_db import sync_test_db_url
    from xenon.db.queries.portfolio import get_latest_portfolio_payload

    _display_then_observation()
    url = sync_test_db_url().replace("postgresql+psycopg://", "postgresql+psycopg_async://", 1)
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            got = await get_latest_portfolio_payload(conn, **SCOPE.as_dict())
    finally:
        await engine.dispose()
    payload = got.get("payload", got)
    assert payload["fetched_at"] == "display"


# ---------------------------------------------------------------------------
# F7 — Futu DB reads in async routes run off the event loop
# ---------------------------------------------------------------------------


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def test_f7_futu_reads_run_off_the_event_loop(monkeypatch):
    from fastapi.testclient import TestClient

    from xenon.api import server
    from xenon.api.routes import futu as futu_routes
    from xenon.api.routes import orders as orders_routes
    from xenon.api.routes import system

    _offline_futu(monkeypatch, None)
    seen: dict[str, bool] = {}

    def _rec(name, ret):
        def fn(*a, **k):
            seen[name] = _on_loop()
            return ret

        return fn

    monkeypatch.setattr(futu_routes, "latest_display_snapshot", _rec("display", None))
    monkeypatch.setattr(futu_routes, "_any_display_snapshot", _rec("any", False))
    monkeypatch.setattr(system, "_compute_futu_health", _rec("health", {}))
    monkeypatch.setattr(orders_routes, "orders_payload_for_scope", _rec("orders", {}))
    client = TestClient(server.app)

    client.get("/futu/portfolio")
    client.get("/health")
    client.get("/orders", params={"broker": "FUTU"})

    assert seen == {"display": False, "any": False, "health": False, "orders": False}


# ---------------------------------------------------------------------------
# F8 — raw Futu status persisted and exposed; FILL_CANCELLED logged
# ---------------------------------------------------------------------------


def test_f8_raw_status_is_stored_and_exposed():
    from xenon.api.routes.orders import orders_payload_for_scope

    _run(FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order("7301", status="WAITING_SUBMIT")]))

    row = _subs()["7301"]
    assert row.state == "UNKNOWN" and row.legs["futu_order"]["status"] == "WAITING_SUBMIT"
    (shown,) = orders_payload_for_scope(SCOPE)["open_orders"]
    assert shown["futuStatus"] == "WAITING_SUBMIT"


def test_f8_fill_cancelled_is_logged(caplog):
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], history=[_order("7302", status="FILL_CANCELLED")])
    with caplog.at_level(logging.WARNING, logger=fo.__name__):
        _run(ctx)
    assert "7302 reports FILL_CANCELLED" in caplog.text


# ---------------------------------------------------------------------------
# F9 — the external submission id carries the scope
# ---------------------------------------------------------------------------


def _obs(order_id, *, code=CALL_CODE, metas=(SPY_META,)):
    cache = fo._MetaCache(resolver(metas))
    row = _order(order_id, code=code)
    cache.prefetch(fo._option_specs([row]))
    return fo._order_observation(row, SCOPE, cache, datetime.now(timezone.utc))


def test_f9_same_order_id_in_two_scopes_gets_two_rows():
    obs = _obs("7401")
    now = datetime.now(timezone.utc)

    a = orders_store.mirror_futu_order(obs, broker_account=ACC, account_env="paper", observed_at=now)
    b = orders_store.mirror_futu_order(obs, broker_account=ACC, account_env="live", observed_at=now)

    assert (a["action"], b["action"]) == ("INSERTED", "INSERTED")
    assert {a["submission_id"], b["submission_id"]} == {f"futu-paper-{ACC}-7401", f"futu-live-{ACC}-7401"}


# ---------------------------------------------------------------------------
# F11 — no default multiplier without qualified metadata
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,metas,expected",
    [(CALL_CODE, (SPY_META,), 100), (CALL_CODE, (), None), ("US.SPY", (), 1)],
)
def test_f11_multiplier_only_from_qualified_metadata(code, metas, expected):
    obs = _obs("7501", code=code, metas=metas)
    orders_store.mirror_futu_order(obs, broker_account=ACC, account_env="paper", observed_at=datetime.now(timezone.utc))

    assert _subs()["7501"].multiplier == expected


# ---------------------------------------------------------------------------
# Strike precision: exact Futu code, not the Numeric(12, 2) column
# ---------------------------------------------------------------------------


def test_strike_with_three_decimals_matches_its_stored_row():
    """Futu "C500125" = 500.125. The stored column value is read back from
    Postgres (500.13, display precision). Later passes compare the exact Futu
    code, so the same order is no identity mismatch and the pass stays
    complete."""
    odd = f"US.SPY{SPY_META['exp'][2:]}C500125"
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order("7601", code=odd)])
    assert _run(ctx)["complete"] is True
    stored = _subs()["7601"].strike
    assert stored == Decimal("500.13")  # display precision only; identity is the Futu code

    ctx.open_orders = [_order("7601", code=odd, status="FILLED_ALL", dealt=1.0, avg=1.20)]
    result = _run(ctx)

    assert result["complete"] is True and result["reads"]["persisted"] is True
    row = _subs()["7601"]
    assert (row.state, row.filled_qty) == ("FILLED", 1)


def test_strike_a_cent_apart_is_still_a_mismatch():
    obs = _obs("7602")  # strike 500.00
    now = datetime.now(timezone.utc)
    orders_store.mirror_futu_order(obs, broker_account=ACC, account_env="paper", observed_at=now)
    other = _obs("7602", code=f"US.SPY{SPY_META['exp'][2:]}C500010", metas=())  # 500.01

    out = orders_store.mirror_futu_order(other, broker_account=ACC, account_env="paper", observed_at=now)

    assert out["identity_mismatch"] == "strike"
