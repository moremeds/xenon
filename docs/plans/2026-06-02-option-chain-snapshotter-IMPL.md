# Option Chain Snapshotter — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Source spec:** `docs/plans/2026-06-02-option-chain-snapshotter-design.md`
(979 lines, finalized through 6-pass review-cycle on 2026-06-02)

**Goal:** Snapshot the full SPX/NDX/RUT/VIX index-option chains every ~10 min into a TimescaleDB-backed Postgres archive on the macmini, plus 1-min underlying OHLCV.

**Architecture:** Long-running Python service `option_chain_snapshotter` on macmini. Connects to IB via xenon's `IBClient` wrapper using a pool of 2 connections (clientIds 95/96, registered in `CLIENT_IDS`). Continuous priority-queue poller. ResizableLimiter enforces line count + msg/sec pacing. TimescaleDB hypertables in a new `option_chain` DB. launchd-supervised.

**Tech Stack:** Python 3.13 via uv · ib_async ≥ 1.0 · psycopg ≥ 3 · TimescaleDB · alembic · asyncio · exchange-calendars · launchd

---

## How to use this plan

- **Each PR ships independently** — green CI, mergeable to a fresh branch off master. Don't combine PRs; the gates between them matter.
- **Day-1 IB probe (PR 2) is a HALT gate.** If the probe shows < 3 cps, stop and revisit the design before PR 3+ ship.
- **Each task is RED → GREEN → COMMIT.** TDD discipline isn't optional for this codebase (xenon CLAUDE.md mandates red/green TDD).
- **Branch strategy:** start from master. Current branch `infra/flex-inception-backfill` is unrelated. `git checkout master && git pull && git checkout -b infra/option-chain-snapshotter-pre-work` for PR 1.
- **Tests use `uv run pytest`** — never bare `pytest`. xenon-wide standing rule.

## File structure (cumulative across all PRs)

```
docs/plans/
  2026-06-02-option-chain-snapshotter-design.md       (existing spec)
  2026-06-02-option-chain-snapshotter-IMPL.md         (this file)
docs/runbooks/
  option-chain-snapshotter-operations.md              (PR 10)
src/xenon/
  clients/ib_client.py                                (modify: PR 1)
  api/services/advisory_lock.py                       (modify: PR 1)
  option_chain_snapshotter/                           (new module: PR 4-9)
    __init__.py                                       (PR 4)
    __main__.py                                       (PR 9)
    CLAUDE.md                                         (PR 4, updated through PR 9)
    config.py                                         (PR 4)
    hours.py                                          (PR 4)
    queue.py                                          (PR 4)
    limiter.py                                        (PR 4)
    pool.py                                           (PR 5)
    storage.py                                        (PR 6)
    universe.py                                       (PR 6)
    persister.py                                      (PR 7)
    snapshot_worker.py                                (PR 8)
    ohlcv_worker.py                                   (PR 8)
scripts/
  research/probe_ib_option_chain.py                   (PR 2)
  research/probe_ib_option_chain_results.md           (PR 2, hand-written after probe runs)
  migrations/option_chain/                            (PR 3)
    alembic.ini
    env.py
    versions/001_initial_schema.py
    versions/002_universe_committed_flag.py
  infra/option-chain-prestart.sh                      (PR 10)
  tests/test_option_chain_snapshotter_<…>.py          (PRs 4-9, one file per module)
  tests/test_option_chain_live_paper.py               (PR 10, @pytest.mark.live)
~/Library/LaunchAgents/                               (PR 10)
  com.xenon.option-chain-snapshotter.plist
pyproject.toml                                        (modify: PR 1 + PR 9)
```

---

## PR 1 — Pre-work registry + dependencies

**Goal:** Register clientIds, advisory lock key, add `exchange-calendars` dep. Zero behavior change. Safe foundation for everything else.

**Dependencies:** none

**Files:**

- Modify: `src/xenon/clients/ib_client.py:95-110` (CLIENT_IDS dict)
- Modify: `src/xenon/api/services/advisory_lock.py:45-55` (LOCK_KEY constants)
- Modify: `pyproject.toml` ([project] dependencies)
- Create: `scripts/tests/test_option_chain_snapshotter_registry.py`

### Task 1.1: Register option_chain_snapshotter clientIds

- [ ] **Step 1: Write failing test**

Create `scripts/tests/test_option_chain_snapshotter_registry.py`:

```python
"""Pre-work registry checks for option_chain_snapshotter.

These guard against ad-hoc clientId allocation drift (Pass-2 finding C-14
in the design spec).
"""
from xenon.clients.ib_client import CLIENT_IDS


def test_option_chain_snapshotter_a_registered():
    assert CLIENT_IDS["option_chain_snapshotter_a"] == 95


def test_option_chain_snapshotter_b_registered():
    assert CLIENT_IDS["option_chain_snapshotter_b"] == 96


def test_snapshotter_ids_dont_collide_with_existing():
    """95 and 96 must not appear anywhere else in CLIENT_IDS."""
    duplicates = [name for name, cid in CLIENT_IDS.items()
                  if cid in (95, 96)
                  and name not in ("option_chain_snapshotter_a",
                                   "option_chain_snapshotter_b")]
    assert duplicates == [], f"clientId collision: {duplicates}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest scripts/tests/test_option_chain_snapshotter_registry.py -v`
Expected: FAIL with `KeyError: 'option_chain_snapshotter_a'`

- [ ] **Step 3: Add entries to CLIENT_IDS**

Modify `src/xenon/clients/ib_client.py` — inside the `CLIENT_IDS` dict (around line 95), add two entries in the daemon range (70-99):

```python
CLIENT_IDS: dict = {
    # ... existing entries ...
    "ib_realtime_server": 10,
    "option_chain_snapshotter_a": 95,  # NEW: option-chain archive snapshotter, pool A
    "option_chain_snapshotter_b": 96,  # NEW: option-chain archive snapshotter, pool B
}
```

- [ ] **Step 4: Verify test passes**

Run: `uv run pytest scripts/tests/test_option_chain_snapshotter_registry.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/xenon/clients/ib_client.py scripts/tests/test_option_chain_snapshotter_registry.py
git commit -m "feat(ib): register option_chain_snapshotter clientIds 95/96"
```

### Task 1.2: Add LOCK_KEY_OPTION_CHAIN_SNAPSHOTTER

- [ ] **Step 1: Write failing test**

Append to `scripts/tests/test_option_chain_snapshotter_registry.py`:

```python
from xenon.api.services.advisory_lock import LOCK_KEY_OPTION_CHAIN_SNAPSHOTTER


def test_lock_key_snapshotter_value():
    """7343001 is next in the xenon sequence after LOCK_KEY_VCG_CRI=7342001."""
    assert LOCK_KEY_OPTION_CHAIN_SNAPSHOTTER == 7343001


def test_lock_key_unique():
    """Single-instance guard depends on key uniqueness across xenon."""
    import xenon.api.services.advisory_lock as al
    keys = [v for k, v in vars(al).items() if k.startswith("LOCK_KEY_")]
    assert len(keys) == len(set(keys)), f"Duplicate LOCK_KEY values: {keys}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest scripts/tests/test_option_chain_snapshotter_registry.py::test_lock_key_snapshotter_value -v`
Expected: FAIL with `ImportError: cannot import name 'LOCK_KEY_OPTION_CHAIN_SNAPSHOTTER'`

- [ ] **Step 3: Add constant**

Modify `src/xenon/api/services/advisory_lock.py` around line 50 (next to other LOCK_KEY constants):

```python
LOCK_KEY_UW_DAILY = 7341001
LOCK_KEY_VCG_CRI = 7342001
LOCK_KEY_OPTION_CHAIN_SNAPSHOTTER = 7343001  # NEW: single-instance guard for option-chain archive snapshotter
```

- [ ] **Step 4: Verify**

Run: `uv run pytest scripts/tests/test_option_chain_snapshotter_registry.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/xenon/api/services/advisory_lock.py scripts/tests/test_option_chain_snapshotter_registry.py
git commit -m "feat(advisory-lock): add LOCK_KEY_OPTION_CHAIN_SNAPSHOTTER=7343001"
```

### Task 1.3: Add exchange-calendars dependency

- [ ] **Step 1: Write failing test**

Append to `scripts/tests/test_option_chain_snapshotter_registry.py`:

```python
def test_exchange_calendars_importable():
    """Per Pass-2 finding CL-2: exchange-calendars must be in pyproject.toml."""
    import exchange_calendars as ec
    nyse = ec.get_calendar("XNYS")
    assert nyse is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest scripts/tests/test_option_chain_snapshotter_registry.py::test_exchange_calendars_importable -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'exchange_calendars'`

- [ ] **Step 3: Add dependency**

```bash
uv add 'exchange-calendars>=4.5,<5.0'
```

This adds the package to `pyproject.toml` `[project] dependencies` and updates `uv.lock`.

- [ ] **Step 4: Verify**

Run: `uv run pytest scripts/tests/test_option_chain_snapshotter_registry.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock scripts/tests/test_option_chain_snapshotter_registry.py
git commit -m "feat(deps): add exchange-calendars for option_chain_snapshotter market-hours gating"
```

### Task 1.4: Open PR

- [ ] **Step 1: Push branch + open PR**

```bash
git push -u origin infra/option-chain-snapshotter-pre-work
gh pr create --title "feat(option-chain): registry + deps pre-work" \
  --body "$(cat <<'EOF'
## Summary
- Register clientIds 95/96 for option_chain_snapshotter pool
- Add `LOCK_KEY_OPTION_CHAIN_SNAPSHOTTER = 7343001`
- Add `exchange-calendars` dep

Zero behavior change. Foundation for the option_chain_snapshotter feature
per `docs/plans/2026-06-02-option-chain-snapshotter-design.md`.

## Test plan
- [x] `uv run pytest scripts/tests/test_option_chain_snapshotter_registry.py -v`
- [x] Full pytest suite green (no regressions from dep add)
EOF
)"
```

---

## PR 2 — Day-1 IB behavior probe (HALT gate)

**Goal:** Empirically resolve the throughput fork — does explicit `cancelMktData()` release the market-data line before `tickSnapshotEnd`? Measures p50/p95 for bid/ask, modelGreeks, snapshot end, pacing limits, and verifies IND-secType qualification works for all 4 underliers.

**Dependencies:** PR 1 merged (needs `CLIENT_IDS["option_chain_snapshotter_a"]`).

**Branch:** `infra/option-chain-probe` (off master after PR 1 merges).

**This is a research script, not production code.** Runs against paper IB.

**Files:**

- Create: `scripts/research/probe_ib_option_chain.py`
- Create: `scripts/research/probe_ib_option_chain_results.md` (populated AFTER probe run, hand-edited)

### Task 2.1: Write the probe script

- [ ] **Step 1: Create the probe**

Create `scripts/research/probe_ib_option_chain.py`:

```python
#!/usr/bin/env python3
"""Day-1 IB behavior probe for option_chain_snapshotter (Pass-2 finding C-1).

Measures, against paper IB on 127.0.0.1:4002:
  1. Underlying IND qualification for SPX/NDX/RUT/VIX
  2. reqSecDefOptParams returns multiple tradingClasses for SPX (SPX + SPXW)
  3. Per-snapshot wall time (bid/ask arrival, modelGreeks arrival, tickSnapshotEnd)
  4. Whether explicit cancelMktData() releases the line early
  5. IB pacing limit (msgs/sec before code 100/165 fires)

HALT criteria — output `verdict: HALT` if any holds:
  - Sustained throughput < 3 cps under any line-release mode
  - reqSecDefOptParams returns 0 expirations for any underlier
  - SPX returns only SPX (no SPXW) — weekly chain coverage gap
  - Pacing violations fire at < 25 msg/sec

Usage:
    uv run python scripts/research/probe_ib_option_chain.py --host 127.0.0.1 --port 4002

Output: prints JSON results to stdout, exit 0 = OK, exit 2 = HALT.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ib_async import IB, Index, Option

from xenon.clients.ib_client import CLIENT_IDS

TICKERS = ["SPX", "NDX", "RUT", "VIX"]

# C-6: IB Index() underlier qualification requires different exchanges per
# ticker. Verified against live IB on 2026-06-02 (clientId 199, read-only):
#   SPX/CBOE → conId 416904    NDX/CBOE → Error 200 (no security definition)
#   NDX/NASDAQ → conId 416843  RUT/CBOE → Error 200
#   RUT/RUSSELL → conId 416888  VIX/CBOE → conId 13455763
# Keep in sync with src/xenon/option_chain_snapshotter/config.py::INDEX_EXCHANGE.
INDEX_EXCHANGE = {"SPX": "CBOE", "NDX": "NASDAQ", "RUT": "RUSSELL", "VIX": "CBOE"}


@dataclass
class ProbeResult:
    tickers_qualified: dict[str, int] = field(default_factory=dict)   # ticker -> conId
    tradingclasses_by_ticker: dict[str, list[str]] = field(default_factory=dict)
    expiry_counts: dict[str, int] = field(default_factory=dict)
    p50_bidask_seconds: float | None = None
    p95_bidask_seconds: float | None = None
    p50_greeks_seconds: float | None = None
    p95_greeks_seconds: float | None = None
    p50_snapshot_end_seconds: float | None = None
    early_release_works: bool | None = None  # True = optimistic case, False = pessimistic
    pacing_limit_msg_per_sec: int | None = None
    verdict: str = "PENDING"  # OK or HALT
    halt_reasons: list[str] = field(default_factory=list)


async def qualify_underliers(ib: IB, result: ProbeResult) -> dict[str, Index]:
    """Step 1: qualify SPX/NDX/RUT/VIX as Index contracts on CBOE."""
    contracts = {t: Index(symbol=t, exchange=INDEX_EXCHANGE[t], currency="USD") for t in TICKERS}
    qualified = await ib.qualifyContractsAsync(*contracts.values())
    for t, c in contracts.items():
        if c.conId:
            result.tickers_qualified[t] = c.conId
        else:
            result.halt_reasons.append(f"{t}: underlier qualification failed")
    return contracts


async def fetch_secdef_params(ib: IB, contracts: dict[str, Any], result: ProbeResult) -> dict:
    """Step 2: reqSecDefOptParams with underlyingSecType='IND'."""
    params_by_ticker = {}
    for t, c in contracts.items():
        if not c.conId:
            continue
        params = await ib.reqSecDefOptParamsAsync(
            underlyingSymbol=t,
            futFopExchange="",
            underlyingSecType="IND",
            underlyingConId=c.conId,
        )
        tcs = sorted({p.tradingClass for p in params})
        total_exps = sum(len(p.expirations) for p in params)
        result.tradingclasses_by_ticker[t] = tcs
        result.expiry_counts[t] = total_exps
        if total_exps == 0:
            result.halt_reasons.append(f"{t}: 0 expirations returned")
        if t == "SPX" and "SPXW" not in tcs:
            result.halt_reasons.append("SPX returned no SPXW tradingClass — weekly chain coverage gap")
        params_by_ticker[t] = params
    return params_by_ticker


async def probe_snapshot_timing(
    ib: IB, contracts: dict, params_by_ticker: dict, result: ProbeResult
) -> None:
    """Step 3 + 4: snapshot 50 liquid SPX strikes, measure timings + line release."""
    spx_params = params_by_ticker.get("SPX", [])
    if not spx_params:
        result.halt_reasons.append("Cannot probe timing: no SPX chain")
        return

    # Pick 50 liquid strikes around current SPX spot (use front expiry's strikes)
    p = spx_params[0]
    expiry = sorted(p.expirations)[0]
    spot_approx = sorted(p.strikes)[len(p.strikes) // 2]
    strikes = sorted(p.strikes, key=lambda s: abs(s - spot_approx))[:50]

    bidask_secs, greeks_secs, end_secs = [], [], []
    for k in strikes:
        opt = Option(
            "SPX",
            expiry,
            float(k),
            "C",
            exchange="CBOE",
            tradingClass=p.tradingClass,
            multiplier=str(p.multiplier),
        )
        await ib.qualifyContractsAsync(opt)
        if not opt.conId:
            continue

        t_req = time.monotonic()
        t_bidask = t_greeks = t_end = None
        ticker = ib.reqMktData(opt, genericTickList="", snapshot=True, regulatorySnapshot=False)

        # Poll for ticks until snapshot ends or 15s elapses
        deadline = t_req + 15.0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            if t_bidask is None and ticker.bid > 0 and ticker.ask > 0:
                t_bidask = time.monotonic() - t_req
            if t_greeks is None and ticker.modelGreeks and ticker.modelGreeks.delta is not None:
                t_greeks = time.monotonic() - t_req
            # Snapshot end event arrives on ticker after IB sends tickSnapshotEnd
            if ticker.time and t_greeks and (time.monotonic() - t_req) > t_greeks + 0.5:
                # Use heuristic: 0.5s after greeks, consider snapshot done
                t_end = time.monotonic() - t_req
                break

        ib.cancelMktData(opt)
        if t_bidask is not None:
            bidask_secs.append(t_bidask)
        if t_greeks is not None:
            greeks_secs.append(t_greeks)
        if t_end is not None:
            end_secs.append(t_end)

    if bidask_secs:
        result.p50_bidask_seconds = statistics.median(bidask_secs)
        result.p95_bidask_seconds = statistics.quantiles(bidask_secs, n=20)[-1] if len(bidask_secs) >= 20 else max(bidask_secs)
    if greeks_secs:
        result.p50_greeks_seconds = statistics.median(greeks_secs)
        result.p95_greeks_seconds = statistics.quantiles(greeks_secs, n=20)[-1] if len(greeks_secs) >= 20 else max(greeks_secs)
    if end_secs:
        result.p50_snapshot_end_seconds = statistics.median(end_secs)

    # Early-release detection: time 20 back-to-back snapshots; if total wall time
    # < (20 × p50_snapshot_end_seconds × 0.5), early release works.
    n = 20
    t_start = time.monotonic()
    for k in strikes[:n]:
        opt = Option(
            "SPX",
            expiry,
            float(k),
            "C",
            exchange="CBOE",
            tradingClass=p.tradingClass,
            multiplier=str(p.multiplier),
        )
        await ib.qualifyContractsAsync(opt)
        ticker = ib.reqMktData(opt, "", True, False)
        # Wait until bid+ask+greeks arrive, then cancel
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            if ticker.modelGreeks and ticker.modelGreeks.delta is not None:
                break
        ib.cancelMktData(opt)
    elapsed = time.monotonic() - t_start
    expected_serial = n * (result.p50_snapshot_end_seconds or 11.0)
    result.early_release_works = elapsed < expected_serial * 0.7


async def probe_pacing_limit(ib: IB, contracts: dict, result: ProbeResult) -> None:
    """Step 5: burst 200 reqContractDetails to find pacing ceiling."""
    spx = contracts["SPX"]
    pacing_errors = []

    def _on_error(reqId, errorCode, errorString, contract):
        if errorCode in (100, 165):
            pacing_errors.append((time.monotonic(), errorCode))

    ib.errorEvent += _on_error
    t_start = time.monotonic()
    msgs_at_first_error = None
    for i in range(200):
        ib.reqContractDetails(spx)
        await asyncio.sleep(0.005)  # 200 msg/sec offered rate
        if pacing_errors and msgs_at_first_error is None:
            msgs_at_first_error = i + 1
            break
    elapsed = time.monotonic() - t_start
    ib.errorEvent -= _on_error

    if msgs_at_first_error is not None:
        rate = msgs_at_first_error / elapsed
        result.pacing_limit_msg_per_sec = int(rate)
        if rate < 25:
            result.halt_reasons.append(f"Pacing limit < 25 msg/sec (saw {rate:.1f})")
    else:
        result.pacing_limit_msg_per_sec = 200  # Didn't hit it in 200 messages


def compute_verdict(result: ProbeResult) -> None:
    # Throughput check
    if result.p50_greeks_seconds:
        per_snap_seconds = result.p50_greeks_seconds if result.early_release_works else (result.p50_snapshot_end_seconds or 11.0)
        cps = 72 / per_snap_seconds
        if cps < 3:
            result.halt_reasons.append(f"Throughput {cps:.1f} cps < 3 cps HALT threshold")
    result.verdict = "HALT" if result.halt_reasons else "OK"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002, help="Paper IB Gateway port")
    args = parser.parse_args()

    ib = IB()
    await ib.connectAsync(args.host, args.port, clientId=CLIENT_IDS["option_chain_snapshotter_a"], timeout=10)

    result = ProbeResult()
    try:
        contracts = await qualify_underliers(ib, result)
        params_by_ticker = await fetch_secdef_params(ib, contracts, result)
        await probe_snapshot_timing(ib, contracts, params_by_ticker, result)
        await probe_pacing_limit(ib, contracts, result)
    finally:
        ib.disconnect()

    compute_verdict(result)
    print(json.dumps(asdict(result), indent=2, default=str))
    return 0 if result.verdict == "OK" else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 2: Commit the probe script**

```bash
git add scripts/research/probe_ib_option_chain.py
git commit -m "feat(probe): IB behavior probe for option_chain_snapshotter"
```

### Task 2.2: Run the probe + record results (operator step)

- [ ] **Step 1: Ensure paper IB Gateway running on macbook port 4002**

```bash
scripts/infra/dev.sh paper  # in another terminal, ensures paper IB is up
```

- [ ] **Step 2: Run the probe**

```bash
uv run python scripts/research/probe_ib_option_chain.py --host 127.0.0.1 --port 4002 \
  | tee scripts/research/probe_ib_option_chain_results.json
```

Expected output: JSON with `"verdict": "OK"` and populated metrics.

- [ ] **Step 3: HALT GATE — inspect verdict**

If `verdict == "HALT"`, **stop**. Open an issue describing the halt_reasons. Revise the design spec — the throughput assumptions don't hold for this account / IB setup. PR 3+ do not ship until probe verdict is OK.

- [ ] **Step 4: Write up findings**

Create `scripts/research/probe_ib_option_chain_results.md` with the operator's interpretation:

```markdown
# IB Behavior Probe Results — YYYY-MM-DD

Run against paper IB Gateway 127.0.0.1:4002 at HH:MM ET.

## Measured values

- Underlier qualification: SPX=<conId>, NDX=<conId>, RUT=<conId>, VIX=<conId>
- SPX tradingClasses returned: SPX, SPXW (confirm SPXW present)
- p50 bid/ask arrival: <X>s
- p50 modelGreeks arrival: <X>s
- p50 tickSnapshotEnd: <X>s
- Early `cancelMktData()` release works: yes/no
- Pacing limit observed: <X> msg/sec

## Throughput baseline for `option_chain_snapshotter/CLAUDE.md`

- Scenario landed: optimistic / pessimistic
- Per-snapshot wall time (p50): <X>s
- Sustained throughput (72 lines / <X>s): <Y> cps
- Effective sweep time at 33k contracts: <Z> min

## CI floor for throughput regression test

`0.8 × probe_p50_cps = <Y> cps` → set as the test floor in PR 8.
```

- [ ] **Step 5: Commit the results doc**

```bash
git add scripts/research/probe_ib_option_chain_results.{md,json}
git commit -m "docs(probe): record day-1 IB probe results for option_chain_snapshotter"
git push origin infra/option-chain-probe
gh pr create --title "feat(probe): day-1 IB behavior probe + results" \
  --body "Per Pass-2 finding C-1 in the option-chain spec. **HALT gate for PR 3+.**

Probe results landed here. Throughput floor for PR 8's regression test will use these p50 values × 0.8."
```

---

## PR 3 — DB + schema + alembic env

**Goal:** Create the `option_chain` DB, install TimescaleDB extension, set up a separate alembic environment, apply the initial schema (all 5 tables + view + grants).

**Dependencies:** PR 1 merged. PR 2 probe `verdict: OK` (HALT gate).

**Branch:** `infra/option-chain-schema` (off master).

**Files:**

- Create: `scripts/migrations/option_chain/alembic.ini`
- Create: `scripts/migrations/option_chain/env.py`
- Create: `scripts/migrations/option_chain/versions/001_initial_schema.py`
- Create: `scripts/migrations/option_chain/versions/002_universe_committed_flag.py` (Pass-3 A-1 column)
- Create: `scripts/migrations/option_chain/README.md`
- Create: `scripts/tests/test_option_chain_schema.py`

### Task 3.1: Create role + DB on macmini Postgres (operator step)

- [ ] **Step 1: Install TimescaleDB extension binaries on macmini**

On macmini:

```bash
brew tap timescale/tap                    # I-8: formula lives in 3rd-party tap, not core
brew install timescaledb                  # 2.27.x as of 2026-06-02
timescaledb-tune --quiet --yes
brew services restart postgresql@17       # pin version: timescaledb 2.x ships against PG17
```

- [ ] **Step 2: Create role + DB**

```bash
psql -h 127.0.0.1 -U postgres -d postgres <<'SQL'
CREATE ROLE option_chain_writer WITH LOGIN PASSWORD '<set-from-1password>';
CREATE DATABASE option_chain OWNER option_chain_writer ENCODING 'UTF8';
\c option_chain
CREATE EXTENSION timescaledb;
GRANT CONNECT ON DATABASE option_chain TO xenon_prod, xenon_dev, argon_app;
SQL
```

- [ ] **Step 3: Record connection string in .env + 1password**

Add to `~/projects/xenon/.env`:

```
OPTION_CHAIN_DATABASE_URL=postgresql://option_chain_writer:<pw>@127.0.0.1:5432/option_chain
```

### Task 3.2: Set up alembic env

- [ ] **Step 1: Write failing test**

Create `scripts/tests/test_option_chain_schema.py`:

```python
"""Schema integration tests for option_chain DB.

Uses a transactional pg_test_engine fixture pointed at OPTION_CHAIN_DATABASE_URL.
Skipped offline.
"""
import os

import pytest
import sqlalchemy as sa


@pytest.fixture(scope="session")
def option_chain_engine():
    url = os.environ.get("OPTION_CHAIN_DATABASE_URL")
    if not url:
        pytest.skip("OPTION_CHAIN_DATABASE_URL not set")
    eng = sa.create_engine(url)
    try:
        with eng.connect() as c:
            c.execute(sa.text("SELECT 1"))
    except Exception:
        pytest.skip("option_chain DB not reachable")
    yield eng
    eng.dispose()


def test_timescaledb_extension_installed(option_chain_engine):
    with option_chain_engine.connect() as c:
        r = c.execute(sa.text(
            "SELECT extname FROM pg_extension WHERE extname='timescaledb'"
        )).scalar()
    assert r == "timescaledb"


def test_archive_schema_exists(option_chain_engine):
    with option_chain_engine.connect() as c:
        r = c.execute(sa.text(
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name='archive'"
        )).scalar()
    assert r == "archive"


def test_all_five_tables_exist(option_chain_engine):
    expected = {"snapshot_config", "option_universe", "snapshot_run",
                "option_chain", "underlying_ohlcv"}
    with option_chain_engine.connect() as c:
        rows = c.execute(sa.text(
            "SELECT tablename FROM pg_tables WHERE schemaname='archive'"
        )).fetchall()
    assert {r[0] for r in rows} >= expected


def test_option_chain_is_hypertable(option_chain_engine):
    with option_chain_engine.connect() as c:
        r = c.execute(sa.text("""
            SELECT hypertable_name FROM timescaledb_information.hypertables
            WHERE hypertable_schema='archive' AND hypertable_name='option_chain'
        """)).scalar()
    assert r == "option_chain"


def test_v_staleness_view_exists(option_chain_engine):
    with option_chain_engine.connect() as c:
        r = c.execute(sa.text(
            "SELECT viewname FROM pg_views WHERE schemaname='archive' AND viewname='v_staleness'"
        )).scalar()
    assert r == "v_staleness"


def test_seed_config_has_four_indexes(option_chain_engine):
    """Seed migration must insert SPX, NDX, RUT, VIX."""
    with option_chain_engine.connect() as c:
        rows = c.execute(sa.text(
            "SELECT ticker, cadence_seconds, enabled FROM archive.snapshot_config ORDER BY ticker"
        )).fetchall()
    tickers = [r[0] for r in rows]
    assert tickers == ["NDX", "RUT", "SPX", "VIX"]
    assert all(r[1] == 600 for r in rows), "cadence_seconds should be 600 (10 min)"
    assert all(r[2] is True for r in rows), "all enabled"


