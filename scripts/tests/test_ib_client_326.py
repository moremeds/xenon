"""IB error 326 (clientId in use) is detected from ``errorEvent``.

ib_async 2.1.0 reports 326 only through ``errorEvent`` and then raises a
``TimeoutError()`` with an EMPTY message (live-verified 2026-10-20), so text
matching on the exception never sees it. No IB connection is used here.
"""

from __future__ import annotations

import pytest
from eventkit import Event

from xenon.clients.ib_client import (
    SUBPROCESS_ID_RANGE,
    IBClient,
    IBConnectionError,
)
from xenon.execution import ib_order_manage
from xenon.execution.ib_order_manage import _is_clientid_in_use, _reconnect_as_owner, classify_failure


pytestmark = pytest.mark.usefixtures("bypass_broker_boundary")


class FakeIB:
    """Minimal IB double: a real eventkit ``errorEvent`` + scripted connect."""

    def __init__(self, busy_ids=(), timeout_ids=()):
        self.errorEvent = Event("errorEvent")
        self.busy_ids = set(busy_ids)
        self.timeout_ids = set(timeout_ids)
        self.attempted: list[int] = []
        self.handlers_during_connect: list[int] = []

    def connect(self, host, port, clientId, timeout=3):
        self.attempted.append(clientId)
        self.handlers_during_connect.append(len(self.errorEvent))
        if clientId in self.busy_ids:
            self.errorEvent.emit(-1, 326, "Unable to connect as the client id is already in use.", None)
            raise TimeoutError()  # ib_async: empty message
        if clientId in self.timeout_ids:
            raise TimeoutError()

    def isConnected(self):
        return False


def make_client(fake: FakeIB) -> IBClient:
    client = IBClient()
    client._ib = fake
    return client


def test_auto_rotates_past_326_with_empty_timeout(monkeypatch):
    lo, _hi = SUBPROCESS_ID_RANGE
    monkeypatch.setattr("random.randint", lambda a, b: a)  # deterministic start = lo
    fake = FakeIB(busy_ids={lo})
    client = make_client(fake)

    client.connect(client_id="auto", timeout=1)

    assert fake.attempted == [lo, lo + 1]
    assert client._last_client_id == lo + 1


def test_explicit_326_raises_typed_error_without_retry(monkeypatch):
    from xenon.clients.ib_client import ClientIdInUse

    sleeps: list[float] = []
    monkeypatch.setattr("xenon.clients.ib_client.time.sleep", sleeps.append)
    fake = FakeIB(busy_ids={95})
    client = make_client(fake)

    with pytest.raises(ClientIdInUse) as ei:
        client.connect(client_id=95, timeout=1, max_retries=3)

    assert ei.value.client_id == 95
    assert ei.value.code == 326
    assert isinstance(ei.value, IBConnectionError)
    assert fake.attempted == [95]  # fail fast: no generic retry loop
    assert sleeps == []


def test_typed_error_is_ownership_and_409_path(capsys, monkeypatch):
    from xenon.clients.ib_client import ClientIdInUse

    exc = ClientIdInUse(95)
    assert _is_clientid_in_use(exc)
    assert classify_failure(None, exc) == "ownership"

    # Owner reconnect: real IBClient + FakeIB, retries 3x then ownership.
    monkeypatch.setattr(ib_order_manage.time, "sleep", lambda *_a, **_k: None)
    fake = FakeIB(busy_ids={95})
    client = make_client(fake)
    with pytest.raises(SystemExit) as se:
        _reconnect_as_owner(client, "127.0.0.1", 4002, 95)
    assert se.value.code == 1
    out = capsys.readouterr().out
    assert '"classification": "ownership"' in out
    assert fake.attempted == [95, 95, 95]


