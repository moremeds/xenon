# UW Analyze Overhaul — Tribunal Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all 17 issues from the codex/gemini/claude tribunal review of the `feat/uw-analyze-overhaul` branch so that the UW Analyze portfolio dashboard actually surfaces OI changes and unusual-flow alerts (the user's reported symptom), and so the rest of the milestones (M1–M8) match the original plan in `~/.claude/plans/mellow-skipping-thunder.md`.

**Architecture:** Four sequential clusters, each independently mergeable. Cluster 1 unblocks the alert/OI surface (the symptom). Cluster 2 fixes daily-job correctness. Cluster 3 cleans architectural drift (legacy cache, serial loop, lifespan). Cluster 4 closes the test debt. TDD throughout. One feature per commit.

**Tech Stack:** Python 3.13 (FastAPI, asyncio, pytest), TypeScript (Next.js 15 App Router, Vitest), UnusualWhales API.

**Source artifacts:**

- Original plan: `~/.claude/plans/mellow-skipping-thunder.md`
- Tribunal review report: in-conversation, summarised below per cluster
- Spec: `docs/superpowers/specs/2026-04-08-uw-analyze-overhaul-design.md`

---

## File Structure

**New files:**

- `scripts/api/services/uw_analyze_oi_snapshots.py` — raw daily OI snapshot persistence + 5-day rolling retention (M4 gap)
- `scripts/tests/test_uw_analyze_oi_snapshots.py`
- `web/tests/useUwPortfolio.test.ts`
- `web/tests/GexProfileChart.test.tsx`
- `scripts/tests/test_uw_analyze_integration.py` — full-stack route → cache → diff → flow log

**Modified files (cluster → file map):**

- **Cluster 1:** `scripts/api/services/uw_analyze_cache.py`, `scripts/api/routes/uw_analyze.py`, `scripts/api/services/uw_analyze_oi_tracker.py`, `scripts/tests/test_uw_analyze_routes.py`
- **Cluster 2:** `scripts/api/services/uw_analyze_flow_tracker.py`, `scripts/api/services/uw_analyze_daily_job.py`, `scripts/api/server.py`
- **Cluster 3:** `scripts/api/routes/uw_analyze.py`, `scripts/api/server.py`
- **Cluster 4:** `scripts/tests/test_uw_analyze_diff.py`, `scripts/tests/test_ticker_data.py` (or `test_analysis_ticker_data.py`), `scripts/tests/test_uw_analyze_daily_job.py`

---

# Cluster 1 — Unblock the Alert/OI Surface (the user-visible bug)

> **Why first:** This is the symptom. Three independently-broken pieces (flow_alerts not in snapshot, OI snapshot file missing, capture-on-read causes phantoms) combine to make the entire alert surface dead. Fix all three, then the dashboard shows real data.

## Task 1: Thread `flow_alerts` into the cached snapshot

**Files:**

- Modify: `scripts/api/services/uw_analyze_cache.py` — `build_snapshot` + `derive_from_report`
- Modify: `scripts/api/routes/uw_analyze.py` — `_runner` (must pass `td.flow_alerts` through)
- Modify: `scripts/tests/test_uw_analyze_routes.py` — rebuild fixture from real `_serialize_report` shape

**Background:** `AnalysisReport` does NOT carry `flow_alerts`; `TickerData` does (`scripts/analysis/models.py:81`). `_serialize_report()` calls `asdict(AnalysisReport)` so `flow_alerts` is stripped. The route reads `snap.report.flow_alerts` which is always `None`, making `capture_from_changes()` return `[]` every time. Fixture in `test_uw_analyze_routes.py:63` injects a fake `report["flow_alerts"]`, masking the bug.

**Resolution:** Store `flow_alerts` as a top-level snapshot key (alongside `report` / `display` / `derived`), not inside `report`. Cache and route both read it from there.

- [ ] **Step 1.1: Write failing test for snapshot containing flow_alerts**

Create or extend `scripts/tests/test_uw_analyze_cache.py`:

```python
def test_build_snapshot_includes_flow_alerts():
    from api.services.uw_analyze_cache import build_snapshot
    report = {"price": 100.0, "regime": {"gex_sign": "POSITIVE"}}
    display = {"max_pain": 99.0}
    flow_alerts = [{"option_type": "call", "strike": 100, "expiration_date": "2026-05-15", "total_premium": 6_000_000}]
    snap = build_snapshot("AAPL", report, display, flow_alerts=flow_alerts)
    assert snap["flow_alerts"] == flow_alerts
    assert snap["derived"]["spot"] == 100.0
```

- [ ] **Step 1.2: Run test, verify it fails**

```bash
pytest scripts/tests/test_uw_analyze_cache.py::test_build_snapshot_includes_flow_alerts -v
```

Expected: FAIL (`build_snapshot()` got an unexpected keyword argument 'flow_alerts')

- [ ] **Step 1.3: Update `build_snapshot` to accept and store `flow_alerts`**

In `scripts/api/services/uw_analyze_cache.py`:

```python
def build_snapshot(ticker: str, report: dict, display: dict, flow_alerts: Optional[list[dict]] = None) -> dict:
    return {
        "ticker": ticker,
        "ts": _now_iso(),
        "report": report,
        "display": display,
        "flow_alerts": flow_alerts or [],
        "derived": derive_from_report(report, display),
    }
```

- [ ] **Step 1.4: Update `_runner` in route to pass `td.flow_alerts`**

In `scripts/api/routes/uw_analyze.py`, replace the existing `_runner`:

```python
async def _runner(ticker: str) -> tuple[dict, dict, list[dict]]:
    """Adapt scripts.uw_analyze.run_analysis_with_data into the cache's
    `(report_dict, display_dict, flow_alerts)` contract."""
    report, td = await asyncio.wait_for(
        asyncio.to_thread(run_analysis_with_data, ticker),
        timeout=60.0,
    )
    return _serialize_report(report), _td_to_display(td).model_dump(), list(td.flow_alerts or [])
```

- [ ] **Step 1.5: Update `UwAnalyzeCache.get_or_run` to consume the 3-tuple**

In `scripts/api/services/uw_analyze_cache.py`, change the runner contract:

```python
async def get_or_run(
    self,
    ticker: str,
    *,
    runner: Callable[[str], Awaitable[tuple[dict, dict, list[dict]]]],
    force: bool = False,
    sources: Iterable[Source] = (),
) -> dict:
    ...
    async with self._semaphore:
        logger.info("uw_analyze_cache running analysis for %s (force=%s)", ticker, force)
        report, display, flow_alerts = await asyncio.wait_for(runner(ticker), timeout=_RUN_TIMEOUT_S)

    new_snapshot = build_snapshot(ticker, report, display, flow_alerts=flow_alerts)
    ...
```

Update existing cache tests' `runner` fakes to return a 3-tuple. The minimal patch:

```python
# in test_uw_analyze_cache.py — anywhere a fake runner returns (report, display)
async def fake_runner(ticker):
    return ({"price": 100}, {"max_pain": 99}, [])  # add the third element
```

- [ ] **Step 1.6: Update `/portfolio` route to read from `snap["flow_alerts"]`**

In `scripts/api/routes/uw_analyze.py::uw_analyze_portfolio`:

```python
        # Capture/upsert any sweep events into the flow log
        flow_alerts = snap.get("flow_alerts") or None  # <-- top-level, not snap.report
        underlying = (snap.get("derived") or {}).get("spot")
```

- [ ] **Step 1.7: Rebuild fixture in `test_uw_analyze_routes.py` from real shape**

Replace the hand-fabricated `report["flow_alerts"]` injection with:

```python
async def fake_runner(ticker):
    report = {
        "ticker": ticker,
        "price": 100.0,
        "regime": {"gex_sign": "POSITIVE"},
        "scores": {"flow": 70, "bias": "Long", "grade": "A"},
    }
    display = {
        "max_pain": 99.0,
        "iv_rank": 50.0,
        "net_call_premium": 1_000_000,
        "net_put_premium": -500_000,
    }
    flow_alerts = [
        {
            "option_type": "call",
            "strike": 105,
            "expiration_date": "2026-05-15",
            "total_premium": 7_000_000,
            "open_interest": 1500,
            "volume": 4000,
            "mid": 2.50,
        }
    ]
    return report, display, flow_alerts
```

- [ ] **Step 1.8: Run all touched tests, verify green**

