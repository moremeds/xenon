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

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

import xenon._test_db as db_fixture
from xenon.execution.account_scope import AccountScope, resolve_from_env


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


def test_truncate_postgres_tables_treats_missing_schema_as_offline(monkeypatch):
    """A reachable but unmigrated test DB should not fail unrelated tests.

    Contract guarded here: when TRUNCATE fails with SQLAlchemyError (the
    typical symptom of a missing schema), the helper must swallow it and
    flip the reachability cache to False so subsequent tests skip TRUNCATE.
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

    db_fixture.truncate_all_xenon_tables()

    assert db_fixture._PG_REACHABLE is False


# ---------------------------------------------------------------------------
# Broker-connection deny (root conftest `_deny_broker_connections`)
# ---------------------------------------------------------------------------


def test_broker_deny_sets_dead_gateway_env():
    """Subprocess CLIs must inherit a dead IB endpoint, not a real gateway."""
    assert os.environ["IB_GATEWAY_HOST"] == "127.0.0.255"
    assert os.environ["IB_GATEWAY_PORT"] == "1"


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

_CHILD_DIAL = (
    "import socket, sys\n"
    "s = socket.socket()\n"
    "try:\n"
    "    s.connect((sys.argv[1], int(sys.argv[2])))\n"
    "    print('CONNECTED')\n"
    "except OSError as e:\n"
    "    print(f'REFUSED {type(e).__name__}: {e}')\n"
)


def _dial_child(host: str, port: int) -> str:
    """Spawn a real Python child that dials host:port; return its output."""
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_DIAL, host, str(port)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.stdout + result.stderr


def test_child_process_4001_dial_denied_any_host():
    """A subprocess dialing live-4001 on a non-localhost IP is refused."""
    out = _dial_child("127.0.0.2", 4001)
    assert "broker connection" in out, f"child was not guarded: {out!r}"
    assert "CONNECTED" not in out


def test_child_process_tws_7497_dial_denied():
    """The child guard covers TWS paper port 7497, not just the gateway."""
    out = _dial_child("127.0.0.1", 7497)
    assert "broker connection" in out, f"child was not guarded: {out!r}"
    assert "CONNECTED" not in out


@pytest.mark.paper_ib
def test_child_process_paper_ib_live_4001_still_denied():
    """paper_ib allowlists ONLY 127.0.0.1:4002 — a live-port dial stays refused."""
    assert os.environ["XENON_TEST_BROKER_ALLOW"] == "127.0.0.1:4002"
    out = _dial_child("127.0.0.1", 4001)
    assert "broker connection" in out, f"child was not guarded: {out!r}"
    assert "CONNECTED" not in out


@pytest.mark.paper_ib
def test_child_process_paper_ib_4002_passes_guard():
    """The exact allowlisted endpoint delegates to the real socket — outcome is
    a real connect or OS-level ECONNREFUSED, never the guard message."""
    out = _dial_child("127.0.0.1", 4002)
    assert "broker connection" not in out
