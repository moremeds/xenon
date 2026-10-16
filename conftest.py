"""Repo-root pytest config.

Set XENON_AUTH_ALLOW_DEV_OPEN=1 for the whole test session so the fail-closed
auth middleware allows TestClient requests (whose client.host is 'testclient',
not localhost). Auth-specific tests override this via patch.dict to observe
gating. Never set in production.

Broker isolation: tests not marked ``live_ib`` / ``paper_ib`` must never open
a broker connection. Two layers enforce that:

* env deny — ``IB_GATEWAY_HOST``/``IB_GATEWAY_PORT`` point at a dead loopback
  address so spawned subprocess CLIs (which re-read env at import time) fail
  fast instead of reaching a real gateway. Note ``IB_GATEWAY_PORT`` is legacy:
  the port the code actually dials comes from ``XENON_TRADING_MODE`` via
  ``trading_mode.EXPECTED_PORT`` (paper→4002, live→4001); the env var is still
  set for any path that reads it.
* socket guard — ``socket.socket.connect``/``connect_ex`` refuse destination
  ports 4001/4002 on ANY host in-process, because module-level ``DEFAULT_*``
  constants in ``ib_client``/``ib_connection``/``ib_gateway`` are bound at
  import time and never see the monkeypatched env. ``connect`` raises
  ``BrokerConnectDenied`` — a ``ConnectionRefusedError`` so probe-style
  callers (``ib_gateway._port_listening`` and other ``except OSError``
  health checks) still see a plain "closed" verdict while the message tells
  the test author exactly why the dial was blocked; ``connect_ex`` returns
  ECONNREFUSED.
"""

import errno
import os
import socket

import pytest

# Imported for fixture registration: `_pg_schema_guard` depends on it so the
# schema check runs after the per-worker DB rewrite under pytest-xdist.
from xenon._test_db import _ensure_worker_db  # noqa: F401

os.environ.setdefault("XENON_AUTH_ALLOW_DEV_OPEN", "1")

# Ports the IB Gateway API listens on (live 4001 / paper 4002). A test that
# dials either without opting in via marker is a bug — make it fail loudly.
_BROKER_PORTS = frozenset({4001, 4002})


@pytest.fixture(scope="session", autouse=True)
def _pg_schema_guard(_ensure_worker_db):
    """Fail loudly when the PG test DB is reachable but schema-incomplete.

    `truncate_all_xenon_tables` treats any SQLAlchemyError (including
    UndefinedTable) as "offline" and flips `mark_offline`, which silently
    skips every PG-dependent test for the rest of the session. A reachable
    but incomplete schema is not the offline case — error once per worker
    with the missing table names instead. Offline (unreachable) stays a
    no-op so offline dev runs are unaffected. Depends on `_ensure_worker_db`
    so the check targets the per-worker DB under xdist.
    """
    from sqlalchemy import text

    from xenon._test_db import XENON_TABLES, get_session_engine, is_pg_reachable

    if not is_pg_reachable():
        return
    with get_session_engine().connect() as conn:
        missing = [
            table
            for table in XENON_TABLES
            if conn.execute(text("SELECT to_regclass(:n)"), {"n": table}).scalar() is None
        ]
    if missing:
        raise RuntimeError(
            "PG test DB schema incomplete — missing tables: "
            + ", ".join(missing)
            + ". Run `alembic upgrade head` against the test DB; a reachable "
            "but incomplete schema must not be downgraded to offline skips."
        )


class BrokerConnectDenied(ConnectionRefusedError):
    """Raised when an unmarked test opens a socket toward a broker port.

    A ``ConnectionRefusedError`` (not RuntimeError) so probe-style callers
    that catch ``OSError`` see the port as simply closed — the same verdict a
    missing gateway produces — while tests that genuinely need the broker
    fail with a message naming the fix."""


def _deny_broker_address(address: object) -> None:
    if isinstance(address, tuple) and len(address) >= 2 and address[1] in _BROKER_PORTS:
        raise BrokerConnectDenied(
            f"test attempted a broker connection to {address[0]}:{address[1]} — "
            "mark it @pytest.mark.live_ib / paper_ib or mock the client"
        )


@pytest.fixture(autouse=True)
def _deny_broker_connections(request, monkeypatch):
    """Deny broker connections for tests without a live_ib/paper_ib marker."""
    if request.node.get_closest_marker("live_ib") or request.node.get_closest_marker("paper_ib"):
        yield
        return

    monkeypatch.setenv("IB_GATEWAY_HOST", "127.0.0.255")
    monkeypatch.setenv("IB_GATEWAY_PORT", "1")

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_connect(sock, address):
        _deny_broker_address(address)
        return real_connect(sock, address)

    def guarded_connect_ex(sock, address):
        try:
            _deny_broker_address(address)
        except BrokerConnectDenied:
            return errno.ECONNREFUSED
        return real_connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    yield