```bash
pytest scripts/tests/test_uw_analyze_cache.py scripts/tests/test_uw_analyze_routes.py -v
```

Expected: PASS

- [ ] **Step 1.9: Commit**

```bash
git add scripts/api/services/uw_analyze_cache.py scripts/api/routes/uw_analyze.py scripts/tests/test_uw_analyze_cache.py scripts/tests/test_uw_analyze_routes.py
git commit -m "fix(uw-analyze): thread flow_alerts into cache snapshot (cluster1.1)"
```

---

## Task 2: Implement raw OI snapshot persistence + on-demand fetch

**Files:**

- Create: `scripts/api/services/uw_analyze_oi_snapshots.py`
- Create: `scripts/tests/test_uw_analyze_oi_snapshots.py`
- Modify: `scripts/api/routes/uw_analyze.py` (`/portfolio` calls the OI fetch on demand for stale entries)

**Background:** Plan M4 required `snapshot_oi(ticker) → data/uw_oi_snapshots/<ticker>.json` (5-day rolling). The current implementation has only `fetch_and_diff()` over UW's pre-diffed endpoint, called from the daily cron at 15:50 ET. So `oi_baseline` is `None` until the cron fires, and `/portfolio` shows zero OI deltas all morning.

**Resolution:** Persist raw per-day OI snapshots, expose `snapshot_oi()` for the cron, and call `fetch_and_diff()` directly from `/portfolio` after a cache refresh so the dashboard surfaces deltas immediately.

- [ ] **Step 2.1: Write failing tests for `snapshot_oi`**

Create `scripts/tests/test_uw_analyze_oi_snapshots.py`:

```python
import json
from pathlib import Path
from datetime import date, timedelta

import pytest

from api.services.uw_analyze_oi_snapshots import snapshot_oi, load_history, RETENTION_DAYS

class FakeChain:
    def __init__(self, rows):
        self.rows = rows
    def get_option_chain(self, ticker, expiry=None, **_):
        return {"data": self.rows}

def test_snapshot_oi_writes_file(tmp_path):
    rows = [
        {"strike": 100, "call_oi": 500, "put_oi": 300, "expiration_date": "2026-05-15"},
        {"strike": 105, "call_oi": 800, "put_oi": 200, "expiration_date": "2026-05-15"},
    ]
    snap = snapshot_oi("AAPL", FakeChain(rows), data_dir=tmp_path)
    assert snap["data_date"] == date.today().isoformat()
    assert snap["strikes"]["100"] == {"call_oi": 500, "put_oi": 300}
    p = tmp_path / "AAPL.json"
    assert p.exists()
    payload = json.loads(p.read_text())
    assert len(payload["history"]) == 1

def test_snapshot_oi_rolling_retention(tmp_path):
    p = tmp_path / "AAPL.json"
    old = [
        {"data_date": (date.today() - timedelta(days=i)).isoformat(), "strikes": {}}
        for i in range(10)
    ]
    p.write_text(json.dumps({"history": old}))
    rows = [{"strike": 100, "call_oi": 1, "put_oi": 1, "expiration_date": "2026-05-15"}]
    snapshot_oi("AAPL", FakeChain(rows), data_dir=tmp_path)
    payload = json.loads(p.read_text())
    assert len(payload["history"]) == RETENTION_DAYS

def test_load_history_missing_returns_empty(tmp_path):
    assert load_history("AAPL", data_dir=tmp_path) == []
```

- [ ] **Step 2.2: Run, verify they fail**

```bash
pytest scripts/tests/test_uw_analyze_oi_snapshots.py -v
```

Expected: FAIL (module not found)

- [ ] **Step 2.3: Implement `uw_analyze_oi_snapshots.py`**

Create `scripts/api/services/uw_analyze_oi_snapshots.py`:

```python
"""Raw daily OI snapshot persistence — 5-day rolling per ticker.

Spec: docs/superpowers/specs/2026-04-08-uw-analyze-overhaul-design.md §"Daily OI tracker"
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Optional

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

logger = logging.getLogger("xenon.uw_analyze_oi_snapshots")

RETENTION_DAYS = 5
_DEFAULT_DIR = _SCRIPTS.parent / "data" / "uw_oi_snapshots"


def _path_for(ticker: str, data_dir: Optional[Path]) -> Path:
    base = Path(data_dir) if data_dir else _DEFAULT_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{ticker.upper()}.json"


def load_history(ticker: str, data_dir: Optional[Path] = None) -> list[dict]:
    p = _path_for(ticker, data_dir)
    if not p.exists():
        return []
    try:
        payload = json.loads(p.read_text())
        return list(payload.get("history") or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("oi snapshot load failed for %s: %s", ticker, exc)
        return []


def _atomic_write(path: Path, payload: dict) -> None:
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=".oi_snap_", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(tmp_fd, "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def snapshot_oi(ticker: str, chain_client: Any, *, data_dir: Optional[Path] = None) -> dict:
    """Fetch full chain OI for `ticker`, persist as today's snapshot, retain 5 days."""
    today = date.today().isoformat()
    rows = chain_client.get_option_chain(ticker, expiry=None) or {}
    raw = rows.get("data") if isinstance(rows, dict) else None
    strikes: dict[str, dict] = {}
    for r in raw or []:
        if not isinstance(r, dict):
            continue
        strike = r.get("strike")
        if strike is None:
            continue
        key = f"{float(strike):g}"
        existing = strikes.setdefault(key, {"call_oi": 0, "put_oi": 0})
        try:
            existing["call_oi"] = int(float(r.get("call_oi") or existing["call_oi"]))
            existing["put_oi"] = int(float(r.get("put_oi") or existing["put_oi"]))
        except (TypeError, ValueError):
            pass

    snap = {"data_date": today, "strikes": strikes}

    history = load_history(ticker, data_dir)
    history = [h for h in history if h.get("data_date") != today]
    history.append(snap)
    history.sort(key=lambda h: h.get("data_date") or "")
    history = history[-RETENTION_DAYS:]

    _atomic_write(_path_for(ticker, data_dir), {"updated_at": today, "history": history})
    return snap
```

- [ ] **Step 2.4: Run tests, verify green**

```bash
pytest scripts/tests/test_uw_analyze_oi_snapshots.py -v
```

Expected: PASS (3 tests)

- [ ] **Step 2.5: Wire on-demand OI fetch into `/portfolio` for stale entries**

In `scripts/api/routes/uw_analyze.py::uw_analyze_portfolio`, add right after `entry = await cache.get_or_run(...)`:

```python
        # If the daily cron hasn't yet stamped oi_baseline (e.g. before 15:50 ET
        # or first boot), fetch on-demand so the dashboard surfaces deltas
        # immediately. Failures are non-fatal — empty oi_changes is fine.
        if not entry.get("oi_baseline"):
            try:
                from api.services.uw_analyze_oi_tracker import fetch_and_diff
                from clients.uw_client import UWClient

                spot = (entry.get("current") or {}).get("derived", {}).get("spot")
                oi_changes = await fetch_and_diff(UWClient(), ticker, spot)
                entry["oi_baseline"] = {
                    "data_date": datetime.now(timezone.utc).date().isoformat(),
                    "changes": [c.to_dict() for c in oi_changes],
                }
            except Exception as exc:  # noqa: BLE001
                logger.debug("on-demand oi fetch failed for %s: %s", ticker, exc)
```

- [ ] **Step 2.6: Add a route test asserting OI changes flow through**

In `scripts/tests/test_uw_analyze_routes.py`, add:

```python
def test_portfolio_surfaces_oi_changes(monkeypatch, client_factory):
    from api.services.uw_analyze_oi_tracker import OiChange
    async def fake_fetch(client, ticker, spot):
        return [OiChange(strike=100, side="call", prev_oi=500, curr_oi=2000, delta=1500, delta_pct=3.0, label="+1.5K calls @ $100 (+300%)")]
    monkeypatch.setattr("api.services.uw_analyze_oi_tracker.fetch_and_diff", fake_fetch)
    client = client_factory()  # use existing fixture pattern
    resp = client.get("/uw-analyze/portfolio")
    assert resp.status_code == 200
    body = resp.json()
    row = next(r for r in body["tickers"] if r["ticker"] == "AAPL")
    assert row["oi_changes"]
    assert row["oi_changes"][0]["label"].startswith("+1.5K")
    # Action items should pick it up
    assert any(a["code"] == "OI_DELTA" for a in body["action_items"])
```

