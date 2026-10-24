"""X3c task 2: Futu order worker (contract item 6) and its lifespan wiring.
Fake trade context only: no OpenD, no IB, no network."""

from __future__ import annotations

import asyncio
import threading

import pandas as pd
import pytest

from scripts.tests.order_owner_fakes import FakePool
from xenon.api import futu_order_worker as fw
from xenon.api.order_owner import OrderOwner
from xenon.execution.account_scope import AccountScope

pytestmark = pytest.mark.asyncio

ACC = "281756479345015383"
SCOPE = AccountScope(broker="FUTU", account_env="paper", broker_account=ACC)
ACC_ROW = {"acc_id": int(ACC), "trd_env": "SIMULATE", "trdmarket_auth": ["US"], "acc_status": "ACTIVE"}


class Ctx:
    def __init__(self, acc_reply=None):
        self.acc_reply = acc_reply or (0, pd.DataFrame([ACC_ROW]))
        self.acc_checks = 0

    def get_acc_list(self):
        self.acc_checks += 1
        if isinstance(self.acc_reply, BaseException):
            raise self.acc_reply
        return self.acc_reply


class Connector:
    def __init__(self, ctx=None, error=None):
        self.ctx = ctx or Ctx()
        self.error = error
        self.connects = 0
        self.closes = 0
        self.scopes = []

    def __call__(self, scope):
        self.scopes.append(scope)
        self.connects += 1
        if self.error is not None:
            raise self.error
        return self.ctx, self._close

    def _close(self):
        self.closes += 1


def _worker(connector=None, **kw):
    connector = connector or Connector()
    return fw.FutuOrderWorker(connect=connector, **kw), connector


class Core:
    """A core that records calls; `mark` sets the possible-send mark first;
    `hold` blocks it (a hung Futu call) until released."""

    def __init__(self, *, mark=True, reply=None, error=None, hold=False):
        self.mark, self.reply, self.error = mark, reply or {"status": "ok"}, error
        self.calls = []
        self.entered, self.release = threading.Event(), threading.Event()
        if not hold:
            self.release.set()

    def __call__(self, ctx, mark_sent):
        self.calls.append(ctx)
        if self.mark:
            mark_sent()
        self.entered.set()
        self.release.wait(5)
        if self.error is not None:
            raise self.error
        return self.reply


async def _entered(core):
    assert await asyncio.to_thread(core.entered.wait, 2)


