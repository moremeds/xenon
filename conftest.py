"""Repo-root pytest config.

Set XENON_AUTH_ALLOW_DEV_OPEN=1 for the whole test session so the fail-closed
auth middleware allows TestClient requests (whose client.host is 'testclient',
not localhost). Auth-specific tests override this via patch.dict to observe
gating. Never set in production.

Broker isolation: tests not marked ``live_ib`` / ``paper_ib`` must never open
a broker connection — not at import time, not in session fixtures, not in
teardown. The guard is installed when THIS conftest is imported (before
collection and any fixture) and stays patched for the life of the process.
Four layers enforce it:

* boundary allowlist — ``XENON_TEST_BROKER_GUARD=1`` is set here at import
  time and inherited by every child process. Every ``ib_async`` connect call
  site routes through ``xenon.utils.ib_connection.assert_broker_endpoint_allowed``,
  which refuses ALL (host, port) endpoints except the exact value of
  ``XENON_TEST_BROKER_ALLOW`` while the guard env is set. In production the
  env is absent and the helper is a no-op.
* socket denylist — ``socket.socket.connect``/``connect_ex`` are patched at
  module level below: while the guard env is set, destination ports
  4001/4002/7496/7497 (IB Gateway + TWS, live and paper) are refused on ANY
  host except the exact allowlisted endpoint. Catches paths that bypass the
  boundary helper (raw sockets, health probes, third-party dials), and works
  even where module-level ``DEFAULT_*`` constants bound a stale env at
  import time. ``connect`` raises ``BrokerConnectDenied`` — a
  ``ConnectionRefusedError`` so probe-style callers (``ib_gateway._port_listening``
  and other ``except OSError`` health checks) still see a plain "closed"
  verdict; ``connect_ex`` returns ECONNREFUSED.
* env deny — unmarked tests also get ``IB_GATEWAY_HOST``/``IB_GATEWAY_PORT``
  pointed at a dead loopback per test so spawned subprocess CLIs (which
  re-read env at import time) fail fast. Note ``IB_GATEWAY_PORT`` is legacy:
  the port the code actually dials comes from ``XENON_TRADING_MODE`` via
  ``trading_mode.EXPECTED_PORT`` (paper→4002, live→4001); the env var is
  still set for any path that reads it.
* child-process guard — ``scripts/tests/broker_guard/sitecustomize.py`` is
  prepended to ``PYTHONPATH`` at conftest load, so every spawned Python child
  auto-imports an identical socket guard at interpreter startup (also gated
  on ``XENON_TEST_BROKER_GUARD=1``).

``paper_ib`` tests set ``XENON_TEST_BROKER_ALLOW=127.0.0.1:4002`` for the
test's duration — the only per-test exception — which all layers honor:
only that exact endpoint passes. ``live_ib`` tests delete the guard env for
the test's duration, making every layer a no-op — they are fully exempt by
design because ``-m live_ib`` is an explicit operator opt-in (live tests are
deselected by the default ``addopts`` and never run without it).
"""

import errno
import os
import socket
from pathlib import Path

import pytest

# Imported for fixture registration: `_pg_schema_guard` depends on it so the
# schema check runs after the per-worker DB rewrite under pytest-xdist.
from xenon._test_db import (
    UnsafeTestDatabaseError,
    _ensure_worker_db,  # noqa: F401
    bind_database_url_to_test_db,
)

# Prod-DB guard (INV-test-db-leak): point DATABASE_URL at the test DB NOW —
# before collection imports any xenon module — so import-time singletons
# (e.g. `uw_api_stats.stats`) and subprocess CLIs never bind to the
# exported/.env DATABASE_URL (prod `core_dev` on a dev machine). Refuse the
# session when the test DB could be that database.
try:
    bind_database_url_to_test_db()
except UnsafeTestDatabaseError as _exc:
    pytest.exit(str(_exc), returncode=pytest.ExitCode.USAGE_ERROR)

os.environ.setdefault("XENON_AUTH_ALLOW_DEV_OPEN", "1")

# Session-level test mode: paper. Forces EXPECTED_PORT=4002 anywhere
# `XENON_TRADING_MODE` is read and is inherited by subprocesses. Hard-set —
# `.env` carries `live`, which must never leak into test children.
os.environ["XENON_TRADING_MODE"] = "paper"

# PR-10 clientId block: pin base 0 so a shell that exported dev.sh's 100 does
# not shift every expected id. Tests that need another base set it per test.
# The API startup base↔DB check accepts base 0 with the test DB only while
# XENON_TEST_BROKER_GUARD=1 (below) — the scoped test mapping.
os.environ["XENON_IB_CLIENT_ID_BASE"] = "0"

# Test-session broker guard: activates the boundary allowlist in
# `xenon.utils.ib_connection` and both socket layers for the whole process.
# Hard-set at import — before collection, fixtures, or test-module imports —
# and inherited by every spawned child. `live_ib` tests delete it per test.
os.environ["XENON_TEST_BROKER_GUARD"] = "1"

