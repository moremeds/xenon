# Position Rules — Phase 0: DST-Correct Market Hours

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `MonitorDaemon.is_market_hours()`'s hardcoded UTC-5 arithmetic with `zoneinfo.ZoneInfo("America/New_York")` so every `requires_market_hours=True` handler ticks correctly during EDT (mid-March → early November).

**Architecture:** Tiny standalone PR. Lands ahead of the position-rules feature work because the bug already affects `wizard_stop_monitor`, `fill_monitor`, `flex_token_check`, and `preset_rebalance`. One module touched, one unit-test file added, one integration-test file added.

**Tech Stack:** Python 3.13, stdlib `zoneinfo`, pytest, `uv`.

**Spec reference:** `docs/superpowers/specs/2026-05-04-position-rules-design.md` §10.4.1 (Phase 0 prerequisite — DST-correct market hours, codex N-S5) and §13.2 (`test_market_hours_dst.py`, T6 integration-level companion).

---

## Why this lands first

The existing `MonitorDaemon.is_market_hours()` (`src/xenon/monitor_daemon/daemon.py:78-93`) computes ET as UTC + `timedelta(hours=-5)`. That's correct in EST but wrong in EDT — for ~7 months of every year, every market-hours-gated handler starts ticking an hour late and stops an hour late. Without this fix, the new `PositionRulesHandler` would be **asleep for the first hour of each EDT trading day** — unacceptable for a stop-loss engine.

The fix benefits all four existing handlers, so it ships as its own commit on its own PR before the main feature branch.

---

## File Structure

### Modified

| Path                                 | Change                                                            |
| ------------------------------------ | ----------------------------------------------------------------- |
| `src/xenon/monitor_daemon/daemon.py` | Replace `is_market_hours()` body with `zoneinfo`-based conversion |

### Created

| Path                                                                 | Purpose                                                                                        |
| -------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `scripts/tests/test_monitor_daemon/test_market_hours_dst.py`         | Unit test: `is_market_hours()` correctness across EST/EDT boundary                             |
| `scripts/tests/test_monitor_daemon/test_market_hours_integration.py` | Integration test (T6): MonitorDaemon under fake clock dispatches handlers correctly during EDT |

`scripts/tests/test_monitor_daemon/` already exists — append the new files.

---

## Task 1: Unit test — DST boundary cases (red)

**Files:**

- Create: `scripts/tests/test_monitor_daemon/test_market_hours_dst.py`
- Reference: `src/xenon/monitor_daemon/daemon.py:78-93`

- [ ] **Step 1: Write the failing tests**

```python
# scripts/tests/test_monitor_daemon/test_market_hours_dst.py
"""DST-correctness tests for MonitorDaemon.is_market_hours().

Spec §10.4.1 (codex N-S5): the existing UTC-5 arithmetic is wrong during EDT.
This test pins the expected behavior across both timezones, weekdays, and the
standard 9:30-16:00 ET window.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from xenon.monitor_daemon.daemon import MonitorDaemon


def _at_utc(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture
def daemon():
    return MonitorDaemon(state_file=None, respect_market_hours=True)


@pytest.mark.parametrize(
    "utc_dt, expected, description",
    [
        # ── EST (winter, UTC-5) ──
        (_at_utc(2026, 1, 12, 14, 35), True,  "Mon 9:35 ET in EST → open"),
        (_at_utc(2026, 1, 12, 13, 35), False, "Mon 8:35 ET in EST → closed"),
        (_at_utc(2026, 1, 12, 21, 5),  False, "Mon 16:05 ET in EST → closed"),
        # ── EDT (summer, UTC-4) ──
        (_at_utc(2026, 7, 13, 13, 35), True,  "Mon 9:35 ET in EDT → open"),
        (_at_utc(2026, 7, 13, 12, 35), False, "Mon 8:35 ET in EDT → closed"),
        (_at_utc(2026, 7, 13, 20, 5),  False, "Mon 16:05 ET in EDT → closed"),
        # ── DST transition: spring forward (2026-03-08 02:00 EST → 03:00 EDT) ──
        (_at_utc(2026, 3, 9, 14, 35), True,  "Mon 10:35 ET first weekday after spring-forward (EDT now); UTC 14:35 = 10:35 EDT → open"),
        (_at_utc(2026, 3, 9, 13, 35), True,  "Mon 9:35 ET first weekday after spring-forward; UTC 13:35 = 9:35 EDT → open"),
        # ── DST transition: fall back (2026-11-01 02:00 EDT → 01:00 EST) ──
        (_at_utc(2026, 11, 2, 14, 35), True, "Mon 9:35 ET first weekday after fall-back (EST now); UTC 14:35 = 9:35 EST → open"),
        # ── Weekends always closed ──
        (_at_utc(2026, 1, 10, 14, 35), False, "Sat 9:35 ET → closed"),
        (_at_utc(2026, 1, 11, 14, 35), False, "Sun 9:35 ET → closed"),
    ],
)
def test_is_market_hours_dst_correctness(daemon, utc_dt, expected, description):
    with patch("xenon.monitor_daemon.daemon.datetime") as mock_dt:
        mock_dt.now.return_value = utc_dt
        # datetime.fromisoformat etc. should still work
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert daemon.is_market_hours() is expected, description


def test_is_market_hours_uses_new_york_timezone(daemon):
    """Smoke check that the helper does not silently fall back to UTC-5 arithmetic.

    By feeding 13:35 UTC on a winter day (=8:35 EST → closed) and 13:35 UTC on a
    summer day (=9:35 EDT → open), we prove the implementation actually walked
    through zoneinfo, not a static offset.
    """
    winter_morning = _at_utc(2026, 1, 12, 13, 35)
    summer_morning = _at_utc(2026, 7, 13, 13, 35)

    with patch("xenon.monitor_daemon.daemon.datetime") as mock_dt:
        mock_dt.now.return_value = winter_morning
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert daemon.is_market_hours() is False

    with patch("xenon.monitor_daemon.daemon.datetime") as mock_dt:
        mock_dt.now.return_value = summer_morning
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert daemon.is_market_hours() is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest scripts/tests/test_monitor_daemon/test_market_hours_dst.py -xvs`

