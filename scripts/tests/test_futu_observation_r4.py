"""PR #236 review round 3 (Astra R2-F1..F3 + Fable R2-1..R2-4) regressions:
one Futu scope for display sync, scoped execution ids, exact option
identity, held-row health and the bounded FUTU refresh.

Real Postgres (`committed_db`), fake Futu clients / trade contexts. All ids,
quantities and prices are made-up test values, not market data.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from sqlalchemy import func, insert, select

from scripts.tests.test_futu_observation import (  # noqa: F401 — autouse fixture
    ACC,
    SCOPE,
    SPY_META,
    FakeTradeCtx,
    _env,
    _fills,
    _order,
    _pos,
    _subs,
    resolver,
)
from scripts.tests.test_futu_observation_r1 import _refresh
from scripts.tests.test_futu_observation_r2 import ByIdCtx
from xenon.api.services import futu_observation as fo
from xenon.db.engine import get_sync_engine
from xenon.db.schema import account_snapshots, order_fills
from xenon.execution import orders_store
from xenon.execution.account_scope import AccountScope

pytestmark = pytest.mark.committed_db

OTHER_LIVE = AccountScope(broker="FUTU", account_env="live", broker_account="111000111")
SAME_ACC_LIVE = AccountScope(broker="FUTU", account_env="live", broker_account=ACC)
EXP = SPY_META["exp"]


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("FUTU_TRADE_ACC_ID_SIMULATE", ACC)
    monkeypatch.setattr(fo, "_IB_META_CACHE", {}, raising=False)
    monkeypatch.setattr(fo, "_FULL_PASSES", {}, raising=False)


def _run(ctx, metas=(SPY_META,)):
    return fo.run_pass(SCOPE, ctx, resolve_meta=resolver(metas))


# ---------------------------------------------------------------------------
# 1. Astra R2-F1 / Fable R2-2 — one Futu scope for display sync
# ---------------------------------------------------------------------------


class _Client:
    """Connected display-client double matched to `scope`."""

    def __init__(self, scope: AccountScope, result: dict | None = None):
        self._acc_id = int(scope.broker_account)
        self._env = "SIMULATE" if scope.account_env == "paper" else "REAL"
        self.result = result or {"count": 5, "positions": [], "warnings": [], "fetched_at": "fresh"}

    def is_connected(self):
        return True

    def connect(self):
        return None

    def trd_env_of_matched_account(self):
        return self._env

    def fetch_portfolio(self, force=False):
        return dict(self.result)


_REQ = type("Req", (), {"query_params": {}})()  # default read env: paper (SIMULATE configured)


@pytest.fixture()
def _sync_state(monkeypatch):
    from xenon.api import server
    from xenon.api.routes import futu as futu_routes

    monkeypatch.setattr(futu_routes, "_futu_last_sync", {})
    monkeypatch.setattr(futu_routes, "_futu_client", None)
    monkeypatch.setattr(futu_routes, "_futu_clients", {})
    monkeypatch.setattr(futu_routes, "_futu_lock", None)
    monkeypatch.setattr(futu_routes, "_futu_sync_future", None)
    monkeypatch.delattr(server.app.state, "db_engine", raising=False)
    return futu_routes


def _store_display(scope: AccountScope, count: int):
    with get_sync_engine().begin() as conn:
        conn.execute(
            insert(account_snapshots).values(
                account=scope.broker_account,
                bankroll=Decimal(1),
                payload={"count": count, "positions": [], "warnings": [], "fetched_at": f"t{count}"},
                broker="FUTU",
                account_env=scope.account_env,
                broker_account=scope.broker_account,
                snapshot_at=func.clock_timestamp(),
            )
        )


def _display_counts(scope: AccountScope) -> list:
    with get_sync_engine().connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                select(account_snapshots.c.payload["count"]).where(
                    account_snapshots.c.broker == "FUTU",
                    account_snapshots.c.account_env == scope.account_env,
                    account_snapshots.c.broker_account == scope.broker_account,
                    ~account_snapshots.c.payload.has_key("observation"),
                )
            )
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("client_scope", [OTHER_LIVE, SAME_ACC_LIVE], ids=["other_account", "paper_vs_live"])
async def test_cooldown_sync_never_serves_another_scope(_sync_state, monkeypatch, client_scope):
    futu_routes = _sync_state
    _store_display(SCOPE, 1)
    _store_display(client_scope, 2)
    monkeypatch.setattr(futu_routes, "_futu_clients", {"paper": _Client(client_scope)})
    monkeypatch.setattr(futu_routes, "_futu_last_sync", {"paper": time.monotonic()})

    body = await futu_routes.futu_sync(_REQ)

    assert body["ok"] is False and body["code"] == "scope_mismatch" and body["count"] == 0
    assert body["scope"] == {"broker": "FUTU", "accountEnv": "paper", "brokerAccount": ACC}


@pytest.mark.asyncio
@pytest.mark.parametrize("client_scope", [OTHER_LIVE, SAME_ACC_LIVE], ids=["other_account", "paper_vs_live"])
async def test_fresh_sync_of_another_scope_is_neither_served_nor_stored(_sync_state, monkeypatch, client_scope):
    futu_routes = _sync_state
    monkeypatch.setattr(futu_routes, "_get_futu_client", lambda *_a: _Client(client_scope))

    body = await futu_routes.futu_sync(_REQ)

    assert body["code"] == "scope_mismatch" and body["count"] == 0
    assert _display_counts(client_scope) == [] and _display_counts(SCOPE) == []


@pytest.mark.asyncio
async def test_fresh_sync_of_the_selected_scope_is_served_and_stored(_sync_state, monkeypatch):
    futu_routes = _sync_state
    monkeypatch.setattr(futu_routes, "_get_futu_client", lambda *_a: _Client(SCOPE))

    body = await futu_routes.futu_sync(_REQ)

    assert body["ok"] is True and body["count"] == 5
    assert _display_counts(SCOPE) == [5]


def test_display_client_uses_the_configured_scope(_sync_state, monkeypatch):
    futu_routes = _sync_state
    monkeypatch.setenv("FUTU_TRD_ENV", "REAL")  # ignored while a scope is configured

    client = futu_routes._get_futu_client()

    assert (client.trd_env, client.requested_acc_id) == ("SIMULATE", int(ACC))


def test_display_client_without_scope_keeps_futu_trd_env(_sync_state, monkeypatch):
    futu_routes = _sync_state
    monkeypatch.delenv("FUTU_TRADE_ACC_ID_SIMULATE")
    monkeypatch.setenv("FUTU_TRD_ENV", "REAL")

    client = futu_routes._get_futu_client()

    assert (client.trd_env, client.requested_acc_id) == ("REAL", None)


def _acc_frame(rows):
    from futu import TrdEnv

    env = {"REAL": TrdEnv.REAL, "SIMULATE": TrdEnv.SIMULATE}
    return pd.DataFrame([{"acc_id": a, "trd_env": env[e]} for a, e in rows])


@pytest.mark.parametrize("wanted,expect", [(200, 200), (300, None)])
def test_client_selects_exactly_the_requested_account(wanted, expect):
    from futu import RET_OK

    from xenon.clients.futu_client import FutuClient
    from xenon.clients.futu_exceptions import FutuConnectionError

    with (
        patch("xenon.clients.futu_client._probe_opend"),
        patch("xenon.clients.futu_client.OpenSecTradeContext") as ctx_cls,
    ):
        ctx = MagicMock()
        ctx_cls.return_value = ctx
        ctx.get_acc_list.return_value = (RET_OK, _acc_frame([(100, "SIMULATE"), (200, "SIMULATE"), (300, "REAL")]))
        client = FutuClient(trd_env="SIMULATE", acc_id=wanted)
        if expect is None:
            with pytest.raises(FutuConnectionError):
                client.connect()  # 300 is REAL: no fallback to another account or env
        else:
            client.connect()
            assert client._acc_id == expect and client.trd_env_of_matched_account() == "SIMULATE"


# ---------------------------------------------------------------------------
# 2. Astra R2-F2 — execution ids carry the scope; conflicts never advance
# ---------------------------------------------------------------------------


def _obs(order_id, *, code=None, metas=(SPY_META,), **order_kw):
    cache = fo._MetaCache(resolver(metas))
    row = _order(order_id, code=code or f"US.SPY{EXP[2:]}C500000", **order_kw)
    cache.prefetch(fo._option_specs([row]))
    return fo._order_observation(row, SCOPE, cache, datetime.now(timezone.utc))


def _mirror(obs, env):
    return orders_store.mirror_futu_order(
        obs, broker_account=ACC, account_env=env, observed_at=datetime.now(timezone.utc)
    )


def test_same_order_fill_in_two_scopes_gets_one_execution_each_and_replays():
    filled = _obs("7801", status="FILLED_ALL", dealt=1.0, avg=1.20)

    paper, live = _mirror(filled, "paper"), _mirror(filled, "live")
    assert (paper["fill"], live["fill"]) == (f"futu:paper:{ACC}:ord:7801:1", f"futu:live:{ACC}:ord:7801:1")
    assert _mirror(filled, "paper")["fill"] is None and _mirror(filled, "live")["fill"] is None  # replay

    by_scope = sorted((f.account_env, f.submission_id, f.qty) for f in _fills())
    assert by_scope == [
        ("live", f"futu-live-{ACC}-7801", 1),
        ("paper", f"futu-paper-{ACC}-7801", 1),
    ]


def test_exec_id_owned_by_another_submission_does_not_advance():
    _mirror(_obs("7802"), "paper")  # an unrelated submission
    other_sid = f"futu-paper-{ACC}-7802"
    with get_sync_engine().begin() as conn:
        conn.execute(
            insert(order_fills).values(
                exec_id=orders_store.futu_exec_id("paper", ACC, "7803", 1),
                submission_id=other_sid,
                ticker="SPY",
                side="SELL",
                qty=Decimal(1),
                price=Decimal("1.0000"),
                filled_at=datetime.now(timezone.utc),
                broker="FUTU",
                account_env="paper",
                broker_account=ACC,
            )
        )

    out = _mirror(_obs("7803", status="FILLED_ALL", dealt=1.0, avg=1.20), "paper")

    row = _subs()["7803"]
    assert out["fill_skipped"] == "exec_id_conflict" and out["fill"] is None
    assert (row.state, row.filled_qty, row.reason_code) == ("UNKNOWN", 0, orders_store.FUTU_FILL_CONFLICT)


# ---------------------------------------------------------------------------
# 3. Astra R2-F3 — exact option identity is the Futu code
# ---------------------------------------------------------------------------


def test_distinct_strike_in_the_same_rounding_interval_is_a_mismatch():
    meta_125 = {**SPY_META, "con_id": 333, "strike": 500.125}
    code_125, code_130 = f"US.SPY{EXP[2:]}C500125", f"US.SPY{EXP[2:]}C500130"
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order("7804", code=code_125)])
    assert _run(ctx, metas=(meta_125,))["complete"] is True
    assert _subs()["7804"].legs["futu_code"] == code_125

    ctx.open_orders = [_order("7804", code=code_130, status="FILLED_ALL", dealt=1.0, avg=1.0)]
    result = _run(ctx, metas=())  # incoming metadata unresolved

    row = _subs()["7804"]
    assert result["complete"] is False and result["reads"]["persisted"] is False
    assert (row.state, row.filled_qty, row.legs["futu_code"], row.con_id) == ("WORKING", 0, code_125, 333)
    assert _fills() == []


def test_row_without_code_needs_an_exactly_equal_strike():
    from types import SimpleNamespace

    obs = _obs("7805", code=f"US.SPY{EXP[2:]}C500125", metas=())
    row = SimpleNamespace(
        ticker="SPY",
        security_type="OPT",
        action="SELL",
        right="C",
        expiry=datetime.strptime(EXP, "%Y%m%d").date(),
        strike=Decimal("500.13"),
        con_id=None,
        legs={"legs": None},
    )

    assert orders_store._futu_identity_mismatch(row, obs) == "strike"  # not established → fail closed
    row.strike = Decimal("500.00")
    assert orders_store._futu_identity_mismatch(row, _obs("7805", metas=())) is None


# ---------------------------------------------------------------------------
# 4 / 5. Fable R2-1 / R2-3 — held rows on /health; slow recheck of UNKNOWN
# ---------------------------------------------------------------------------


def _held():
    from xenon.api.routes.system import _compute_futu_health

    return _compute_futu_health()["held_rows"]


def test_health_reports_unpriced_rows():
    _run(FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], history=[_order("7901", status="FILLED_ALL", dealt=1.0)]))

    held = _held()
    assert held[orders_store.FUTU_FILL_UNPRICED] == {"count": 1, "order_ids": ["7901"]}
    assert held[orders_store.FUTU_NOT_OBSERVED] == {"count": 0, "order_ids": []}


def test_not_observed_row_is_rechecked_every_tenth_full_pass_and_resolves():
    ctx = ByIdCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order("7902")])
    _run(ctx)  # full pass 0
    ctx.open_orders = []
    _run(ctx)  # 1: miss 1
    _run(ctx)  # 2: miss 2 → UNKNOWN
    assert _subs()["7902"].reason_code == orders_store.FUTU_NOT_OBSERVED
    assert _held()[orders_store.FUTU_NOT_OBSERVED] == {"count": 1, "order_ids": ["7902"]}

    def lookups():
        return sum(1 for name, kw in ctx.calls if name == "order_by_id" and kw["order_id"] == "7902")

    before = lookups()
    for _ in range(3, 10):
        _run(ctx)
    assert lookups() == before  # passes 3-9: not looked up

    ctx.by_id["7902"] = [_order("7902", status="FILLED_ALL", dealt=1.0, avg=1.10)]
    assert _run(ctx)["complete"] is True  # pass 10: rechecked
    assert lookups() == before + 1
    row = _subs()["7902"]
    assert (row.state, row.filled_qty, row.reason_code) == ("FILLED", 1, None)


# ---------------------------------------------------------------------------
# 6. Fable R2-4 — bounded FUTU refresh wait
# ---------------------------------------------------------------------------


def test_refresh_wait_is_bounded(monkeypatch):
    from xenon.api.routes import portfolio as portfolio_routes

    class _SlowLoop:
        scope = SCOPE
        finished = False

        async def refresh(self):
            await asyncio.sleep(0.5)
            _SlowLoop.finished = True

    monkeypatch.setattr(portfolio_routes, "FUTU_REFRESH_WAIT_S", 0.05, raising=False)
    t0 = time.monotonic()
    resp = _refresh(monkeypatch, SCOPE, _SlowLoop())

    assert resp.status_code == 200, resp.text
    assert resp.json()["refresh"] == "in_progress"
    assert time.monotonic() - t0 < 0.45
