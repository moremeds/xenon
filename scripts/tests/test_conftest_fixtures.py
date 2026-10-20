"""Verify the scoped-test fixtures exist and behave per the PG migration plan.

These fixtures are the prerequisite layer for the clean-cutoff PG migration
(`docs/plans/2026-05-03-pg-migration-clean-cutoff.md`). Every migrated CLI calls
`AccountScope.resolve_from_env()`, which raises if `XENON_BROKER_ACCOUNT` is
unset — so the autouse env fixture must export it for every test.
"""

from __future__ import annotations

import errno
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

import xenon._test_db as db_fixture
from xenon.execution.account_scope import AccountScope, resolve_from_env
from xenon.utils.ib_connection import (
    BrokerEndpointDenied,
    assert_broker_endpoint_allowed,
)

# Captured at THIS module's import — the root conftest's socket guard is
# installed at conftest-import time (before collection of any test module),
# so a broker-port dial made here must already be refused. If the guard were
# only per-test, this would hit the real OS (or hang on a live gateway).
_IMPORT_TIME_DIAL_ERROR: OSError | None = None
_dial_sock = socket.socket()
try:
    _dial_sock.connect(("127.0.0.1", 4001))
except OSError as _dial_exc:
    _IMPORT_TIME_DIAL_ERROR = _dial_exc
finally:
    _dial_sock.close()


def test_xenon_broker_account_env_set_for_every_test():
    assert os.environ.get("XENON_BROKER_ACCOUNT") == "DU0000000"


def test_xenon_trading_mode_env_set_for_every_test():
    assert os.environ.get("XENON_TRADING_MODE") == "paper"


def test_resolve_from_env_returns_paper_scope_in_test_context():
    scope = resolve_from_env()
    assert scope.broker == "IB"
    assert scope.account_env == "paper"
    assert scope.broker_account == "DU0000000"


def test_scope_fixture_yields_paper_ib_scope(scope_fixture):
    assert isinstance(scope_fixture, AccountScope)
    assert scope_fixture.broker == "IB"
    assert scope_fixture.account_env == "paper"
    assert scope_fixture.broker_account == "DU0000000"


def test_pg_test_engine_is_sync_engine(pg_test_engine):
    assert isinstance(pg_test_engine, Engine)
    url = str(pg_test_engine.url)
    assert "psycopg" in url, f"pg_test_engine must be sync (psycopg), got {url}"
    assert "asyncpg" not in url, f"pg_test_engine must not be async, got {url}"


def test_pg_test_engine_points_at_test_db(pg_test_engine):
    db_name = pg_test_engine.url.database or ""
    assert "test" in db_name.lower(), (
        f"pg_test_engine must point at a *_test database (defense in depth), got {db_name!r}"
    )


def test_truncate_postgres_tables_reraises_missing_schema(monkeypatch):
    """A TRUNCATE failure that is not a connection error must surface.

    Contract guarded here: a non-connection SQLAlchemyError (missing schema,
    deadlock) re-raises and leaves the reachability cache alone. The old
    "swallow and go offline" contract let later tests write through a cached
    non-test engine (INV-test-db-leak).
    """

    class FakeConnection:
        def execute(self, _statement):
            raise SQLAlchemyError("missing schema")

    class FakeBegin:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, *_exc_info):
            return False

    class FakeEngine:
        def begin(self):
            return FakeBegin()

    # Skip the reachability probe (pretend the DB is up) and inject our fake
    # session engine so TRUNCATE attempts hit it.
    monkeypatch.setattr(db_fixture, "_PG_REACHABLE", True)
    monkeypatch.setattr(db_fixture, "_SESSION_ENGINE", FakeEngine())

    with pytest.raises(SQLAlchemyError, match="missing schema"):
        db_fixture.truncate_all_xenon_tables()

    assert db_fixture._PG_REACHABLE is True


# ---------------------------------------------------------------------------
# Broker-connection deny (root conftest `_deny_broker_connections`)
# ---------------------------------------------------------------------------


def test_broker_deny_sets_dead_gateway_env():
    """Subprocess CLIs must inherit a dead IB endpoint, not a real gateway."""
    assert os.environ["IB_GATEWAY_HOST"] == "127.0.0.255"
    assert os.environ["IB_GATEWAY_PORT"] == "1"