- [ ] **Step 2.7: Run, verify green**

```bash
pytest scripts/tests/test_uw_analyze_routes.py -v
```

Expected: PASS

- [ ] **Step 2.8: Commit**

```bash
git add scripts/api/services/uw_analyze_oi_snapshots.py scripts/tests/test_uw_analyze_oi_snapshots.py scripts/api/routes/uw_analyze.py scripts/tests/test_uw_analyze_routes.py
git commit -m "feat(uw-analyze): raw OI snapshots + on-demand /portfolio fetch (cluster1.2)"
```

---

## Task 3: Move flow capture from read path to write path (kill phantom events)

**Files:**

- Modify: `scripts/api/services/uw_analyze_cache.py` — `get_or_run` records `materialized_changes` after a refresh
- Modify: `scripts/api/routes/uw_analyze.py::uw_analyze_portfolio` — only call `capture_from_changes` when the cache reports a fresh write
- Modify: `scripts/tests/test_uw_analyze_routes.py` — re-call `/portfolio` twice in a row, assert flow_log size doesn't grow

**Background:** Currently `compute_changes(prev, curr)` and `capture_from_changes` run on every GET. Combined with `make_event_id(..., trade_date=date.today())`, a stale one-shot diff turns into a brand-new FlowEvent on the next calendar day. The fix is to mark the entry "diff materialized" once and only run capture when a fresh refresh happens.

- [ ] **Step 3.1: Write failing test asserting idempotent capture**

In `scripts/tests/test_uw_analyze_routes.py`:

```python
def test_portfolio_does_not_re_capture_on_repeat_get(monkeypatch, client_factory):
    client = client_factory()
    r1 = client.get("/uw-analyze/portfolio")
    r2 = client.get("/uw-analyze/portfolio")
    assert r1.status_code == 200 and r2.status_code == 200
    events_1 = sum(len(row["unusual_flow_events"]) for row in r1.json()["tickers"])
    events_2 = sum(len(row["unusual_flow_events"]) for row in r2.json()["tickers"])
    assert events_2 == events_1, "second GET must not create duplicate flow events"
```

- [ ] **Step 3.2: Run, verify it fails**

```bash
pytest scripts/tests/test_uw_analyze_routes.py::test_portfolio_does_not_re_capture_on_repeat_get -v
```

Expected: FAIL (event count grows on second GET if prev/curr both contain a sweep delta)

- [ ] **Step 3.3: Add `did_refresh` return signal from `get_or_run`**

In `scripts/api/services/uw_analyze_cache.py`, change `get_or_run` to return `(entry, did_refresh)`:

```python
async def get_or_run(
    self,
    ticker: str,
    *,
    runner,
    force: bool = False,
    sources: Iterable[Source] = (),
) -> tuple[dict, bool]:
    ticker = ticker.upper()
    self._ensure_loaded()
    async with self._lock_for(ticker):
        entry = self._entries.get(ticker)
        if (not force) and entry and self._is_fresh(entry):
            if sources:
                self._merge_sources(entry, sources)
                await self._persist()
            return entry, False
        async with self._semaphore:
            logger.info("uw_analyze_cache running analysis for %s (force=%s)", ticker, force)
            report, display, flow_alerts = await asyncio.wait_for(runner(ticker), timeout=_RUN_TIMEOUT_S)
        new_snapshot = build_snapshot(ticker, report, display, flow_alerts=flow_alerts)
        prev_snapshot = entry.get("current") if entry else None
        existing_sources = list(entry.get("sources") or []) if entry else []
        merged_sources = sorted(set(existing_sources) | set(sources))
        new_entry = {
            "current": new_snapshot,
            "previous": prev_snapshot,
            "oi_baseline": entry.get("oi_baseline") if entry else None,
            "sources": merged_sources or list(sources),
        }
        self._entries[ticker] = new_entry
        await self._persist()
        return new_entry, True
```

Update every existing caller in tests:

```python
# In test_uw_analyze_cache.py, replace:
entry = await cache.get_or_run(...)
# With:
entry, _ = await cache.get_or_run(...)
```

- [ ] **Step 3.4: Update `/portfolio` and `/refresh` to use `did_refresh`**

In `scripts/api/routes/uw_analyze.py`:

```python
        try:
            entry, did_refresh = await cache.get_or_run(
                ticker,
                runner=_runner,
                force=False,
                sources=sources,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("uw-analyze portfolio: %s failed: %s", ticker, exc)
            continue

        snap = entry.get("current") or {}
        prev = entry.get("previous")
        changes = compute_changes(prev, snap) if did_refresh else []
        change_dicts = [c.to_dict() for c in changes]

        # Only capture sweep events on a fresh refresh — never on plain reads.
        if did_refresh:
            flow_alerts = snap.get("flow_alerts") or None
            underlying = (snap.get("derived") or {}).get("spot")
            if changes and flow_alerts and underlying is not None:
                new_events = capture_from_changes(
                    ticker=ticker,
                    changes=changes,
                    flow_alerts=flow_alerts,
                    underlying_price=underlying,
                )
                for ev in new_events:
                    flow_log.upsert(ev)
```

Apply the same `_, _` unpacking in `uw_analyze_refresh`:

```python
            await cache.get_or_run(...)
            # becomes:
            await cache.get_or_run(...)  # tuple discarded
```

Actually:

```python
            _, _ = await cache.get_or_run(
                ticker,
                runner=_runner,
                force=True,
                sources=["adhoc"] if req.adhoc else (),
            )
```

- [ ] **Step 3.5: Persist `changes` on the entry so the GET path can still display them between refreshes**

Otherwise the dashboard would show CHANGED badges only on the exact GET that triggered the refresh, then they vanish. In `uw_analyze_cache.py::get_or_run` after building `new_entry`:

```python
        # Materialize and persist the diff alongside the entry so subsequent
        # reads can display it without re-running compute_changes.
        from api.services.uw_analyze_diff import compute_changes as _cc
        materialized = [c.to_dict() for c in _cc(prev_snapshot, new_snapshot)]
        new_entry["materialized_changes"] = materialized
```

Then in the route GET path, prefer the persisted version:

```python
        change_dicts = entry.get("materialized_changes") or []
```

