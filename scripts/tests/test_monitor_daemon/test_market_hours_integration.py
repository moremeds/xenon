"""Integration test for spec §13.2 T6.

Pins MonitorDaemon's clock to EDT (March 2026, post-spring-forward) and verifies
that run_once() actually invokes a requires_market_hours=True handler during
9:30-16:00 ET.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import patch

import pytest

from xenon.monitor_daemon.daemon import MonitorDaemon
from xenon.monitor_daemon.handlers.base import BaseHandler


class FakeMarketHoursHandler(BaseHandler):
    name = "fake_market_hours_test"
    interval_seconds = 1
    requires_market_hours = True

    def __init__(self) -> None:
        super().__init__()
        self.executed = 0

    def execute(self) -> Dict[str, Any]:
        self.executed += 1
        return {"executed": self.executed}


@pytest.fixture
def daemon():
    return MonitorDaemon(state_file=None, respect_market_hours=True, loop_interval=1)


def _patch_now(utc_dt):
    """Patch datetime.now in monitor_daemon.daemon to return a fixed instant."""

    def _now(tz=None):
        return utc_dt.astimezone(tz) if tz else utc_dt.replace(tzinfo=None)

    return patch(
        "xenon.monitor_daemon.daemon.datetime",
        **{
            "now.side_effect": _now,
            "side_effect": lambda *a, **kw: datetime(*a, **kw),
        },
    )


def test_run_once_executes_handler_during_edt_open(daemon):
    """On 2026-03-09, 14:35 UTC = 10:35 EDT, so the handler runs."""
    handler = FakeMarketHoursHandler()
    daemon.register(handler)

    edt_open = datetime(2026, 3, 9, 14, 35, tzinfo=timezone.utc)

    with _patch_now(edt_open):
        results = daemon.run_once()

    assert handler.executed == 1
    assert "fake_market_hours_test" in results


def test_run_once_skips_handler_during_edt_closed(daemon):
    """On 2026-03-09, 13:00 UTC = 9:00 EDT, so the handler is skipped."""
    handler = FakeMarketHoursHandler()
    daemon.register(handler)

    edt_closed = datetime(2026, 3, 9, 13, 0, tzinfo=timezone.utc)

    with _patch_now(edt_closed):
        results = daemon.run_once()

    assert handler.executed == 0
    assert "fake_market_hours_test" not in results


def test_run_once_skips_handler_during_edt_after_close(daemon):
    """On 2026-07-13, 21:00 UTC = 17:00 EDT, so the handler is skipped."""
    handler = FakeMarketHoursHandler()
    daemon.register(handler)

    edt_late = datetime(2026, 7, 13, 21, 0, tzinfo=timezone.utc)

    with _patch_now(edt_late):
        results = daemon.run_once()

    assert handler.executed == 0
    assert "fake_market_hours_test" not in results