def test_non_326_timeout_unchanged(monkeypatch):
    from xenon.clients.ib_client import ClientIdInUse

    monkeypatch.setattr("xenon.clients.ib_client.time.sleep", lambda *_a: None)
    lo, _hi = SUBPROCESS_ID_RANGE

    # auto: a plain timeout is not a conflict -> no rotation, IBConnectionError.
    monkeypatch.setattr("random.randint", lambda a, b: a)
    fake = FakeIB(timeout_ids={lo, lo + 1})
    with pytest.raises(IBConnectionError) as ei:
        make_client(fake).connect(client_id="auto", timeout=1)
    assert not isinstance(ei.value, ClientIdInUse)
    assert fake.attempted == [lo]

    # explicit: generic retries run, result is a plain IBConnectionError.
    fake = FakeIB(timeout_ids={95})
    with pytest.raises(IBConnectionError) as ei:
        make_client(fake).connect(client_id=95, timeout=1, max_retries=2)
    assert not isinstance(ei.value, ClientIdInUse)
    assert fake.attempted == [95, 95]
    assert classify_failure(None, ei.value) == "connection"
    assert not _is_clientid_in_use(ei.value)


def test_error_handler_unsubscribed_after_every_attempt(monkeypatch):
    lo, _hi = SUBPROCESS_ID_RANGE
    monkeypatch.setattr("random.randint", lambda a, b: a)
    fake = FakeIB(busy_ids={lo, lo + 1})
    client = make_client(fake)
    base = len(fake.errorEvent)

    client.connect(client_id="auto", timeout=1)

    assert fake.attempted == [lo, lo + 1, lo + 2]
    # exactly one capture handler was live during each attempt, none leaked
    assert fake.handlers_during_connect == [base + 1] * 3
    assert len(fake.errorEvent) == base

    # also released on the failure path
    from xenon.clients.ib_client import ClientIdInUse

    fake2 = FakeIB(busy_ids={95})
    with pytest.raises(ClientIdInUse):
        make_client(fake2).connect(client_id=95, timeout=1)
    assert len(fake2.errorEvent) == 0

    # ... and after a non-326 timeout
    fake3 = FakeIB(timeout_ids={95})
    with pytest.raises(IBConnectionError):
        make_client(fake3).connect(client_id=95, timeout=1)
    assert len(fake3.errorEvent) == 0


class _BrokenSubscribeEvent:
    """errorEvent whose ``+=`` raises, so capture entry fails before any connect."""

    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.adds = 0

    def __iadd__(self, handler):
        self.adds += 1
        if self.adds <= self.fail_times:
            raise RuntimeError("subscribe failed")
        return self

    def __isub__(self, handler):
        return self


def test_capture_entry_failure_keeps_original_error_and_no_stale_326(monkeypatch):
    monkeypatch.setattr("xenon.clients.ib_client.time.sleep", lambda *_a: None)

    # explicit: entry fails on every attempt -> wrapped IBConnectionError, no UnboundLocalError
    fake = FakeIB()
    fake.errorEvent = _BrokenSubscribeEvent(fail_times=99)
    with pytest.raises(IBConnectionError, match="subscribe failed") as ei:
        make_client(fake).connect(client_id=95, timeout=1, max_retries=2)
    assert not isinstance(ei.value, ClientIdInUseType())
    assert fake.attempted == []  # never reached ib.connect

    # explicit: 326 on attempt 1 is not carried into a later attempt's classification
    # (first attempt busy -> typed error immediately; a fresh client/attempt starts clean)
    lo, _hi = SUBPROCESS_ID_RANGE
    monkeypatch.setattr("random.randint", lambda a, b: a)
    fake = FakeIB(busy_ids={lo})
    client = make_client(fake)
    client.connect(client_id="auto", timeout=1)  # rotates past lo
    fake.busy_ids = set()
    fake.timeout_ids = {95}
    with pytest.raises(IBConnectionError) as ei:
        client.connect(client_id=95, timeout=1, max_retries=2)
    assert not isinstance(ei.value, ClientIdInUseType())
    assert fake.attempted[-2:] == [95, 95]

    # auto: entry fails on the first id, works afterwards; failure is non-conflict -> original error
    fake = FakeIB()
    fake.errorEvent = _BrokenSubscribeEvent(fail_times=1)
    with pytest.raises(IBConnectionError, match="subscribe failed"):
        make_client(fake).connect(client_id="auto", timeout=1)
    assert fake.attempted == []


def ClientIdInUseType():
    from xenon.clients.ib_client import ClientIdInUse

    return ClientIdInUse