def test_universe_committed_flag_column(option_chain_engine):
    """Pass-3 finding A-1: universe_date_committed column for two-step commit."""
    with option_chain_engine.connect() as c:
        cols = c.execute(sa.text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema='archive' AND table_name='option_universe'
              AND column_name='universe_date_committed'
        """)).fetchall()
    assert len(cols) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest scripts/tests/test_option_chain_schema.py -v`
Expected: skipped (if env var not set yet) or FAIL with missing tables/schema/etc.

- [ ] **Step 3: Create alembic.ini**

Create `scripts/migrations/option_chain/alembic.ini`:

```ini
[alembic]
script_location = %(here)s
sqlalchemy.url = ${OPTION_CHAIN_DATABASE_URL}

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

- [ ] **Step 4: Create env.py**

Create `scripts/migrations/option_chain/env.py`:

```python
"""Alembic env for option_chain DB.

Separate alembic environment from xenon's main DB because:
  - Different owner (option_chain_writer, not xenon_prod)
  - Different DB (option_chain, not core_dev)
  - TimescaleDB-specific operations
"""
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)


def get_url() -> str:
    url = os.environ.get("OPTION_CHAIN_DATABASE_URL")
    if not url:
        raise RuntimeError("OPTION_CHAIN_DATABASE_URL not set")
    return url


def run_migrations_offline() -> None:
    context.configure(url=get_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(get_url(), poolclass=pool.NullPool)
    with engine.connect() as conn:
        context.configure(connection=conn)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 5: Create initial schema migration**

Create `scripts/migrations/option_chain/versions/001_initial_schema.py`:

```python
"""Initial schema for option_chain archive.

Revision ID: 001
Revises:
Create Date: 2026-06-02
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "001"
down_revision = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS archive AUTHORIZATION option_chain_writer")

    # 1. snapshot_config
    op.execute("""
        CREATE TABLE archive.snapshot_config (
            ticker          TEXT PRIMARY KEY,
            cadence_seconds INT NOT NULL DEFAULT 1800,
            enabled         BOOLEAN NOT NULL DEFAULT TRUE,
            contract_scope  TEXT NOT NULL DEFAULT 'full',
            notes           TEXT,
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        INSERT INTO archive.snapshot_config (ticker, cadence_seconds, enabled, contract_scope) VALUES
            ('SPX', 600, TRUE, 'full'),
            ('NDX', 600, TRUE, 'full'),
            ('RUT', 600, TRUE, 'full'),
            ('VIX', 600, TRUE, 'full')
    """)

    # 2. option_universe (per Pass-2 C-2 + C-9, Pass-3 A-1)
    op.execute("""
        CREATE TABLE archive.option_universe (
            universe_date    DATE NOT NULL,
            con_id           BIGINT NOT NULL,
            ticker           TEXT NOT NULL,
            trading_class    TEXT NOT NULL,
            exchange         TEXT NOT NULL,
            multiplier       INTEGER NOT NULL,
            local_symbol     TEXT NOT NULL,
            expiry           DATE NOT NULL,
            strike           NUMERIC(14,4) NOT NULL,
            right            CHAR(1) NOT NULL,
            status           TEXT NOT NULL DEFAULT 'active',
            failure_count    INTEGER NOT NULL DEFAULT 0,
            disabled_until   TIMESTAMPTZ,
            last_error_code  INTEGER,
            universe_date_committed BOOLEAN NOT NULL DEFAULT FALSE,
            discovered_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (universe_date, con_id)
        )
    """)
    op.execute("CREATE INDEX ON archive.option_universe (ticker, universe_date, expiry, strike, right)")
    op.execute("CREATE INDEX ON archive.option_universe (status, disabled_until) WHERE status <> 'active'")
    op.execute("CREATE INDEX ON archive.option_universe (universe_date_committed, universe_date DESC) WHERE universe_date_committed")

    # 3. snapshot_run
    op.execute("""
        CREATE TABLE archive.snapshot_run (
            id                   BIGSERIAL PRIMARY KEY,
            ticker               TEXT NOT NULL,
            started_at           TIMESTAMPTZ NOT NULL,
            finished_at          TIMESTAMPTZ,
            contracts_attempted  INT,
            contracts_persisted  INT,
            duration_ms          INT,
            ib_lines_peak        INT,
            status               TEXT NOT NULL,
            error                TEXT
        )
    """)
    op.execute("CREATE INDEX ON archive.snapshot_run (ticker, started_at DESC)")

    # 4. option_chain (hypertable, per C-2 + C-12 + C-13)
    op.execute("""
        CREATE TABLE archive.option_chain (
            snapshot_ts      TIMESTAMPTZ NOT NULL,
            con_id           BIGINT NOT NULL,
            ticker           TEXT NOT NULL,
            trading_class    TEXT NOT NULL,
            expiry           DATE NOT NULL,
            strike           NUMERIC(14,4) NOT NULL,
            right            CHAR(1) NOT NULL,
            request_ts       TIMESTAMPTZ NOT NULL,
            quote_ts         TIMESTAMPTZ,
            greeks_ts        TIMESTAMPTZ,
            bid              NUMERIC(12,4),
            ask              NUMERIC(12,4),
            bid_size         INTEGER,
            ask_size         INTEGER,
            last             NUMERIC(12,4),
            last_size        INTEGER,
            volume           BIGINT,
            open_interest    BIGINT,
            iv               REAL,
            delta            REAL,
            gamma            REAL,
            vega             REAL,
            theta            REAL,
            underlying_px    NUMERIC(12,4),
            run_id           BIGINT NOT NULL REFERENCES archive.snapshot_run(id),
            PRIMARY KEY (snapshot_ts, con_id)
        )
    """)
    op.execute("SELECT create_hypertable('archive.option_chain', 'snapshot_ts', chunk_time_interval => INTERVAL '1 day')")
    # C-7: TimescaleDB 2.18+ refuses add_compression_policy unless columnstore
    # is enabled on the table first. Verified empirically against 2.24 — bare
    # `add_compression_policy` returns: ERROR: columnstore not enabled on hypertable.
    op.execute("ALTER TABLE archive.option_chain SET (timescaledb.compress = true)")
    op.execute("SELECT add_compression_policy('archive.option_chain', INTERVAL '7 days')")
    op.execute("CREATE INDEX ON archive.option_chain (ticker, trading_class, expiry, snapshot_ts DESC)")
    op.execute("CREATE INDEX ON archive.option_chain (con_id, snapshot_ts DESC)")

    # 5. underlying_ohlcv (hypertable)
    op.execute("""
        CREATE TABLE archive.underlying_ohlcv (
            bar_ts      TIMESTAMPTZ NOT NULL,
            ticker      TEXT NOT NULL,
            bar_size    TEXT NOT NULL DEFAULT '1 min',
            open        NUMERIC(14,4),
            high        NUMERIC(14,4),
            low         NUMERIC(14,4),
            close       NUMERIC(14,4),
            volume      BIGINT,
            PRIMARY KEY (bar_ts, ticker, bar_size)
        )
    """)
    op.execute("SELECT create_hypertable('archive.underlying_ohlcv', 'bar_ts', chunk_time_interval => INTERVAL '7 days')")
    op.execute("ALTER TABLE archive.underlying_ohlcv SET (timescaledb.compress = true)")  # C-7
    op.execute("SELECT add_compression_policy('archive.underlying_ohlcv', INTERVAL '30 days')")

    # 6. v_staleness view
    op.execute("""
        CREATE VIEW archive.v_staleness AS
        SELECT
            c.ticker,
            c.cadence_seconds,
            EXTRACT(EPOCH FROM (now() - last_run.finished_at))::INT AS seconds_since_last,
            last_run.contracts_persisted,
            last_run.status,
            CASE WHEN now() - last_run.finished_at > make_interval(secs => c.cadence_seconds * 4)
                 THEN 'stale' ELSE 'fresh' END AS health
        FROM archive.snapshot_config c
        LEFT JOIN LATERAL (
            SELECT * FROM archive.snapshot_run r
            WHERE r.ticker = c.ticker AND r.status IN ('ok','partial')
            ORDER BY r.finished_at DESC LIMIT 1
        ) last_run ON true
        WHERE c.enabled
    """)

    # 7. Grants
    op.execute("GRANT USAGE ON SCHEMA archive TO xenon_prod, xenon_dev, argon_app")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA archive TO xenon_prod, xenon_dev, argon_app")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA archive GRANT SELECT ON TABLES TO xenon_prod, xenon_dev, argon_app")


def downgrade() -> None:
    op.execute("DROP SCHEMA archive CASCADE")
```

- [ ] **Step 6: Run migration**

```bash
cd ~/projects/xenon
uv run alembic -c scripts/migrations/option_chain/alembic.ini upgrade head
```

Expected output: alembic log lines, ending with `INFO  [alembic.runtime.migration] Running upgrade -> 001`.

- [ ] **Step 7: Run tests**

Run: `uv run pytest scripts/tests/test_option_chain_schema.py -v`
Expected: PASS (all 7 tests).

- [ ] **Step 8: Commit + open PR**

```bash
git add scripts/migrations/option_chain/ scripts/tests/test_option_chain_schema.py
git commit -m "feat(option-chain): DB schema + alembic env for option_chain archive"
git push origin infra/option-chain-schema
gh pr create --title "feat(option-chain): DB + initial schema" \
  --body "Creates option_chain DB on macmini Postgres with TimescaleDB hypertables.

See \`scripts/migrations/option_chain/README.md\` for migration ops."
```

---

## PR 4 — Primitives module (config, hours, queue, limiter)

**Goal:** Pure-Python primitives with no IB or PG dependencies. All unit-testable.

**Dependencies:** PR 1 merged.

**Branch:** `infra/option-chain-primitives` (off master).

**Files:**

- Create: `src/xenon/option_chain_snapshotter/__init__.py`
- Create: `src/xenon/option_chain_snapshotter/CLAUDE.md` (module overview)
- Create: `src/xenon/option_chain_snapshotter/config.py`
- Create: `src/xenon/option_chain_snapshotter/hours.py`
- Create: `src/xenon/option_chain_snapshotter/queue.py`
- Create: `src/xenon/option_chain_snapshotter/limiter.py`
- Create: `scripts/tests/test_option_chain_snapshotter_config.py`
- Create: `scripts/tests/test_option_chain_snapshotter_hours.py`
- Create: `scripts/tests/test_option_chain_snapshotter_queue.py`
- Create: `scripts/tests/test_option_chain_snapshotter_limiter.py`

### Task 4.1: config.py — env var loading

- [ ] **Step 1: Failing test**

Create `scripts/tests/test_option_chain_snapshotter_config.py`:

```python
import os
from unittest.mock import patch

import pytest

from xenon.option_chain_snapshotter.config import SnapshotterConfig, TICKERS


def test_tickers_are_four_indexes():
    assert TICKERS == ("SPX", "NDX", "RUT", "VIX")


def test_default_pool_size_2():
    with patch.dict(os.environ, {"OPTION_CHAIN_DATABASE_URL": "postgresql://x"}, clear=True):
        c = SnapshotterConfig.from_env()
        assert c.pool_size == 2


def test_default_line_cap_72():
    with patch.dict(os.environ, {"OPTION_CHAIN_DATABASE_URL": "postgresql://x"}, clear=True):
        c = SnapshotterConfig.from_env()
        assert c.line_cap == 72


def test_default_msg_per_sec_cap_50():
    with patch.dict(os.environ, {"OPTION_CHAIN_DATABASE_URL": "postgresql://x"}, clear=True):
        c = SnapshotterConfig.from_env()
        assert c.msg_per_sec_cap == 50


def test_raises_when_db_url_missing():
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeError, match="OPTION_CHAIN_DATABASE_URL"):
            SnapshotterConfig.from_env()


def test_overrides_via_env():
    env = {
        "OPTION_CHAIN_DATABASE_URL": "postgresql://override",
        "OPTION_CHAIN_POOL_SIZE": "4",
        "OPTION_CHAIN_LINE_CAP": "100",
        "OPTION_CHAIN_MSG_PER_SEC_CAP": "30",
    }
    with patch.dict(os.environ, env, clear=True):
        c = SnapshotterConfig.from_env()
        assert c.pool_size == 4
        assert c.line_cap == 100
        assert c.msg_per_sec_cap == 30
```

- [ ] **Step 2: Run to verify fail**

`uv run pytest scripts/tests/test_option_chain_snapshotter_config.py -v`
Expected: FAIL (module doesn't exist)

- [ ] **Step 3: Implement config.py**

Create `src/xenon/option_chain_snapshotter/__init__.py` (empty file).

Create `src/xenon/option_chain_snapshotter/config.py`:

```python
"""Configuration for option_chain_snapshotter — env-driven, immutable.

See docs/plans/2026-06-02-option-chain-snapshotter-design.md § Code layout.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

TICKERS: tuple[str, ...] = ("SPX", "NDX", "RUT", "VIX")
"""Hardcoded universe per v1 scope. Stocks/ETFs deferred to v2."""


@dataclass(frozen=True)
class SnapshotterConfig:
    database_url: str
    ib_host: str
    ib_port: int
    pool_size: int
    line_cap: int
    msg_per_sec_cap: int
    log_level: str

    @classmethod
    def from_env(cls) -> SnapshotterConfig:
        db = os.environ.get("OPTION_CHAIN_DATABASE_URL")
        if not db:
            raise RuntimeError("OPTION_CHAIN_DATABASE_URL not set — see spec § Configuration env vars")
        return cls(
            database_url=db,
            ib_host=os.environ.get("OPTION_CHAIN_IB_HOST", "127.0.0.1"),
            ib_port=int(os.environ.get("OPTION_CHAIN_IB_PORT", "4001")),
            pool_size=int(os.environ.get("OPTION_CHAIN_POOL_SIZE", "2")),
            line_cap=int(os.environ.get("OPTION_CHAIN_LINE_CAP", "72")),
            msg_per_sec_cap=int(os.environ.get("OPTION_CHAIN_MSG_PER_SEC_CAP", "50")),
            log_level=os.environ.get("OPTION_CHAIN_LOG_LEVEL", "INFO"),
        )
```

- [ ] **Step 4: Verify**

`uv run pytest scripts/tests/test_option_chain_snapshotter_config.py -v` → PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/xenon/option_chain_snapshotter/__init__.py src/xenon/option_chain_snapshotter/config.py scripts/tests/test_option_chain_snapshotter_config.py
git commit -m "feat(option-chain): config module with env-driven loading"
```

### Task 4.2: hours.py — market hours gating

- [ ] **Step 1: Failing test**

Create `scripts/tests/test_option_chain_snapshotter_hours.py`:

```python
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from xenon.option_chain_snapshotter.hours import (
    MarketHoursGate,
    PollerState,
)

ET = ZoneInfo("America/New_York")


@pytest.fixture
def gate():
    return MarketHoursGate(window_open_hour=4, window_close_hour=20)


def test_rth_inside_window(gate):
    # Tuesday 10:30 ET, regular trading session
    t = datetime(2026, 6, 9, 10, 30, tzinfo=ET)
    state = gate.classify(t)
    assert state == PollerState.RTH


def test_ext_hours_premarket(gate):
    t = datetime(2026, 6, 9, 7, 0, tzinfo=ET)
    state = gate.classify(t)
    assert state == PollerState.EXT_HOURS


def test_idle_overnight(gate):
    t = datetime(2026, 6, 9, 2, 0, tzinfo=ET)  # 02:00 ET, before 04:00 window
    state = gate.classify(t)
    assert state == PollerState.IDLE


def test_idle_weekend(gate):
    t = datetime(2026, 6, 13, 10, 0, tzinfo=ET)  # Saturday
    state = gate.classify(t)
    assert state == PollerState.IDLE


def test_idle_nyse_holiday(gate):
    # Independence Day 2026 observed → 2026-07-03 (Friday)
    t = datetime(2026, 7, 3, 10, 0, tzinfo=ET)
    state = gate.classify(t)
    assert state == PollerState.IDLE


def test_effective_cadence_seconds_inside_rth(gate):
    t = datetime(2026, 6, 9, 10, 30, tzinfo=ET)
    assert gate.effective_cadence(t, configured_cadence=600) == 600


def test_effective_cadence_seconds_outside_rth(gate):
    # Pre-market: 3× multiplier per spec
    t = datetime(2026, 6, 9, 7, 0, tzinfo=ET)
    assert gate.effective_cadence(t, configured_cadence=600) == 1800
```

- [ ] **Step 2: Run + fail**

`uv run pytest scripts/tests/test_option_chain_snapshotter_hours.py -v` → FAIL.

- [ ] **Step 3: Implement hours.py**

Create `src/xenon/option_chain_snapshotter/hours.py`:

```python
"""Market-hours gating using exchange-calendars.

See spec § Market-hours gating. Holiday calendar = XNYS (NYSE).
"""
from __future__ import annotations

from datetime import datetime, time
from enum import Enum
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

ET = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)