Expected: failures on every EDT case (UTC-5 arithmetic is one hour off during EDT). Specifically the parametrize rows for `2026-07-13 13:35 UTC` (closed under buggy code, should be open) and `2026-03-09 13:35 UTC` (closed under buggy code, should be open).

- [ ] **Step 3: Commit the red test**

```bash
git add scripts/tests/test_monitor_daemon/test_market_hours_dst.py
git commit -m "test(monitor-daemon): add DST-correctness pin for is_market_hours"
```

---

## Task 2: Implement zoneinfo-based market hours (green)

**Files:**

- Modify: `src/xenon/monitor_daemon/daemon.py:78-93`

- [ ] **Step 1: Replace the body of `is_market_hours()`**

Edit `src/xenon/monitor_daemon/daemon.py` — change `is_market_hours()` from the existing UTC-5 arithmetic to:

```python
def is_market_hours(self) -> bool:
    """Check if current time is within US market hours (DST-correct)."""
    from zoneinfo import ZoneInfo

    et_now = datetime.now(ZoneInfo("America/New_York"))

    return self._is_market_hours_time(
        et_now.hour,
        et_now.minute,
        et_now.weekday(),
    )
```

The local import keeps the module top imports unchanged (so existing call-sites that monkey-patch `xenon.monitor_daemon.daemon.datetime` still work in tests).

- [ ] **Step 2: Run unit tests to verify they pass**

Run: `uv run pytest scripts/tests/test_monitor_daemon/test_market_hours_dst.py -xvs`

Expected: all rows green.

- [ ] **Step 3: Run the broader monitor_daemon test suite**

Run: `uv run pytest scripts/tests/test_monitor_daemon/ -xvs`

Expected: all green. No existing test should regress.

- [ ] **Step 4: Commit the fix**

```bash
git add src/xenon/monitor_daemon/daemon.py
git commit -m "fix(monitor-daemon): use zoneinfo for is_market_hours (DST-correct)"
```

---

## Task 3: Integration test — MonitorDaemon dispatches under fake EDT clock

**Files:**