(Remove the conditional `compute_changes` call from the route — it's now in the cache.)

- [ ] **Step 3.6: Run, verify green**

```bash
pytest scripts/tests/test_uw_analyze_cache.py scripts/tests/test_uw_analyze_routes.py -v
```

Expected: PASS

- [ ] **Step 3.7: Commit**

```bash
git add scripts/api/services/uw_analyze_cache.py scripts/api/routes/uw_analyze.py scripts/tests/test_uw_analyze_cache.py scripts/tests/test_uw_analyze_routes.py
git commit -m "fix(uw-analyze): capture flow events on refresh only, not on every GET (cluster1.3)"
```

---

# Cluster 2 — Daily Job Correctness

## Task 4: Add the missing closing-volume-spike anomaly rule

**Files:**

- Modify: `scripts/api/services/uw_analyze_flow_tracker.py` — add `volume` to `FlowDailyRow`, implement rule 3 in `classify_anomaly`
- Modify: `scripts/api/services/uw_analyze_daily_job.py::run_once` — pass `volume` through
- Modify: `scripts/tests/test_uw_analyze_flow_tracker.py` — add fires/silent cases for the new rule

**Background:** Plan §M4 lists 3 anomaly rules. `CLOSING_VOLUME_OI_FRAC = 0.80` is defined but unused; `FlowDailyRow` has no `volume` field. The rule: a single trading day's volume > 80% of OI = closeout/exit signal.

- [ ] **Step 4.1: Write failing test**

Add to `scripts/tests/test_uw_analyze_flow_tracker.py`:

```python
def test_classify_anomaly_closing_volume_spike():
    from api.services.uw_analyze_flow_tracker import (
        FlowEvent, FlowInitial, FlowDailyRow, classify_anomaly
    )
    ev = FlowEvent(
        id="x", ticker="AAPL", side="call", strike=100, expiry="2026-09-15",
        detected_at="2026-04-08T15:50:00+00:00",
        initial=FlowInitial(premium_usd=7e6, oi=1000, volume=4000, mid=2.5, underlying_price=99),
    )
    ev.daily_track.append(FlowDailyRow(date="2026-04-09", oi=950, mid=2.4, underlying_price=99.5, pct_change_premium=-0.04, volume=850))
    assert "closing volume" in (classify_anomaly(ev) or "").lower()


def test_classify_anomaly_closing_volume_silent_below_threshold():
    from api.services.uw_analyze_flow_tracker import (
        FlowEvent, FlowInitial, FlowDailyRow, classify_anomaly
    )
    ev = FlowEvent(
        id="x", ticker="AAPL", side="call", strike=100, expiry="2026-09-15",
        detected_at="2026-04-08T15:50:00+00:00",
        initial=FlowInitial(premium_usd=7e6, oi=1000, volume=4000, mid=2.5, underlying_price=99),
    )
    ev.daily_track.append(FlowDailyRow(date="2026-04-09", oi=950, mid=2.4, underlying_price=99.5, pct_change_premium=-0.04, volume=500))
    assert classify_anomaly(ev) is None
```

- [ ] **Step 4.2: Run, verify failure**

```bash
pytest scripts/tests/test_uw_analyze_flow_tracker.py::test_classify_anomaly_closing_volume_spike -v
```

Expected: FAIL (`FlowDailyRow.__init__()` got an unexpected keyword argument 'volume')

- [ ] **Step 4.3: Add `volume` to `FlowDailyRow` and implement rule 3**

In `scripts/api/services/uw_analyze_flow_tracker.py`:

```python
@dataclass
class FlowDailyRow:
    date: str
    oi: int
    mid: float
    underlying_price: float
    pct_change_premium: float
    volume: int = 0
```

Then in `classify_anomaly`, after the OI evaporation block:

```python
    # Rule 3: closing volume spike — single day volume > 80% of current OI
    if latest.oi > 0 and latest.volume:
        ratio = latest.volume / latest.oi
        if ratio >= CLOSING_VOLUME_OI_FRAC:
            return f"closing volume spike: {int(ratio * 100)}% of OI traded in one day"
```

- [ ] **Step 4.4: Update `advance_daily_track` and `progress_event` signatures**

```python
def advance_daily_track(
    event: FlowEvent,
    *,
    today: str,
    oi: int,
    mid: float,
    underlying_price: float,
    volume: int = 0,
) -> FlowEvent:
    if any(row.date == today for row in event.daily_track):
        return event
    pct = 0.0
    if event.initial.mid:
        pct = (mid - event.initial.mid) / event.initial.mid
    event.daily_track.append(
        FlowDailyRow(date=today, oi=oi, mid=mid, underlying_price=underlying_price, pct_change_premium=pct, volume=volume)
    )
    return event


def progress_event(
    event: FlowEvent,
    *,
    today: str,
    oi: int,
    mid: float,
    underlying_price: float,
    volume: int = 0,
) -> FlowEvent:
    advance_daily_track(event, today=today, oi=oi, mid=mid, underlying_price=underlying_price, volume=volume)
    ...
```

Also update `_event_from_dict` to read `volume` defaulting to 0.

- [ ] **Step 4.5: Update `daily_job.run_once` to pass volume from contract_state**

```python
        progress_event(
            event,
            today=today_iso,
            oi=int(contract_state.get("oi", event.initial.oi)),
            mid=float(contract_state.get("mid", event.initial.mid)),
            underlying_price=float(contract_state.get("underlying_price", event.initial.underlying_price)),
            volume=int(contract_state.get("volume", 0)),
        )
```

- [ ] **Step 4.6: Run, verify green**

```bash
pytest scripts/tests/test_uw_analyze_flow_tracker.py scripts/tests/test_uw_analyze_daily_job.py -v
```

Expected: PASS

- [ ] **Step 4.7: Commit**

```bash
git add scripts/api/services/uw_analyze_flow_tracker.py scripts/api/services/uw_analyze_daily_job.py scripts/tests/test_uw_analyze_flow_tracker.py
git commit -m "feat(uw-analyze): closing-volume-spike anomaly rule (cluster2.1)"
```

---

## Task 5: ET-correct dates everywhere + trading-day window for OI evaporation

**Files:**

- Modify: `scripts/api/services/uw_analyze_flow_tracker.py` — replace `date.today()` with an injectable `now_et_date()` from `daily_job`
- Modify: `scripts/api/services/uw_analyze_daily_job.py` — export `now_et_date()`, use trading-day diff
- Modify: `scripts/tests/test_uw_analyze_flow_tracker.py` — Friday→Monday case

**Background:** All `date.today()` calls in `flow_tracker.py` (lines 105, 287) and `daily_job.py:128` use host-local time, which corrupts trade_date and DTE windows on non-ET hosts. OI evaporation also uses calendar days, so Friday→Monday counts as 3 days instead of 1.

- [ ] **Step 5.1: Write failing test for trading-day window**

```python
def test_oi_evaporation_uses_trading_days_not_calendar(monkeypatch):
    from api.services.uw_analyze_flow_tracker import (
        FlowEvent, FlowInitial, FlowDailyRow, classify_anomaly
    )
    # Detected Friday, evaluated Monday (3 calendar days, 1 trading day)
    ev = FlowEvent(
        id="x", ticker="AAPL", side="call", strike=100, expiry="2026-09-15",
        detected_at="2026-04-03T20:00:00+00:00",  # Fri
        initial=FlowInitial(premium_usd=7e6, oi=1000, volume=4000, mid=2.5, underlying_price=99),
    )
    # OI down 60% on Monday — within 3 trading days, but 3 calendar days
    ev.daily_track.append(FlowDailyRow(date="2026-04-06", oi=400, mid=2.4, underlying_price=99.0, pct_change_premium=-0.04, volume=100))
    reason = classify_anomaly(ev)
    assert reason and "OI evaporated" in reason  # Must fire — only 1 trading day passed
```

- [ ] **Step 5.2: Run, verify failure (calendar diff = 3, but window = 3 days, would still fire — choose a different boundary case)**

Use a 4-calendar-day, 2-trading-day case to make the test meaningful:

```python
def test_oi_evaporation_uses_trading_days_not_calendar():
    # Detected Thursday, evaluated next Wednesday (6 cal days, 4 trading days — silent under trading-day rule)
    ev = FlowEvent(
        id="x", ticker="AAPL", side="call", strike=100, expiry="2026-09-15",
        detected_at="2026-04-02T20:00:00+00:00",  # Thu
        initial=FlowInitial(premium_usd=7e6, oi=1000, volume=4000, mid=2.5, underlying_price=99),
    )
    ev.daily_track.append(FlowDailyRow(date="2026-04-08", oi=400, mid=2.4, underlying_price=99.0, pct_change_premium=-0.04, volume=100))
    # 4 trading days > OI_EVAPORATION_DAYS (3), so the OI rule must NOT fire
    from api.services.uw_analyze_flow_tracker import classify_anomaly
    assert classify_anomaly(ev) is None
```

- [ ] **Step 5.3: Add a `trading_days_between` helper in `daily_job.py`**

```python
def now_et_date():
    return now_et().date()


def trading_days_between(d1: date, d2: date) -> int:
    """Inclusive count of trading days from d1 to d2, or 0 if d2 < d1."""
    if d2 < d1:
        return 0
    n = 0
    cur = d1
    while cur < d2:
        cur = cur + timedelta(days=1)
        if cur.weekday() < 5 and is_trading_day(datetime.combine(cur, time(12, 0)).replace(tzinfo=_ET)):
            n += 1
    return n
```

- [ ] **Step 5.4: Update `flow_tracker.classify_anomaly` to use it**

```python
    try:
        detected = datetime.fromisoformat(event.detected_at).date()
    except ValueError:
        detected = date.today()
    try:
        from api.services.uw_analyze_daily_job import trading_days_between
        days_since = trading_days_between(detected, date.fromisoformat(latest.date))
    except Exception:  # noqa: BLE001
        days_since = (date.fromisoformat(latest.date) - detected).days
    if 0 <= days_since <= OI_EVAPORATION_DAYS and event.initial.oi > 0:
        ...
```

- [ ] **Step 5.5: Replace `date.today()` with ET equivalents**

In `flow_tracker.py`:

```python
def _today_iso() -> str:
    try:
        from api.services.uw_analyze_daily_job import now_et_date
        return now_et_date().isoformat()
    except Exception:
        return date.today().isoformat()
```

Apply the same in `_dte` and `maybe_close_or_expire` (default `today` arg).

- [ ] **Step 5.6: Run, verify green**

```bash
pytest scripts/tests/test_uw_analyze_flow_tracker.py scripts/tests/test_uw_analyze_daily_job.py -v
```

Expected: PASS

- [ ] **Step 5.7: Commit**

```bash
git add scripts/api/services/uw_analyze_flow_tracker.py scripts/api/services/uw_analyze_daily_job.py scripts/tests/test_uw_analyze_flow_tracker.py
git commit -m "fix(uw-analyze): use ET dates + trading-day window for evaporation rule (cluster2.2)"
```

---

## Task 6: Allow anomaly events to still close/expire + fix contract_fetcher signature + wire default in server

**Files:**

- Modify: `scripts/api/services/uw_analyze_flow_tracker.py::progress_event`
- Modify: `scripts/api/services/uw_analyze_daily_job.py::run_once` — `contract_fetcher` takes `(ticker, side, strike, expiry)` not opaque id
- Modify: `scripts/api/server.py` lifespan — provide a real default fetcher

**Background:** Three coupled issues:

1. `progress_event` skips `maybe_close_or_expire` once status becomes anomaly → events frozen forever
2. `contract_fetcher` is typed to take `event.id` (a sha1 hash) — no fetcher can resolve that
3. `server.py` calls `run_loop` without any `contract_fetcher` → events never advance in production

- [ ] **Step 6.1: Write failing test for anomaly→expire transition**

```python
def test_progress_event_anomaly_can_still_expire():
    from datetime import date, timedelta
    from api.services.uw_analyze_flow_tracker import (
        FlowEvent, FlowInitial, FlowDailyRow, progress_event
    )
    expiry = (date.today() - timedelta(days=1)).isoformat()
    ev = FlowEvent(
        id="x", ticker="AAPL", side="call", strike=100, expiry=expiry,
        detected_at="2026-04-08T15:50:00+00:00",
        initial=FlowInitial(premium_usd=7e6, oi=1000, volume=4000, mid=2.5, underlying_price=99),
        status="anomaly",
        anomaly_reason="premium collapsed -70%",
    )
    progress_event(ev, today=date.today().isoformat(), oi=950, mid=0.7, underlying_price=99, volume=200)
    assert ev.status == "expired"
```

- [ ] **Step 6.2: Run, verify failure**

```bash
pytest scripts/tests/test_uw_analyze_flow_tracker.py::test_progress_event_anomaly_can_still_expire -v
```

Expected: FAIL — status stays "anomaly"

- [ ] **Step 6.3: Decouple anomaly flag from lifecycle**

In `progress_event`:

```python
def progress_event(
    event: FlowEvent,
    *,
    today: str,
    oi: int,
    mid: float,
    underlying_price: float,
    volume: int = 0,
) -> FlowEvent:
    advance_daily_track(event, today=today, oi=oi, mid=mid, underlying_price=underlying_price, volume=volume)
    reason = classify_anomaly(event)
    if reason and event.anomaly_reason is None:
        event.anomaly_reason = reason
        if event.status == "open":
            event.status = "anomaly"
    # Always check close/expire — anomaly is informational, not terminal.
    if event.status in ("open", "anomaly"):
        maybe_close_or_expire(event)
    return event
```

- [ ] **Step 6.4: Run, verify green**

```bash
pytest scripts/tests/test_uw_analyze_flow_tracker.py -v
```

Expected: PASS

- [ ] **Step 6.5: Change `contract_fetcher` signature in `run_once`**

```python
async def run_once(
    *,
    cache,
    flow_log,
    uw_client,
    oi_fetcher: Optional[Callable[[str, Optional[float]], Awaitable[list]]] = None,
    contract_fetcher: Optional[Callable[..., Awaitable[Optional[dict]]]] = None,
) -> dict:
    ...
    for event in flow_log.all():
        if event.status not in ("open", "anomaly"):
            continue
        contract_state = None
        if contract_fetcher is not None:
            try:
                contract_state = await contract_fetcher(
                    ticker=event.ticker,
                    side=event.side,
                    strike=event.strike,
                    expiry=event.expiry,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("contract fetch failed for %s %s %s %s: %s",
                               event.ticker, event.side, event.strike, event.expiry, exc)
```

Also change the test in `test_uw_analyze_daily_job.py` that calls `run_once` — fixtures should accept the new kwargs.

- [ ] **Step 6.6: Implement default `contract_fetcher` in server lifespan**

In `scripts/api/server.py` lifespan, replace the existing block:

```python
        from clients.uw_client import UWClient

        from api.routes.uw_analyze import get_flow_log, get_portfolio_cache
        from api.services.uw_analyze_daily_job import run_loop as uw_daily_run_loop

        _uw_client = UWClient() if uw_available else None

        async def _default_contract_fetcher(*, ticker, side, strike, expiry):
            """Resolve a single OCC contract's current oi/mid/underlying/volume.
            Hits UW chain for the expiry, finds the matching strike+side row.
            """
            if _uw_client is None:
                return None
            try:
                resp = await asyncio.to_thread(_uw_client.get_option_chain, ticker, expiry=expiry)
            except Exception as exc:  # noqa: BLE001
                logger.debug("default contract_fetcher chain fetch failed for %s: %s", ticker, exc)
                return None
            rows = resp.get("data") if isinstance(resp, dict) else None
            if not isinstance(rows, list):
                return None
            for r in rows:
                if not isinstance(r, dict):
                    continue
                try:
                    if abs(float(r.get("strike", -1)) - float(strike)) > 1e-6:
                        continue
                except (TypeError, ValueError):
                    continue
                key = "call" if side == "call" else "put"
                return {
                    "oi": int(float(r.get(f"{key}_oi") or 0)),
                    "mid": float(r.get(f"{key}_mid") or r.get(f"{key}_last") or 0.0),
                    "underlying_price": float(r.get("underlying_price") or 0.0),
                    "volume": int(float(r.get(f"{key}_volume") or 0)),
                }
            return None

        uw_daily_task = asyncio.create_task(
            uw_daily_run_loop(
                cache=get_portfolio_cache(),
                flow_log=get_flow_log(),
                uw_client=_uw_client,
                contract_fetcher=_default_contract_fetcher,
            )
        )
```

Add `contract_fetcher` to `run_loop`'s signature too:

```python
async def run_loop(
    *,
    cache,
    flow_log,
    uw_client,
    oi_fetcher=None,
    contract_fetcher=None,
    test_trigger_now: bool = False,
):
    ...
    await run_once(cache=cache, flow_log=flow_log, uw_client=uw_client,
                   oi_fetcher=oi_fetcher, contract_fetcher=contract_fetcher)
```

- [ ] **Step 6.7: Run all daily-job tests, verify green**

```bash
pytest scripts/tests/test_uw_analyze_daily_job.py scripts/tests/test_uw_analyze_flow_tracker.py -v
```

Expected: PASS

- [ ] **Step 6.8: Commit**

```bash
git add scripts/api/services/uw_analyze_flow_tracker.py scripts/api/services/uw_analyze_daily_job.py scripts/api/server.py scripts/tests/test_uw_analyze_flow_tracker.py scripts/tests/test_uw_analyze_daily_job.py
git commit -m "fix(uw-analyze): anomaly→expire transition + real contract_fetcher (cluster2.3)"
```

---

# Cluster 3 — Architectural Cleanup

## Task 7: Delete legacy 30s `_cache`, route legacy POST through `UwAnalyzeCache`

**Files:**

- Modify: `scripts/api/routes/uw_analyze.py`

**Background:** Plan §M6 explicitly required deleting `_cache`/`_CACHE_TTL_SECONDS` from the legacy POST route and delegating to `cache.get_or_run(force=True)`. Both Codex and Gemini flagged it. Currently the legacy POST bypasses singleflight entirely.

- [ ] **Step 7.1: Write failing test asserting legacy POST honours singleflight**

In `scripts/tests/test_uw_analyze_routes.py`:

```python
def test_legacy_post_uses_unified_cache(monkeypatch, client_factory):
    """Two near-simultaneous POSTs for the same ticker collapse to one runner call."""
    import asyncio
    call_count = 0
    async def slow_runner(t):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.1)
        return ({"price": 100}, {"max_pain": 99}, [])
    monkeypatch.setattr("api.routes.uw_analyze._runner", slow_runner)
    client = client_factory()
    # ... fire two POSTs concurrently with httpx AsyncClient (or sequentially if simpler)
    r1 = client.post("/uw-analyze", json={"ticker": "AAPL"})
    r2 = client.post("/uw-analyze", json={"ticker": "AAPL"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert call_count == 1, "second POST must hit unified cache, not legacy 30s _cache"
```

- [ ] **Step 7.2: Run, verify failure**

Expected: FAIL — call_count == 1 only because of legacy `_cache`, but if monkeypatch isolates `_runner`, you'll see the legacy path called separately.

- [ ] **Step 7.3: Delete `_cache`/`_CACHE_TTL_SECONDS` and rewrite legacy POST**

In `scripts/api/routes/uw_analyze.py`:

```python
# DELETE these lines:
# _CACHE_TTL_SECONDS = 30.0
# _cache: dict[str, tuple[float, "UwAnalyzeResponse"]] = {}

# In reset_state_for_tests, drop the _cache reset.

# Rewrite the route:
@router.post("/uw-analyze", response_model=UwAnalyzeResponse)
async def uw_analyze(req: UwAnalyzeRequest) -> UwAnalyzeResponse:
    raw_ticker = (req.ticker or "").strip().upper()
    if not raw_ticker or not raw_ticker.replace(".", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="invalid ticker")

    cache = get_portfolio_cache()
    try:
        entry, _ = await cache.get_or_run(raw_ticker, runner=_runner, force=False)
    except UWNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"ticker not found: {raw_ticker}") from exc
    except UWAPIError as exc:
        logger.warning("uw-analyze upstream error for %s: %s", raw_ticker, exc)
        raise HTTPException(status_code=502, detail="UW upstream failed") from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="analysis timed out") from exc

    snap = entry.get("current") or {}
    return UwAnalyzeResponse(
        report=snap.get("report") or {},
        display=UwAnalyzeDisplay(**(snap.get("display") or {})),
        generated_at=snap.get("ts") or datetime.now(timezone.utc).isoformat(),
    )
```

- [ ] **Step 7.4: Run, verify green**

```bash
pytest scripts/tests/test_uw_analyze_routes.py -v
```

Expected: PASS

- [ ] **Step 7.5: Commit**

```bash
git add scripts/api/routes/uw_analyze.py scripts/tests/test_uw_analyze_routes.py
git commit -m "refactor(uw-analyze): delete legacy _cache, route POST through UwAnalyzeCache (cluster3.1)"
```

---

## Task 8: Parallelize the `/portfolio` and `/refresh` loops

**Files:**

- Modify: `scripts/api/routes/uw_analyze.py`

**Background:** Plan called for the cache's per-ticker locks + `Semaphore(3)` to bound concurrency. Currently both routes `await` per ticker in a serial loop, so the singleflight protection never matters.

- [ ] **Step 8.1: Write failing test asserting concurrent fan-out**

```python
def test_portfolio_runs_tickers_concurrently(monkeypatch, client_factory):
    import asyncio
    in_flight = 0
    peak = 0
    async def slow_runner(t):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return ({"price": 100}, {"max_pain": 99}, [])
    monkeypatch.setattr("api.routes.uw_analyze._runner", slow_runner)
    # seed 5 candidates via fixture (mocked seed_candidates)
    monkeypatch.setattr("api.routes.uw_analyze.seed_candidates",
                        lambda: {f"T{i}": ["watchlist"] for i in range(5)})
    client = client_factory()
    resp = client.get("/uw-analyze/portfolio")
    assert resp.status_code == 200
    assert peak >= 2, f"expected concurrent runs, peak={peak}"
```

- [ ] **Step 8.2: Run, verify failure**

Expected: FAIL — peak == 1 with the serial loop.

- [ ] **Step 8.3: Use `asyncio.gather` to fan out cache calls**

In `scripts/api/routes/uw_analyze.py::uw_analyze_portfolio`, replace the for-loop with:

```python
    candidates = seed_candidates()

    async def _process(ticker: str, sources: list[str]) -> Optional[dict]:
        try:
            entry, did_refresh = await cache.get_or_run(ticker, runner=_runner, force=False, sources=sources)
        except Exception as exc:  # noqa: BLE001
            logger.warning("uw-analyze portfolio: %s failed: %s", ticker, exc)
            return None

        if not entry.get("oi_baseline"):
            try:
                from api.services.uw_analyze_oi_tracker import fetch_and_diff
                from clients.uw_client import UWClient
                spot = (entry.get("current") or {}).get("derived", {}).get("spot")
                oi_changes = await fetch_and_diff(UWClient(), ticker, spot)
                entry["oi_baseline"] = {
                    "data_date": datetime.now(timezone.utc).date().isoformat(),
                    "changes": [c.to_dict() for c in oi_changes],
                }
            except Exception as exc:  # noqa: BLE001
                logger.debug("on-demand oi fetch failed for %s: %s", ticker, exc)

        snap = entry.get("current") or {}
        prev = entry.get("previous")
        change_dicts = entry.get("materialized_changes") or []

        if did_refresh:
            flow_alerts = snap.get("flow_alerts") or None
            underlying = (snap.get("derived") or {}).get("spot")
            changes_objs = compute_changes(prev, snap)
            if changes_objs and flow_alerts and underlying is not None:
                new_events = capture_from_changes(
                    ticker=ticker, changes=changes_objs,
                    flow_alerts=flow_alerts, underlying_price=underlying,
                )
                for ev in new_events:
                    flow_log.upsert(ev)

        oi_baseline = entry.get("oi_baseline") or {}
        return {
            "ticker": ticker,
            "sources": list(sources),
            "snapshot": snap,
            "prev_ts": (prev or {}).get("ts") if isinstance(prev, dict) else None,
            "changes": change_dicts,
            "oi_changes": oi_baseline.get("changes") or [],
            "unusual_flow_events": [e.to_dict() for e in flow_log.for_ticker(ticker)],
        }

    results = await asyncio.gather(*[_process(t, s) for t, s in sorted(candidates.items())])
    rows = [r for r in results if r is not None]
```

Apply the same `asyncio.gather` pattern to `uw_analyze_refresh`.

- [ ] **Step 8.4: Run, verify green**

```bash
pytest scripts/tests/test_uw_analyze_routes.py -v
```

Expected: PASS (peak ≥ 2, capped by Semaphore(3))

- [ ] **Step 8.5: Commit**

```bash
git add scripts/api/routes/uw_analyze.py scripts/tests/test_uw_analyze_routes.py
git commit -m "perf(uw-analyze): fan out /portfolio with asyncio.gather (cluster3.2)"
```

---

## Task 9: Lifespan `try/finally` + multi-worker guard

**Files:**

- Modify: `scripts/api/server.py`

**Background:** If anything in the app's run raises through `yield`, neither `uw_daily_task.cancel()` nor `ib_pool.disconnect_all()` ever runs. Also, under `--workers N`, every worker fires the daily job, causing N× duplicate API calls and `daily_track` rows.

- [ ] **Step 9.1: Wrap lifespan in `try/finally`**

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    global ib_pool, uw_available
    # ... existing setup ...
    uw_daily_task = None
    try:
        # ... existing startup including create_task ...
        try:
            yield
        finally:
            if uw_daily_task is not None:
                uw_daily_task.cancel()
                try:
                    await uw_daily_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            if ib_pool:
                await ib_pool.disconnect_all()
            if _futu_client is not None:
                await _futu_client.disconnect()
    except Exception:
        logger.exception("lifespan startup failed")
        raise
```

- [ ] **Step 9.2: Add multi-worker guard for daily job**

Right before `asyncio.create_task(uw_daily_run_loop(...))`:

```python
        # Multi-worker guard: only worker 0 runs the daily job. Set
        # XENON_DAILY_JOB_WORKER_ID=0 (default) when launching with uvicorn
        # --workers N to ensure exactly one instance fires.
        worker_id = os.environ.get("XENON_DAILY_JOB_WORKER_ID")
        primary_worker = os.environ.get("UVICORN_WORKER_ID", "0")
        should_run_job = worker_id is None or worker_id == primary_worker
        if not should_run_job:
            logger.info("uw_analyze_daily_job suppressed on worker %s", primary_worker)
        else:
            uw_daily_task = asyncio.create_task(uw_daily_run_loop(...))
```

- [ ] **Step 9.3: Smoke test — start API in test mode, confirm clean shutdown logs**

```bash
XENON_TEST_MODE=1 uvicorn scripts.api.server:app --port 8399 &
sleep 1
kill -INT %1
wait
```

Expected: shutdown logs include "uw_analyze daily job cancelled" (or absence on test mode), no traceback.

- [ ] **Step 9.4: Commit**

```bash
git add scripts/api/server.py
git commit -m "fix(api): lifespan try/finally + multi-worker guard for uw daily job (cluster3.3)"
```

---

# Cluster 4 — Test Debt

## Task 10: Fix `test_call_sweep_null_skipped` missing assert + add symmetric null cases

**Files:**

- Modify: `scripts/tests/test_uw_analyze_diff.py`

- [ ] **Step 10.1: Replace the dead test**

```python
def test_call_sweep_null_skipped():
    from api.services.uw_analyze_diff import compute_changes
    prev = {"derived": {"net_call_premium": None}}
    curr = {"derived": {"net_call_premium": 10_000_000}}
    out = compute_changes(prev, curr)
    assert all(c.code != "UNUSUAL_CALL_SWEEP" for c in out)


def test_call_sweep_prev_null_skipped():
    from api.services.uw_analyze_diff import compute_changes
    prev = {"derived": {"net_call_premium": 0}}
    curr = {"derived": {"net_call_premium": None}}
    assert compute_changes(prev, curr) == []


def test_max_pain_zero_spot_skipped():
    from api.services.uw_analyze_diff import compute_changes
    prev = {"derived": {"max_pain": 100}}
    curr = {"derived": {"max_pain": 110, "spot": 0}}
    assert compute_changes(prev, curr) == []
```

- [ ] **Step 10.2: Run**

```bash
pytest scripts/tests/test_uw_analyze_diff.py -v
```

Expected: PASS

- [ ] **Step 10.3: Commit**

```bash
git add scripts/tests/test_uw_analyze_diff.py
git commit -m "test(uw-analyze): fix dead null-guard assertions in diff tests (cluster4.1)"
```

---

## Task 11: Add `max_pain` regression coverage

**Files:**

- Modify: `scripts/tests/test_analysis_ticker_data.py` (or create `scripts/tests/test_ticker_data.py` per plan name; check which exists first)

- [ ] **Step 11.1: Inspect existing test file**

```bash
ls scripts/tests/test_*ticker_data*.py
```

- [ ] **Step 11.2: Add tests**

Append:

```python
def test_ticker_data_populates_max_pain(monkeypatch):
    from analysis import ticker_data
    class FakeUW:
        def get_max_pain(self, ticker):
            return {"data": [{"max_pain": 123.45}]}
        def __getattr__(self, _):
            return lambda *a, **k: {"data": []}
    # Patch the module-level client used by ticker_data
    monkeypatch.setattr(ticker_data, "UWClient", lambda: FakeUW())
    td = ticker_data.build_ticker_data("AAPL")
    assert td.max_pain == 123.45


def test_ticker_data_max_pain_null_safe(monkeypatch):
    from analysis import ticker_data
    class FakeUW:
        def get_max_pain(self, ticker):
            raise RuntimeError("UW down")
        def __getattr__(self, _):
            return lambda *a, **k: {"data": []}
    monkeypatch.setattr(ticker_data, "UWClient", lambda: FakeUW())
    td = ticker_data.build_ticker_data("AAPL")
    assert td.max_pain is None
```

(If `build_ticker_data` has a different name or signature, inspect it first via `grep -n "def build" scripts/analysis/ticker_data.py` and adjust.)

- [ ] **Step 11.3: Run, verify green**

```bash
pytest scripts/tests/test_analysis_ticker_data.py -k max_pain -v
```

Expected: PASS

- [ ] **Step 11.4: Commit**

```bash
git add scripts/tests/test_analysis_ticker_data.py
git commit -m "test(uw-analyze): regression coverage for max_pain plumbing (cluster4.2)"
```

---

## Task 12: `_job_running` double-start + cancellation tests

**Files:**

- Modify: `scripts/tests/test_uw_analyze_daily_job.py`

- [ ] **Step 12.1: Add tests**

```python
import asyncio
import pytest

@pytest.mark.asyncio
async def test_run_loop_double_start_suppressed(monkeypatch):
    from api.services import uw_analyze_daily_job as job
    job._job_running = False
    started = []
    async def fake_run_once(**kwargs):
        started.append(1)
        return {}
    monkeypatch.setattr(job, "run_once", fake_run_once)
    monkeypatch.setattr(job, "seconds_until_next_trigger", lambda: 0.01)
    t1 = asyncio.create_task(job.run_loop(cache=None, flow_log=None, uw_client=None))
    await asyncio.sleep(0.05)
    t2 = asyncio.create_task(job.run_loop(cache=None, flow_log=None, uw_client=None))
    await asyncio.sleep(0.05)
    assert job._job_running is True
    t1.cancel(); t2.cancel()
    for t in (t1, t2):
        try: await t
        except (asyncio.CancelledError, Exception): pass
    assert job._job_running is False


@pytest.mark.asyncio
async def test_run_loop_test_trigger_now_fires_once(monkeypatch):
    from api.services import uw_analyze_daily_job as job
    job._job_running = False
    fired = []
    async def fake_run_once(**kwargs):
        fired.append(1)
    monkeypatch.setattr(job, "run_once", fake_run_once)
    async def long_sleep(*a, **k):
        await asyncio.sleep(10)
    monkeypatch.setattr(asyncio, "sleep", long_sleep)
    t = asyncio.create_task(job.run_loop(cache=None, flow_log=None, uw_client=None, test_trigger_now=True))
    await asyncio.sleep(0.1)
    t.cancel()
    try: await t
    except (asyncio.CancelledError, Exception): pass
    assert len(fired) == 1
```

- [ ] **Step 12.2: Run, verify green**

```bash
pytest scripts/tests/test_uw_analyze_daily_job.py -v
```

Expected: PASS

- [ ] **Step 12.3: Commit**

```bash
git add scripts/tests/test_uw_analyze_daily_job.py
git commit -m "test(uw-analyze): _job_running double-start + cancellation coverage (cluster4.3)"
```

---

## Task 13: Frontend Vitest coverage for `useUwPortfolio` and `GexProfileChart`

**Files:**

- Create: `web/tests/useUwPortfolio.test.ts`
- Create: `web/tests/GexProfileChart.test.tsx`

- [ ] **Step 13.1: Inspect existing Vitest setup**

```bash
ls web/tests/ | head; cat web/vitest.config.ts 2>/dev/null || cat web/vitest.config.js 2>/dev/null
```

- [ ] **Step 13.2: Write `useUwPortfolio.test.ts`**

```ts
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useUwPortfolio } from "@/lib/useUwPortfolio";