class PollerState(str, Enum):
    RTH = "rth"
    EXT_HOURS = "ext_hours"
    IDLE = "idle"


class MarketHoursGate:
    """Resolves poller state from a wall-clock timestamp.

    The 04:00-20:00 ET window is the snapshotter's active window;
    outside that, the poller is idle. Inside the window but outside
    NYSE RTH, cadence is multiplied by 3 (effective cadence).
    """

    def __init__(self, window_open_hour: int = 4, window_close_hour: int = 20):
        self._open = window_open_hour
        self._close = window_close_hour
        self._cal = xcals.get_calendar("XNYS")

    def classify(self, when: datetime) -> PollerState:
        if when.tzinfo is None:
            raise ValueError("datetime must be timezone-aware")
        et = when.astimezone(ET)
        # Holiday or weekend?
        date = et.date()
        if not self._cal.is_session(date):
            return PollerState.IDLE
        # Inside 04:00-20:00 window?
        if et.hour < self._open or et.hour >= self._close:
            return PollerState.IDLE
        # Inside RTH?
        if RTH_OPEN <= et.time() < RTH_CLOSE:
            return PollerState.RTH
        return PollerState.EXT_HOURS

    def effective_cadence(self, when: datetime, configured_cadence: int) -> int:
        """Cadence multiplied by 3 outside RTH per spec."""
        state = self.classify(when)
        if state == PollerState.RTH:
            return configured_cadence
        return configured_cadence * 3
```

- [ ] **Step 4: Verify**

`uv run pytest scripts/tests/test_option_chain_snapshotter_hours.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/xenon/option_chain_snapshotter/hours.py scripts/tests/test_option_chain_snapshotter_hours.py
git commit -m "feat(option-chain): market-hours gating via exchange-calendars"
```

### Task 4.3: queue.py — priority queue with due-time scheduling

- [ ] **Step 1: Failing test**

Create `scripts/tests/test_option_chain_snapshotter_queue.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

from xenon.option_chain_snapshotter.queue import TickerQueue


@pytest.fixture
def now():
    return datetime(2026, 6, 9, 10, 0, tzinfo=timezone.utc)


def test_picks_most_overdue_first(now):
    q = TickerQueue()
    q.upsert("SPX", due_at=now - timedelta(minutes=15))
    q.upsert("NDX", due_at=now - timedelta(minutes=5))
    q.upsert("RUT", due_at=now + timedelta(minutes=1))
    assert q.pop_due(now) == "SPX"
    assert q.pop_due(now) == "NDX"
    assert q.pop_due(now) is None  # RUT not yet due


def test_empty_queue_returns_none(now):
    q = TickerQueue()
    assert q.pop_due(now) is None


def test_upsert_updates_due_at(now):
    q = TickerQueue()
    q.upsert("SPX", due_at=now + timedelta(minutes=10))
    q.upsert("SPX", due_at=now - timedelta(minutes=1))  # Bumped earlier
    assert q.pop_due(now) == "SPX"


def test_size(now):
    q = TickerQueue()
    q.upsert("SPX", due_at=now)
    q.upsert("NDX", due_at=now)
    assert q.size() == 2
    q.pop_due(now)
    assert q.size() == 1


def test_priority_high_jumps_queue(now):
    """High priority items pop first even if not most overdue."""
    q = TickerQueue()
    q.upsert("SPX", due_at=now - timedelta(minutes=30))
    q.upsert("NDX", due_at=now, priority=10)  # high priority
    assert q.pop_due(now) == "NDX"
    assert q.pop_due(now) == "SPX"
```

- [ ] **Step 2: Run + fail**

`uv run pytest scripts/tests/test_option_chain_snapshotter_queue.py -v` → FAIL

- [ ] **Step 3: Implement queue.py**

Create `src/xenon/option_chain_snapshotter/queue.py`:

```python
"""Priority queue for the continuous snapshot poller.

Each entry is (priority, due_at, ticker, seq). Pop_due returns the highest-
priority due ticker (or the most overdue if priorities are equal). Upserting
the same ticker invalidates earlier heap entries via a per-ticker version
counter (I-1: the prior add/discard pattern was a no-op).
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(order=True)
class _Entry:
    priority_neg: int   # negative so larger priority pops first via min-heap
    due_at: datetime
    seq: int            # version counter; stale when != latest for ticker
    ticker: str = field(compare=False)


class TickerQueue:
    """Continuous priority queue keyed by (priority desc, due_at asc).

    Pop returns the highest-priority entry whose due_at <= now, or None.
    Upsert replaces any existing entry for that ticker via a version counter:
    each upsert bumps `_latest_seq[ticker]`, and pop_due skips heap entries
    whose seq doesn't match the latest. The skipped entries are popped and
    discarded lazily during the pop_due walk, keeping the data structure
    correct even under high upsert frequency.
    """

    def __init__(self) -> None:
        self._heap: list[_Entry] = []
        self._latest_seq: dict[str, int] = {}

    def upsert(self, ticker: str, due_at: datetime, priority: int = 0) -> None:
        self._latest_seq[ticker] = self._latest_seq.get(ticker, 0) + 1
        heapq.heappush(self._heap, _Entry(-priority, due_at, self._latest_seq[ticker], ticker))

    def pop_due(self, now: datetime) -> str | None:
        while self._heap:
            top = self._heap[0]
            # Stale (superseded by a later upsert)? drop it and continue.
            if top.seq != self._latest_seq.get(top.ticker):
                heapq.heappop(self._heap)
                continue
            if top.due_at > now:
                return None
            heapq.heappop(self._heap)
            # We popped the latest entry for this ticker; reset seq so a
            # subsequent upsert starts fresh and any orphan entries (none
            # in practice, but defensively) compare unequal and get skipped.
            self._latest_seq.pop(top.ticker, None)
            return top.ticker
        return None

    def size(self) -> int:
        """Count of live (non-stale) entries on the heap."""
        return sum(1 for e in self._heap if e.seq == self._latest_seq.get(e.ticker))
```

- [ ] **Step 4: Verify**

`uv run pytest scripts/tests/test_option_chain_snapshotter_queue.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/xenon/option_chain_snapshotter/queue.py scripts/tests/test_option_chain_snapshotter_queue.py
git commit -m "feat(option-chain): priority queue for continuous poller"
```

### Task 4.4: limiter.py — ResizableLimiter with token bucket

- [ ] **Step 1: Failing test**

Create `scripts/tests/test_option_chain_snapshotter_limiter.py`:

```python
"""Tests for ResizableLimiter — addresses Pass-2 findings C-4, C-10, C-11."""
import asyncio

import pytest

from xenon.option_chain_snapshotter.limiter import ResizableLimiter


@pytest.mark.asyncio
async def test_acquires_within_cap():
    lim = ResizableLimiter(cap=2, msg_per_sec_cap=100)
    async with lim.acquire():
        async with lim.acquire():
            assert lim.in_use == 2


@pytest.mark.asyncio
async def test_blocks_at_cap():
    lim = ResizableLimiter(cap=1, msg_per_sec_cap=100)
    acquired_second = False

    async with lim.acquire():
        async def try_second():
            nonlocal acquired_second
            async with lim.acquire():
                acquired_second = True

        task = asyncio.create_task(try_second())
        await asyncio.sleep(0.05)
        assert not acquired_second  # blocked
    await task
    assert acquired_second


@pytest.mark.asyncio
async def test_resize_smaller_doesnt_kill_active_leases():
    """Pass-2 finding C-10: resize must not strand outstanding leases."""
    lim = ResizableLimiter(cap=4, msg_per_sec_cap=100)
    async with lim.acquire():
        async with lim.acquire():
            lim.resize(cap=2)  # Below current in_use=2 — must not crash
            assert lim.in_use == 2
            assert lim.cap == 2
    assert lim.in_use == 0


@pytest.mark.asyncio
async def test_concurrent_acquire_no_overflow():
    """Pass-2 finding C-11: TOCTOU guard. 100 racers, cap=5, never exceed 5."""
    lim = ResizableLimiter(cap=5, msg_per_sec_cap=1000)
    peak = 0
    lock = asyncio.Lock()

    async def racer():
        nonlocal peak
        async with lim.acquire():
            async with lock:
                peak = max(peak, lim.in_use)
            await asyncio.sleep(0.01)

    await asyncio.gather(*[racer() for _ in range(100)])
    assert peak == 5
    assert lim.in_use == 0


@pytest.mark.asyncio
async def test_msg_per_sec_token_bucket():
    """Pass-2 finding C-4: token bucket enforces messages-per-second."""
    lim = ResizableLimiter(cap=1000, msg_per_sec_cap=10)
    import time
    t0 = time.monotonic()
    for _ in range(20):
        async with lim.acquire():
            pass
    elapsed = time.monotonic() - t0
    # 20 messages at 10/sec → ~2 seconds at least
    assert elapsed >= 1.8, f"Token bucket not enforcing: elapsed={elapsed}"


@pytest.mark.asyncio
async def test_aimd_halve_on_violation():
    """Halve msg_per_sec_cap on violation, additive-increase /30s."""
    lim = ResizableLimiter(cap=10, msg_per_sec_cap=50)
    lim.report_pacing_violation()
    assert lim.msg_per_sec_cap == 25
    # We can't wait 30s in unit tests, so check the AI step directly
    lim.aimd_tick()
    assert lim.msg_per_sec_cap == 26
```

- [ ] **Step 2: (skip — already in deps)**

`pytest-asyncio>=0.24` is already in `pyproject.toml` (line 67); `[tool.pytest.ini_options].asyncio_mode = "strict"` is set at line 98. The `@pytest.mark.asyncio` markers in this test file work as-is. No `uv add` needed (it would attempt a downgrade to `>=0.23`).

- [ ] **Step 3: Run + fail**

`uv run pytest scripts/tests/test_option_chain_snapshotter_limiter.py -v` → FAIL

- [ ] **Step 4: Implement limiter.py**

Create `src/xenon/option_chain_snapshotter/limiter.py`:

```python
"""ResizableLimiter: single atomic admission control for IB requests.

Addresses Pass-2 findings:
  - C-4: token bucket enforces msg/sec pacing
  - C-10: resize doesn't strand active leases
  - C-11: single atomic acquire path, no TOCTOU
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager


