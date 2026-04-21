"""Tests for the owner-clientId registry in ib_pool (F5.2).

Serializes short-lived IB connects that share a clientId slot — primarily
the naked-short audit (clientId 25) and cancel subprocess (20-49 range).
"""

from __future__ import annotations

import threading
import time

import pytest

from xenon.api.ib_pool import ClientIdBusy, _busy_owners, acquire_owner


@pytest.fixture(autouse=True)
def _clear_registry():
    """Ensure a clean registry between tests (module-level state)."""
    _busy_owners.clear()
    yield
    _busy_owners.clear()


def test_audit_holds_client_25_blocks_cancel_same_slot():
    """Audit holding clientId 25 must block another caller wanting 25."""
    acquired_a = threading.Event()
    release_a = threading.Event()
    errors: list[BaseException] = []

    def thread_a():
        try:
            with acquire_owner(25, timeout_ms=1000):
                acquired_a.set()
                release_a.wait(timeout=2.0)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=thread_a)
    t.start()
    assert acquired_a.wait(timeout=1.0), "thread A never acquired"

    # Thread B tries the same slot with a tight timeout — must raise.
    with pytest.raises(ClientIdBusy) as excinfo:
        with acquire_owner(25, timeout_ms=100):
            pytest.fail("thread B should not have acquired clientId 25")
    assert excinfo.value.client_id == 25

    release_a.set()
    t.join(timeout=2.0)
    assert not errors, f"thread A errored: {errors}"


def test_audit_holds_25_does_not_block_cancel_on_different_slot():
    """Holding 25 must not block acquisition of a different slot (27)."""
    acquired_a = threading.Event()
    release_a = threading.Event()

    def thread_a():
        with acquire_owner(25, timeout_ms=1000):
            acquired_a.set()
            release_a.wait(timeout=2.0)

    t = threading.Thread(target=thread_a)
    t.start()
    assert acquired_a.wait(timeout=1.0)

    # Different slot — must succeed immediately.
    start = time.monotonic()
    with acquire_owner(27, timeout_ms=100):
        elapsed = time.monotonic() - start
        assert elapsed < 0.1, f"acquire on different slot was slow: {elapsed}s"

    release_a.set()
    t.join(timeout=2.0)


def test_acquire_releases_on_exit():
    """After the context exits, the same clientId must be re-acquirable."""
    with acquire_owner(25, timeout_ms=100):
        pass
    # Re-acquire right after — must succeed.
    with acquire_owner(25, timeout_ms=100):
        pass


def test_acquire_raises_clientid_busy_after_timeout():
    """When slot stays held past the deadline, ClientIdBusy is raised."""
    acquired_a = threading.Event()
    release_a = threading.Event()

    def thread_a():
        with acquire_owner(25, timeout_ms=2000):
            acquired_a.set()
            release_a.wait(timeout=3.0)

    t = threading.Thread(target=thread_a)
    t.start()
    assert acquired_a.wait(timeout=1.0)

    start = time.monotonic()
    with pytest.raises(ClientIdBusy):
        with acquire_owner(25, timeout_ms=100):
            pytest.fail("should not acquire")
    elapsed = time.monotonic() - start
    # Deadline ~100ms, allow 50ms poll tolerance on either side.
    assert 0.08 <= elapsed <= 0.3, f"timeout not respected: {elapsed}s"

    release_a.set()
    t.join(timeout=2.0)


def test_nested_acquire_different_ids_ok():
    """Same thread may acquire two different clientIds concurrently."""
    with acquire_owner(25, timeout_ms=100):
        with acquire_owner(26, timeout_ms=100):
            assert 25 in _busy_owners
            assert 26 in _busy_owners
        assert 26 not in _busy_owners
        assert 25 in _busy_owners
    assert 25 not in _busy_owners