const okBody = {
  fetched_at: "2026-04-08T20:00:00Z",
  market_state: "open",
  ttl_seconds: 300,
  tickers: [],
  action_items: [],
};

beforeEach(() => {
  vi.useFakeTimers();
  global.fetch = vi
    .fn()
    .mockResolvedValue({ ok: true, json: async () => okBody, status: 200 });
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("useUwPortfolio", () => {
  it("fetches on mount", async () => {
    const { result } = renderHook(() => useUwPortfolio());
    await waitFor(() => expect(result.current.data).toBeTruthy());
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it("polls on the open-market cadence (2 min)", async () => {
    renderHook(() => useUwPortfolio());
    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(1));
    await act(async () => {
      vi.advanceTimersByTime(2 * 60 * 1000);
    });
    expect(global.fetch).toHaveBeenCalledTimes(2);
  });

  it("refreshAll triggers an immediate refetch", async () => {
    const { result } = renderHook(() => useUwPortfolio());
    await waitFor(() => expect(result.current.data).toBeTruthy());
    await act(async () => {
      await result.current.refreshAll();
    });
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/uw-analyze/refresh"),
      expect.anything(),
    );
  });

  it("surfaces errors", async () => {
    (global.fetch as any).mockRejectedValueOnce(new Error("boom"));
    const { result } = renderHook(() => useUwPortfolio());
    await waitFor(() => expect(result.current.error).toBeTruthy());
  });
});
```

- [ ] **Step 13.3: Write `GexProfileChart.test.tsx`**

```tsx
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import GexProfileChart from "@/components/charts/GexProfileChart";

describe("GexProfileChart", () => {
  it("renders bars for each bucket", () => {
    const buckets = [
      { strike: 100, net_gex: 1_000_000, pct_from_spot: -0.02, tag: null },
      {
        strike: 105,
        net_gex: -500_000,
        pct_from_spot: 0.03,
        tag: "call_wall" as const,
      },
    ];
    const { container } = render(
      <GexProfileChart buckets={buckets} spot={102} />,
    );
    const bars = container.querySelectorAll("[data-bar]");
    expect(bars.length).toBe(buckets.length);
  });

  it("renders an empty-state when buckets is empty", () => {
    const { getByText } = render(<GexProfileChart buckets={[]} spot={100} />);
    expect(getByText(/no gex/i)).toBeTruthy();
  });
});
```

(Adjust selectors and props to match the actual `GexProfileChart` API after reading `web/components/charts/GexProfileChart.tsx`.)

- [ ] **Step 13.4: Run**

```bash
npm --prefix web run test -- useUwPortfolio GexProfileChart
```

Expected: PASS

- [ ] **Step 13.5: Commit**

```bash
git add web/tests/useUwPortfolio.test.ts web/tests/GexProfileChart.test.tsx
git commit -m "test(uw-analyze): vitest coverage for useUwPortfolio + GexProfileChart (cluster4.4)"
```

---

## Task 14: Full-stack integration test (M9)

**Files:**

- Create: `scripts/tests/test_uw_analyze_integration.py`

**Goal:** Boot a `TestClient`, mock `_runner` to return realistic snapshots that produce a sweep, hit `/portfolio` twice, advance daily job once via `test_trigger_now`, assert end state surfaces oi_changes + an anomaly through `action_items`.

- [ ] **Step 14.1: Write the integration test**

```python
import asyncio
import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("XENON_TEST_MODE", "1")
    # Repoint persistence to tmp
    monkeypatch.setattr("api.services.uw_analyze_cache._DEFAULT_CACHE_PATH",
                        tmp_path / "cache.json")
    monkeypatch.setattr("api.services.uw_analyze_flow_tracker._DEFAULT_PATH",
                        tmp_path / "flow.json")
    from api.routes import uw_analyze as routes
    routes.reset_state_for_tests()

    # Seed candidates
    monkeypatch.setattr(routes, "seed_candidates", lambda: {"AAPL": ["watchlist"]})

    # Two-call runner: first call has no sweep, second call has +$10M call premium delta
    call_n = {"n": 0}
    async def fake_runner(t):
        call_n["n"] += 1
        ncp = 100_000 if call_n["n"] == 1 else 10_100_000
        report = {"price": 100.0, "regime": {"gex_sign": "POSITIVE"}, "scores": {"flow": 70}}
        display = {"max_pain": 99.0, "iv_rank": 50.0, "net_call_premium": ncp, "net_put_premium": -200_000}
        flow_alerts = [{"option_type": "call", "strike": 105, "expiration_date": "2026-09-15",
                        "total_premium": 11_000_000, "open_interest": 800, "volume": 1500, "mid": 2.5}]
        return report, display, flow_alerts
    monkeypatch.setattr(routes, "_runner", fake_runner)

    from api.server import app as fastapi_app
    return fastapi_app


def test_full_stack_surfaces_change_and_event(app):
    client = TestClient(app)
    r1 = client.get("/uw-analyze/portfolio")  # baseline, no diff yet
    r2 = client.post("/uw-analyze/refresh", json={"tickers": ["AAPL"]})  # force refresh
    assert r2.status_code == 200
    r3 = client.get("/uw-analyze/portfolio")
    body = r3.json()
    row = next(r for r in body["tickers"] if r["ticker"] == "AAPL")
    assert any(c["code"] == "UNUSUAL_CALL_SWEEP" for c in row["changes"])
    assert row["unusual_flow_events"], "flow event must be captured after refresh"
    assert any(a["code"] == "UNUSUAL_CALL_SWEEP" for a in body["action_items"])
```

- [ ] **Step 14.2: Run**

```bash
pytest scripts/tests/test_uw_analyze_integration.py -v
```

Expected: PASS

- [ ] **Step 14.3: Run the coverage gate**

```bash
pytest --cov=scripts/api/services/uw_analyze --cov=scripts/api/routes/uw_analyze \
  scripts/tests/test_uw_analyze_*.py --cov-report=term-missing
```

Expected: ≥95% line coverage on `uw_analyze_*` modules. Add targeted tests for any uncovered branches.

- [ ] **Step 14.4: Commit**

```bash
git add scripts/tests/test_uw_analyze_integration.py
git commit -m "test(uw-analyze): full-stack integration + coverage gate (cluster4.5)"
```

---

## Task 15: E2E browser verification (CLAUDE.md mandatory gate)

**Files:** none — verification only

- [ ] **Step 15.1: Start the API + Next dev server in cloud mode**

```bash
scripts/cloud.sh
```

Wait for `curl http://localhost:8321/health` to return `{"ib_gateway":{"port_listening":true}, ...}`.

- [ ] **Step 15.2: Run chrome-cdp checklist**

Navigate `http://localhost:3000/uw-analyze`. Verify:

1. Rows render for portfolio + watchlist tickers
2. Rows with `changes.length > 0` are auto-expanded and sorted to the top
3. Click toggles row collapse/expand
4. ACTION ITEMS box appears when any row has changes/anomalies
5. **OI delta sub-panel renders** for at least one row (forced via on-demand fetch)
6. **Unusual flow tracker sub-panel renders** for at least one row after a manual refresh that produces a sweep
7. Ad-hoc form adds a ticker and triggers a fetch
8. Manual refresh-all fires `/api/uw-analyze/refresh` (verify in DevTools network tab)
9. Brand check: `border-radius` ≤ 4px on `.section`, no inline raw hex

- [ ] **Step 15.3: Capture screenshots, attach to PR**

- [ ] **Step 15.4: Tag the branch ready-to-merge**

```bash
git tag uw-analyze-overhaul-fixes-ready
```

---

## End-to-end verification (run before opening PR)

```bash
# Backend
pytest scripts/tests/test_uw_analyze_*.py scripts/tests/test_analysis_ticker_data.py -v
pytest --cov=scripts/api/services/uw_analyze --cov=scripts/api/routes/uw_analyze \
  scripts/tests/test_uw_analyze_*.py --cov-report=term-missing

# Frontend
npm --prefix web run test -- useUwPortfolio GexProfileChart

# Live API
curl http://localhost:8321/health
curl http://localhost:8321/uw-analyze/portfolio | jq '.tickers[0] | {ticker, changes, oi_changes, unusual_flow_events}'

# Daily-job smoke
UW_ANALYZE_JOB_TEST_TRIGGER=now scripts/cloud.sh   # tail logs for "uw_analyze_daily_job run_once stats"
ls data/uw_oi_snapshots/                            # one file per cached ticker
```

## Issue → Task traceability matrix

| Tribunal Issue                                    | Severity  | Task                     |
| ------------------------------------------------- | --------- | ------------------------ |
| 1. flow_alerts missing from snapshot              | CRITICAL  | Task 1                   |
| 2. Daily job has no contract_fetcher in prod      | CRITICAL  | Task 6                   |
| 3. Re-capture on every GET creates phantom events | IMPORTANT | Task 3                   |
| 4. Closing-volume-spike rule missing              | IMPORTANT | Task 4                   |
| 5. OI evaporation uses calendar days              | IMPORTANT | Task 5                   |
| 6. Anomaly events can't close/expire              | IMPORTANT | Task 6                   |
| 7. `date.today()` instead of ET dates             | IMPORTANT | Task 5                   |
| 8. Legacy `_cache` not deleted                    | IMPORTANT | Task 7                   |
| 9. M4 OI snapshot persistence missing             | IMPORTANT | Task 2                   |
| 10. /portfolio loop is serial                     | IMPORTANT | Task 8                   |
| 11. Test fixture fabricates flow_alerts           | IMPORTANT | Task 1 (fixture rewrite) |
| 12. test_call_sweep_null_skipped no assert        | IMPORTANT | Task 10                  |
| 13. No max_pain regression test                   | IMPORTANT | Task 11                  |
| 14. No useUwPortfolio/GexProfileChart tests       | IMPORTANT | Task 13                  |
| 15. No `_job_running`/cancel test                 | MINOR     | Task 12                  |
| Gemini A. Daily job duplicates under workers>1    | IMPORTANT | Task 9                   |
| Gemini B. Lifespan no try/finally                 | IMPORTANT | Task 9                   |
| (Plan §M9) Integration test + coverage gate       | —         | Task 14                  |
| (CLAUDE.md mandate) E2E browser verification      | —         | Task 15                  |