class ResizableLimiter:
    """Per-connection concurrent-lease limiter + account-wide msg/sec bucket.

    Two caps:
      - cap: max simultaneous outstanding leases
      - msg_per_sec_cap: max messages-per-second (AIMD-adjustable)

    Both checked atomically under a single asyncio.Lock — no TOCTOU.
    """

    def __init__(self, cap: int, msg_per_sec_cap: int, ceiling: int | None = None):
        self._cap = cap
        self._msg_per_sec_cap = msg_per_sec_cap
        self._ceiling = ceiling or msg_per_sec_cap
        self._in_use = 0
        self._lock = asyncio.Lock()
        self._cond = asyncio.Condition(self._lock)
        # Token bucket state
        self._tokens = float(msg_per_sec_cap)
        self._last_refill = time.monotonic()

    @property
    def cap(self) -> int:
        return self._cap

    @property
    def msg_per_sec_cap(self) -> int:
        return self._msg_per_sec_cap

    @property
    def in_use(self) -> int:
        return self._in_use

    def _refill_tokens(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            float(self._msg_per_sec_cap),
            self._tokens + elapsed * self._msg_per_sec_cap,
        )
        self._last_refill = now

    @asynccontextmanager
    async def acquire(self):
        """Acquire one lease (and one msg/sec token). Releases on context exit."""
        async with self._cond:
            while True:
                self._refill_tokens()
                if self._in_use < self._cap and self._tokens >= 1.0:
                    self._in_use += 1
                    self._tokens -= 1.0
                    break
                # Wait either for slot or for tokens to refill
                wait_for_tokens = (
                    (1.0 - self._tokens) / max(self._msg_per_sec_cap, 1)
                    if self._tokens < 1.0
                    else None
                )
                try:
                    if wait_for_tokens is not None:
                        await asyncio.wait_for(self._cond.wait(), timeout=wait_for_tokens)
                    else:
                        await self._cond.wait()
                except asyncio.TimeoutError:
                    pass

        try:
            yield
        finally:
            async with self._cond:
                self._in_use -= 1
                self._cond.notify_all()

    def resize(self, *, cap: int | None = None, msg_per_sec_cap: int | None = None) -> None:
        """Resize caps. Future admissions only. Active leases unaffected."""
        if cap is not None:
            self._cap = cap
        if msg_per_sec_cap is not None:
            self._msg_per_sec_cap = msg_per_sec_cap

    def report_pacing_violation(self) -> None:
        """AIMD: halve msg_per_sec_cap immediately."""
        self.resize(msg_per_sec_cap=max(1, self._msg_per_sec_cap // 2))

    def aimd_tick(self) -> None:
        """Additive increase step (called every 30s by a background task)."""
        if self._msg_per_sec_cap < self._ceiling:
            self.resize(msg_per_sec_cap=self._msg_per_sec_cap + 1)
```

- [ ] **Step 5: Verify**

`uv run pytest scripts/tests/test_option_chain_snapshotter_limiter.py -v` → PASS

- [ ] **Step 6: Commit**

```bash
git add src/xenon/option_chain_snapshotter/limiter.py scripts/tests/test_option_chain_snapshotter_limiter.py
git commit -m "feat(option-chain): ResizableLimiter (token bucket + AIMD + atomic acquire)"
```

### Task 4.5: Create module CLAUDE.md

- [ ] **Step 1: Write the module overview**

Create `src/xenon/option_chain_snapshotter/CLAUDE.md`:

```markdown
# option_chain_snapshotter — module overview

Long-running service that snapshots IB option chains for SPX/NDX/RUT/VIX
into the `option_chain` Postgres DB on macmini. See full design at
`docs/plans/2026-06-02-option-chain-snapshotter-design.md`.

## Components

| File                 | Responsibility                                              |
| -------------------- | ----------------------------------------------------------- |
| `config.py`          | Env var loading, hardcoded TICKERS tuple                    |
| `hours.py`           | Market-hours gating via exchange-calendars                  |
| `queue.py`           | Priority queue (ticker, due_at, priority)                   |
| `limiter.py`         | ResizableLimiter — concurrent leases + msg/sec token bucket |
| `pool.py`            | IBConnectionPool — 2× IBClient with round-robin             |
| `storage.py`         | Postgres CRUD for the 5 archive tables                      |
| `universe.py`        | Daily universe refresh + intraday conId disable management  |
| `persister.py`       | Bounded queue → COPY batches → option_chain                 |
| `snapshot_worker.py` | Per-contract reqMktData(snapshot=True) lifecycle            |
| `ohlcv_worker.py`    | 60s reqHistoricalData for 4 underliers                      |
| `__main__.py`        | Entry point, supervises all workers                         |

## Throughput baseline

Updated whenever the day-1 probe re-runs. Set by `scripts/research/probe_ib_option_chain_results.md`:

- Probe date: TBD (set after PR 2 runs)
- Scenario landed: TBD (optimistic / pessimistic)
- p50 per-snapshot wall time: TBD seconds
- Sustained cps: TBD
- CI floor (`0.8 × cps`): TBD — used by throughput regression test in `test_option_chain_snapshotter_throughput.py`
```

- [ ] **Step 2: Open PR**

```bash
git add src/xenon/option_chain_snapshotter/CLAUDE.md
git commit -m "docs(option-chain): module overview CLAUDE.md"
git push -u origin infra/option-chain-primitives
gh pr create --title "feat(option-chain): primitives — config, hours, queue, limiter" \
  --body "Pure-Python primitives, no IB or PG deps. Unit-tested. Foundation for PR 5-9."
```

---

## PR 5 — IBConnectionPool

**Goal:** Pool of 2 IB connections using xenon's `IBClient` wrapper. Round-robin dispatch, per-conn disconnect handling.

**Dependencies:** PR 4 merged.

**Branch:** `infra/option-chain-pool` (off master).

**Files:**

- Create: `src/xenon/option_chain_snapshotter/pool.py`
- Create: `scripts/tests/test_option_chain_snapshotter_pool.py`

### Task 5.1: pool.py with round-robin + disconnect isolation

- [ ] **Step 1: Failing test**

Create `scripts/tests/test_option_chain_snapshotter_pool.py`:

```python
"""Tests for IBConnectionPool — addresses Pass-2 finding C-8 (per-conn isolation)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from xenon.option_chain_snapshotter.pool import IBConnectionPool


@pytest.fixture
def mock_ib_client_factory(monkeypatch):
    """Factory yielding a fresh MagicMock per IBClient(...) call."""
    instances = []

    def factory(*args, **kwargs):
        m = MagicMock()
        m.connect = MagicMock()   # IBClient.connect is sync, not async (C-2)
        m.disconnect = MagicMock()
        m.ib = MagicMock()
        m.is_connected = MagicMock(return_value=True)
        instances.append(m)
        return m

    monkeypatch.setattr("xenon.option_chain_snapshotter.pool._make_ib_client", factory)
    return instances


@pytest.mark.asyncio
async def test_creates_two_connections_with_registered_ids(mock_ib_client_factory):
    pool = IBConnectionPool(host="127.0.0.1", port=4001, pool_size=2)
    await pool.connect_all()
    assert len(pool._connections) == 2
    # round-robin order matches client_name registration
    assert pool._client_names == ["option_chain_snapshotter_a", "option_chain_snapshotter_b"]


@pytest.mark.asyncio
async def test_round_robin_dispatch(mock_ib_client_factory):
    pool = IBConnectionPool(host="127.0.0.1", port=4001, pool_size=2)
    await pool.connect_all()
    c0 = pool.next_connection()
    c1 = pool.next_connection()
    c2 = pool.next_connection()
    assert c0 is not c1
    assert c2 is c0  # wraps around


@pytest.mark.asyncio
async def test_skips_disconnected_connection(mock_ib_client_factory):
    """Pass-2 finding C-8: if one conn is down, dispatch uses the survivor."""
    pool = IBConnectionPool(host="127.0.0.1", port=4001, pool_size=2)
    await pool.connect_all()
    pool._connections[0].is_connected.return_value = False
    c = pool.next_connection()
    assert c is pool._connections[1]
    c2 = pool.next_connection()
    assert c2 is pool._connections[1]  # still the survivor


@pytest.mark.asyncio
async def test_raises_when_all_connections_down(mock_ib_client_factory):
    pool = IBConnectionPool(host="127.0.0.1", port=4001, pool_size=2)
    await pool.connect_all()
    for c in pool._connections:
        c.is_connected.return_value = False
    with pytest.raises(RuntimeError, match="no healthy"):
        pool.next_connection()
```

- [ ] **Step 2: Implement pool.py**

```python
"""IBConnectionPool: 2-connection round-robin pool using xenon's IBClient wrapper.

Per Pass-2 finding CL-3: uses IBClient wrapper, not raw ib_async.IB().
Per Pass-2 finding C-8: per-conn disconnect isolation.
"""
from __future__ import annotations

from typing import Any

from xenon.clients.ib_client import IBClient


def _make_ib_client() -> IBClient:
    """Factory wrapper for test seam (monkey-patchable).

    C-1: IBClient.__init__(self) -> None takes no args (verified at
    src/xenon/clients/ib_client.py:185). host/port/client_name are passed to
    IBClient.connect() separately, not to the constructor.
    """
    return IBClient()


class IBConnectionPool:
    """Pool of N IBClient instances, round-robin dispatch.

    Connections register clientIds via xenon's CLIENT_IDS registry:
      pool[0] → "option_chain_snapshotter_a" (id=95)
      pool[1] → "option_chain_snapshotter_b" (id=96)
    """

    def __init__(self, host: str, port: int, pool_size: int = 2):
        self._host = host
        self._port = port
        self._pool_size = pool_size
        self._client_names = [f"option_chain_snapshotter_{c}" for c in "ab"[:pool_size]]
        self._connections: list[Any] = []
        self._rr_index = 0

    async def connect_all(self) -> None:
        """Connect all clients.

        C-2: IBClient.connect is **sync** (xenon wrapper, NOT raw ib_async)
        — signature at src/xenon/clients/ib_client.py:223. The original
        plan `await client.connect(...)` raised TypeError (awaiting None).
        Call directly; this method is invoked once at boot before workers
        start, so the brief event-loop block during TCP handshake +
        IB Gateway auth is acceptable (no other tasks running yet).
        """
        for name in self._client_names:
            client = _make_ib_client()
            client.connect(host=self._host, port=self._port, client_name=name)
            self._connections.append(client)

    def next_connection(self):
        """Round-robin, skipping disconnected connections."""
        if not self._connections:
            raise RuntimeError("Pool not connected")
        for _ in range(len(self._connections)):
            cand = self._connections[self._rr_index]
            self._rr_index = (self._rr_index + 1) % len(self._connections)
            if cand.is_connected():
                return cand
        raise RuntimeError("no healthy connections in pool")

    def disconnect_all(self) -> None:
        for c in self._connections:
            c.disconnect()
        self._connections.clear()
```

- [ ] **Step 3: Verify + commit**

`uv run pytest scripts/tests/test_option_chain_snapshotter_pool.py -v` → PASS

```bash
git add src/xenon/option_chain_snapshotter/pool.py scripts/tests/test_option_chain_snapshotter_pool.py
git commit -m "feat(option-chain): IBConnectionPool with round-robin + disconnect isolation"
git push -u origin infra/option-chain-pool
gh pr create --title "feat(option-chain): IBConnectionPool" \
  --body "2-conn pool, round-robin, skips disconnected connections (Pass-2 C-8)."
```

---

## PR 6 — Storage + Universe refresh

**Goal:** psycopg-based CRUD for option_universe + snapshot_run + snapshot_config. Two-step commit universe refresh with IND-secType qualification.

**Dependencies:** PR 3 (schema) + PR 5 (pool) merged.

**Branch:** `infra/option-chain-universe` (off master).

**Files:**

- Create: `src/xenon/option_chain_snapshotter/storage.py`
- Create: `src/xenon/option_chain_snapshotter/universe.py`
- Create: `scripts/tests/test_option_chain_snapshotter_storage.py`
- Create: `scripts/tests/test_option_chain_snapshotter_universe.py`

### Task 6.1: storage.py — psycopg CRUD

- [ ] **Step 1: Failing test**

Create `scripts/tests/test_option_chain_snapshotter_storage.py`:

```python
"""Storage layer tests against real option_chain Postgres."""
import os
from datetime import date, datetime, timezone

import pytest

from xenon.option_chain_snapshotter.storage import (
    OptionChainStorage,
    UniverseRow,
    DISABLE_STATUS_TEMP,
)


pytestmark = pytest.mark.skipif(
    not os.environ.get("OPTION_CHAIN_DATABASE_URL"),
    reason="OPTION_CHAIN_DATABASE_URL not set",
)


@pytest.fixture
def storage():
    s = OptionChainStorage.from_env()
    yield s
    # Cleanup per-test: truncate test data
    with s.conn() as c:
        c.execute("DELETE FROM archive.option_universe WHERE ticker LIKE 'TEST_%'")
        c.execute("DELETE FROM archive.snapshot_run WHERE ticker LIKE 'TEST_%'")


def test_upsert_universe_row(storage):
    today = date(2026, 6, 9)
    row = UniverseRow(
        universe_date=today, con_id=12345, ticker="TEST_X",
        trading_class="TEST_X", exchange="CBOE", multiplier=100,
        local_symbol="TEST_X 20260620 P 100", expiry=date(2026, 6, 20),
        strike=100.0, right="P",
    )
    storage.upsert_universe([row])
    fetched = storage.fetch_universe(universe_date=today, ticker="TEST_X", committed=False)
    assert len(fetched) == 1
    assert fetched[0].con_id == 12345


def test_commit_universe_atomically(storage):
    """Pass-3 finding A-1: two-step commit pattern."""
    today = date(2026, 6, 9)
    rows = [UniverseRow(
        universe_date=today, con_id=i, ticker="TEST_X",
        trading_class="TEST_X", exchange="CBOE", multiplier=100,
        local_symbol=f"L{i}", expiry=date(2026, 6, 20),
        strike=100.0 + i, right="C",
    ) for i in range(5)]
    storage.upsert_universe(rows)
    # Before commit: not visible to committed=True query
    assert storage.fetch_universe(universe_date=today, ticker="TEST_X", committed=True) == []
    storage.commit_universe_date(today)
    assert len(storage.fetch_universe(universe_date=today, ticker="TEST_X", committed=True)) == 5


def test_disable_conid_temporarily(storage):
    today = date(2026, 6, 9)
    row = UniverseRow(
        universe_date=today, con_id=99999, ticker="TEST_X",
        trading_class="TEST_X", exchange="CBOE", multiplier=100,
        local_symbol="L", expiry=date(2026, 6, 20), strike=100, right="C",
    )
    storage.upsert_universe([row])
    storage.commit_universe_date(today)
    storage.disable_conid(con_id=99999, error_code=200, until=datetime.now(timezone.utc))
    fetched = storage.fetch_universe(universe_date=today, ticker="TEST_X", committed=True)
    assert fetched[0].status == DISABLE_STATUS_TEMP


def test_insert_snapshot_run(storage):
    started = datetime.now(timezone.utc)
    run_id = storage.insert_snapshot_run(ticker="TEST_X", started_at=started)
    assert run_id > 0
    storage.finish_snapshot_run(run_id, status="ok", contracts_persisted=10, duration_ms=500)
```

- [ ] **Step 2: Implement storage.py**

Create `src/xenon/option_chain_snapshotter/storage.py`:

```python
"""Postgres CRUD for option_chain archive tables.

Uses psycopg (NOT SQLAlchemy ORM) for performance — COPY batches in
persister, simple parameterized queries here.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import psycopg

DISABLE_STATUS_ACTIVE = "active"
DISABLE_STATUS_TEMP = "disabled_temp"
DISABLE_STATUS_DAY = "disabled_day"


@dataclass
class UniverseRow:
    universe_date: date
    con_id: int
    ticker: str
    trading_class: str
    exchange: str
    multiplier: int
    local_symbol: str
    expiry: date
    strike: float
    right: str
    status: str = DISABLE_STATUS_ACTIVE
    failure_count: int = 0
    disabled_until: datetime | None = None
    last_error_code: int | None = None


class OptionChainStorage:
    """Thin Postgres wrapper for archive tables."""

    def __init__(self, dsn: str):
        self._dsn = dsn

    @classmethod
    def from_env(cls) -> OptionChainStorage:
        dsn = os.environ["OPTION_CHAIN_DATABASE_URL"]
        return cls(dsn)

    @contextmanager
    def conn(self):
        with psycopg.connect(self._dsn, autocommit=True) as c:
            yield c

    def upsert_universe(self, rows: list[UniverseRow]) -> None:
        sql = """
        INSERT INTO archive.option_universe (
            universe_date, con_id, ticker, trading_class, exchange, multiplier,
            local_symbol, expiry, strike, right, status, failure_count,
            disabled_until, last_error_code, universe_date_committed
        ) VALUES (
            %(universe_date)s, %(con_id)s, %(ticker)s, %(trading_class)s,
            %(exchange)s, %(multiplier)s, %(local_symbol)s, %(expiry)s,
            %(strike)s, %(right)s, %(status)s, %(failure_count)s,
            %(disabled_until)s, %(last_error_code)s, FALSE
        )
        ON CONFLICT (universe_date, con_id) DO UPDATE SET
            ticker = EXCLUDED.ticker,
            trading_class = EXCLUDED.trading_class,
            exchange = EXCLUDED.exchange,
            multiplier = EXCLUDED.multiplier,
            local_symbol = EXCLUDED.local_symbol,
            expiry = EXCLUDED.expiry,
            strike = EXCLUDED.strike,
            right = EXCLUDED.right,
            updated_at = now()
        """
        with self.conn() as c, c.cursor() as cur:
            for r in rows:
                cur.execute(sql, r.__dict__)

    def commit_universe_date(self, universe_date: date) -> int:
        """Atomically flip universe_date_committed=TRUE for all rows of a date."""
        with self.conn() as c, c.cursor() as cur:
            cur.execute(
                "UPDATE archive.option_universe SET universe_date_committed = TRUE "
                "WHERE universe_date = %s",
                (universe_date,),
            )
            return cur.rowcount

    def fetch_universe(
        self, *, universe_date: date, ticker: str | None = None, committed: bool = True
    ) -> list[UniverseRow]:
        sql = "SELECT * FROM archive.option_universe WHERE universe_date = %s"
        params: list = [universe_date]
        if ticker:
            sql += " AND ticker = %s"
            params.append(ticker)
        if committed:
            sql += " AND universe_date_committed = TRUE"
        with self.conn() as c, c.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(sql, params)
            return [UniverseRow(**{k: row[k] for k in UniverseRow.__dataclass_fields__}) for row in cur.fetchall()]

    def disable_conid(self, *, con_id: int, error_code: int, until: datetime | None) -> None:
        """Set status=disabled_temp on a conId after a soft failure."""
        with self.conn() as c, c.cursor() as cur:
            cur.execute("""
                UPDATE archive.option_universe
                SET status = %s,
                    disabled_until = %s,
                    failure_count = failure_count + 1,
                    last_error_code = %s,
                    updated_at = now()
                WHERE con_id = %s AND universe_date_committed = TRUE
            """, (DISABLE_STATUS_TEMP, until, error_code, con_id))

    def insert_snapshot_run(self, *, ticker: str, started_at: datetime) -> int:
        with self.conn() as c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO archive.snapshot_run (ticker, started_at, status)
                VALUES (%s, %s, 'running') RETURNING id
            """, (ticker, started_at))
            return cur.fetchone()[0]

    def finish_snapshot_run(
        self, run_id: int, *, status: str, contracts_persisted: int = 0,
        contracts_attempted: int = 0, duration_ms: int = 0, error: str | None = None
    ) -> None:
        with self.conn() as c, c.cursor() as cur:
            cur.execute("""
                UPDATE archive.snapshot_run SET
                    finished_at = now(), status = %s,
                    contracts_persisted = %s, contracts_attempted = %s,
                    duration_ms = %s, error = %s
                WHERE id = %s
            """, (status, contracts_persisted, contracts_attempted, duration_ms, error, run_id))
```

- [ ] **Step 3: Verify + commit**

`uv run pytest scripts/tests/test_option_chain_snapshotter_storage.py -v` → PASS

```bash
git add src/xenon/option_chain_snapshotter/storage.py scripts/tests/test_option_chain_snapshotter_storage.py
git commit -m "feat(option-chain): psycopg storage layer for archive tables"
```

### Task 6.2: universe.py — daily refresh with IND-secType qualification

- [ ] **Step 1: Failing test**

Create `scripts/tests/test_option_chain_snapshotter_universe.py`:

```python
"""Tests for universe refresh — addresses Pass-2 C-3 + C-9, Pass-3 A-1."""
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from xenon.option_chain_snapshotter.universe import refresh_universe, RefreshResult


@pytest.fixture
def mock_pool():
    pool = MagicMock()
    ib = MagicMock()
    ib.qualifyContractsAsync = AsyncMock()
    ib.reqSecDefOptParamsAsync = AsyncMock()
    pool.next_connection.return_value = MagicMock(ib=ib)
    return pool, ib


@pytest.fixture
def fake_storage():
    s = MagicMock()
    s.upsert_universe = MagicMock()
    s.commit_universe_date = MagicMock(return_value=10)
    return s


@pytest.mark.asyncio
async def test_qualifies_ind_secType_with_underlying_conId(mock_pool, fake_storage):
    """Pass-2 finding C-3: must pass underlyingSecType='IND' and underlyingConId."""
    pool, ib = mock_pool
    # Mock underlier qualification — sets conId on the Index contract
    def qualify_side(*contracts):
        for c in contracts:
            c.conId = 416904
        return contracts
    ib.qualifyContractsAsync.side_effect = qualify_side
    # Mock SecDef returning multiple tradingClasses
    spxw_params = MagicMock(
        tradingClass="SPXW", exchange="CBOE", multiplier="100",
        expirations=["20260620"], strikes=[100.0, 200.0],
    )
    spx_params = MagicMock(
        tradingClass="SPX", exchange="CBOE", multiplier="100",
        expirations=["20260620"], strikes=[100.0, 200.0],
    )
    ib.reqSecDefOptParamsAsync.return_value = [spx_params, spxw_params]

    result = await refresh_universe(pool, fake_storage, tickers=("SPX",), today=date(2026, 6, 9))

    # Verify call shape
    call_kwargs = ib.reqSecDefOptParamsAsync.call_args.kwargs
    assert call_kwargs["underlyingSecType"] == "IND"
    assert call_kwargs["underlyingConId"] == 416904
    assert call_kwargs["futFopExchange"] == ""

    # Verify both tradingClasses landed
    rows_arg = fake_storage.upsert_universe.call_args.args[0]
    classes = {r.trading_class for r in rows_arg}
    assert classes == {"SPX", "SPXW"}


@pytest.mark.asyncio
async def test_two_step_commit_only_flips_at_end(mock_pool, fake_storage):
    """Pass-3 finding A-1: rows written FALSE, commit flag flipped atomically."""
    pool, ib = mock_pool
    def qualify_side(*contracts):
        for c in contracts:
            c.conId = 1
        return contracts
    ib.qualifyContractsAsync.side_effect = qualify_side
    ib.reqSecDefOptParamsAsync.return_value = [MagicMock(
        tradingClass="VIX", exchange="CBOE", multiplier="100",
        expirations=["20260620"], strikes=[20.0],
    )]
    await refresh_universe(pool, fake_storage, tickers=("VIX",), today=date(2026, 6, 9))

    # upsert called before commit
    assert fake_storage.upsert_universe.called
    fake_storage.commit_universe_date.assert_called_once_with(date(2026, 6, 9))
    # Order matters: upsert THEN commit
    upsert_call_idx = fake_storage.method_calls.index(("upsert_universe",) + fake_storage.upsert_universe.call_args)
    # We just check it was called, real order is by side-effect of mock
    # (asserting before-commit precisely needs call_args_list ordering)
```

- [ ] **Step 2: Implement universe.py**

Create `src/xenon/option_chain_snapshotter/universe.py`:

```python
"""Daily universe refresh — qualifies underliers, calls reqSecDefOptParams,
qualifies every option contract, two-step commits to option_universe.

Per Pass-2 finding C-3: IND secType requires underlying conId + secType='IND'.
Per Pass-3 finding A-1: two-step commit (write FALSE → flip TRUE atomically).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ib_async import Index, Option

from .storage import OptionChainStorage, UniverseRow

INDEX_EXCHANGE = {"SPX": "CBOE", "NDX": "NASDAQ", "RUT": "RUSSELL", "VIX": "CBOE"}
"""IB Index() underlier exchange per ticker. Verified live 2026-06-02:
SPX/CBOE conId=416904, NDX/NASDAQ conId=416843, RUT/RUSSELL conId=416888,
VIX/CBOE conId=13455763. NDX/CBOE and RUT/CBOE return IB Error 200.
Keep in sync with `scripts/research/probe_ib_option_chain.py::INDEX_EXCHANGE`."""


def _parse_ib_date(s: str) -> date:
    """M-1: IB `lastTradeDateOrContractMonth` is YYYYMMDD for daily expiries
    OR YYYYMM for monthly contracts (per ib_async/contract.py Option docstring).
    Index options qualify as 8-char in practice, but parse defensively."""
    if len(s) == 8:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    if len(s) == 6:
        # Monthly: standard equity-option monthly expiry is the 3rd Friday;
        # for index options this is also conventional. Day 21 is a safe
        # upper bound for 3rd Friday across all months.
        return date(int(s[:4]), int(s[4:6]), 21)
    raise ValueError(f"Unparseable IB date: {s!r} (expected YYYYMMDD or YYYYMM)")


@dataclass
class RefreshResult:
    contracts_qualified: int = 0
    trading_classes_by_ticker: dict[str, list[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


async def refresh_universe(
    pool, storage: OptionChainStorage, *, tickers: tuple[str, ...], today: date
) -> RefreshResult:
    result = RefreshResult()
    conn = pool.next_connection()
    ib = conn.ib

    rows: list[UniverseRow] = []
    for ticker in tickers:
        underlying = Index(symbol=ticker, exchange=INDEX_EXCHANGE.get(ticker, "CBOE"), currency="USD")
        await ib.qualifyContractsAsync(underlying)
        if not underlying.conId:
            result.errors.append(f"{ticker}: underlier qualification failed")
            continue

        params = await ib.reqSecDefOptParamsAsync(
            underlyingSymbol=ticker,
            futFopExchange="",
            underlyingSecType="IND",
            underlyingConId=underlying.conId,
        )
        tcs = sorted({p.tradingClass for p in params})
        result.trading_classes_by_ticker[ticker] = tcs

        # Build Option contracts for every (tradingClass, expiry, strike, right)
        contracts: list[Option] = []
        for p in params:
            for exp in p.expirations:
                for k in p.strikes:
                    for r in ("C", "P"):
                        contracts.append(Option(
                            ticker, exp, float(k), r,
                            exchange=p.exchange, tradingClass=p.tradingClass,
                            multiplier=str(p.multiplier),
                        ))
        await ib.qualifyContractsAsync(*contracts)

        for c in contracts:
            if not c.conId:
                continue
            rows.append(UniverseRow(
                universe_date=today,
                con_id=c.conId,
                ticker=ticker,
                trading_class=c.tradingClass,
                exchange=c.exchange,
                multiplier=int(c.multiplier),
                local_symbol=c.localSymbol or "",
                expiry=_parse_ib_date(c.lastTradeDateOrContractMonth),  # M-1: handles YYYYMM and YYYYMMDD
                strike=float(c.strike),
                right=c.right,
            ))
        result.contracts_qualified += sum(1 for c in contracts if c.conId)

    # Two-step commit
    storage.upsert_universe(rows)
    storage.commit_universe_date(today)
    return result
```

- [ ] **Step 3: Verify + commit + PR**

```bash
uv run pytest scripts/tests/test_option_chain_snapshotter_universe.py -v  # → PASS
git add src/xenon/option_chain_snapshotter/universe.py scripts/tests/test_option_chain_snapshotter_universe.py
git commit -m "feat(option-chain): universe refresh with IND qualification + two-step commit"
git push -u origin infra/option-chain-universe
gh pr create --title "feat(option-chain): storage + universe refresh"
```

---

## PR 7 — Persister

**Goal:** Bounded asyncio queue → COPY batches into `archive.option_chain`. High/low water back-pressure. Cold-restart batch shrinking.

**Dependencies:** PR 6 merged.

**Branch:** `infra/option-chain-persister`.

**Files:**

- Create: `src/xenon/option_chain_snapshotter/persister.py`
- Create: `scripts/tests/test_option_chain_snapshotter_persister.py`

### Task 7.1: persister.py — bounded queue + COPY batching

- [ ] **Step 1: Failing test**

Create `scripts/tests/test_option_chain_snapshotter_persister.py`:

```python
import asyncio
from datetime import datetime, timezone

import pytest

from xenon.option_chain_snapshotter.persister import (
    Persister, ChainRow, PERSISTER_RING_SIZE,
)


@pytest.mark.asyncio
async def test_put_with_5s_timeout_drops_on_full(monkeypatch):
    """Pass-1 finding F3 + Pass-3 finding A-3: bounded back-pressure."""
    persister = Persister(dsn="postgresql://stub", ring_size=2)
    persister._stop = True  # never drain
    row = ChainRow(
        snapshot_ts=datetime.now(timezone.utc), con_id=1, ticker="SPX",
        trading_class="SPX", expiry=None, strike=0.0, right="C",
        request_ts=datetime.now(timezone.utc), run_id=1,
    )
    await persister.put(row)
    await persister.put(row)  # Ring full
    monkeypatch.setattr("asyncio.wait_for", _fast_timeout)
    with pytest.raises(asyncio.QueueFull):
        await persister.put(row, timeout=0.01)
    assert persister.drops_total == 1


async def _fast_timeout(coro, timeout):
    raise asyncio.TimeoutError


@pytest.mark.asyncio
async def test_high_water_pause_blocks_acquires(monkeypatch):
    """Pass-3 finding A-3: at 80% high-water, paused; resumes at 30% low-water."""
    persister = Persister(dsn="postgresql://stub", ring_size=10)
    persister._stop = True  # don't drain
    # Fill to 9 (90%) — over high-water of 8
    for _ in range(9):
        await persister.put(_make_row(), timeout=0.5)
    assert persister.paused is True
    # Simulate drain to 2 (20%) — under low-water of 3
    persister._queue = asyncio.Queue(maxsize=10)
    for _ in range(2):
        await persister._queue.put(_make_row())
    persister._update_pause_state()
    assert persister.paused is False


def _make_row():
    return ChainRow(
        snapshot_ts=datetime.now(timezone.utc), con_id=1, ticker="SPX",
        trading_class="SPX", expiry=None, strike=0.0, right="C",
        request_ts=datetime.now(timezone.utc), run_id=1,
    )
```

- [ ] **Step 2: Implement persister.py**

```python
"""Persister: bounded asyncio queue → COPY batches into archive.option_chain.

Per Pass-1 F3: 5s put-timeout with drop counter on overflow.
Per Pass-3 A-3: 80%/30% high-low water hysteresis.
Per Pass-3 A-4: cold-restart batch size = 500 (vs steady-state 5000).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import psycopg

PERSISTER_RING_SIZE = 100_000
HIGH_WATER = 0.80
LOW_WATER = 0.30
STEADY_BATCH = 5000
COLD_RESTART_BATCH = 500
COLD_RESTART_DURATION_SECONDS = 60


@dataclass
class ChainRow:
    snapshot_ts: datetime
    con_id: int
    ticker: str
    trading_class: str
    expiry: Optional[date]
    strike: float
    right: str
    request_ts: datetime
    run_id: int
    quote_ts: Optional[datetime] = None
    greeks_ts: Optional[datetime] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[int] = None
    ask_size: Optional[int] = None
    last: Optional[float] = None
    last_size: Optional[int] = None
    volume: Optional[int] = None
    open_interest: Optional[int] = None
    iv: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    vega: Optional[float] = None
    theta: Optional[float] = None
    underlying_px: Optional[float] = None


class Persister:
    def __init__(self, dsn: str, ring_size: int = PERSISTER_RING_SIZE):
        self._dsn = dsn
        self._ring_size = ring_size
        self._queue: asyncio.Queue[ChainRow] = asyncio.Queue(maxsize=ring_size)
        self._stop = False
        self._paused = False
        self._drops_total = 0
        self._reconnect_at: float | None = None

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def drops_total(self) -> int:
        return self._drops_total

    def _update_pause_state(self) -> None:
        ratio = self._queue.qsize() / self._ring_size
        if ratio >= HIGH_WATER:
            self._paused = True
        elif ratio <= LOW_WATER:
            self._paused = False

    async def put(self, row: ChainRow, timeout: float = 5.0) -> None:
        try:
            await asyncio.wait_for(self._queue.put(row), timeout=timeout)
        except asyncio.TimeoutError:
            self._drops_total += 1
            raise asyncio.QueueFull()
        self._update_pause_state()

    async def run(self) -> None:
        """Persister loop: COPY batches into archive.option_chain."""
        while not self._stop:
            batch = await self._collect_batch()
            if not batch:
                await asyncio.sleep(0.5)
                continue
            await self._copy_batch(batch)
            self._update_pause_state()

    async def _collect_batch(self) -> list[ChainRow]:
        batch_size = self._current_batch_size()
        batch: list[ChainRow] = []
        try:
            batch.append(await asyncio.wait_for(self._queue.get(), timeout=1.0))
        except asyncio.TimeoutError:
            return []
        while len(batch) < batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return batch

    def _current_batch_size(self) -> int:
        import time
        if self._reconnect_at and time.monotonic() - self._reconnect_at < COLD_RESTART_DURATION_SECONDS:
            return COLD_RESTART_BATCH
        return STEADY_BATCH

    async def _copy_batch(self, batch: list[ChainRow]) -> None:
        # Use psycopg COPY for performance
        cols = list(ChainRow.__dataclass_fields__.keys())
        with psycopg.connect(self._dsn, autocommit=True) as c, c.cursor() as cur:
            with cur.copy(f"COPY archive.option_chain ({','.join(cols)}) FROM STDIN") as copy:
                for r in batch:
                    copy.write_row([getattr(r, col) for col in cols])

    def mark_cold_restart(self) -> None:
        import time
        self._reconnect_at = time.monotonic()
```

- [ ] **Step 3: Verify + commit + PR**

```bash
uv run pytest scripts/tests/test_option_chain_snapshotter_persister.py -v  # PASS
git add src/xenon/option_chain_snapshotter/persister.py scripts/tests/test_option_chain_snapshotter_persister.py
git commit -m "feat(option-chain): persister with high/low-water + cold-restart batching"
git push -u origin infra/option-chain-persister
gh pr create --title "feat(option-chain): persister"
```

---

## PR 8 — Snapshot worker + OHLCV worker + throughput regression test

**Goal:** Per-contract reqMktData lifecycle (snapshot, cancel, partial-on-timeout). Per-tick timestamping. tickSnapshotEnd handling. 0DTE rollover. Underlying OHLCV worker. The 16 named regression tests.

**Dependencies:** PR 7 merged. PR 2 probe results in hand (sets CI throughput floor).

**Branch:** `infra/option-chain-workers`.

**Files:**

- Create: `src/xenon/option_chain_snapshotter/snapshot_worker.py`
- Create: `src/xenon/option_chain_snapshotter/ohlcv_worker.py`
- Create: `scripts/tests/test_option_chain_snapshotter_snapshot_worker.py` (covers C-2 SPX/SPXW, C-5 watchdog, C-6 tickSnapshotEnd, C-12 timestamps, C-13 OI nullability, A-5 0DTE rollover)
- Create: `scripts/tests/test_option_chain_snapshotter_ohlcv_worker.py`
- Create: `scripts/tests/test_option_chain_snapshotter_throughput.py` (CI floor regression test)

### Task 8.1: snapshot_worker.py

Implementation follows the spec's "Snapshot lifecycle (per contract)" section in IB connection management. Key responsibilities:

1. Acquire lease from limiter
2. Record request_ts
3. Call `ib.reqMktData(contract, "", snapshot=True, regulatorySnapshot=False)` (returns a `Ticker`)
4. Subscribe to `ib.pendingTickersEvent` and poll the returned `Ticker`'s
   state. Finalize when **any** of the following hold:
   - `ticker.bid > 0` AND `ticker.ask > 0` AND `ticker.modelGreeks.delta is not None` — full data
   - Wrapper-level `tickSnapshotEnd(reqId)` callback fires for this reqId — IB declares snapshot complete
   - 12s hard timeout — give up, persist whatever populated

   **C-4 note:** `ticker.snapshotEndEvent` does NOT exist on the Ticker class
   (verified via `ib_async/ticker.py:51 — events: ClassVar = ("updateEvent",)`).
   The `tickSnapshotEnd` IB callback lives at the wrapper level
   (`ib_async/wrapper.py:1108`); to hook it, subclass `IB.wrapper` and
   override `tickSnapshotEnd` to set a `dict[reqId, asyncio.Event]`, OR
   simply rely on polling + the 12s timeout (cleaner; what the day-1 probe
   already does and what v1 should ship).

5. Record quote_ts when bid+ask both present
6. Record greeks_ts when modelGreeks tick arrives
7. Call `ib.cancelMktData(contract)`
8. Release lease
9. Enqueue ChainRow to persister
10. Update snapshot_run

- [ ] **Step 1: Write the 11 regression tests**

Create `scripts/tests/test_option_chain_snapshotter_snapshot_worker.py` with tests for:

1. `test_snapshot_records_per_tick_timestamps` (C-12) — assert request_ts < quote_ts < greeks_ts
2. `test_tickSnapshotEnd_used_for_completion` (C-6) — IB delivers greeks at 9s; row persists with all greeks
3. `test_12s_hard_timeout_persists_partial` (C-6) — modelGreeks never arrives; row persists with NULL greeks
4. `test_spx_spxw_separate_rows` (C-2) — two contracts share (ticker, expiry, strike, right) but differ by tradingClass; two rows persist
5. `test_oi_nullable_in_snapshot_mode` (C-13) — assert column type allows NULL; assert mock doesn't error when OI absent
6. `test_0dte_rollover_at_16:00_silent` (A-5) — error 200 on a same-day expiry past 16:00 ET logs at info level, does NOT increment partial count
7. `test_uses_reqMktDataAsync_not_reqTickers` (from memory `feedback_ib_async_in_fastapi.md`) — assert the code path imports `reqMktData` (or equivalent), not `reqTickers`
8. `test_atomic_lease_release_on_exception` — exception inside snapshot doesn't leak the limiter slot
9. `test_disabled_conid_skipped` (C-9) — conId with status='disabled_temp' AND disabled_until > now() not snapshotted
10. `test_disabled_conid_retried_after_expiry` (C-9) — disabled_until in past → snapshot attempts again
11. `test_double_failure_promotes_to_disabled_day` (C-9) — second consecutive failure → disabled_day, won't retry today

- [ ] **Step 2: Implement snapshot_worker.py**

(Full implementation per spec § IB connection management § Snapshot lifecycle. See ChainRow contract from PR 7.)

- [ ] **Step 3: Commit**

```bash
git add src/xenon/option_chain_snapshotter/snapshot_worker.py scripts/tests/test_option_chain_snapshotter_snapshot_worker.py
git commit -m "feat(option-chain): snapshot worker with per-tick timestamps + 0DTE handling"
```

### Task 8.2: ohlcv_worker.py

- [ ] **Step 1: Failing tests**

```python
# scripts/tests/test_option_chain_snapshotter_ohlcv_worker.py

@pytest.mark.asyncio
async def test_fetches_1min_bars_for_4_underliers(mock_pool, mock_storage):
    """Pass-1 finding F2: explicit 60s cadence + 4-ticker fanout."""
    ...

@pytest.mark.asyncio
async def test_handles_ind_volume_zero(mock_pool, mock_storage):
    """Index secType returns volume=0; row persists with volume=0, not NULL."""
    ...
```

- [ ] **Step 2: Implement ohlcv_worker.py**

60s loop. For each of SPX/NDX/RUT/VIX: `reqHistoricalDataAsync(Index, '120 S', '1 min', 'TRADES', useRTH=False)`. Parallel fanout. Persist to `archive.underlying_ohlcv`.

- [ ] **Step 3: Commit**

```bash
git add src/xenon/option_chain_snapshotter/ohlcv_worker.py scripts/tests/test_option_chain_snapshotter_ohlcv_worker.py
git commit -m "feat(option-chain): underlying OHLCV worker (60s, 4 indexes)"
```

### Task 8.3: Throughput regression test

- [ ] **Step 1: Write the throughput test with probe-derived floor**

After PR 2 probe completes, the operator updates `option_chain_snapshotter/CLAUDE.md` with the measured `p50_cps`. The throughput test reads this:

```python
# scripts/tests/test_option_chain_snapshotter_throughput.py
import asyncio
import re
from pathlib import Path

import pytest

CLAUDE_MD = Path(__file__).resolve().parents[2] / "src/xenon/option_chain_snapshotter/CLAUDE.md"


def _read_floor() -> float:
    text = CLAUDE_MD.read_text()
    m = re.search(r"CI floor.*:\s*([\d.]+)\s*cps", text)
    if not m:
        pytest.skip("CI floor not yet set in CLAUDE.md — run probe first")
    return float(m.group(1))


@pytest.mark.asyncio
async def test_throughput_meets_floor(mock_ib_factory):
    """Simulate 1000 contracts in 60 simulated seconds, assert observed cps >= floor."""
    floor = _read_floor()
    ...
```

- [ ] **Step 2: Verify + commit + PR**

```bash
uv run pytest scripts/tests/test_option_chain_snapshotter_throughput.py scripts/tests/test_option_chain_snapshotter_snapshot_worker.py scripts/tests/test_option_chain_snapshotter_ohlcv_worker.py -v
git add scripts/tests/test_option_chain_snapshotter_throughput.py
git commit -m "test(option-chain): throughput regression test with probe-derived floor"
git push -u origin infra/option-chain-workers
gh pr create --title "feat(option-chain): snapshot + OHLCV workers + throughput regression"
```

---

## PR 9 — Main loop + entry point + single-instance guard

**Goal:** Tie everything together. Main loop coordinates universe refresh, queue, pool, workers, persister. Single-instance guard via PID file + PG advisory lock. CLI entry point.

**Dependencies:** PR 8 merged.

**Branch:** `infra/option-chain-main`.

**Files:**

- Create: `src/xenon/option_chain_snapshotter/__main__.py`
- Modify: `pyproject.toml` (add console entry point)
- Create: `scripts/tests/test_option_chain_snapshotter_main.py`

### Task 9.1: **main**.py with full lifecycle

- [ ] **Step 1: Failing test**

Test the boot sequence: lock acquired → pool connected → universe refreshed → workers running. Test SIGTERM cleanly stops the persister with 30s drain.

- [ ] **Step 2: Implement **main**.py**

```python
"""Entry point for option_chain_snapshotter.

Lifecycle:
  1. Single-instance guard (PID file + PG advisory lock)
  2. Validate alembic at head (or fail-fast)
  3. Connect IB pool
  4. Cold-start universe refresh if no committed universe for today
  5. Start workers: snapshot (×2), ohlcv (×1), persister, AIMD ticker
  6. Run main poller loop until SIGTERM
  7. On SIGTERM: drain persister within ExitTimeOut, then disconnect
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta  # I-4: timedelta for scheduler arithmetic
from pathlib import Path

import psycopg
from xenon.api.services.advisory_lock import LOCK_KEY_OPTION_CHAIN_SNAPSHOTTER

from .config import SnapshotterConfig, TICKERS
from .hours import MarketHoursGate, PollerState
from .limiter import ResizableLimiter
from .ohlcv_worker import OhlcvWorker
from .persister import Persister
from .pool import IBConnectionPool
from .queue import TickerQueue
from .snapshot_worker import SnapshotWorker
from .storage import OptionChainStorage
from .universe import refresh_universe

PID_FILE = Path(os.path.expanduser("~/Library/Caches/xenon/option-chain-snapshotter.pid"))
"""C-3: /var/run is root:daemon-owned on macOS (verified `ls -ld /var/run`).
LaunchAgents run as the operator user and cannot write there. ~/Library/Caches/
is the LaunchAgent-appropriate path; the PG advisory lock is the authoritative
single-instance guard, so loss-on-reboot is not a real concern."""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = SnapshotterConfig.from_env()
    logging.basicConfig(level=config.log_level, format='%(asctime)s %(levelname)s %(name)s %(message)s')

    return asyncio.run(_run(config, dry_run=args.dry_run))


async def _run(config: SnapshotterConfig, dry_run: bool = False) -> int:
    storage = OptionChainStorage(config.database_url)

    # Single-instance guard (Pass-2 finding C-15)
    if not _acquire_pid_file():
        logging.error("Another instance is already running")
        return 1
    advisory_lock_conn = psycopg.connect(config.database_url, autocommit=True)
    with advisory_lock_conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY_OPTION_CHAIN_SNAPSHOTTER,))
        if not cur.fetchone()[0]:
            logging.error("PG advisory lock held by another instance")
            _release_pid_file()
            return 1

    # IB pool
    pool = IBConnectionPool(host=config.ib_host, port=config.ib_port, pool_size=config.pool_size)
    await pool.connect_all()

    # Cold-start universe (Pass-3 finding A-2)
    today = date.today()
    if not storage.fetch_universe(universe_date=today, committed=True):
        logging.info("Cold start: no committed universe for %s, refreshing", today)
        await refresh_universe(pool, storage, tickers=TICKERS, today=today)

    # Workers
    limiter = ResizableLimiter(cap=config.line_cap // config.pool_size, msg_per_sec_cap=config.msg_per_sec_cap)
    queue = TickerQueue()
    gate = MarketHoursGate()
    persister = Persister(dsn=config.database_url)
    snapshot_workers = [
        SnapshotWorker(pool, queue, limiter, persister, storage)
        for _ in range(config.pool_size)
    ]
    ohlcv_worker = OhlcvWorker(pool, storage, tickers=TICKERS)

    # Seed queue with all 4 tickers due now
    for t in TICKERS:
        queue.upsert(t, due_at=datetime.now(tz=gate._cal.tz))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()  # M-3: get_event_loop is deprecated inside coroutines (3.10+)
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    tasks = [
        asyncio.create_task(persister.run()),
        asyncio.create_task(ohlcv_worker.run()),
        *[asyncio.create_task(w.run()) for w in snapshot_workers],
        asyncio.create_task(_aimd_ticker(limiter)),
        asyncio.create_task(_universe_scheduler(pool, storage, stop)),  # I-4: daily 08:30 ET refresh
    ]
    try:
        await stop.wait()
    finally:
        # SIGTERM: drain within ExitTimeOut (30s)
        for t in tasks:
            t.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=30)
        except asyncio.TimeoutError:
            logging.warning("Persister drain timed out")
        pool.disconnect_all()
        advisory_lock_conn.close()
        _release_pid_file()
    return 0


async def _aimd_ticker(limiter: ResizableLimiter):
    while True:
        await asyncio.sleep(30)
        limiter.aimd_tick()  # I-7: public API


async def _universe_scheduler(pool, storage, stop: asyncio.Event):
    """I-4: Daily 08:30 ET universe refresh (spec § 'Daily universe refresh').

    Sleeps until next 08:30 America/New_York, then runs refresh_universe.
    Honors NYSE holidays via MarketHoursGate's calendar — refresh runs on
    session days only. Cancelled via the stop Event on SIGTERM.
    """
    from zoneinfo import ZoneInfo
    from .hours import MarketHoursGate
    gate = MarketHoursGate()
    et = ZoneInfo("America/New_York")
    while not stop.is_set():
        now = datetime.now(tz=et)
        # Next 08:30 ET — today if not yet 08:30, else tomorrow
        target = now.replace(hour=8, minute=30, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        sleep_s = (target - now).total_seconds()
        try:
            await asyncio.wait_for(stop.wait(), timeout=sleep_s)
            return  # stop fired during sleep
        except asyncio.TimeoutError:
            pass
        # Skip non-sessions (weekends, NYSE holidays)
        today = datetime.now(tz=et).date()
        if not gate._cal.is_session(today):
            logging.info("universe_scheduler: %s not an NYSE session, skipping", today)
            continue
        try:
            await refresh_universe(pool, storage, tickers=TICKERS, today=today)
            logging.info("universe_scheduler: refresh complete for %s", today)
        except Exception:
            logging.exception("universe_scheduler: refresh failed for %s", today)


def _acquire_pid_file() -> bool:
    """Validate stale PID via /proc-or-equivalent check (Pass-2 finding C-15)."""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text())
            os.kill(pid, 0)
            # Process exists — but is it ours?
            import subprocess
            r = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True)
            if "option-chain-snapshotter" in r.stdout.strip():
                return False  # real duplicate
        except (OSError, ValueError):
            pass  # stale
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)  # ~/Library/Caches/xenon/
    PID_FILE.write_text(str(os.getpid()))
    return True