async def _until(pred, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


# -- lifecycle -----------------------------------------------------------------


async def test_core_runs_on_the_worker_ctx_after_a_fresh_account_check():
    w, conn = _worker()
    core, settled = Core(), []
    reply = await w.run(core, timeout=2, settle=settled.append, scope=SCOPE)
    assert reply == {"status": "ok"} and settled == [reply]
    assert core.calls == [conn.ctx] and conn.ctx.acc_checks == 1
    await w.stop()


async def test_queue_timeout_never_sends_and_settles_once():
    w, _ = _worker()
    blocker, queued, settled = Core(hold=True), Core(), []
    first = asyncio.create_task(w.run(blocker, timeout=5, scope=SCOPE))
    await _entered(blocker)

    reply = await w.run(queued, timeout=0.05, settle=settled.append, scope=SCOPE)

    assert reply["amend_outcome"] == "not_sent" and settled == [reply]
    blocker.release.set()
    await first
    await asyncio.sleep(0.05)
    assert queued.calls == [] and len(settled) == 1
    await w.stop()


@pytest.mark.parametrize(
    "core,outcome",
    [
        (Core(error=RuntimeError("socket gone")), "unconfirmed"),
        (Core(error=TimeoutError()), "unconfirmed"),
        (Core(reply={"status": "error", "message": "no answer"}), "unconfirmed"),
        (Core(mark=False, error=RuntimeError("bad input")), "not_sent"),
    ],
)
async def test_after_the_mark_a_failure_is_unconfirmed_never_not_sent(core, outcome):
    w, conn = _worker()
    settled = []
    reply = await w.run(core, timeout=2, settle=settled.append, scope=SCOPE)
    assert reply["amend_outcome"] == outcome and settled == [reply]
    if core.error is not None:
        assert conn.closes == 1  # the ctx is retired; the next job reopens it
        assert (await w.run(Core(), timeout=2, scope=SCOPE))["status"] == "ok" and conn.connects == 2
    await w.stop()


async def test_late_ack_after_caller_timeout_settles_on_the_worker_once():
    w, _ = _worker()
    core, settled = Core(hold=True, reply={"status": "ok", "order_id": "8801"}), []

    reply = await w.run(core, timeout=0.05, settle=settled.append, scope=SCOPE)

    assert reply["amend_outcome"] == "unconfirmed" and settled == []  # started: the worker owns it
    core.release.set()
    await _until(lambda: settled)
    assert settled == [{"status": "ok", "order_id": "8801"}]
    await w.stop()


async def test_caller_cancel_while_queued_never_sends():
    w, _ = _worker()
    blocker, queued, settled = Core(hold=True), Core(), []
    first = asyncio.create_task(w.run(blocker, timeout=5, scope=SCOPE))
    await _entered(blocker)
    task = asyncio.create_task(w.run(queued, timeout=5, settle=settled.append, scope=SCOPE))
    await asyncio.sleep(0.02)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [s["amend_outcome"] for s in settled] == ["not_sent"]
    blocker.release.set()
    await first
    await asyncio.sleep(0.05)
    assert queued.calls == [] and len(settled) == 1
    await w.stop()


async def test_caller_cancel_after_start_keeps_the_job_owned():
    w, _ = _worker()
    core, settled = Core(hold=True), []
    task = asyncio.create_task(w.run(core, timeout=5, settle=settled.append, scope=SCOPE))
    await _entered(core)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert settled == []

    core.release.set()
    await _until(lambda: settled)
    assert settled == [{"status": "ok"}]
    await w.stop()


async def test_shutdown_abandons_queued_work_and_closes_the_ctx_after_the_running_job():
    w, conn = _worker()
    running, queued, settled = Core(hold=True), Core(), []
    first = asyncio.create_task(w.run(running, timeout=5, settle=settled.append, scope=SCOPE))
    await _entered(running)
    second = asyncio.create_task(w.run(queued, timeout=5, settle=settled.append, scope=SCOPE))
    await asyncio.sleep(0.02)

    await w.stop(wait_s=0.05)  # the running job is hung: stop does not wait for it

    assert (await w.run(Core(), timeout=1, scope=SCOPE))["reason_code"] == fw.WORKER_STOPPED
    assert conn.closes == 0
    running.release.set()
    assert (await first)["status"] == "ok"
    assert (await second)["amend_outcome"] == "not_sent"
    await _until(lambda: conn.closes == 1)
    assert queued.calls == []
    assert sorted(s.get("amend_outcome", "ok") for s in settled) == ["not_sent", "ok"]


async def test_hung_futu_call_does_not_block_an_ib_order():
    w, _ = _worker()
    hung = Core(hold=True)
    futu = asyncio.create_task(w.run(hung, timeout=5, scope=SCOPE))
    await _entered(hung)

    pool = FakePool()
    owner = OrderOwner(pool, port_listening=lambda: True)

    def ib_core(client):
        client.place_order("contract", "order")
        return {"status": "ok"}

    reply = await asyncio.wait_for(owner.run(ib_core, timeout=2), 2)

    assert reply == {"status": "ok"} and pool.client.sent == [("place", ("contract", "order"))]
    assert not futu.done()
    hung.release.set()
    await futu
    await w.stop()
    await owner.stop()


async def test_busy_worker_refuses_at_admission():
    w, _ = _worker(max_live=1)
    hung = Core(hold=True)
    first = asyncio.create_task(w.run(hung, timeout=5, scope=SCOPE))
    await _entered(hung)
    extra, settled = Core(), []

    reply = await w.run(extra, timeout=1, settle=settled.append, scope=SCOPE)

    assert reply["reason_code"] == fw.WORKER_BUSY and reply["amend_outcome"] == "not_sent" and settled == [reply]
    hung.release.set()
    await first
    assert extra.calls == []
    await w.stop()


# -- readiness -----------------------------------------------------------------


@pytest.mark.parametrize(
    "connector,code,closes",
    [
        (Connector(error=ConnectionError("OpenD down")), fw.OPEND_UNREACHABLE, 0),
        (Connector(ctx=Ctx(acc_reply=(-1, "Timeout"))), fw.NOT_READY, 1),
        (Connector(ctx=Ctx(acc_reply=RuntimeError("closed"))), fw.NOT_READY, 1),
        (Connector(ctx=Ctx(acc_reply=(0, pd.DataFrame([{**ACC_ROW, "acc_id": 1}])))), "FUTU_ACCOUNT_ABSENT", 0),
        (
            Connector(ctx=Ctx(acc_reply=(0, pd.DataFrame([{**ACC_ROW, "trd_env": "REAL"}])))),
            "FUTU_ACCOUNT_ENV_MISMATCH",
            0,
        ),
    ],
)
async def test_not_ready_refuses_before_the_core(connector, code, closes):
    w, _ = _worker(connector)
    core, settled = Core(), []
    reply = await w.run(core, timeout=2, settle=settled.append, scope=SCOPE)
    assert reply["reason_code"] == code and reply["amend_outcome"] == "not_sent" and settled == [reply]
    assert core.calls == [] and connector.closes == closes
    await w.stop()


# -- start gating + lifespan -----------------------------------------------------


@pytest.fixture
def futu_env(monkeypatch):
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)
    monkeypatch.setenv("XENON_FUTU_TRADING", "simulate")
    monkeypatch.setenv("FUTU_TRADE_ACC_ID_SIMULATE", ACC)
    return monkeypatch


