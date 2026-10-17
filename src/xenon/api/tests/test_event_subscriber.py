"""DATA-1 — EventSubscriber reconnects with backoff after a dropped LISTEN.

Fakes cover the unit contract (re-LISTEN, on_reconnect hook, backoff, stop
semantics); a real-Postgres test (skipped when PG is unreachable) closes the
underlying connection client-side and verifies end-to-end NOTIFY delivery on
the re-LISTENed socket. No `pg_terminate_backend` — only our own connection is
closed.
"""

from __future__ import annotations

import asyncio
import re

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio

from xenon.db.events import EventSubscriber  # noqa: E402


class _FakeConnection:
    """Minimal asyncpg.Connection stand-in for a LISTEN socket."""

    def __init__(self) -> None:
        self.listeners: dict[str, object] = {}
        self._termination_listeners: list = []
        self._closed = False

    async def add_listener(self, channel: str, callback) -> None:
        self.listeners[channel] = callback

    async def remove_listener(self, channel: str, callback) -> None:
        self.listeners.pop(channel, None)

    def add_termination_listener(self, callback) -> None:
        self._termination_listeners.append(callback)

    def is_closed(self) -> bool:
        return self._closed

    def terminate(self) -> None:
        """Client-side abort — fires termination listeners like asyncpg."""
        self._closed = True
        for cb in list(self._termination_listeners):
            cb(self)

    async def close(self) -> None:
        self.terminate()

    def emit_notify(self, channel: str, payload: str, pid: int = 4242) -> None:
        cb = self.listeners.get(channel)
        if cb is not None:
            cb(self, pid, channel, payload)


class _ConnectProbe:
    """asyncpg.connect stand-in — records connections, injects failures."""

    def __init__(self) -> None:
        self.created: list[_FakeConnection] = []
        self.errors: list[BaseException] = []
        self.calls = 0
        self.factory = _FakeConnection

    async def __call__(self, dsn: str) -> _FakeConnection:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        conn = self.factory()
        self.created.append(conn)
        return conn


async def _wait_for(predicate, timeout: float = 3.0, what: str = "condition") -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{what} not met within {timeout}s")


@pytest.fixture()
def probe(monkeypatch) -> _ConnectProbe:
    p = _ConnectProbe()
    monkeypatch.setattr(asyncpg, "connect", p)
    return p


def _subscriber(**overrides) -> EventSubscriber:
    kwargs = {
        "dsn": "postgresql://u@h/db",
        "channels": ["trade.closed"],
        "reconnect_min_delay": 0.01,
        "reconnect_max_delay": 0.05,
        "poll_interval": 0.05,
    }
    kwargs.update(overrides)
    return EventSubscriber(**kwargs)


async def test_reconnects_and_fires_on_reconnect_after_drop(probe):
    replays = []
    sub = _subscriber(on_reconnect=lambda: replays.append(1))
    await sub.start()
    assert probe.calls == 1
    conn1 = probe.created[0]

    conn1.terminate()

    await _wait_for(lambda: len(probe.created) == 2, what="reconnect")
    conn2 = probe.created[1]
    # Listeners re-registered on the new socket and the replay hook ran.
    assert "trade.closed" in conn2.listeners
    assert replays == [1]

    await sub.stop()
    assert conn2.is_closed()


async def test_reconnect_backoff_retries_until_success(probe):
    sub = _subscriber()
    await sub.start()
    probe.created[0].terminate()
    probe.errors.extend([OSError("down"), OSError("down again")])

    await _wait_for(lambda: len(probe.created) == 2, what="reconnect")
    # 1 initial + 2 failed attempts + 1 successful reconnect.
    assert probe.calls == 4
    await sub.stop()