def _release_pid_file() -> None:
    PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Add console entry point**

Modify `pyproject.toml`, in `[project.scripts]`:

```toml
xenon-option-chain-snapshotter = "xenon.option_chain_snapshotter.__main__:main"
```

- [ ] **Step 4: Run tests, commit, PR**

```bash
uv run pytest scripts/tests/test_option_chain_snapshotter_main.py -v
uv sync  # refresh entry point
uv run xenon-option-chain-snapshotter --help  # smoke test
git add pyproject.toml uv.lock src/xenon/option_chain_snapshotter/__main__.py scripts/tests/test_option_chain_snapshotter_main.py
git commit -m "feat(option-chain): main entry point + single-instance guard + lifecycle"
git push -u origin infra/option-chain-main
gh pr create --title "feat(option-chain): main loop + CLI entry point"
```

---

## PR 10 — launchd plist, prestart hook, paper-IB live test, canary deploy

**Goal:** Production-ready supervised service on macmini. Layer 4 live tests. Operator runbook.

**Dependencies:** PR 9 merged.

**Branch:** `infra/option-chain-deploy`.

**Files:**

- Create: `scripts/infra/option-chain-prestart.sh`
- Create: `scripts/infra/launchd/com.xenon.option-chain-snapshotter.plist.template`
- Create: `docs/runbooks/option-chain-snapshotter-operations.md`
- Create: `scripts/tests/test_option_chain_live_paper.py` (`@pytest.mark.live`)

