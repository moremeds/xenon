"""X3b Task 2: GET /orders + refresh for FUTU read the execution ledger.

Rows come from `order_submissions` + `order_fills` of the configured Futu
trade scope (FUTU_TRADE_ACC_ID + FUTU_TRADE_ENV), written by a real
observation pass over a fake Futu trade context. `futu_orders` /
`futu_trades` are history only and never read here. 19-digit Futu ids stay
strings; no display hash is returned. All ids, quantities and prices are
made-up test values, not market data.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import update

from scripts.tests.test_futu_observation import (  # noqa: F401 — autouse fixture
    ACC,
    CALL_CODE,
    ORDER_COLS,
    OTHER_ACC,
    SCOPE,
    SPY_META,
    FakeTradeCtx,
    _acc_row,
    _env,
    _frame,
    _order,
    _pos,
    resolver,
)
from xenon.api.routes.orders import orders_payload_for_scope
from xenon.api.services import futu_observation as fo
from xenon.db.engine import get_sync_engine
from xenon.db.queries.futu_history import insert_orders
from xenon.db.schema import order_submissions
from xenon.execution.account_scope import AccountScope

pytestmark = pytest.mark.committed_db

os.environ.setdefault("XENON_API_TEST_MODE", "1")

# A display/history scope that differs from the configured trade scope.
OTHER = AccountScope(broker="FUTU", account_env="live", broker_account="999000111")
WORKING_ID = "8812345678901234567"  # 19 digits: beyond JS Number precision
FILLED_ID = "8812345678901234999"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("FUTU_TRADE_ACC_ID_SIMULATE", ACC)


def _observe():
    ctx = FakeTradeCtx(
        positions=[_pos("US.SPY", 100.0)],
        open_orders=[_order(order_id=WORKING_ID, side="SELL", qty=2.0, status="FILLED_PART", dealt=1.0, avg=1.5)],
        history=[
            _order(order_id=FILLED_ID, code="US.SPY", side="BUY", qty=10.0, status="FILLED_ALL", dealt=10.0, avg=2.0)
        ],
    )
    result = fo.run_pass(SCOPE, ctx, resolve_meta=resolver(), audit=False)
    assert result["complete"], result


def test_futu_open_orders_come_from_the_ledger_with_string_ids():
    _observe()
    with get_sync_engine().begin() as conn:
        conn.execute(
            update(order_submissions).where(order_submissions.c.broker_order_id == WORKING_ID).values(modify_sequence=3)
        )

    body = orders_payload_for_scope(SCOPE)

    assert body["scope"] == {"broker": "FUTU", "accountEnv": "paper", "brokerAccount": ACC}
    assert body["open_count"] == 1
    row = body["open_orders"][0]
    assert row["submissionId"] == f"futu-paper-{ACC}-{WORKING_ID}"
    assert row["brokerOrderId"] == WORKING_ID and isinstance(row["brokerOrderId"], str)
    assert row["modifySequence"] == 3
    assert (row["broker"], row["accountEnv"], row["brokerAccount"]) == ("FUTU", "paper", ACC)
    # No display hash: there is no numeric IB id for a Futu order.
    assert row["orderId"] == 0 and row["permId"] == 0
    assert row["status"] == "PartiallyFilled"
    assert (row["totalQuantity"], row["filled"], row["remaining"]) == (2, 1, 1)
    assert row["contract"]["secType"] == "OPT" and row["contract"]["symbol"] == "SPY"


def test_futu_executed_orders_come_from_order_fills():
    _observe()

    body = orders_payload_for_scope(SCOPE)

    by_order = {r["brokerOrderId"]: r for r in body["executed_orders"]}
    assert set(by_order) == {WORKING_ID, FILLED_ID}
    stock = by_order[FILLED_ID]
    assert stock["execId"] == f"futu:paper:{ACC}:ord:{FILLED_ID}:10"
    assert stock["submissionId"] == f"futu-paper-{ACC}-{FILLED_ID}"
    assert (stock["side"], stock["quantity"], stock["avgPrice"]) == ("BOT", 10.0, 2.0)
    assert stock["exchange"] == "FUTU" and stock["broker"] == "FUTU" and stock["brokerAccount"] == ACC
    assert by_order[WORKING_ID]["side"] == "SLD" and by_order[WORKING_ID]["quantity"] == 1.0


@pytest.mark.asyncio
async def test_futu_history_tables_are_not_the_read_path():
    """A futu_orders history row of the scope is not an open order here."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from xenon._test_db import sync_test_db_url

    eng = create_async_engine(sync_test_db_url().replace("postgresql+psycopg://", "postgresql+asyncpg://"))
    now = datetime.now(timezone.utc)
    await insert_orders(
        eng,
        SCOPE,
        [
            {
                "futu_order_id": "100200300",
                "ticker": "QQQ",
                "futu_code": "US.QQQ",
                "market": "US",
                "action": "BUY",
                "order_type": "NORMAL",
                "quantity": 1,
                "limit_price": 1.0,
                "aux_price": None,
                "status": "SUBMITTED",
                "tif": "GTC",
                "filled_qty": 0,
                "avg_fill_price": None,
                "created_at": now,
                "updated_at": now,
                "raw": {},
            }
        ],
    )
    await eng.dispose()

    body = orders_payload_for_scope(SCOPE)

    assert body["open_orders"] == [] and body["executed_orders"] == []


