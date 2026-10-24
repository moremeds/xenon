"""X3c task 2b: SIMULATE / REAL switch, server side (plan §11 + A1-A3).

A1: one observation loop per exact scope; a hung paper pass never blocks
live, and its retired result cannot publish. A2: stored scopes with
unresolved rows stay observed after an account change (also after a
restart); the per-env FUTU_SCOPE_CHANGE_PENDING veto; no veto on a toggle.
A3: `account_env` in the query is the only wire name. Fakes only; no OpenD.
Account ids are made up.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from scripts.tests.test_futu_observation import (  # noqa: F401 — autouse fixture
    ACC,
    SCOPE,
    FakeTradeCtx,
    _env,
    _order,
    _pos,
    resolver,
)
from scripts.tests.test_futu_observation_loop import Connector
from scripts.tests.test_futu_observation_r1 import _store_display
from xenon.api import guards
from xenon.api.services import futu_observation as fo
from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_submissions
from xenon.execution.account_scope import AccountScope

pytestmark = pytest.mark.committed_db

LIVE_ACC = "281756479345099999"
NEW_ACC = "281756479345077777"
LIVE = AccountScope(broker="FUTU", account_env="live", broker_account=LIVE_ACC)
NEW_PAPER = AccountScope(broker="FUTU", account_env="paper", broker_account=NEW_ACC)


@pytest.fixture(autouse=True)
def _env_clean(monkeypatch):
    for name in ("XENON_READ_ONLY", "XENON_FUTU_TRADING", "FUTU_TRADE_ACC_ID_SIMULATE", "FUTU_TRADE_ACC_ID_REAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(guards, "_pending_logged", set())


def _ok_pass(scope, ctx, *, resolve_meta=None, may_publish=lambda: True):
    return {"published": may_publish(), "complete": True, "reads": {"account": True}, "scope": scope}


def _loops(run=_ok_pass, **kw):
    return fo.ObservationLoops(
        make_loop=lambda scope: fo.ObservationLoop(scope, connect=Connector(), run=run, **kw), interval_s=3600
    )


async def _until(pred, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


# -- A1: per-scope isolation ----------------------------------------------------


@pytest.mark.asyncio
async def test_a1_blocked_paper_pass_never_blocks_live_and_cannot_publish_late(monkeypatch):
    monkeypatch.setenv("FUTU_TRADE_ACC_ID_SIMULATE", ACC)
    monkeypatch.setenv("FUTU_TRADE_ACC_ID_REAL", LIVE_ACC)
    entered, release = threading.Event(), threading.Event()
    paper_may, live_runs = [], []

    def run(scope, ctx, *, resolve_meta=None, may_publish=lambda: True):
        if scope.account_env == "paper":
            entered.set()
            release.wait(5)  # a permanently hung SDK call until released
            paper_may.append(may_publish())
            return _ok_pass(scope, ctx, may_publish=may_publish)
        live_runs.append(may_publish())
        return _ok_pass(scope, ctx, may_publish=may_publish)

    loops = _loops(run=run, pass_timeout_s=0.1)
    await loops.reconcile()
    paper, live = loops.loop_for(SCOPE), loops.loop_for(LIVE)
    assert paper is not None and live is not None and paper is not live
    await asyncio.to_thread(entered.wait, 2)

    await _until(lambda: live.last_result is not None)
    assert (await asyncio.wait_for(live.refresh(), 1))["published"] is True  # live refresh while paper hangs
    assert live_runs == [True, True]  # first pass + refresh, both while paper hangs

    await asyncio.sleep(0.2)  # the paper pass times out and is retired
    release.set()
    await _until(lambda: paper_may)
    assert paper_may == [False]  # the retired result cannot publish
    await loops.stop()


# -- A2: stored scopes, veto, restart -------------------------------------------


def _observe_working(scope=SCOPE):
    ctx = FakeTradeCtx(
        positions=[_pos("US.SPY", 100.0)], open_orders=[_order("7701", code="US.SPY", side="BUY", qty=5.0)]
    )
    assert fo.run_pass(scope, ctx, resolve_meta=resolver())["complete"]


def _resolve_all():
    with get_sync_engine().begin() as conn:
        conn.execute(update(order_submissions).where(order_submissions.c.broker == "FUTU").values(state="CANCELLED"))


def _state():
    return SimpleNamespace(broker="FUTU", mode_verified=True, trading_mode="paper", account="DU1")


@pytest.mark.asyncio
async def test_a2_replaced_account_stays_observed_and_vetoes_only_its_env(monkeypatch, caplog):
    monkeypatch.setenv("FUTU_TRADE_ACC_ID_SIMULATE", ACC)
    _observe_working()  # an unresolved row of account A (paper)
    monkeypatch.setenv("FUTU_TRADE_ACC_ID_SIMULATE", NEW_ACC)  # A replaced by B; capability off
    monkeypatch.setenv("FUTU_TRADE_ACC_ID_REAL", LIVE_ACC)

    loops = _loops()
    await loops.reconcile()
    assert set(loops.loops) == {NEW_PAPER, LIVE, SCOPE}  # A is still reconciled, read-only

    monkeypatch.setenv("XENON_FUTU_TRADING", "simulate,real")
    with caplog.at_level(logging.ERROR):
        for _ in range(3):
            resp = guards.ensure_order_allowed("order placement", _state(), account_env="paper")
            assert resp.status_code == 403 and resp.body and b"FUTU_SCOPE_CHANGE_PENDING" in resp.body
    assert len([r for r in caplog.records if "FUTU_SCOPE_CHANGE_PENDING" in r.getMessage()]) == 1
    assert guards.ensure_order_allowed("order placement", _state(), account_env="live") is None  # per env

    _resolve_all()  # the old rows are resolved
    await loops.reconcile()
    assert set(loops.loops) == {NEW_PAPER, LIVE}
    assert guards.ensure_order_allowed("order placement", _state(), account_env="paper") is None
    await loops.stop()


@pytest.mark.asyncio
async def test_a2_removed_account_is_found_again_after_a_restart(monkeypatch):
    monkeypatch.setenv("FUTU_TRADE_ACC_ID_SIMULATE", ACC)
    _observe_working()
    monkeypatch.delenv("FUTU_TRADE_ACC_ID_SIMULATE")  # SIMULATE account removed, trading off

    first = _loops()
    await first.reconcile()
    assert set(first.loops) == {SCOPE}
    await first.stop()

    restarted = _loops()  # a new process: discovery from Postgres only
    await restarted.reconcile()
    assert set(restarted.loops) == {SCOPE}
    _resolve_all()
    await restarted.reconcile()
    assert restarted.loops == {}
    await restarted.stop()


def test_a2_plain_toggle_between_configured_scopes_never_vetoes(monkeypatch):
    monkeypatch.setenv("FUTU_TRADE_ACC_ID_SIMULATE", ACC)
    monkeypatch.setenv("FUTU_TRADE_ACC_ID_REAL", LIVE_ACC)
    monkeypatch.setenv("XENON_FUTU_TRADING", "simulate,real")
    _observe_working()  # unresolved paper row of the configured paper account

    for env in ("paper", "live", "paper"):
        assert guards.ensure_order_allowed("order placement", _state(), account_env=env) is None  # no veto
    assert guards.futu_scope_change_pending("paper") == [] and guards.futu_scope_change_pending("live") == []


# -- A3: account_env on the query is the only wire name ---------------------------


@pytest.fixture
def both_envs(monkeypatch):
    from xenon.api import server
    from xenon.api.routes import futu as futu_routes

    monkeypatch.setenv("FUTU_TRADE_ACC_ID_SIMULATE", ACC)
    monkeypatch.setenv("FUTU_TRADE_ACC_ID_REAL", LIVE_ACC)
    offline = type("Offline", (), {"is_connected": lambda self: False})()
    monkeypatch.setattr(futu_routes, "_get_futu_client", lambda *_a: offline)
    monkeypatch.setattr(server, "_is_test_mode", lambda: False)
    return TestClient(server.app)


def test_a3_portfolio_and_orders_answer_per_env(both_envs):
    _store_display(SCOPE, 1)
    _store_display(LIVE, 2)
    _observe_working()

    paper = both_envs.get("/futu/portfolio?account_env=paper").json()
    live = both_envs.get("/futu/portfolio?account_env=live").json()
    assert (paper["count"], paper["scope"]["accountEnv"]) == (1, "paper")
    assert (live["count"], live["scope"]["accountEnv"]) == (2, "live")
    assert both_envs.get("/futu/portfolio").json()["count"] == 1  # missing on a read → paper (SIMULATE configured)

    po = both_envs.get("/orders", params={"broker": "FUTU", "account_env": "paper"}).json()
    lo = both_envs.get("/orders", params={"broker": "FUTU", "account_env": "live"}).json()
    assert [r["brokerOrderId"] for r in po["open_orders"]] == ["7701"] and po["scope"]["accountEnv"] == "paper"
    assert lo["open_orders"] == [] and lo["scope"]["accountEnv"] == "live"


def test_a3_refresh_runs_only_the_request_scope_loop(both_envs, monkeypatch):
    from xenon.api import server

    refreshed = []

    def loop(scope):
        async def refresh():
            refreshed.append(scope.account_env)

        return SimpleNamespace(scope=scope, refresh=refresh)

    by_scope = {SCOPE: loop(SCOPE), LIVE: loop(LIVE)}
    monkeypatch.setattr(
        server.app.state, "futu_observation_loops", SimpleNamespace(loop_for=by_scope.get), raising=False
    )

    for env in ("live", "paper"):
        body = both_envs.post("/orders/refresh", params={"broker": "FUTU", "account_env": env}).json()
        assert body["scope"]["accountEnv"] == env
    assert refreshed == ["live", "paper"]


def test_a3_sync_uses_the_request_env_client(both_envs, monkeypatch):
    from scripts.tests.test_futu_observation_r4 import _Client
    from xenon.api.routes import futu as futu_routes

    monkeypatch.setattr(futu_routes, "_futu_last_sync", {})
    monkeypatch.setattr(futu_routes, "_futu_clients", {})
    monkeypatch.setattr(futu_routes, "_futu_sync_future", None)
    monkeypatch.setattr(futu_routes, "_futu_lock", None)
    monkeypatch.setattr(futu_routes, "_persist_display_snapshot", lambda client, result: True)
    from xenon.api import server

    monkeypatch.delattr(server.app.state, "db_engine", raising=False)
    asked = []
    monkeypatch.setattr(
        futu_routes,
        "_get_futu_client",
        lambda env=None: asked.append(env) or _Client(SCOPE if env == "paper" else LIVE),
    )

    assert both_envs.post("/futu/sync?account_env=live").json()["ok"] is True
    assert both_envs.post("/futu/sync?account_env=paper").json()["ok"] is True
    assert asked == ["live", "paper"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/orders?broker=FUTU"),
        ("post", "/orders/refresh?broker=FUTU"),
        ("post", "/futu/sync"),
        ("get", "/futu/portfolio"),
        ("get", "/blotter?broker=FUTU"),
        ("get", "/performance?broker=FUTU"),
    ],
)
@pytest.mark.parametrize("bad", ["PAPER", "real", "simulate", "x"])
def test_a3_invalid_account_env_on_a_read_is_400(both_envs, method, path, bad):
    sep = "&" if "?" in path else "?"
    resp = getattr(both_envs, method)(f"{path}{sep}account_env={bad}")
    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "FUTU_SCOPE_INVALID"


def test_a3_env_without_an_account_reads_empty(both_envs, monkeypatch):
    monkeypatch.delenv("FUTU_TRADE_ACC_ID_REAL")
    _observe_working()
    body = both_envs.get("/orders", params={"broker": "FUTU", "account_env": "live"}).json()
    assert body["open_orders"] == [] and body["scope_reason"] == "not_configured" and body["scope_env"] == "live"
    assert both_envs.get("/futu/portfolio?account_env=live").json()["code"] == "not_configured"


# -- worker: one worker, both envs ------------------------------------------------


@pytest.mark.asyncio
async def test_one_worker_serves_both_envs_with_the_request_scope():
    from xenon.api import futu_order_worker as fw

    rows = pd.DataFrame(
        [
            {"acc_id": int(ACC), "trd_env": "SIMULATE", "trdmarket_auth": ["US"], "acc_status": "ACTIVE"},
            {"acc_id": int(LIVE_ACC), "trd_env": "REAL", "trdmarket_auth": ["US"], "acc_status": "ACTIVE"},
        ]
    )
    ctx = SimpleNamespace(get_acc_list=lambda: (0, rows))
    opened = []
    w = fw.FutuOrderWorker(connect=lambda scope: opened.append(scope) or (ctx, lambda: None))
    seen = []

    for scope in (SCOPE, LIVE, NEW_PAPER):
        seen.append(await w.run(lambda c, mark: {"status": "ok", "ctx": c is ctx}, scope=scope, timeout=2))

    assert seen[0] == seen[1] == {"status": "ok", "ctx": True} and opened == [SCOPE]  # one context
    assert seen[2]["reason_code"] == "FUTU_ACCOUNT_ABSENT"  # verified per job scope
    await w.stop()


def test_display_client_per_env_selects_that_account(monkeypatch):
    from xenon.api.routes import futu as futu_routes

    monkeypatch.setenv("FUTU_TRADE_ACC_ID_SIMULATE", ACC)
    monkeypatch.setenv("FUTU_TRADE_ACC_ID_REAL", LIVE_ACC)
    monkeypatch.setattr(futu_routes, "_futu_clients", {})
    monkeypatch.setattr(futu_routes, "_futu_client", None)

    paper, live = futu_routes._get_futu_client("paper"), futu_routes._get_futu_client("live")

    assert (paper.trd_env, paper.requested_acc_id) == ("SIMULATE", int(ACC))
    assert (live.trd_env, live.requested_acc_id) == ("REAL", int(LIVE_ACC))
    assert futu_routes._get_futu_client("live") is live and futu_routes.display_clients() == [paper, live]