def test_broker_guard_env_armed_for_session():
    """XENON_TEST_BROKER_GUARD=1 is set at conftest import for the whole
    session — unmarked tests see it, and children inherit it."""
    assert os.environ["XENON_TEST_BROKER_GUARD"] == "1"


def test_broker_dial_at_module_import_was_refused():
    """Regression: the guard exists at import time, not just per-test."""
    assert type(_IMPORT_TIME_DIAL_ERROR).__name__ == "BrokerConnectDenied"
    assert "broker connection" in str(_IMPORT_TIME_DIAL_ERROR)


@pytest.fixture(scope="session")
def _session_scope_dial_error():
    """Dials live-4001 inside a session-scoped fixture — session fixtures
    initialize before the test's function-scoped monkeypatch policy, so only
    the import-time-installed guard can be responsible for the refusal."""
    sock = socket.socket()
    try:
        sock.connect(("127.0.0.1", 4001))
    except OSError as exc:
        return exc
    finally:
        sock.close()
    return None


def test_session_scoped_fixture_dial_refused(_session_scope_dial_error):
    """Regression: pre-collection guard covers session fixtures too."""
    assert type(_session_scope_dial_error).__name__ == "BrokerConnectDenied"
    assert "broker connection" in str(_session_scope_dial_error)


def test_broker_port_4001_connect_fails_loudly():
    """An unmarked test dialing the live gateway must raise, not connect."""
    sock = socket.socket()
    try:
        with pytest.raises(ConnectionRefusedError, match="broker connection"):
            sock.connect(("127.0.0.1", 4001))
    finally:
        sock.close()


def test_broker_port_4001_denied_on_any_host():
    """The guard matches on the port — non-localhost 4001 is denied too."""
    sock = socket.socket()
    try:
        with pytest.raises(ConnectionRefusedError, match="broker connection"):
            sock.connect(("192.0.2.1", 4001))
    finally:
        sock.close()


def test_broker_port_4002_connect_ex_denied():
    """connect_ex probes (e.g. health checks) get ECONNREFUSED, not a dial."""
    sock = socket.socket()
    try:
        assert sock.connect_ex(("127.0.0.1", 4002)) == errno.ECONNREFUSED
    finally:
        sock.close()


def test_non_broker_port_unaffected():
    """The guard delegates every non-broker connect to the real socket."""
    sock = socket.socket()
    try:
        # Port 1 (nothing listens) proves delegation: a real connect attempt
        # yields ECONNREFUSED, not BrokerConnectDenied.
        assert sock.connect_ex(("127.0.0.1", 1)) == errno.ECONNREFUSED
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Child-process broker guard — scripts/tests/broker_guard/sitecustomize.py is
# on PYTHONPATH (set by the root conftest), so every spawned Python child
# auto-imports the identical socket guard at interpreter startup.
# ---------------------------------------------------------------------------

# Denied dial: the child lets the exception traceback out → nonzero exit,
# guard message on stderr.
_CHILD_DIAL = (
    "import socket, sys\n"
    "socket.socket().connect((sys.argv[1], int(sys.argv[2])))\n"
    "print('CONNECTED')\n"
)

# Delegation probe: the child swaps sitecustomize's underlying-connect
# delegate for a recorder, so a PERMITTED endpoint is proven to reach the
# real socket layer WITHOUT a real dial (no listener needed, nothing opened).
_CHILD_DIAL_FAKE_DELEGATE = (
    "import socket, sys\n"
    "import sitecustomize\n"
    "calls = []\n"
    "def _fake(sock, addr):\n"
    "    calls.append(addr)\n"
    "sitecustomize._real_connect = _fake\n"
    "socket.socket().connect((sys.argv[1], int(sys.argv[2])))\n"
    "assert calls == [(sys.argv[1], int(sys.argv[2]))], calls\n"
    "print('DELEGATED')\n"
)


def _dial_child(host: str, port: int, *, env=None, script: str = _CHILD_DIAL):
    """Spawn a real Python child that dials host:port; return the result."""
    return subprocess.run(
        [sys.executable, "-c", script, host, str(port)],
        capture_output=True,
        text=True,
        timeout=15,
        env=os.environ if env is None else env,
    )


def test_child_process_4001_dial_denied_any_host():
    """A subprocess dialing live-4001 on a non-localhost IP is refused."""
    result = _dial_child("127.0.0.2", 4001)
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "broker connection" in out, f"child was not guarded: {out!r}"
    assert "CONNECTED" not in out


