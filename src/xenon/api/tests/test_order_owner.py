"""PR-10 T3 — single order owner: invocation lifecycle, pre-send readiness /
halt re-check, orders supervisor + latched halt. Fakes only; no IB, no DB."""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from scripts.tests.order_owner_fakes import FakePool
from xenon.api.order_owner import BROKER_DISCONNECTED, ORDERS_HALTED, OrderOwner
from xenon.clients.ib_client import ClientIdInUse

pytestmark = pytest.mark.asyncio


def _owner(pool=None, *, port_listening=True, **kw) -> tuple[OrderOwner, FakePool]:
    pool = pool or FakePool()
    return OrderOwner(pool, port_listening=lambda: port_listening, poll_s=0.01, backoff_s=(0.01,), **kw), pool


def _sending_core(reply=None):
    calls = []

    def core(client):
        calls.append(client)
        client.place_order("contract", "order")
        return reply or {"status": "ok", "message": "Modified"}

    return core, calls


def _blocker(pool):
    """Occupy the single orders worker until released."""
    entered, release = threading.Event(), threading.Event()

    def hold():
        entered.set()
        release.wait(5)

    return hold, entered, release


# -- invocation lifecycle ------------------------------------------------------


async def test_owner_path_sends_on_the_orders_client_and_settles_once():
    owner, pool = _owner()
    core, calls = _sending_core()
    settled = []
    reply = await owner.run(core, timeout=2, settle=settled.append)
    assert reply["status"] == "ok"
    assert len(calls) == 1 and pool.client.sent == [("place", ("contract", "order"))]
    assert pool.client.ready_checks == 1  # fresh round trip right before the send
    assert settled == [reply]


async def test_queue_cancel_abandons_with_zero_sends():
    """Request deadline passes while the job is still queued → abandoned:
    the job never runs, nothing is sent, the request settles not_sent once."""
    owner, pool = _owner()
    hold, entered, release = _blocker(pool)
    busy = asyncio.ensure_future(pool.run_sync("orders", hold))
    await asyncio.to_thread(entered.wait, 2)
    core, calls = _sending_core()
    settled = []
    reply = await owner.run(core, timeout=0.05, settle=settled.append)
    assert (reply["status"], reply["amend_outcome"]) == ("error", "not_sent")
    release.set()
    await busy
    await pool.run_sync("orders", lambda: None)  # the abandoned job has run (and exited)
    assert calls == [] and pool.client.sent == []
    assert settled == [reply]


async def test_timeout_while_started_returns_unconfirmed_and_worker_settles_late():
    """Timed out after start → unconfirmed; the WORKER settles with the final
    reply (exactly once), the request path never does."""
    owner, pool = _owner()
    go = threading.Event()
    settled = []

    def slow_core(client):
        client.place_order("c", "o")
        go.wait(5)
        return {"status": "ok", "confirmedPrice": 1.25}

    reply = await owner.run(slow_core, timeout=0.05, settle=settled.append)
    assert (reply["status"], reply["amend_outcome"]) == ("error", "unconfirmed")
    assert settled == []
    go.set()
    await pool.run_sync("orders", lambda: None)
    assert settled == [{"status": "ok", "confirmedPrice": 1.25}]


async def test_exception_after_send_is_unconfirmed_never_not_sent():
    owner, _ = _owner()

    def core(client):
        client.cancel_order("o")
        raise RuntimeError("socket died mid-cancel")

    reply = await owner.run(core, timeout=2)
    assert (reply["classification"], reply["amend_outcome"]) == ("connection", "unconfirmed")


async def test_exception_before_send_is_not_sent():
    owner, _ = _owner()

    def core(client):
        raise RuntimeError("lookup failed")

    reply = await owner.run(core, timeout=2)
    assert reply["amend_outcome"] == "not_sent"


async def test_core_error_after_send_keeps_explicit_rejection():
    owner, _ = _owner()
    core, _ = _sending_core({"status": "error", "classification": "ib_reject", "amend_outcome": "rejected"})
    assert (await owner.run(core, timeout=2))["amend_outcome"] == "rejected"


async def test_connection_reply_after_cancel_send_becomes_unconfirmed():
    """The cancel core stamps not_sent throughout; once the owner saw the
    possible-send mark, a non-reject failure is unconfirmed."""
    owner, _ = _owner()

    def core(client):
        client.cancel_order("o")
        return {"status": "error", "classification": "connection", "amend_outcome": "not_sent"}

    assert (await owner.run(core, timeout=2))["amend_outcome"] == "unconfirmed"


