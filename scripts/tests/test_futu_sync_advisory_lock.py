"""Singleflight: concurrent sync calls return in_progress; only one runs.

The advisory lock is held on a sync SQLAlchemy connection. We simulate
contention by patching _run_sync_under_lock to hold a barrier while a
second invocation tries to acquire the lock — it should short-circuit.
"""

import threading
from unittest.mock import patch

from xenon.api.services.futu_history import futu_history_sync
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_concurrent_invocations_singleflight(clean_scope):
    started = threading.Event()
    release = threading.Event()
    second_result_holder: dict = {}

    def fake_run(*args, **kwargs):
        started.set()
        release.wait(timeout=5.0)
        return {
            "status": "ok",
            "fills_added": 0,
            "cash_flows_added": 0,
            "nav_rows_added": 0,
            "warnings": [],
        }

    with patch(
        "xenon.api.services.futu_history._run_sync_under_lock",
        side_effect=fake_run,
    ):
        t1 = threading.Thread(
            target=futu_history_sync,
            kwargs={"scope": SCOPE, "since": None},
        )
        t1.start()
        assert started.wait(timeout=5.0), "first invocation never started"

        # Second call must short-circuit with status='in_progress'.
        second_result_holder["r"] = futu_history_sync(scope=SCOPE, since=None)
        release.set()
        t1.join(timeout=5.0)

    assert second_result_holder["r"]["status"] == "in_progress"