async def test_notifications_flow_on_reconnected_connection(probe):
    received = []
    sub = _subscriber()
    sub.on("trade.closed", lambda _ch, payload: received.append(payload))
    await sub.start()

    probe.created[0].terminate()
    await _wait_for(lambda: len(probe.created) == 2, what="reconnect")

    probe.created[1].emit_notify("trade.closed", "77")
    await _wait_for(lambda: received == ["77"], what="notification")
    await sub.stop()


async def test_stop_does_not_reconnect(probe):
    sub = _subscriber()
    await sub.start()
    await sub.stop()
    await asyncio.sleep(0.2)
    assert probe.calls == 1


async def test_journal_consumer_replays_unconsumed_on_reconnect(monkeypatch):
    """start() must wire on_reconnect so a re-LISTEN replays missed events."""
    from xenon.api.services import journal_auto_import as jai

    captured: dict = {}

    class _StubSubscriber:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def on(self, channel, cb):
            captured.setdefault("channels", []).append(channel)

        async def start(self):
            captured["started"] = True

        async def stop(self):
            captured["stopped"] = True

    monkeypatch.setattr(jai, "EventSubscriber", _StubSubscriber)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@h/db")

    sub = jai.JournalAutoImportSubscriber()
    replay_calls = []
    monkeypatch.setattr(sub, "replay_unconsumed", lambda: replay_calls.append(1) or 0)

    await sub.start()
    assert captured.get("started") is True
    assert replay_calls == [1]  # boot-time replay

    on_reconnect = captured.get("on_reconnect")
    assert callable(on_reconnect)
    await on_reconnect()
    assert replay_calls == [1, 1]  # replay ran again after the reconnect


async def test_failed_replay_retries_while_socket_healthy(probe):
    """A failing on_reconnect must retry with backoff on the SAME socket —
    events missed during the outage can't wait for another disconnect."""
    attempts = []

    def _hook():
        attempts.append(1)
        if len(attempts) < 2:
            raise RuntimeError("transient replay failure")

    sub = _subscriber(on_reconnect=_hook)
    await sub.start()
    probe.created[0].terminate()
    await _wait_for(lambda: len(probe.created) == 2, what="reconnect")

    # Socket stays healthy — the failed replay must still retry until it
    # succeeds, with no further reconnect.
    await _wait_for(lambda: len(attempts) >= 2, what="replay retry")
    assert probe.calls == 2
    assert not sub._replay_pending
    await sub.stop()


async def test_journal_boot_replay_failure_requests_retry(monkeypatch):
    """If the boot replay has failed rows, the subscriber is armed to retry."""
    from xenon.api.services import journal_auto_import as jai

    captured: dict = {}

    class _StubSubscriber:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def on(self, channel, cb):
            captured.setdefault("channels", []).append(channel)

        def request_replay(self):
            captured["replay_requested"] = True

        async def start(self):
            captured["started"] = True

        async def stop(self):
            captured["stopped"] = True

    monkeypatch.setattr(jai, "EventSubscriber", _StubSubscriber)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@h/db")

    sub = jai.JournalAutoImportSubscriber()

    def _bad_replay():
        sub.last_replay_failed = 1
        return 0

    monkeypatch.setattr(sub, "replay_unconsumed", _bad_replay)
    await sub.start()
    assert captured.get("replay_requested") is True


async def test_replay_unconsumed_counts_row_failures(monkeypatch):
    """Per-row failures are counted, not swallowed — a later pass retries them."""
    from types import SimpleNamespace

    from xenon.api.services import journal_auto_import as jai

    class _Rows:
        def fetchall(self):
            return [SimpleNamespace(id=1), SimpleNamespace(id=2), SimpleNamespace(id=3)]

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, _stmt):
            return _Rows()

    class _Eng:
        def connect(self):
            return _Conn()

    sub = jai.JournalAutoImportSubscriber()
    monkeypatch.setattr(jai, "get_sync_engine", lambda: _Eng())

    calls: list[int] = []

    def _handle(oid):
        calls.append(oid)
        if oid == 2 and calls.count(2) == 1:
            raise RuntimeError("transient row failure")

    monkeypatch.setattr(sub, "handle_notification_id", _handle)

    assert sub.replay_unconsumed() == 2
    assert sub.last_replay_failed == 1
    # Second pass (the retry): the transient failure succeeds.
    assert sub.replay_unconsumed() == 3
    assert sub.last_replay_failed == 0