def test_child_process_tws_7497_dial_denied():
    """The child guard covers TWS paper port 7497, not just the gateway."""
    result = _dial_child("127.0.0.1", 7497)
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "broker connection" in out, f"child was not guarded: {out!r}"
    assert "CONNECTED" not in out


def test_child_guard_inactive_without_guard_env():
    """A child that does NOT inherit XENON_TEST_BROKER_GUARD (spawned under
    live_ib, or outside a test session entirely) is unguarded — production
    parity. Proven by the fake delegate: no real dial occurs."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("XENON_TEST_BROKER_")}
    result = _dial_child("127.0.0.1", 4001, env=env, script=_CHILD_DIAL_FAKE_DELEGATE)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "DELEGATED" in result.stdout, out
    assert "broker connection" not in out


@pytest.mark.paper_ib
def test_child_process_paper_ib_live_4001_still_denied():
    """paper_ib allowlists ONLY 127.0.0.1:4002 — a live-port dial stays refused."""
    assert os.environ["XENON_TEST_BROKER_ALLOW"] == "127.0.0.1:4002"
    result = _dial_child("127.0.0.1", 4001)
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "broker connection" in out, f"child was not guarded: {out!r}"
    assert "CONNECTED" not in out


@pytest.mark.paper_ib
def test_child_process_paper_ib_4002_delegates():
    """paper_ib's exact endpoint bypasses the child guard — proven with a
    child-side fake underlying connect; no real 4002 dial is ever opened."""
    result = _dial_child("127.0.0.1", 4002, script=_CHILD_DIAL_FAKE_DELEGATE)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "DELEGATED" in result.stdout, out
    assert "broker connection" not in out


# ---------------------------------------------------------------------------
# Broker-boundary allowlist — xenon.utils.ib_connection.assert_broker_endpoint_allowed
# is routed through by every ib_async connect call site. While
# XENON_TEST_BROKER_GUARD=1 it refuses EVERY (host, port) except the exact
# XENON_TEST_BROKER_ALLOW endpoint — including custom ports the socket
# denylist doesn't know.
# ---------------------------------------------------------------------------


def test_boundary_helper_noop_without_guard_env(monkeypatch):
    """Production parity: guard env unset → the helper is a pure no-op, even
    for endpoints the test layers would deny."""
    monkeypatch.delenv("XENON_TEST_BROKER_GUARD", raising=False)
    monkeypatch.delenv("XENON_TEST_BROKER_ALLOW", raising=False)
    assert_broker_endpoint_allowed("127.0.0.1", 4001)
    assert_broker_endpoint_allowed("10.9.9.9", 4500)


def test_boundary_helper_denies_everything_without_allow():
    """Unmarked: GUARD=1 armed, ALLOW unset — every endpoint refused."""
    with pytest.raises(BrokerEndpointDenied, match="broker guard refused"):
        assert_broker_endpoint_allowed("127.0.0.1", 4002)
    with pytest.raises(BrokerEndpointDenied):
        assert_broker_endpoint_allowed("127.0.0.1", 4001)
    with pytest.raises(BrokerEndpointDenied):
        assert_broker_endpoint_allowed("10.9.9.9", 4500)


@pytest.mark.paper_ib
def test_boundary_helper_allows_only_exact_paper_endpoint():
    assert_broker_endpoint_allowed("127.0.0.1", 4002)  # the one allowed
    with pytest.raises(BrokerEndpointDenied):
        assert_broker_endpoint_allowed("127.0.0.1", 4001)
    with pytest.raises(BrokerEndpointDenied):
        assert_broker_endpoint_allowed("127.0.0.2", 4002)  # wrong host
    with pytest.raises(BrokerEndpointDenied):
        assert_broker_endpoint_allowed("127.0.0.1", 14002)  # port prefix trap
    with pytest.raises(BrokerEndpointDenied):
        assert_broker_endpoint_allowed("10.0.0.5", 4500)  # custom port


def test_ibclient_connect_refused_at_boundary_no_dial():
    """IBClient.connect must refuse before ib.connect is ever invoked —
    sentinel transport proves zero dial attempts on a custom endpoint."""
    from xenon.clients.ib_client import IBClient

    client = IBClient()
    calls: list = []
    client._ib.connect = lambda *a, **k: calls.append((a, k))
    with pytest.raises(ConnectionRefusedError, match="broker guard refused"):
        client.connect(host="10.9.9.9", port=4500, client_id=0)
    assert calls == []


def test_ibclient_auto_allocate_refused_at_boundary():
    """The 'auto' client-id allocation path is guarded the same way."""
    from xenon.clients.ib_client import IBClient

    client = IBClient()
    calls: list = []
    client._ib.connect = lambda *a, **k: calls.append((a, k))
    with pytest.raises(ConnectionRefusedError, match="broker guard refused"):
        client.connect(host="127.0.0.1", port=4002, client_id="auto")
    assert calls == []


@pytest.mark.paper_ib
def test_ibclient_paper_ib_delegates_to_ib_connect():
    """paper_ib: the exact allowlisted endpoint passes the boundary and
    reaches the underlying transport (sentinel — no real dial)."""
    from xenon.clients.ib_client import IBClient

    client = IBClient()
    calls: list = []
    client._ib.connect = lambda *a, **k: calls.append((a, k))
    client.connect(host="127.0.0.1", port=4002, client_id=90)
    assert len(calls) == 1
    assert calls[0][0][:2] == ("127.0.0.1", 4002)


class _FakeIBAsync:
    """Sentinel ib_async.IB stand-in — records (never performs) dials."""

    def __init__(self):
        self.connect_calls: list = []

    async def connectAsync(self, *a, **k):
        self.connect_calls.append((a, k))

    def disconnect(self):
        return None


@pytest.mark.asyncio
async def test_ib_realtime_probe_connect_async_refused_at_boundary(monkeypatch):
    """scripts/infra/ib_realtime's connectivity probe records the refusal as
    a failed connect result and never reaches connectAsync."""
    import importlib.util

    import ib_async

    spec = importlib.util.spec_from_file_location(
        "_ib_realtime_probe",
        Path(__file__).parent.parent / "infra" / "ib_realtime" / "test_ib_realtime.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    fake_ib = _FakeIBAsync()
    monkeypatch.setattr(ib_async, "IB", lambda: fake_ib)
    await mod.test_ib_connection("10.9.9.9", 4500)
    assert fake_ib.connect_calls == []
    assert any(
        name == "IB connect" and "broker guard refused" in msg
        for name, msg in mod.results.errors
    )


@pytest.mark.live_ib
def test_live_ib_boundary_fully_unguarded():
    """Operator opt-in: under `-m live_ib` the guard env is deleted, so the
    boundary helper no-ops and connect delegates to the real transport.
    (Deselected by default addopts; runs only with `pytest -m live_ib`.)"""
    assert "XENON_TEST_BROKER_GUARD" not in os.environ
    from xenon.clients.ib_client import IBClient

    client = IBClient()
    calls: list = []
    client._ib.connect = lambda *a, **k: calls.append((a, k))
    client.connect(host="10.0.0.6", port=4001, client_id=0)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# async_engine fixture — the PG reachability probe runs at most once per
# worker; a cached True must not re-probe.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_pg_probe_runs_once_per_worker(monkeypatch):
    """Two async_engine fixture uses → a single SELECT 1 probe."""
    import importlib.util
    from pathlib import Path

    import sqlalchemy.ext.asyncio as sa_asyncio

    # Load the sibling conftest as a plain module so its probe cache is
    # isolated from the already-registered plugin instance.
    spec = importlib.util.spec_from_file_location(
        "_scripts_tests_conftest", Path(__file__).parent / "conftest.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    counter = {"probes": 0, "engines": 0}

    class _FakeConn:
        async def execute(self, _stmt):
            counter["probes"] += 1

    class _FakeConnect:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, *_a):
            return False

    class _FakeAsyncEngine:
        def connect(self):
            return _FakeConnect()

        async def dispose(self):
            return None

    def _fake_create_async_engine(*_a, **_k):
        counter["engines"] += 1
        return _FakeAsyncEngine()

    monkeypatch.setattr(mod, "is_pg_reachable", lambda: True)
    monkeypatch.setattr(sa_asyncio, "create_async_engine", _fake_create_async_engine)

    async def _use_fixture_once():
        gen = mod.async_engine._get_wrapped_function()()
        try:
            await gen.__anext__()
        finally:
            await gen.aclose()

    await _use_fixture_once()
    await _use_fixture_once()
    assert counter["probes"] == 1  # one probe across both fixture uses
    assert counter["engines"] == 3  # probe engine + one fresh engine per use