# Child-process broker guard: prepending this dir to PYTHONPATH makes Python
# auto-import `sitecustomize.py` (which patches socket.connect/connect_ex) at
# interpreter startup in every spawned subprocess — the socket-level mirror of
# the in-process `_deny_broker_connections` fixture below.
_BROKER_GUARD_DIR = str(Path(__file__).resolve().parent / "scripts" / "tests" / "broker_guard")
_pythonpath = os.environ.get("PYTHONPATH", "")
if _BROKER_GUARD_DIR not in _pythonpath.split(os.pathsep):
    os.environ["PYTHONPATH"] = _BROKER_GUARD_DIR + os.pathsep + _pythonpath if _pythonpath else _BROKER_GUARD_DIR

# Broker API ports: IB Gateway live 4001 / paper 4002, TWS live 7496 /
# paper 7497. While the guard env is set, a dial to any of them on any host
# (except the exact allowlisted endpoint) fails loudly.
_BROKER_PORTS = frozenset({4001, 4002, 7496, 7497})

# paper_ib tests get exactly one allowed endpoint; must match the value the
# child-process sitecustomize guard and the boundary helper check.
_BROKER_GUARD_ENV = "XENON_TEST_BROKER_GUARD"
_BROKER_ALLOW_ENV = "XENON_TEST_BROKER_ALLOW"
_BROKER_ALLOWED_ENDPOINT = ("127.0.0.1", 4002)


@pytest.fixture(scope="session", autouse=True)
def _pg_schema_guard(_ensure_worker_db):  # noqa: F811 — param shadows the import on purpose
    """Fail loudly when the PG test DB is reachable but schema-incomplete.

    A reachable but incomplete schema is not the offline case — error once
    per worker with the missing table names instead of failing every
    TRUNCATE (`truncate_all_xenon_tables` re-raises non-connection errors). Offline (unreachable) stays a
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


def _broker_allowed(address: object) -> bool:
    return (
        os.environ.get(_BROKER_ALLOW_ENV) == "127.0.0.1:4002"
        and isinstance(address, tuple)
        and tuple(address[:2]) == _BROKER_ALLOWED_ENDPOINT
    )


def _deny_broker_address(address: object) -> None:
    if (
        isinstance(address, tuple)
        and len(address) >= 2
        and address[1] in _BROKER_PORTS
        and not _broker_allowed(address)
    ):
        raise BrokerConnectDenied(
            f"test attempted a broker connection to {address[0]}:{address[1]} — "
            "mark it @pytest.mark.live_ib / paper_ib or mock the client"
        )


# ---------------------------------------------------------------------------
# Import-time parent socket guard — patched here, at conftest import, so the
# deny is in place before collection, test-module imports, and any fixture;
# it stays installed through session teardown for the life of the process.
# The deny decision is re-evaluated per call from the env: XENON_TEST_BROKER_
# GUARD=1 (set above) arms it, XENON_TEST_BROKER_ALLOW whitelists one exact
# endpoint, and `live_ib` tests disarm it by deleting the guard env.
# ---------------------------------------------------------------------------
_REAL_SOCK_CONNECT = socket.socket.connect
_REAL_SOCK_CONNECT_EX = socket.socket.connect_ex


def _guarded_sock_connect(sock, address):
    if os.environ.get(_BROKER_GUARD_ENV) == "1":
        _deny_broker_address(address)
    return _REAL_SOCK_CONNECT(sock, address)


def _guarded_sock_connect_ex(sock, address):
    if os.environ.get(_BROKER_GUARD_ENV) == "1":
        try:
            _deny_broker_address(address)
        except BrokerConnectDenied:
            return errno.ECONNREFUSED
    return _REAL_SOCK_CONNECT_EX(sock, address)


socket.socket.connect = _guarded_sock_connect
socket.socket.connect_ex = _guarded_sock_connect_ex


@pytest.fixture(autouse=True)
def _deny_broker_connections(request, monkeypatch):
    """Per-test broker-connection policy. The socket guard itself is installed
    at conftest import time (module level above) and never removed — this
    fixture only adjusts the env the guards evaluate per call.

    live_ib: fully exempt — deletes XENON_TEST_BROKER_GUARD so the parent
      socket layer, the child sitecustomize, and the boundary helper all
      become no-ops for this test and its children. Exemption is deliberate:
      live tests are operator opt-in (`pytest -m live_ib`) and deselected by
      the default addopts.
    paper_ib: sets XENON_TEST_BROKER_ALLOW=127.0.0.1:4002 — the only
      endpoint any layer lets through; all other broker ports/hosts refused.
    unmarked: no allowlist + IB_GATEWAY_* env poison so subprocess CLIs that
      re-read env at import fail fast.
    """
    if request.node.get_closest_marker("live_ib"):
        monkeypatch.delenv(_BROKER_GUARD_ENV, raising=False)
        monkeypatch.delenv(_BROKER_ALLOW_ENV, raising=False)
        yield
        return

    if request.node.get_closest_marker("paper_ib"):
        # No env poisoning — the test may legitimately reach the paper gw;
        # every guard layer still refuses everything but 127.0.0.1:4002.
        monkeypatch.setenv(_BROKER_ALLOW_ENV, "127.0.0.1:4002")
    else:
        monkeypatch.delenv(_BROKER_ALLOW_ENV, raising=False)
        monkeypatch.setenv("IB_GATEWAY_HOST", "127.0.0.255")
        monkeypatch.setenv("IB_GATEWAY_PORT", "1")
    yield