async def test_replay_after_reconnect_raises_on_incomplete(monkeypatch):
    """Failed rows must surface so the subscriber keeps the retry armed."""
    from xenon.api.services import journal_auto_import as jai

    sub = jai.JournalAutoImportSubscriber()
    sub.last_replay_failed = 2
    monkeypatch.setattr(sub, "replay_unconsumed", lambda: 5)
    with pytest.raises(RuntimeError, match="incomplete"):
        await sub._replay_after_reconnect()


async def test_open_connection_closes_on_listen_failure(probe):
    """A LISTEN setup error must close the not-yet-owned connection."""

    class _FailListen(_FakeConnection):
        async def add_listener(self, channel, callback):
            raise RuntimeError("listen refused")

    probe.factory = _FailListen
    sub = _subscriber()
    with pytest.raises(RuntimeError, match="listen refused"):
        await sub._open_connection()
    assert probe.created[0].is_closed()


async def test_open_connection_closes_when_cancelled_during_listen(probe):
    """Cancellation mid-LISTEN (e.g. watchdog stopped) must not leak the conn."""

    class _StuckListen(_FakeConnection):
        async def add_listener(self, channel, callback):
            await asyncio.Event().wait()  # never returns

    probe.factory = _StuckListen
    sub = _subscriber()
    task = asyncio.create_task(sub._open_connection())
    await _wait_for(lambda: len(probe.created) == 1, what="connect")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert probe.created[0].is_closed()


async def test_stop_during_pending_listen_closes_new_conn(probe):
    """stop() while the watchdog's reconnect is inside add_listener must not
    leave the half-configured socket open."""

    class _StuckListen(_FakeConnection):
        async def add_listener(self, channel, callback):
            await asyncio.Event().wait()  # never returns

    sub = _subscriber()
    await sub.start()
    probe.created[0].terminate()
    probe.factory = _StuckListen

    await _wait_for(lambda: len(probe.created) == 2, what="reconnect attempt")
    await sub.stop()
    assert probe.created[1].is_closed()


async def test_reconnect_against_real_postgres():
    """Close the underlying LISTEN conn client-side → re-LISTEN + replay + NOTIFY.

    Only our own connection is terminated — no other session's backend is
    touched (no pg_terminate_backend).
    """
    from xenon._test_db import async_test_db_url, is_pg_reachable

    if not is_pg_reachable():
        pytest.skip("PG test DB unreachable")

    dsn = async_test_db_url()
    raw_dsn = re.sub(r"^postgresql\+\w+://", "postgresql://", dsn)

    reconnected = asyncio.Event()
    received = asyncio.Event()
    got: list[str] = []

    sub = EventSubscriber(
        dsn,
        ["trade.closed"],
        on_reconnect=reconnected.set,
        reconnect_min_delay=0.05,
        reconnect_max_delay=0.5,
        poll_interval=0.1,
    )
    sub.on("trade.closed", lambda _ch, payload: (got.append(payload), received.set()))
    await sub.start()
    try:
        conn1 = sub._conn
        assert conn1 is not None
        conn1.terminate()

        await asyncio.wait_for(reconnected.wait(), timeout=10)
        assert sub._conn is not None
        assert sub._conn is not conn1
        assert not sub._conn.is_closed()

        admin = await asyncpg.connect(raw_dsn)
        try:
            await admin.execute("SELECT pg_notify('trade.closed', '31337')")
        finally:
            await admin.close()

        await asyncio.wait_for(received.wait(), timeout=10)
        assert "31337" in got
    finally:
        await sub.stop()