- Create: `scripts/tests/test_monitor_daemon/test_market_hours_integration.py`
- Reference: spec §13.2 T6 — "include an integration-level test that constructs a `MonitorDaemon` instance with a fake clock pinned to a March 2026 EDT date."

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_monitor_daemon/test_market_hours_integration.py
"""Integration test for spec §13.2 T6.

Pins MonitorDaemon's clock to EDT (March 2026, post-spring-forward) and verifies
that `run_once()` actually invokes a `requires_market_hours=True` handler during
9:30-16:00 ET. Pure unit-test of `is_market_hours()` is necessary but not
sufficient — this catches "is_market_hours returned True but the calling code
converts the timestamp incorrectly" regressions.
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
    """Patch datetime.now in monitor_daemon.daemon to return a fixed UTC instant."""
    return patch("xenon.monitor_daemon.daemon.datetime", **{
        "now.return_value": utc_dt,
        "side_effect": lambda *a, **kw: datetime(*a, **kw),
    })


def test_run_once_executes_handler_during_edt_open(daemon):
    """On 2026-03-09 (Monday after spring-forward), 14:35 UTC = 10:35 EDT → handler runs."""
    handler = FakeMarketHoursHandler()
    daemon.register(handler)
    edt_open = datetime(2026, 3, 9, 14, 35, tzinfo=timezone.utc)
    with _patch_now(edt_open):
        results = daemon.run_once()
    assert handler.executed == 1
    assert "fake_market_hours_test" in results


def test_run_once_skips_handler_during_edt_closed(daemon):
    """On 2026-03-09, 13:00 UTC = 9:00 EDT → market closed, handler skipped."""
    handler = FakeMarketHoursHandler()
    daemon.register(handler)
    edt_closed = datetime(2026, 3, 9, 13, 0, tzinfo=timezone.utc)
    with _patch_now(edt_closed):
        results = daemon.run_once()
    assert handler.executed == 0
    assert "fake_market_hours_test" not in results


def test_run_once_skips_handler_during_edt_after_close(daemon):
    """On 2026-07-13, 21:00 UTC = 17:00 EDT → after close, handler skipped."""
    handler = FakeMarketHoursHandler()
    daemon.register(handler)
    edt_late = datetime(2026, 7, 13, 21, 0, tzinfo=timezone.utc)
    with _patch_now(edt_late):
        results = daemon.run_once()
    assert handler.executed == 0
```

- [ ] **Step 2: Run integration tests to verify they pass**

Run: `uv run pytest scripts/tests/test_monitor_daemon/test_market_hours_integration.py -xvs`

Expected: all 3 tests green. (The implementation from Task 2 is already correct; this test is the integration-level proof per T6.)

- [ ] **Step 3: Commit**

```bash
git add scripts/tests/test_monitor_daemon/test_market_hours_integration.py
git commit -m "test(monitor-daemon): integration test for EDT-correct dispatch"
```

---

## Task 4: Verify all market-hours-gated handlers benefit (no code change)

**Files:**

- Reference (read-only): `src/xenon/monitor_daemon/handlers/wizard_stop_monitor.py`, `fill_monitor.py`, `flex_token_check.py`, `preset_rebalance.py`

- [ ] **Step 1: Confirm the four existing handlers all flow through `is_market_hours()`**

Run: `grep -n "requires_market_hours" src/xenon/monitor_daemon/handlers/*.py`

Expected output: each handler module declares `requires_market_hours = True` (or relies on the BaseHandler default of `True`). All of them are dispatched through `MonitorDaemon._handler_can_run_now()` (`daemon.py:95-106`), which now uses the corrected `is_market_hours()`.

No further code changes needed — the fix is universal by construction.

- [ ] **Step 2: Run the full Python affected-tests suite to verify no regression**

Run: `uv run python scripts/infra/dev/run_pytest_affected.py`

Expected: green. If any existing market-hours test was implicitly relying on the UTC-5 bug (e.g., a fixture pinned at a UTC time that only worked because of the missing DST shift), it surfaces here. Fix at the test-fixture level — the production code is now correct.

---

## Task 5: Open the PR

- [ ] **Step 1: Push the branch and open the PR**

```bash
git push -u origin <current-branch>
gh pr create --title "fix(monitor-daemon): use zoneinfo for DST-correct market hours" --body "$(cat <<'EOF'
## Summary

Replaces `MonitorDaemon.is_market_hours()`'s hardcoded UTC-5 arithmetic with `zoneinfo.ZoneInfo("America/New_York")`. Without this fix, every `requires_market_hours=True` handler — `wizard_stop_monitor`, `fill_monitor`, `flex_token_check`, `preset_rebalance` — starts and ends an hour late during EDT (mid-March → early November).

This is Phase 0 of the position-rules engine work (spec §10.4.1, codex review N-S5), but ships as its own PR because the bug pre-exists the new feature.

## Changes

- `src/xenon/monitor_daemon/daemon.py` — `is_market_hours()` now uses `zoneinfo`
- `scripts/tests/test_monitor_daemon/test_market_hours_dst.py` — unit pin across EST/EDT boundaries
- `scripts/tests/test_monitor_daemon/test_market_hours_integration.py` — integration test running `MonitorDaemon.run_once()` under a faked EDT clock

## Test plan

- [ ] `uv run pytest scripts/tests/test_monitor_daemon/ -xvs` green
- [ ] `uv run python scripts/infra/dev/run_pytest_affected.py` green
- [ ] CI green
EOF
)"
```

- [ ] **Step 2: Confirm CI green, then merge.**

This unblocks Plan 2 (backend infra). Plan 2's pure-module work doesn't depend on the DST fix, but Plan 3's `PositionRulesHandler` does — so Phase 0 must be merged before Plan 3 lands.

---

## Self-Review

**Spec coverage:**

- §10.4.1 Phase 0 prerequisite ✓ (Task 2)
- §13.2 `test_market_hours_dst.py` four cases (winter open, winter closed, summer open, summer closed) ✓ (Task 1)
- §13.2 T6 integration-level test under fake clock March 2026 EDT ✓ (Task 3)
- "Existing handlers (wizard_stop_monitor, fill_monitor) verified to tick at the correct ET boundaries year-round" §20 ✓ (Task 4)

**Placeholder scan:** none — every step has full code or a concrete command.

**Type consistency:** `MonitorDaemon`, `BaseHandler`, the parameter shape of `_is_market_hours_time(hour, minute, weekday)` — all match `daemon.py:55-76`.

**One potential hazard:** the unit-test `_patch_now` helper relies on patching `xenon.monitor_daemon.daemon.datetime` at module level. The implementation keeps `datetime` as a top-level import, so this works. If a future refactor moves the `datetime` import inside the function, the patch path needs updating; the integration test (Task 3) catches that drift because it would silently no-op.

---