@pytest.mark.parametrize(
    "change",
    [
        ("setenv", "XENON_READ_ONLY", "1"),
        ("delenv", "XENON_FUTU_TRADING"),
        ("setenv", "XENON_FUTU_TRADING", "1"),  # old value = off
        ("setenv", "XENON_FUTU_TRADING", "real"),  # REAL enabled but not configured
        ("delenv", "FUTU_TRADE_ACC_ID_SIMULATE"),
    ],
)

async def test_start_is_skipped_without_capability_or_under_read_only(futu_env, change):
    made = []
    getattr(futu_env, change[0])(*change[1:])
    assert fw.start_futu_order_worker(connector=made.append) is None
    assert made == []


async def test_start_builds_a_lazy_worker_for_the_configured_scope(futu_env):
    conn = Connector()
    w = fw.start_futu_order_worker(connector=conn)
    assert isinstance(w, fw.FutuOrderWorker) and conn.connects == 0
    assert (await w.run(Core(), scope=SCOPE, timeout=2))["status"] == "ok" and conn.scopes == [SCOPE]
    await w.stop()


from scripts.tests.test_futu_observation_loop import boot  # noqa: E402,F401 — lifespan fixture


@pytest.fixture
def lifespan_worker(boot, monkeypatch):  # noqa: F811 — pytest fixture injection
    from xenon.api import server as server_mod

    monkeypatch.setattr(server_mod, "_maybe_start_futu_observation_loop", lambda app: None)
    monkeypatch.setattr(server_mod.app.state, "futu_observation_loops", None, raising=False)
    made = []
    real = fw.start_futu_order_worker

    def start(**kw):
        w = real(connector=Connector(), **kw)
        made.append(w)
        return w

    monkeypatch.setattr(fw, "start_futu_order_worker", start)

    async def run():
        await boot()
        return made

    return run


async def test_lifespan_starts_and_stops_the_futu_order_worker(futu_env, lifespan_worker):
    (w,) = await lifespan_worker()
    assert isinstance(w, fw.FutuOrderWorker)
    assert w.admission()["reason_code"] == fw.WORKER_STOPPED  # stopped by the lifespan shutdown


async def test_lifespan_skips_the_futu_order_worker_under_read_only(futu_env, lifespan_worker):
    futu_env.setenv("XENON_READ_ONLY", "1")
    assert await lifespan_worker() == [None]