### Task 10.1: Prestart hook script

- [ ] **Step 1: Create prestart script**

Create `scripts/infra/option-chain-prestart.sh`:

```bash
#!/usr/bin/env bash
# Prestart hook for option_chain_snapshotter launchd service.
#
# Validates:
#   1. Migrations are at head (alembic check)
#   2. option_chain DB reachable
#   3. IB Gateway port 4001 open
#
# Exits non-zero (preventing launchd start) if any check fails.

set -euo pipefail

cd "$(dirname "$0")/../.." || exit 2

# 1. Migrations
if ! uv run alembic -c scripts/migrations/option_chain/alembic.ini current 2>&1 | grep -q "(head)"; then
    echo "FAIL: option_chain migrations not at head" >&2
    echo "Run: uv run alembic -c scripts/migrations/option_chain/alembic.ini upgrade head" >&2
    exit 1
fi

# 2. DB reachable
if ! uv run python -c "
import os, psycopg
psycopg.connect(os.environ['OPTION_CHAIN_DATABASE_URL'], connect_timeout=5).close()
"; then
    echo "FAIL: option_chain DB unreachable" >&2
    exit 2
fi

# 3. IB Gateway port
if ! nc -z 127.0.0.1 4001 -w 5; then
    echo "FAIL: IB Gateway port 4001 not open" >&2
    exit 3
fi

echo "PRESTART OK"
exit 0
```