async def test_sequential_calls_after_an_exception_share_no_state():
    owner, pool = _owner()

    def bad(client):
        client.place_order("c", "o")
        raise RuntimeError("x")

    await owner.run(bad, timeout=2)
    core, _ = _sending_core()
    assert (await owner.run(core, timeout=2))["status"] == "ok"


# -- readiness / halt (A4) ----------------------------------------------------


async def test_broker_lost_refused_at_admission():
    """1100 / 2110 → readiness lost → 503 IB_BROKER_DISCONNECTED, no job."""
    owner, pool = _owner()
    pool.client.readiness = "lost"
    core, calls = _sending_core()
    reply = await owner.run(core, timeout=2)
    assert (reply["classification"], reply["amend_outcome"]) == (BROKER_DISCONNECTED, "not_sent")
    assert calls == []


async def test_broker_ready_again_after_1102_admits():
    owner, pool = _owner()
    pool.client.readiness = "lost"
    core, _ = _sending_core()
    assert (await owner.run(core, timeout=2))["classification"] == BROKER_DISCONNECTED
    pool.client.readiness = "ready"  # 1102: connectivity restored
    assert (await owner.run(core, timeout=2))["status"] == "ok"


async def test_healthy_admission_then_loss_while_queued_sends_nothing():
    """Admitted while ready; broker lost before the job reaches its send →
    the pre-send round trip refuses: zero sends, not_sent."""
    owner, pool = _owner()
    hold, entered, release = _blocker(pool)
    busy = asyncio.ensure_future(pool.run_sync("orders", hold))
    await asyncio.to_thread(entered.wait, 2)
    core, calls = _sending_core()
    task = asyncio.ensure_future(owner.run(core, timeout=2))
    await asyncio.sleep(0.05)
    pool.client.readiness = "unknown"  # socket replaced / round trip not confirmed
    release.set()
    await busy
    reply = await task
    assert (reply["classification"], reply["amend_outcome"]) == (BROKER_DISCONNECTED, "not_sent")
    assert len(calls) == 1 and pool.client.sent == []


async def test_halt_latched_while_queued_sends_nothing():
    owner, pool = _owner()
    hold, entered, release = _blocker(pool)
    busy = asyncio.ensure_future(pool.run_sync("orders", hold))
    await asyncio.to_thread(entered.wait, 2)
    core, calls = _sending_core()
    task = asyncio.ensure_future(owner.run(core, timeout=2))
    await asyncio.sleep(0.05)
    owner._latch_halt("test")
    release.set()
    await busy
    reply = await task
    assert reply["classification"] == ORDERS_HALTED
    assert calls == [] and pool.client.sent == []


async def test_halted_owner_refuses_at_admission():
    owner, _ = _owner()
    owner._latch_halt("test")
    core, calls = _sending_core()
    assert (await owner.run(core, timeout=2))["classification"] == ORDERS_HALTED
    assert calls == []


# -- supervisor (A2) ------------------------------------------------------------


