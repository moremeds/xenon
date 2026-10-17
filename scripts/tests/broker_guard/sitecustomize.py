"""Broker-connection guard for test-session CHILD PROCESSES.

The repo-root conftest prepends this directory to PYTHONPATH before any test
spawns a subprocess, so Python auto-imports this module at interpreter
startup in every child (``subprocess``, spawned CLIs, multiprocessing
workers using spawn). It patches ``socket.socket.connect`` / ``connect_ex``
to refuse the broker API ports — IB Gateway 4001 (live) / 4002 (paper) and
TWS 7496 (live) / 7497 (paper) — on ANY host.

The guard is armed only while ``XENON_TEST_BROKER_GUARD`` equals ``"1"`` —
the root conftest sets it for the pytest session and children inherit it.
Outside a test session the env is absent and the patched functions are a
pure pass-through, so production behavior is unchanged even if this file
somehow lands on PYTHONPATH.

Escape hatch: when ``XENON_TEST_BROKER_ALLOW`` equals exactly
``"127.0.0.1:4002"`` (set by the ``paper_ib`` marker in the parent pytest),
a connect to exactly ``("127.0.0.1", 4002)`` delegates to the real socket.
Everything else broker-port-bound is denied regardless. ``live_ib`` tests
delete the guard env, so their children connect unguarded — ``-m live_ib``
is an explicit operator opt-in.
"""

import errno
import os
import socket
import sys
from pathlib import Path

# Chain to a sitecustomize that ours shadows via PYTHONPATH precedence (e.g.
# a venv-level sitecustomize). Checked 2026-10: .venv ships none — this stays
# as a safeguard so adding one later silently keeps working.
_HERE = Path(__file__).resolve().parent
for _p in sys.path:
    if _p:
        _cand = Path(_p).resolve() / "sitecustomize.py"
        if _cand.is_file() and _cand.parent != _HERE:
            exec(compile(_cand.read_bytes(), str(_cand), "exec"), {"__file__": str(_cand)})
            break


_BROKER_PORTS = frozenset({4001, 4002, 7496, 7497})
_GUARD_ENV = "XENON_TEST_BROKER_GUARD"
_ALLOW_ENV = "XENON_TEST_BROKER_ALLOW"
_ALLOWED_ENDPOINT = ("127.0.0.1", 4002)


class BrokerConnectDenied(ConnectionRefusedError):
    """Refused broker-port connect inside a test child process.

    ConnectionRefusedError (not RuntimeError) so probe-style callers that
    catch OSError keep working; the message names the fix.
    """


def _denied(address) -> bool:
    if os.environ.get(_GUARD_ENV) != "1":
        return False
    if not (isinstance(address, tuple) and len(address) >= 2 and address[1] in _BROKER_PORTS):
        return False
    if (
        os.environ.get(_ALLOW_ENV) == "127.0.0.1:4002"
        and tuple(address[:2]) == _ALLOWED_ENDPOINT
    ):
        return False
    return True


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _guarded_connect(sock, address):
    if _denied(address):
        raise BrokerConnectDenied(
            f"test child process attempted a broker connection to "
            f"{address[0]}:{address[1]} — refuse via XENON_TEST_BROKER_ALLOW is "
            "only '127.0.0.1:4002' (paper_ib); live endpoints are never allowed"
        )
    return _real_connect(sock, address)


def _guarded_connect_ex(sock, address):
    if _denied(address):
        return errno.ECONNREFUSED
    return _real_connect_ex(sock, address)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