def test_no_configured_scope_returns_empty_with_reason(monkeypatch):
    _observe()
    monkeypatch.delenv("FUTU_TRADE_ACC_ID_SIMULATE")

    body = orders_payload_for_scope(SCOPE)

    assert body["open_orders"] == [] and body["executed_orders"] == []
    assert body["scope"] is None and body["scope_reason"] == "not_configured" and body["scope_env"] == "paper"


def test_get_orders_route_serves_ledger_while_opend_offline(monkeypatch):
    """OpenD offline (client never connected): the route still serves the
    persisted ledger rows of the configured scope."""
    from fastapi.testclient import TestClient

    from xenon.api import guards, server
    from xenon.api.routes import futu as futu_routes

    _observe()
    offline = type("Offline", (), {"is_connected": lambda self: False})()
    monkeypatch.setattr(futu_routes, "_get_futu_client", lambda *_a: offline)
    monkeypatch.setattr(guards, "_futu_scope_from_db", lambda: SCOPE)

    resp = TestClient(server.app).get("/orders", params={"broker": "FUTU"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [r["brokerOrderId"] for r in body["open_orders"]] == [WORKING_ID]
    assert body["scope"]["brokerAccount"] == ACC


@pytest.mark.parametrize(
    "request_scope",
    [
        AccountScope(broker="FUTU", account_env="live", broker_account=ACC),  # REAL tab, SIMULATE configured
        OTHER,  # another account
    ],
)
def test_request_scope_other_than_configured_gets_no_rows(monkeypatch, request_scope):
    """Paper and live never blend: the configured scope's orders are served
    only under exactly that scope."""
    _observe()

    body = orders_payload_for_scope(request_scope)  # REAL has no account

    assert body["open_orders"] == [] and body["executed_orders"] == []
    assert body["open_count"] == 0 and body["executed_count"] == 0
    assert (body["scope"], body["scope_reason"], body["scope_env"]) == (None, "not_configured", "live")

    monkeypatch.setenv("FUTU_TRADE_ACC_ID_REAL", "123123123")  # REAL configured, another account
    body = orders_payload_for_scope(request_scope)

    assert body["open_orders"] == [] and body["executed_orders"] == []
    assert body["scope"] is None and body["scope_reason"] == "scope_mismatch"
    assert body["request_scope"] == {
        "broker": "FUTU",
        "accountEnv": request_scope.account_env,
        "brokerAccount": request_scope.broker_account,
    }
    assert body["configured_scope"] == {"broker": "FUTU", "accountEnv": "live", "brokerAccount": "123123123"}


def _route_client(monkeypatch, db_scope):
    from fastapi.testclient import TestClient

    from xenon.api import guards, server
    from xenon.api.routes import futu as futu_routes

    offline = type("Offline", (), {"is_connected": lambda self: False})()
    monkeypatch.setattr(futu_routes, "_get_futu_client", lambda *_a: offline)
    monkeypatch.setattr(guards, "_futu_scope_from_db", lambda: db_scope)
    return TestClient(server.app)


def test_get_orders_route_uses_the_configured_scope_not_the_db_fallback(monkeypatch):
    """R2 F5: with a configured trade scope, the route never takes another
    account's last-synced scope; the configured ledger is served."""
    _observe()

    body = _route_client(monkeypatch, OTHER).get("/orders", params={"broker": "FUTU"}).json()

    assert [r["brokerOrderId"] for r in body["open_orders"]] == [WORKING_ID]
    assert body.get("scope_reason") is None


@pytest.mark.parametrize("db_scope", [SCOPE, OTHER, None])
def test_orders_refresh_applies_the_same_scope_rule(monkeypatch, db_scope):
    """No observation loop running (e.g. OpenD scope not started): refresh
    returns the persisted ledger of the configured scope, whatever the DB
    fallback would say (and with no synced history at all)."""
    expected = [WORKING_ID]
    from xenon.api import server

    _observe()
    monkeypatch.setattr(server, "_is_test_mode", lambda: False)
    monkeypatch.setattr(server.app.state, "futu_observation_loop", None, raising=False)

    resp = _route_client(monkeypatch, db_scope).post("/orders/refresh", params={"broker": "FUTU"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [r["brokerOrderId"] for r in body["open_orders"]] == expected
    assert body.get("scope_reason") is None


def test_ib_payload_shape_unchanged():
    body = orders_payload_for_scope(AccountScope(broker="IB", account_env="paper", broker_account="DU0000000"))
    assert set(body) == {"last_sync", "open_orders", "executed_orders", "open_count", "executed_count"}


# -- X3c Task 3: FUTU dispatch (place / cancel / modify) ----------------------
#
# A fake Futu trade context (no OpenD), driven through the real routes, the
# real Futu order worker, Gate 4 and the observation mirror. Ids, quantities
# and prices are made-up test values, not market data.

LIVE_ACC = "281756479345015777"
SECRET = "0123456789abcdef0123456789ABCDEF"  # made-up, format-valid


class FakeOrderCtx(FakeTradeCtx):
    """FakeTradeCtx plus the trade mutations, over an in-memory order book.
    Every mutation is first bound against `create_autospec` of the installed
    SDK's `OpenSecTradeContext` (an unsupported keyword raises TypeError)."""

    TERMS = ("aux_price", "trail_type", "trail_value", "trail_spread")

    def __init__(self, *, place="ok", modify="apply", cancel="cancelled"):
        from unittest.mock import create_autospec

        from futu import OpenSecTradeContext

        super().__init__(accounts=[_acc_row(), _acc_row(LIVE_ACC, env="REAL")])
        self.place_mode, self.modify_mode, self.cancel_mode = place, modify, cancel
        self.book: dict[str, dict] = {}
        self.mutations: list[tuple[str, dict]] = []
        self.unlocks = 0
        self._next = 8812345678900000001
        self._sdk = create_autospec(OpenSecTradeContext, instance=True)

    def unlock_trade(self, **kw):
        self._sdk.unlock_trade(**kw)
        self.unlocks += 1
        self.calls.append(("unlock_trade", {}))
        return 0, "ok"

    def place_order(self, **kw):
        self._sdk.place_order(**kw)
        self.mutations.append(("place_order", kw))
        if self.place_mode == "lost":
            return -1, "timeout"
        oid = str(self._next)
        self._next += 1
        row = _order(order_id=oid, code=kw["code"], side=kw["trd_side"], qty=float(kw["qty"]), remark=kw["remark"])
        row["price"] = kw["price"]
        # The booked order carries the type and terms the SDK was sent.
        row["order_type"] = kw["order_type"]
        row.update({k: kw[k] for k in self.TERMS + ("session",) if k in kw})
        self.book[oid] = row
        if self.place_mode == "error_after_send":
            return -1, "timeout"
        return 0, _frame(ORDER_COLS, [row])

    def modify_order(self, **kw):
        self._sdk.modify_order(**kw)
        self.mutations.append(("modify_order", kw))
        row = self.book[kw["order_id"]]
        if kw["modify_order_op"] == "CANCEL":
            row["order_status"] = {"cancelled": "CANCELLED_ALL", "pending": "SUBMITTED", "filled": "FILLED_ALL"}[
                self.cancel_mode
            ]
            if self.cancel_mode == "filled":  # filled before the cancel arrived
                row["dealt_qty"], row["dealt_avg_price"] = row["qty"], row["price"]
        elif self.modify_mode == "apply":
            row["qty"], row["price"] = float(kw["qty"]), kw["price"]
            row.update({k: kw[k] for k in self.TERMS if k in kw})
        return 0, _frame(["trd_env", "order_id"], [{"trd_env": kw["trd_env"], "order_id": kw["order_id"]}])

    def order_list_query(self, **kw):
        rows = [
            r
            for r in self.book.values()
            if kw.get("order_id") in (None, r["order_id"]) and kw.get("code") in (None, r["code"])
        ]
        return self._after("open_orders", kw, _frame(ORDER_COLS, rows))


class _NoIB:
    """server.order_owner / ib_pool stand-in: any IB use fails the test."""

    def __getattr__(self, name):
        raise AssertionError(f"FUTU request reached IB ({name})")


@pytest.fixture
def futu_ib_quotes(monkeypatch):
    """FUTU trading on for both envs; the IB quote gate is the real one."""
    from fastapi.testclient import TestClient

    from xenon.api import server
    from xenon.api.futu_order_worker import FutuOrderWorker

    monkeypatch.setenv("FUTU_TRADE_ACC_ID_REAL", LIVE_ACC)
    monkeypatch.setenv("XENON_FUTU_TRADING", "simulate,real")
    monkeypatch.setenv("FUTU_TRADE_PWD_MD5", SECRET)
    ctx = FakeOrderCtx()
    worker = FutuOrderWorker(connect=lambda scope: (ctx, lambda: None))
    monkeypatch.setattr(server.app.state, "futu_order_worker", worker, raising=False)
    monkeypatch.setattr(server, "order_owner", _NoIB())
    ns = type("Futu", (), {})()
    ns.ctx, ns.client, ns.worker = ctx, TestClient(server.app), worker
    return ns


@pytest.fixture
def futu(futu_ib_quotes, monkeypatch):
    """As futu_ib_quotes, with the IB quote gate passing."""
    from xenon.api.services import order_entry

    async def _quote_ok(body, submission_id):
        return None

    monkeypatch.setattr(order_entry, "_quote_gate", _quote_ok)
    return futu_ib_quotes


def _stock(cid="futu-place-1", **kw):
    return {
        "type": "stock",
        "symbol": "SPY",
        "action": "BUY",
        "quantity": 2,
        "limitPrice": 500.2,
        "client_attempt_id": cid,
        **kw,
    }


def _post(futu, path, body, env="paper"):
    return futu.client.post(path, params={"broker": "FUTU", "account_env": env}, json=body)


def _row(sid):
    with get_sync_engine().connect() as conn:
        return conn.execute(order_submissions.select().where(order_submissions.c.submission_id == sid)).first()


def _fill_count():
    from sqlalchemy import func, select

    from xenon.db.schema import order_fills

    with get_sync_engine().connect() as conn:
        return conn.execute(select(func.count()).select_from(order_fills)).scalar()


@pytest.mark.parametrize("env, acc, trd_env, unlocks", [("paper", ACC, "SIMULATE", 0), ("live", LIVE_ACC, "REAL", 1)])
def test_futu_place_binds_own_row_in_the_request_scope(futu, env, acc, trd_env, unlocks):
    resp = _post(futu, "/orders/place", _stock(), env=env)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    sid, oid = body["submission_id"], body["brokerOrderId"]
    assert isinstance(oid, str) and body["broker"] == "FUTU" and body["accountEnv"] == env
    assert acc not in resp.text  # no account number in the reply
    [(name, kw)] = futu.ctx.mutations
    assert name == "place_order" and kw["acc_id"] == int(acc) and kw["trd_env"] == trd_env
    assert kw["remark"] == f"xo:{sid}" and kw["code"] == "US.SPY" and kw["trd_side"] == "BUY"
    assert futu.ctx.unlocks == unlocks  # unlock only for REAL
    row = _row(sid)
    assert (row.broker, row.account_env, row.broker_account) == ("FUTU", env, acc)
    assert row.broker_order_id == oid and row.state == "WORKING" and row.ib_order_id is None
    assert (row.ticker, row.action, row.quantity) == ("SPY", "BUY", 2)
    assert row.legs["futu_code"] == "US.SPY"
    assert _fill_count() == 0  # the dispatch never writes order_fills


def test_futu_place_option_row_carries_the_futu_code(futu, monkeypatch):
    from xenon.api.services import order_entry

    async def _meta(body):
        return [dict(SPY_META)]

    monkeypatch.setattr(order_entry, "_resolve_gate4_leg_meta", _meta)
    body = _stock(type="option", expiry="2026-12-18", right="C", strike=500, limitPrice=1.25)

    resp = _post(futu, "/orders/place", body)

    assert resp.status_code == 200, resp.text
    row = _row(resp.json()["submission_id"])
    assert futu.ctx.mutations[0][1]["code"] == CALL_CODE == row.legs["futu_code"]
    assert (row.security_type, row.right, float(row.strike)) == ("OPT", "C", 500.0)
    assert row.expiry.strftime("%Y%m%d") == "20261218"


@pytest.mark.parametrize("mode", ["lost", "error_after_send"])
def test_futu_uncertain_place_is_unknown_or_recovered_by_remark_never_resent(futu, mode):
    futu.ctx.place_mode = mode

    resp = _post(futu, "/orders/place", _stock())

    assert [m[0] for m in futu.ctx.mutations] == ["place_order"]  # never re-sent
    remark_reads = [kw for name, kw in futu.ctx.calls if name == "open_orders" and kw.get("code") == "US.SPY"]
    assert remark_reads and all(kw["refresh_cache"] is True and kw["acc_id"] == int(ACC) for kw in remark_reads)
    if mode == "lost":  # no order carries the remark → UNKNOWN, demand kept
        assert resp.status_code == 502 and resp.json()["reason_code"] == "PLACE_OUTCOME_UNKNOWN"
        row = _row(resp.json()["submission_id"])
        assert row.state == "UNKNOWN" and row.broker_order_id is None
    else:  # the order exists: found by its exact remark and bound
        assert resp.status_code == 200, resp.text
        row = _row(resp.json()["submission_id"])
        assert row.state == "WORKING" and row.broker_order_id == next(iter(futu.ctx.book))


def test_futu_place_with_ib_quotes_down_is_503_naming_ib(futu_ib_quotes, monkeypatch):
    from xenon.api import server
    from xenon.api.services import order_entry

    monkeypatch.setattr(server, "ib_pool", None)  # IB data role down
    monkeypatch.setattr(order_entry, "_now", lambda: datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc))  # RTH

    resp = _post(futu_ib_quotes, "/orders/place", _stock(cid="futu-ib-down"))

    assert resp.status_code == 503 and resp.json()["reason_code"] == "QUOTE_UNAVAILABLE"
    assert "IB" in resp.json()["detail"]
    assert futu_ib_quotes.ctx.mutations == []  # never sent, never re-routed


@pytest.mark.parametrize(
    "path, body, code",
    [
        ("/orders/place", {**_stock(), "type": "combo", "legs": []}, "READ_ONLY_BROKER"),
        ("/orders/replace", {"replaceOrder": _stock()}, "READ_ONLY_BROKER"),
    ],
)
def test_futu_combo_and_replace_are_refused_and_never_reach_ib(futu, path, body, code):
    resp = _post(futu, path, body)

    assert resp.status_code == 403 and resp.json()["reason_code"] == code
    assert futu.ctx.mutations == [] and futu.ctx.calls == []


def test_futu_mutation_without_account_env_is_refused(futu):
    resp = futu.client.post("/orders/place", params={"broker": "FUTU"}, json=_stock())

    assert resp.status_code == 400 and resp.json()["reason_code"] == "FUTU_SCOPE_REQUIRED"
    assert futu.ctx.mutations == []


def _placed(futu, cid="futu-manage-1"):
    resp = _post(futu, "/orders/place", _stock(cid=cid))
    assert resp.status_code == 200, resp.text
    return resp.json()["submission_id"], resp.json()["brokerOrderId"]


def test_futu_cancel_keeps_demand_until_fresh_evidence_is_terminal(futu):
    sid, oid = _placed(futu)
    futu.ctx.cancel_mode = "pending"  # RET_OK, but Futu still reports the order working

    resp = _post(futu, "/orders/cancel", {"brokerOrderId": oid})

    assert resp.status_code == 200 and resp.json()["confirmed"] is False
    assert _row(sid).state == "WORKING"  # demand kept
    futu.ctx.cancel_mode = "cancelled"

    resp = _post(futu, "/orders/cancel", {"brokerOrderId": oid})

    assert resp.status_code == 200 and resp.json()["confirmed"] is True
    assert _row(sid).state == "CANCELLED"
    cancels = [kw for name, kw in futu.ctx.mutations if name == "modify_order"]
    assert all(kw["modify_order_op"] == "CANCEL" and kw["acc_id"] == int(ACC) for kw in cancels)


def test_futu_management_ids_resolve_inside_the_request_scope_only(futu):
    _sid, oid = _placed(futu)

    for path, body in (
        ("/orders/cancel", {"brokerOrderId": oid}),
        ("/orders/modify", {"brokerOrderId": oid, "newPrice": 500.1, "modifySequence": 1}),
    ):
        resp = _post(futu, path, body, env="live")  # the paper order, asked in the live scope
        assert resp.status_code == 404 and resp.json()["reason_code"] == "ORDER_NOT_FOUND"
    resp = _post(futu, "/orders/cancel", {"orderId": 5})  # an IB id is not a Futu id
    assert resp.status_code == 400 and resp.json()["reason_code"] == "ORDER_IDENTIFIER_REQUIRED"
    assert [m[0] for m in futu.ctx.mutations] == ["place_order"]


def test_futu_modify_runs_the_real_path_under_test_mode(futu, monkeypatch):
    """The IB modify returns early under XENON_API_TEST_MODE; the FUTU modify
    never does: it runs on the fake context and settles from evidence."""
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    sid, oid = _placed(futu)

    resp = _post(
        futu, "/orders/modify", {"brokerOrderId": oid, "newQuantity": 3, "newPrice": 500.1, "modifySequence": 1}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["confirmed"] is True and resp.json()["confirmedQuantity"] == 3
    [(_, kw)] = [m for m in futu.ctx.mutations if m[0] == "modify_order"]
    assert (kw["modify_order_op"], kw["qty"], kw["price"], kw["acc_id"]) == ("NORMAL", 3, 500.1, int(ACC))
    row = _row(sid)
    assert row.quantity == 3 and float(row.limit_price) == 500.1 and row.modify_sequence == 1
    assert row.legs.get("pending_amend_qty") is None and row.legs.get("pending_amend_id") is None


def test_futu_modify_without_fresh_evidence_keeps_the_larger_reservation(futu):
    sid, oid = _placed(futu)
    futu.ctx.modify_mode = "ignore"  # RET_OK, but Futu reports the old terms

    resp = _post(futu, "/orders/modify", {"brokerOrderId": oid, "newQuantity": 5, "modifySequence": 1})

    assert resp.status_code == 200 and resp.json()["confirmed"] is False
    row = _row(sid)
    assert row.quantity == 2 and row.legs["pending_amend_qty"] == 5  # Gate 4 reserves max(2, 5)
    resp = _post(futu, "/orders/modify", {"brokerOrderId": oid, "newQuantity": 4, "modifySequence": 2})
    assert resp.status_code == 409 and resp.json()["reason_code"] == "MODIFY_PENDING"
    assert resp.json()["detail"]["applied_sequence"] == 2  # the sequence already advanced
    stale = _post(futu, "/orders/modify", {"brokerOrderId": oid, "newPrice": 500.1, "modifySequence": 1})
    assert stale.status_code == 409 and stale.json()["reason_code"] == "MODIFY_STALE"
    assert len([m for m in futu.ctx.mutations if m[0] == "modify_order"]) == 1


def test_futu_modify_after_the_send_window_sends_nothing(futu, monkeypatch):
    from xenon.execution import gate4

    sid, oid = _placed(futu)
    monkeypatch.setattr(gate4, "MODIFY_SEND_WINDOW_S", -1)  # send_by already passed

    resp = _post(futu, "/orders/modify", {"brokerOrderId": oid, "newQuantity": 3, "modifySequence": 1})

    assert resp.status_code == 503 and resp.json()["reason_code"] == "FUTU_SEND_WINDOW_CLOSED"
    assert [m[0] for m in futu.ctx.mutations] == ["place_order"]
    assert _row(sid).legs.get("pending_amend_id") is None  # proven unsent: the amend is released


# -- /health futu.trading -----------------------------------------------------


def _trading():
    from xenon.api.routes import system

    health = system._compute_futu_health()
    assert ACC not in str(health) and LIVE_ACC not in str(health)
    return health["trading"]


def test_health_trading_all_true(futu):
    assert _trading() == {"paper": True, "live": True}


@pytest.mark.parametrize(
    "change, expected",
    [
        (lambda mp: mp.setenv("XENON_FUTU_TRADING", "real"), {"paper": False, "live": True}),  # env not enabled
        (lambda mp: mp.delenv("FUTU_TRADE_ACC_ID_REAL"), {"paper": True, "live": False}),  # no account
        (lambda mp: mp.delenv("FUTU_TRADE_PWD_MD5"), {"paper": True, "live": False}),  # secret missing
        (lambda mp: mp.setenv("FUTU_TRADE_PWD_MD5", "not-md5"), {"paper": True, "live": False}),  # malformed
        (lambda mp: mp.setenv("XENON_READ_ONLY", "1"), {"paper": False, "live": False}),
    ],
)
def test_health_trading_false_reasons(futu, monkeypatch, change, expected):
    change(monkeypatch)
    assert _trading() == expected


def test_health_trading_false_under_scope_change_veto(futu, monkeypatch):
    other = AccountScope(broker="FUTU", account_env="paper", broker_account=OTHER_ACC)
    ctx = FakeTradeCtx(open_orders=[_order(order_id="7101", code="US.SPY", side="BUY")])
    assert fo.run_pass(other, ctx, resolve_meta=resolver(), audit=False)["complete"]

    assert _trading() == {"paper": False, "live": True}  # unresolved row of another paper account


# -- X3c Task 3b: review fixes (Astra F1/F2, Fable P2/P3) ---------------------


def _modifies(futu):
    return [kw for name, kw in futu.ctx.mutations if name == "modify_order"]


def test_futu_price_only_modify_sends_the_broker_quantity(futu):
    sid, oid = _placed(futu)
    futu.ctx.book[oid]["qty"] = 1.0  # the user cut the order in the Futu app

    resp = _post(futu, "/orders/modify", {"brokerOrderId": oid, "newPrice": 500.1, "modifySequence": 1})

    assert resp.status_code == 200, resp.text
    [kw] = _modifies(futu)
    assert (kw["qty"], kw["price"]) == (1, 500.1)  # not the stored quantity 2


def _reserved(sid):
    """What Gate 4 reserves for this row now: max(qty incl. observed_qty, pending_amend_qty)."""
    from xenon.execution import gate4

    o = gate4.order_from_row(_row(sid), filled_total=0, filled_after=0)
    return max(o.qty, o.pending_amend_qty or 0)


def test_futu_price_only_modify_reserves_the_outgoing_quantity_against_a_lowering_observation(futu):
    """Astra R2-F1: row qty 2, observed 10, price-only modify; an observation
    lowers observed_qty to 5 before the send. The send must carry a quantity
    that begin_modify reserved under the scope lock."""
    sid, oid = _placed(futu)
    futu.ctx.book[oid]["qty"] = 10.0  # raised in the Futu app
    fo.run_pass(SCOPE, futu.ctx, resolve_meta=resolver(), audit=False)
    assert _row(sid).legs["observed_qty"] == 10 and _reserved(sid) == 10
    at_send = {}
    send = futu.ctx.modify_order

    def lowered_then_send(**kw):
        futu.ctx.book[oid]["qty"] = 5.0  # cut in the app; observed before Xenon's send
        fo.run_pass(SCOPE, futu.ctx, resolve_meta=resolver(), audit=False)
        at_send.update(observed=_row(sid).legs["observed_qty"], reserved=_reserved(sid))
        return send(**kw)

    futu.ctx.modify_order = lowered_then_send
    futu.ctx.modify_mode = "ignore"  # unconfirmed: the reservation must outlive the reply

    resp = _post(futu, "/orders/modify", {"brokerOrderId": oid, "newPrice": 500.1, "modifySequence": 1})

    assert resp.status_code == 200 and resp.json()["confirmed"] is False, resp.text
    [kw] = _modifies(futu)
    assert at_send["observed"] == 5  # the observation did lower observed_qty
    assert kw["qty"] == 10 and at_send["reserved"] >= kw["qty"]  # the send stays reserved
    assert _row(sid).legs["pending_amend_qty"] == 10 and _reserved(sid) == 10


def test_futu_modify_sends_nothing_when_gate4_refuses_the_outgoing_quantity(futu, monkeypatch):
    from xenon.execution import gate4

    sid, oid = _placed(futu)
    futu.ctx.book[oid]["qty"] = 5.0
    seen = []

    def refuse(submission_id, new_qty, scope):
        seen.append(new_qty)
        return gate4.ModifyReservationOutcome(accept=False, reasons=["uncovered_short_call:SPY"])

    monkeypatch.setattr(gate4, "begin_modify", refuse)

    resp = _post(futu, "/orders/modify", {"brokerOrderId": oid, "newPrice": 500.1, "modifySequence": 1})

    assert resp.status_code == 400 and resp.json()["detail"]["amend_outcome"] == "not_sent", resp.text
    assert resp.json()["detail"]["applied_sequence"] == 1
    assert seen == [5]  # Gate 4 checked the full outgoing (broker) quantity, also price-only
    assert _modifies(futu) == []


def test_futu_quantity_only_modify_sends_the_broker_price(futu):
    sid, oid = _placed(futu)
    futu.ctx.book[oid]["price"] = 499.5  # changed in the Futu app (or an unconfirmed earlier amend)

    resp = _post(futu, "/orders/modify", {"brokerOrderId": oid, "newQuantity": 3, "modifySequence": 1})

    assert resp.status_code == 200 and resp.json()["confirmed"] is True, resp.text
    [kw] = _modifies(futu)
    assert (kw["qty"], kw["price"]) == (3, 499.5)  # not the stored 500.2
    assert float(_row(sid).limit_price) == 499.5


def test_futu_modify_sends_nothing_when_the_order_read_fails(futu):
    sid, oid = _placed(futu)
    futu.ctx.fail["open_orders"] = (-1, "read failed")

    resp = _post(futu, "/orders/modify", {"brokerOrderId": oid, "newPrice": 500.1, "modifySequence": 1})

    assert resp.status_code == 503 and resp.json()["reason_code"] == "FUTU_ORDER_UNVERIFIED"
    assert _modifies(futu) == []
    assert _row(sid).legs.get("pending_amend_id") is None


def test_futu_sub_dollar_one_tick_mismatch_stays_unconfirmed(futu):
    sid, oid = _placed(futu, cid="futu-subdollar")
    futu.ctx.book[oid]["price"] = 0.1  # a sub-dollar stock: tick 0.0001 (made-up price)
    futu.ctx.modify_mode = "ignore"  # RET_OK, Futu still reports 0.1000

    resp = _post(futu, "/orders/modify", {"brokerOrderId": oid, "newPrice": 0.1009, "modifySequence": 1})

    assert resp.status_code == 200 and resp.json()["confirmed"] is False, resp.text
    assert resp.json()["confirmedPrice"] is None


def _age_amend(sid):
    """Move the open amend's send_by and deadline into the past (the send
    window closed long ago)."""
    from datetime import timedelta

    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    legs = dict(_row(sid).legs)
    legs["pending_amend_send_by"] = legs["pending_amend_deadline"] = past
    with get_sync_engine().begin() as conn:
        conn.execute(update(order_submissions).where(order_submissions.c.submission_id == sid).values(legs=legs))


def test_futu_pending_amend_is_not_cleared_by_an_observation_pass(futu):
    """Astra R2-F2: a pass read after send_by does not prove the SDK can no
    longer dispatch the modify, so no pass confirms or expires the amend."""
    sid, oid = _placed(futu)
    futu.ctx.modify_mode = "ignore"
    resp = _post(futu, "/orders/modify", {"brokerOrderId": oid, "newQuantity": 5, "modifySequence": 1})
    assert resp.json()["confirmed"] is False
    _age_amend(sid)
    futu.ctx.book[oid]["qty"] = 5.0  # Futu shows the pending quantity

    fo.run_pass(SCOPE, futu.ctx, resolve_meta=resolver(), audit=False)

    row = _row(sid)
    assert row.quantity == 2 and row.legs["pending_amend_qty"] == 5 and row.legs["pending_amend_id"]
    blocked = _post(futu, "/orders/modify", {"brokerOrderId": oid, "newPrice": 500.1, "modifySequence": 2})
    assert blocked.status_code == 409 and blocked.json()["reason_code"] == "MODIFY_PENDING"
    assert _post(futu, "/orders/cancel", {"brokerOrderId": oid}).status_code == 200  # cancel still works


def test_futu_accepted_place_without_a_fresh_read_keeps_the_order_id(futu):
    futu.ctx.fail["open_orders"] = (-1, "read failed")

    resp = _post(futu, "/orders/place", _stock(cid="futu-unread"))

    assert resp.status_code == 502 and resp.json()["reason_code"] == "PLACE_OUTCOME_UNKNOWN"
    oid = next(iter(futu.ctx.book))
    assert resp.json()["brokerOrderId"] == oid
    row = _row(resp.json()["submission_id"])
    assert row.state == "UNKNOWN" and row.broker_order_id == oid
    del futu.ctx.fail["open_orders"]
    cancel = _post(futu, "/orders/cancel", {"brokerOrderId": oid})  # manageable before the remark bind
    assert cancel.status_code == 200 and cancel.json()["confirmed"] is True


def test_futu_cancel_of_a_filled_order_is_not_reported_cancelled(futu):
    sid, oid = _placed(futu)
    futu.ctx.cancel_mode = "filled"

    resp = _post(futu, "/orders/cancel", {"brokerOrderId": oid})

    body = resp.json()
    assert resp.status_code == 200 and body["state"] == "FILLED" and body["confirmed"] is False
    assert "FILLED" in body["message"] and "confirmed" not in body["message"]


def test_health_trading_false_without_a_running_worker(futu, monkeypatch):
    import asyncio

    from xenon.api import server

    asyncio.run(futu.worker.stop())
    assert _trading() == {"paper": False, "live": False}
    monkeypatch.setattr(server.app.state, "futu_order_worker", None, raising=False)
    assert _trading() == {"paper": False, "live": False}


# -- X5a task 1: allowed tuples through the routes (upgrade fixture, R3-1) ----


def _set_tif(sid, tif):
    with get_sync_engine().begin() as conn:
        conn.execute(update(order_submissions).where(order_submissions.c.submission_id == sid).values(tif=tif))


def test_upgrade_live_gtc_lmt_place_modify_cancel_unchanged(futu):
    """Unset tuple variables: a REAL install keeps GTC LMT (the v0.16 code surface)."""
    resp = _post(futu, "/orders/place", _stock(cid="futu-live-gtc", tif="GTC"), env="live")
    assert resp.status_code == 200, resp.text
    sid, oid = resp.json()["submission_id"], resp.json()["brokerOrderId"]
    [(_, place_kw)] = futu.ctx.mutations
    assert place_kw["time_in_force"] == "GTC" and place_kw["order_type"] == "NORMAL" and "session" not in place_kw
    assert _row(sid).tif == "GTC"

    mod = _post(futu, "/orders/modify", {"brokerOrderId": oid, "newPrice": 500.1, "modifySequence": 1}, env="live")
    assert mod.status_code == 200 and mod.json()["confirmed"] is True, mod.text
    can = _post(futu, "/orders/cancel", {"brokerOrderId": oid}, env="live")
    assert can.status_code == 200 and can.json()["confirmed"] is True
    assert [kw["modify_order_op"] for kw in _modifies(futu)] == ["NORMAL", "CANCEL"]


def test_upgrade_paper_gtc_place_is_refused_before_a_row_is_reserved(futu):
    resp = _post(futu, "/orders/place", _stock(cid="futu-paper-gtc", tif="GTC"))

    assert resp.status_code == 400 and resp.json()["reason_code"] == "FUTU_TIF_UNSUPPORTED"
    assert "GTC" in resp.json()["detail"]
    assert futu.ctx.mutations == [] and futu.ctx.calls == []  # no OpenD round trip at all
    with get_sync_engine().connect() as conn:
        assert conn.execute(order_submissions.select()).first() is None  # nothing reserved


def test_upgrade_pre_existing_paper_gtc_keeps_cancel_and_demand_modify_refused(futu):
    sid, oid = _placed(futu, cid="futu-paper-old-gtc")
    _set_tif(sid, "GTC")  # a paper GTC LMT placed before the upgrade
    before = _row(sid)

    mod = _post(futu, "/orders/modify", {"brokerOrderId": oid, "newPrice": 500.1, "modifySequence": 1})

    assert mod.status_code == 400 and mod.json()["reason_code"] == "FUTU_TIF_UNSUPPORTED", mod.text
    assert _modifies(futu) == []  # nothing sent
    after = _row(sid)
    assert (after.state, after.quantity, after.modify_sequence) == (
        before.state,
        before.quantity,
        before.modify_sequence,
    )
    assert after.legs.get("pending_amend_id") is None  # no amend opened: demand unchanged
    can = _post(futu, "/orders/cancel", {"brokerOrderId": oid})
    assert can.status_code == 200 and can.json()["confirmed"] is True
    assert _row(sid).state == "CANCELLED"


def test_a_tuple_outside_the_operator_allowlist_is_refused_before_io(futu, monkeypatch):
    monkeypatch.setenv("XENON_FUTU_TUPLES_LIVE", "STK:LMT:GTC:RTH")  # DAY withdrawn by the operator

    resp = _post(futu, "/orders/place", _stock(cid="futu-live-day"), env="live")

    assert resp.status_code == 400 and resp.json()["reason_code"] == "FUTU_TIF_UNSUPPORTED"
    assert futu.ctx.mutations == [] and futu.ctx.calls == []