async def _wait_for(pred, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


async def test_isolated_orders_disconnect_latches_halt_with_zero_reconnects(caplog):
    """orders lost while sync/data are up, the gateway port listens and the
    broker was ready → ambiguous (possible competing owner): latched halt,
    no reconnect attempt, CRITICAL log, red health status."""
    owner, pool = _owner()
    owner.start()
    with caplog.at_level(logging.CRITICAL, logger="xenon.order_owner"):
        pool.connected["orders"] = False
        assert await _wait_for(lambda: owner.halted)
        await asyncio.sleep(0.05)
    assert pool.reconnects == []
    assert any(r.levelno == logging.CRITICAL and "ORDERS_HALTED" in r.message for r in caplog.records)
    assert owner.status()["halted"] is True
    pool.connected["orders"] = True  # even if the socket came back, the latch holds
    core, calls = _sending_core()
    assert (await owner.run(core, timeout=2))["classification"] == ORDERS_HALTED
    assert calls == []
    await owner.stop()


@pytest.mark.parametrize("cause", ["port_down", "whole_pool", "broker_lost_before", "restart_noted"])
async def test_gateway_outage_gets_supervised_recovery(cause):
    owner, pool = _owner(port_listening=cause != "port_down")
    owner.start()
    if cause == "whole_pool":
        pool.connected.update(sync=False, data=False)
    if cause == "broker_lost_before":
        pool.client.readiness = "lost"
    if cause == "restart_noted":
        owner.note_gateway_restart()
    pool.reconnect_error = ConnectionError("gateway still down")
    pool.connected["orders"] = False
    assert await _wait_for(lambda: len(pool.reconnects) >= 2)
    # Requests meanwhile: 503, nothing queued to the broker.
    core, calls = _sending_core()
    assert (await owner.run(core, timeout=2))["classification"] == BROKER_DISCONNECTED
    pool.client.readiness = "ready"
    pool.reconnect_error = None
    assert await _wait_for(lambda: pool.connected["orders"])
    assert not owner.halted
    assert (await owner.run(core, timeout=2))["status"] == "ok"
    await owner.stop()


async def test_recovery_runs_one_replacement_at_a_time():
    """Only the supervisor replaces orders, serially: never two concurrent
    reconnects, and request handlers never reconnect."""
    pool = FakePool()
    in_flight, peak = [0], [0]
    original = pool.reconnect_role

    async def counted(role):
        in_flight[0] += 1
        peak[0] = max(peak[0], in_flight[0])
        await asyncio.sleep(0.02)
        in_flight[0] -= 1
        raise ConnectionError("down")

    pool.reconnect_role = counted
    owner, _ = _owner(pool, port_listening=False)
    owner.start()
    pool.connected["orders"] = False
    core, _ = _sending_core()
    for _ in range(5):
        await owner.run(core, timeout=0.1)
    await asyncio.sleep(0.1)
    assert peak[0] == 1
    pool.reconnect_role = original
    await owner.stop()


async def test_competing_owner_during_recovery_halts_without_further_attempts():
    """Takeover: clientId in use by another process → latched halt; no
    subsequent socket attempt."""
    owner, pool = _owner(port_listening=False)
    pool.reconnect_error = ClientIdInUse("326: client id is already in use")
    owner.start()
    pool.connected["orders"] = False
    assert await _wait_for(lambda: owner.halted)
    attempts = len(pool.reconnects)
    await asyncio.sleep(0.1)
    assert attempts == 1 and len(pool.reconnects) == 1
    await owner.stop()


async def test_startup_with_only_orders_down_halts_and_whole_outage_recovers():
    owner, pool = _owner()
    pool.connected["orders"] = False
    await owner.supervise()  # returns once latched
    assert owner.halted and pool.reconnects == []

    owner2, pool2 = _owner(port_listening=False)
    pool2.connected.update(sync=False, orders=False, data=False)
    owner2.start()
    assert await _wait_for(lambda: pool2.connected["orders"])
    assert not owner2.halted
    await owner2.stop()


async def test_intentional_shutdown_never_reconnects():
    owner, pool = _owner(port_listening=False)
    owner.start()
    await owner.stop()
    pool.connected["orders"] = False
    await asyncio.sleep(0.05)
    assert pool.reconnects == [] and not owner.halted


async def test_shutdown_with_queued_work_abandons_it():
    owner, pool = _owner()
    hold, entered, release = _blocker(pool)
    busy = asyncio.ensure_future(pool.run_sync("orders", hold))
    await asyncio.to_thread(entered.wait, 2)
    core, calls = _sending_core()
    settled = []
    task = asyncio.ensure_future(owner.run(core, timeout=2, settle=settled.append))
    await asyncio.sleep(0.05)
    await owner.stop()
    release.set()
    await busy
    reply = await task
    assert reply["amend_outcome"] == "not_sent"
    assert calls == [] and pool.client.sent == []
    assert settled == [reply]


async def test_executor_shutdown_cancels_queued_job_as_not_sent():
    """disconnect_all's executor shutdown cancels queued futures: provably
    never started → not_sent."""
    owner, pool = _owner()
    hold, entered, release = _blocker(pool)
    busy = asyncio.ensure_future(pool.run_sync("orders", hold))
    await asyncio.to_thread(entered.wait, 2)
    core, calls = _sending_core()
    task = asyncio.ensure_future(owner.run(core, timeout=2))
    await asyncio.sleep(0.05)
    pool._executor.shutdown(wait=False, cancel_futures=True)
    release.set()
    await busy
    reply = await task
    assert reply["amend_outcome"] == "not_sent" and calls == []