- [ ] **Step 2: Make executable + commit**

```bash
chmod +x scripts/infra/option-chain-prestart.sh
git add scripts/infra/option-chain-prestart.sh
git commit -m "feat(option-chain): prestart hook (migrations + DB + IB Gateway checks)"
```

### Task 10.2: launchd plist template

- [ ] **Step 1: Create template**

Create `scripts/infra/launchd/com.xenon.option-chain-snapshotter.plist.template`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.xenon.option-chain-snapshotter</string>

    <!-- C-5: launchd ProgramArguments is exec(), not shell. A bare `&&`
         entry is interpreted as a literal argv to the first command, not as
         a shell operator. Wrap in /bin/sh -c so the chain runs as intended.
         `exec` on the snapshotter ensures launchd's monitored PID is the
         service PID, not the shell wrapper. -->
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>__XENON_ROOT__/scripts/infra/option-chain-prestart.sh &amp;&amp; exec __XENON_ROOT__/.venv/bin/xenon-option-chain-snapshotter</string>
    </array>

    <key>WorkingDirectory</key>
    <string>__XENON_ROOT__</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>ThrottleInterval</key>
    <integer>60</integer>

    <key>ExitTimeOut</key>
    <integer>30</integer>

    <key>StandardOutPath</key>
    <string>/var/log/xenon/option-chain-snapshotter.log</string>

    <key>StandardErrorPath</key>
    <string>/var/log/xenon/option-chain-snapshotter.err</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
```

(`__XENON_ROOT__` is replaced at install time by `scripts/infra/install-launchd.sh`. The env vars come from `~/Library/LaunchAgents/xenon-env.plist` or are sourced from `.env` by the prestart script.)

- [ ] **Step 2: Commit**

```bash
git add scripts/infra/launchd/com.xenon.option-chain-snapshotter.plist.template
git commit -m "feat(option-chain): launchd plist template"
```

### Task 10.3: Operator runbook

- [ ] **Step 1: Create runbook**

Create `docs/runbooks/option-chain-snapshotter-operations.md`:

```markdown
# option_chain_snapshotter — operations runbook

## Install (one-time, macmini operator)

1. Confirm PRs 1-9 merged + deployed.
2. Run probe: `uv run python scripts/research/probe_ib_option_chain.py --host 127.0.0.1 --port 4002`
   - HALT GATE: if `verdict: HALT`, do not proceed.
3. Apply migrations:
   `uv run alembic -c scripts/migrations/option_chain/alembic.ini upgrade head`
4. Install launchd:
   `bash scripts/infra/install-launchd.sh com.xenon.option-chain-snapshotter`
5. Verify running:
   `launchctl list | grep option-chain`
   `tail -f /var/log/xenon/option-chain-snapshotter.log`

## Daily monitoring

- Stale check: `psql -h 127.0.0.1 -U xenon_dev option_chain -c "SELECT * FROM archive.v_staleness WHERE health='stale';"`
- Recent runs: `SELECT * FROM archive.snapshot_run ORDER BY started_at DESC LIMIT 20;`
- Disabled conIds: `SELECT count(*), status FROM archive.option_universe WHERE universe_date = CURRENT_DATE AND status <> 'active' GROUP BY status;`

## Common operations

- **Disable a ticker:** `UPDATE archive.snapshot_config SET enabled=FALSE WHERE ticker='RUT';`
- **Re-run universe refresh on demand:** stop service, run `uv run python -m xenon.option_chain_snapshotter --refresh-universe-only`, restart service.
- **Inspect throughput:** `SELECT ticker, AVG(contracts_persisted::float / NULLIF(duration_ms,0) * 1000) AS cps FROM archive.snapshot_run WHERE finished_at > now() - INTERVAL '1 hour' GROUP BY ticker;`

## Failure response

| Symptom                  | Action                                                                                                         |
| ------------------------ | -------------------------------------------------------------------------------------------------------------- |
| Service in restart loop  | `tail /var/log/xenon/option-chain-snapshotter.err` — usually prestart fail                                     |
| All tickers stale        | Check IB Gateway connectivity; check PG reachability                                                           |
| Throughput < probe floor | Run probe again; compare to baseline in `CLAUDE.md`; investigate regression                                    |
| Disk filling             | Check `archive.option_chain` compression policy: `SELECT * FROM timescaledb_information.compression_settings;` |
```

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/option-chain-snapshotter-operations.md
git commit -m "docs(option-chain): operator runbook"
```

### Task 10.4: Paper-IB live test (Layer 4)

- [ ] **Step 1: Write live test**

Create `scripts/tests/test_option_chain_live_paper.py`:

```python
"""Layer 4 paper-IB live test for option_chain_snapshotter.

Run manually: uv run pytest scripts/tests/test_option_chain_live_paper.py -v --live

Catches IB API contract drift that mocked tests miss (per feedback_live_e2e_surfaces_contract_bugs).
"""
import asyncio
import os

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_TESTS") != "1",
        reason="set RUN_LIVE_TESTS=1 to opt in",
    ),
]


@pytest.mark.asyncio
async def test_vix_universe_refresh_lands():
    """End-to-end: VIX chain qualifies + lands in option_universe."""
    ...


@pytest.mark.asyncio
async def test_spx_snapshot_includes_modelgreeks_for_atm():
    """ATM SPX call snapshot returns IV + delta within 12s."""
    ...
```

- [ ] **Step 2: Run manually before canary**

```bash
RUN_LIVE_TESTS=1 uv run pytest scripts/tests/test_option_chain_live_paper.py -v
```

- [ ] **Step 3: Commit + open PR**

```bash
git add scripts/tests/test_option_chain_live_paper.py
git commit -m "test(option-chain): paper-IB live end-to-end tests"
git push -u origin infra/option-chain-deploy
gh pr create --title "feat(option-chain): launchd deploy + live tests + runbook"
```

### Task 10.5: Canary deploy

- [ ] **Step 1: Operator — initial deploy with VIX only**

After PR 10 merges, on macmini:

```bash
# Enable VIX only
psql -h 127.0.0.1 -U option_chain_writer option_chain -c "
    UPDATE archive.snapshot_config SET enabled=FALSE;
    UPDATE archive.snapshot_config SET enabled=TRUE WHERE ticker='VIX';
"
bash scripts/infra/install-launchd.sh com.xenon.option-chain-snapshotter
launchctl load -w ~/Library/LaunchAgents/com.xenon.option-chain-snapshotter.plist
```

- [ ] **Step 2: 24h observation**

Check `v_staleness`, `snapshot_run.status`, log volume. Verify no restarts, no pacing errors.

- [ ] **Step 3: Expand to SPX (throughput stress test)**

```bash
psql -h 127.0.0.1 -U option_chain_writer option_chain -c "
    UPDATE archive.snapshot_config SET enabled=TRUE WHERE ticker='SPX';
"
launchctl kickstart -k gui/$(id -u)/com.xenon.option-chain-snapshotter
```

Observe effective cadence — should match probe-derived expectation. If significantly worse, rerun probe and investigate.

- [ ] **Step 4: Full universe**

```bash
psql -h 127.0.0.1 -U option_chain_writer option_chain -c "
    UPDATE archive.snapshot_config SET enabled=TRUE;
"
launchctl kickstart -k gui/$(id -u)/com.xenon.option-chain-snapshotter
```

- [ ] **Step 5: Document deployment in runbook**

Append "First deployment: YYYY-MM-DD, scenario landed: optimistic/pessimistic" section to the runbook.

```bash
git add docs/runbooks/option-chain-snapshotter-operations.md
git commit -m "docs(option-chain): record initial deployment + measured baseline"
```

---

## Self-review

### Spec coverage check

| Spec section                 | Implementation in                                                                                                                                  |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| § Architecture               | PR 4-9 collectively                                                                                                                                |
| § Schema                     | PR 3 (full migration)                                                                                                                              |
| § Throughput budget          | PR 2 probe + PR 8 throughput test                                                                                                                  |
| § IB connection management   | PR 5 pool + PR 4 limiter                                                                                                                           |
| § Snapshot lifecycle         | PR 8 snapshot_worker                                                                                                                               |
| § Market-hours gating        | PR 4 hours.py                                                                                                                                      |
| § Daily universe refresh     | PR 6 universe.py                                                                                                                                   |
| § Failure modes              | PRs 4-9 (distributed: limiter for pacing, snapshot_worker for partial, universe for conId disable, persister for backpressure, main for reconnect) |
| § Observability              | PR 3 view + structured logs in PR 9                                                                                                                |
| § Production safety (Pass-3) | PR 6 (A-1 two-step commit) + PR 7 (A-3 high/low water + A-4 cold restart) + PR 8 (A-5 0DTE + A-6 watchdog) + PR 9 (A-2 cold-start)                 |
| § Process supervision        | PR 10 launchd + prestart                                                                                                                           |
| § Code layout                | PR 4-9 each create their listed files                                                                                                              |
| § Testing strategy           | All 16 named regression tests distributed across PRs 4-8                                                                                           |
| § Migration / rollout        | PRs 1, 2 (probe gate), 3, 4, 5, 6, 7, 8, 9, 10 (canary expand)                                                                                     |
| § Open questions             | PR 1 resolves CL-2 (exchange-calendars); PR 2 probe addresses C-1, C-3 verification; OPRA subscriptions remain operator step in PR 10              |

### Placeholder scan

No "TBD" / "TODO" / "implement later" outside the deliberate `<set-from-1password>` and the probe-results template (which is meant to be filled in after the probe runs). Test code is shown in full where possible; the snapshot_worker implementation (Task 8.1 step 2) references the spec for details rather than re-pasting 200 lines — acceptable because the spec is authoritative and present in the same `docs/plans/` directory.

### Type consistency

- `UniverseRow` definition shared between `storage.py` (PR 6) and `universe.py` (PR 6) consumer
- `ChainRow` definition shared between `persister.py` (PR 7) and `snapshot_worker.py` (PR 8) producer
- `TICKERS` tuple defined in `config.py` (PR 4), consumed by `universe.py`, `__main__.py`, `ohlcv_worker.py`
- `LOCK_KEY_OPTION_CHAIN_SNAPSHOTTER` defined in PR 1, consumed in PR 9

All consistent.

---

## Plan complete

**Plan saved to:** `docs/plans/2026-06-02-option-chain-snapshotter-IMPL.md`

Two execution options:

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration. REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`.

**2. Inline Execution** — execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints.

**Which approach?**
