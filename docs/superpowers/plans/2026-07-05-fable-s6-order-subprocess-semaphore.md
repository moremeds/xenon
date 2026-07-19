# S6 — Bound order-subprocess concurrency (OP-7)

- **Date:** 2026-07-05
- **Branch:** `fix/order-subprocess-semaphore`
- **Finding IDs:** OP-7 (Medium, Confirmed) — `docs/fable/03-findings-table.md` row OP-7; roadmap item S6 (`docs/fable/10-roadmap.md`).
- **Severity:** Medium.
- **Goal (one line):** Cap concurrent order-**mutating** IB subprocesses (place / cancel / modify) at 2 via an `asyncio.Semaphore`, and expose an in-flight gauge in `/health`, without throttling read-only CLIs (chain / depth / greeks / sync / orders-refresh).

---

## 1. Context (what exists today, verified at HEAD)

- `src/xenon/api/server.py` has one shared helper `_run_ib_script_with_recovery(entry, args, timeout=30)` (def at **line 3120**, verified). Every IB-dependent route calls it. It applies a cooldown + gateway-health pre-flight, then delegates to `run_entry_point(...)` (imported from `xenon.api.subprocess`) which actually spawns the subprocess. **There is no concurrency bound today** — a burst of N places spawns N subprocesses, each connecting to the shared IB Gateway on its own auto-allocated clientId (range 20–49).

- **9 call sites** of `_run_ib_script_with_recovery` in `server.py` (verified with grep). Categorised:

  | Line     | Entry point                                              | Order-mutating?     | Wrap in semaphore? |
  | -------- | -------------------------------------------------------- | ------------------- | ------------------ |
  | 1450     | `xenon-ib-sync` (`/portfolio/sync`)                      | No (portfolio pull) | **No**             |
  | 1486     | `xenon-ib-sync` (`_bg_sync_via_subprocess`)              | No                  | **No**             |
  | 1565     | `xenon-ib-orders` (`/orders/refresh`)                    | No (read pull)      | **No**             |
  | **2277** | **`xenon-ib-place-order`** (`_orders_place_from_body`)   | **Yes**             | **Yes**            |
  | **2431** | **`xenon-ib-order-manage`** (`_orders_cancel_from_body`) | **Yes**             | **Yes**            |
  | **2604** | **`xenon-ib-order-manage`** (`_orders_modify_from_body`) | **Yes**             | **Yes**            |
  | 2969     | `xenon-ib-option-chain` (`/options/chain`)               | No (read-only)      | **No**             |
  | 3028     | `xenon-ib-market-depth` (`/market-depth`)                | No (read-only)      | **No**             |
  | 3070     | `xenon-ib-option-greeks` (`/options/greeks`)             | No (read-only)      | **No**             |

  **Only rows 2277 / 2431 / 2604 get the semaphore.** The read-only market-data CLIs (chain/depth/greeks) and the portfolio/orders pull CLIs (sync/orders) are deliberately left unbounded — throttling them would stall the UI's quote/greeks panels behind order traffic.

- **Reservation / sequence-gate ordering (verified):**
  - Place (`_orders_place_from_body`): `orders_store.reserve_attempt(...)` runs at **line 2227**, the subprocess at **2277**. The semaphore wraps 2277 → acquisition happens **after** idempotency reservation. A queued duplicate still returns the `duplicate`/`terminal` short-circuit (lines 2234–2252) before ever reaching the subprocess, so dedup is preserved.
  - Modify (`_orders_modify_from_body`): the sequence gate `apply_modify` / `apply_modify_by_perm_id` runs at **lines 2566/2568**, the subprocess at **2604**. Semaphore wraps 2604 → after the sequence gate. A queued stale modify still loses the `MODIFY_STALE` 409 before the subprocess.
  - Cancel (`_orders_cancel_from_body`): no reservation; subprocess at **2431**.

- **Wizard/combo submit path (verified — critical, memory `in_process_route_bypass`):** `src/xenon/api/routes/wizard.py::submit_combo` → `xenon.execution.combo_wizard.session.submit_combo` (line 293) calls **`server_mod._orders_place_from_body(payload)`** at **line 327** — the same in-process place handler that owns line 2277. Because the semaphore is placed **inside** `_orders_place_from_body` (wrapping line 2277), the wizard combo submit inherits the bound automatically. No separate wizard change is needed. (The `combo_wizard/ib_adapter.py::place_combo_tp` protect-side path uses the IB **pool**, not `_run_ib_script_with_recovery`, so it is out of scope — see Non-goals.)

- **`/health` handler:** `server.py::health` at **line 1141** returns a flat dict of subsystem keys (line 1144–1161). This is where the gauge key goes.

- **Python 3.13** (`.python-version`). Verified: `asyncio.Semaphore` has `_get_loop` → it **binds to the event loop of first use and raises `RuntimeError` if awaited from a different loop**. `TestClient(app)` spins a **fresh AnyIO portal loop per instance**, so a single module-level `asyncio.Semaphore(2)` would bind to the first test's loop and then blow up on the second test. **This is why the plan uses a per-loop `WeakKeyDictionary` accessor, not a bare module-level semaphore** (this deviates from the naive sketch in §3 of `docs/fable/11-code-sketches.md` — see Drift).

**What the executor does NOT need to understand:** IB clientId allocation internals, the cooldown/gateway-restart logic inside `_run_ib_script_with_recovery`, combo BAG semantics, or the naked-short guard. This change is a thin `async with` wrapper + one health key.

---

## 2. Drift from review

- **Sketch §3 of `docs/fable/11-code-sketches.md` uses a bare module-level `asyncio.Semaphore(2)`.** On Python 3.13 that raises `RuntimeError: <Semaphore> is bound to a different event loop` the moment a second `TestClient` (or a second `asyncio.run`) awaits it — every existing place/cancel/modify route test creates its own `TestClient`, so the bare version would break the suite. **Adapted:** a per-running-loop semaphore cached in a `weakref.WeakKeyDictionary` keyed on the loop. Same bound (2), same gauge, loop-safe. Verified via `asyncio.Semaphore` having `_LoopBoundMixin._get_loop` at HEAD Python.
- The sketch's `_orders_inflight` gauge and the wrapper name `_run_order_subprocess` are kept (renamed slightly to house style — see below). No other drift; line numbers in the finding (`2277,2431,2604`) are exact at HEAD.

---

## 3. Goal / Non-goals

**Goal:** ≤2 concurrent order-mutating subprocesses under burst; a burst of 5 concurrent places runs ≤2 subprocesses at once and all 5 still complete; `/health` exposes the current in-flight count. Scope of the bound: per FastAPI process/event loop — prod runs single-worker uvicorn so it is effectively global there; it is NOT a cross-process gateway-wide cap.

**Non-goals (explicitly NOT fixed here — one change, one PR):**

- OP-1 (timeout-after-IB-accept / UNCERTAIN state). Not touched.
- The cooldown-only-fires-after-failure behaviour noted in the OP-7 finding — left as-is; this PR only adds the bound.
- Read-only CLI throttling (chain/depth/greeks/sync/orders) — deliberately excluded.
- The combo-wizard pool protect-side path (`place_combo_tp`) — uses the IB pool, not the subprocess helper; out of scope.
- Making the bound env-tunable — kept a fixed `2` with a ponytail comment naming the upgrade path; do **not** add an env var in this PR.

---

## 4. Key facts (verified)

| Fact                                   | Value                                                                                                 | Source (verified)                                           |
| -------------------------------------- | ----------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| Recovery helper signature              | `async def _run_ib_script_with_recovery(entry: str, args: list, timeout: float = 30) -> ScriptResult` | `server.py:3120`                                            |
| `ScriptResult` import for tests        | `from xenon.api.subprocess import ScriptResult`                                                       | `server.py:64`; used in `test_orders_routes_failures.py:27` |
| Place subprocess call                  | `server.py:2277` inside `_orders_place_from_body` (def `server.py:2139`)                              | verified                                                    |
| Cancel subprocess call                 | `server.py:2431` inside `_orders_cancel_from_body` (def `server.py:2414`)                             | verified                                                    |
| Modify subprocess call                 | `server.py:2604` inside `_orders_modify_from_body` (def `server.py:2515`)                             | verified                                                    |
| Reservation before place subprocess    | `reserve_attempt` at `server.py:2227`                                                                 | verified                                                    |
| Sequence gate before modify subprocess | `apply_modify` / `apply_modify_by_perm_id` at `server.py:2566/2568`                                   | verified                                                    |
| Wizard reuses place handler            | `session.py:327` → `server_mod._orders_place_from_body(payload)`                                      | verified                                                    |
| `/health` dict                         | `server.py:1144–1161`                                                                                 | verified                                                    |
| `asyncio` imported                     | `server.py:12`                                                                                        | verified                                                    |
| `weakref` imported                     | **NO — must add**                                                                                     | grep: absent                                                |
| Python                                 | 3.13; `asyncio.Semaphore` is loop-bound                                                               | `.python-version`; `_get_loop` present                      |
| Next incident-history row number       | **24** (last is 23)                                                                                   | `order-path-incident-history.md`                            |

---

## 5. Steps (strictly ordered, TDD)

### Step 0 — branch

```bash
cd /Users/chenxi/projects/xenon
git checkout -b fix/order-subprocess-semaphore
```

### Step 1 — Write the failing concurrency test FIRST

Create `src/xenon/api/tests/test_order_subprocess_semaphore.py` with the full content below. This test calls the new wrapper `_run_order_subprocess` (which does not exist yet) 5 times concurrently, with `_run_ib_script_with_recovery` monkeypatched to an instrumented slow stub that records the peak number of simultaneous in-flight executions. It asserts peak ≤ 2 and all 5 complete. A second, static test proves by source inspection that exactly the three mutating call sites were repointed (and the six read-only sites were not). A third asserts `/health` reflects the live gauge.

> **Why test the wrapper directly, not the route in test mode:** `_orders_place_from_body` short-circuits at `server.py:2258` (`if _is_test_mode()`) **before** the subprocess call, so a test-mode route request never reaches the semaphore. The wrapper-level test is the deterministic, DB-free way to exercise the bound. It is the exact invariant OP-7 asks for.

```python
"""S6 / OP-7 — order-mutating subprocesses are bounded to 2 concurrent.

Exercises the `_run_order_subprocess` wrapper directly (the route short-circuits
in test mode before the subprocess call, so the semaphore is only reachable at
the wrapper level). Instruments `_run_ib_script_with_recovery` with a slow stub
that records peak simultaneous executions.
"""

from __future__ import annotations

import asyncio

import pytest

from xenon.api import server as server_mod
from xenon.api.subprocess import ScriptResult


@pytest.mark.asyncio
async def test_order_subprocess_bounded_to_two_under_burst(monkeypatch):
    live = 0
    peak = 0
    lock = asyncio.Lock()

    async def slow_stub(entry, args, timeout=30):
        nonlocal live, peak
        async with lock:
            live += 1
            peak = max(peak, live)
        try:
            await asyncio.sleep(0.05)  # hold the slot so overlap is observable
            return ScriptResult(ok=True, data={"status": "ok", "echo": entry})
        finally:
            async with lock:
                live -= 1

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", slow_stub)

    results = await asyncio.gather(
        *[
            server_mod._run_order_subprocess("xenon-ib-place-order", ["--json", "{}"], timeout=15)
            for _ in range(5)
        ]
    )

    assert len(results) == 5
    assert all(r.ok for r in results)      # all five complete
    assert peak <= 2                        # never more than 2 at once
    assert peak == 2                        # and the bound is actually reached (5 > 2)
    assert server_mod._orders_inflight == 0  # gauge returns to zero


def test_call_sites_repointed_statically():
    """Static source assertion — proves the three mutating call sites were
    repointed and the six read-only sites were NOT. Route-level tests can't
    catch a missed edit (they monkeypatch `_run_ib_script_with_recovery`,
    which the wrapper also calls), so inspect the source directly."""
    import inspect

    src = inspect.getsource(server_mod)

    # Mutating entries must no longer call the raw helper anywhere:
    assert '_run_ib_script_with_recovery("xenon-ib-place-order"' not in src
    assert '_run_ib_script_with_recovery("xenon-ib-order-manage"' not in src
    # Exactly three call sites use the bounded wrapper (place, cancel, modify);
    # +1 for the wrapper's own `def` line:
    assert src.count("_run_order_subprocess(") == 4
    # The six read-only/pull entries stay on the raw helper:
    for entry in ("xenon-ib-option-chain", "xenon-ib-market-depth", "xenon-ib-option-greeks"):
        assert f'_run_ib_script_with_recovery("{entry}"' in src, entry
    # xenon-ib-sync / xenon-ib-orders call sites use multi-line form; count
    # total raw-helper occurrences: 6 non-mutating call sites + its own def
    # line + the wrapper's internal delegation call = 8:
    assert src.count("_run_ib_script_with_recovery(") == 8


def test_health_exposes_orders_inflight_gauge(monkeypatch):
    # House pattern from test_health_observability.py: stub the gateway
    # probe (the real handler awaits a TCP check) and use TestClient
    # WITHOUT `with` — /health needs no lifespan state.
    from fastapi.testclient import TestClient

    async def fake_gateway():
        return {"port_listening": True}

    monkeypatch.setattr(server_mod, "check_ib_gateway", fake_gateway)
    body = TestClient(server_mod.app).get("/health").json()
    assert body["orders_inflight"] == 0

    # Prove the key reflects the live gauge, not a hardcoded 0:
    monkeypatch.setattr(server_mod, "_orders_inflight", 2)
    body = TestClient(server_mod.app).get("/health").json()
    assert body["orders_inflight"] == 2
```

Run it — it MUST fail (AttributeError: no `_run_order_subprocess` / no `_orders_inflight`):

```bash
uv run pytest src/xenon/api/tests/test_order_subprocess_semaphore.py -xvs
```

> **TRIPWIRE:** if any of these tests **passes** before Step 2, STOP — the wrapper name or gauge name already exists and you must reconcile, not blindly add.

### Step 2 — Add the wrapper, per-loop semaphore, and gauge

**2a. Add `import weakref`.** In `src/xenon/api/server.py`, find the `import time` line (line 18, verified) and add `weakref` alongside the stdlib imports. Anchor on the existing line:

```python
import time
```

Insert **only** the line `import weakref` immediately after it (do not duplicate `import time`), so the file reads:

```python
import time
import weakref
```

**2b. Add the semaphore accessor + wrapper.** Insert the following block **immediately before** the `_run_ib_script_with_recovery` definition. Anchor on this exact existing line (`server.py:3120`):

```python
async def _run_ib_script_with_recovery(entry: str, args: list, timeout: float = 30) -> ScriptResult:
```

Insert this block directly above that line:

```python
# --- OP-7: bound concurrent order-MUTATING subprocesses (place/cancel/modify) ---
# Each order subprocess opens its own IB connection on an auto-allocated clientId
# (range 20-49) against the shared Gateway. An unbounded burst churns clientIds and
# loads the Gateway. Cap simultaneous order subprocesses; read-only market-data CLIs
# (chain/depth/greeks) and portfolio/order pulls are intentionally NOT throttled.
#
# ponytail: fixed bound of 2. Chosen for pool clientId pressure + single-Gateway
# load, not scaling headroom. Upgrade path if contention is ever observed: make it
# env-tunable (e.g. XENON_ORDER_EXEC_CONCURRENCY) — do NOT add that knob speculatively.
_ORDER_EXEC_MAX_CONCURRENCY = 2

# asyncio.Semaphore binds to the loop of first use and raises across loops; TestClient
# spins a fresh portal loop per instance, so key one semaphore per running loop. A
# WeakKeyDictionary drops the entry when a loop is GC'd (no id-reuse hazard).
_order_semaphores: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)
_orders_inflight = 0  # gauge surfaced in /health (best-effort, process-global)


def _order_exec_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _order_semaphores.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(_ORDER_EXEC_MAX_CONCURRENCY)
        _order_semaphores[loop] = sem
    return sem


async def _run_order_subprocess(entry: str, args: list, timeout: float = 30, **kwargs) -> ScriptResult:
    """Concurrency-bounded wrapper around `_run_ib_script_with_recovery` for the
    three order-MUTATING call sites (place/cancel/modify). Unbounded wait on the
    semaphore. A held slot is bounded by the subprocess `timeout` (15s) PLUS the
    recovery helper's own pre-flight gateway check and post-failure
    restart/reconnect logic — under a gateway restart a slot can be held
    considerably longer, which is intentional: queueing order mutations behind
    a restarting gateway beats spawning more subprocesses at it. The bound is
    per FastAPI process/event loop (prod runs single-worker uvicorn, so it is
    effectively global there); it is NOT a cross-process gateway-wide cap.
    Do NOT route read-only CLIs through this wrapper.
    """
    global _orders_inflight
    async with _order_exec_semaphore():
        _orders_inflight += 1
        try:
            # **kwargs forwards future options (e.g. S2's runner=run_place_subprocess)
            # so the semaphore wrapper composes with the staged place runner
            # regardless of merge order.
            return await _run_ib_script_with_recovery(entry, args, timeout=timeout, **kwargs)
        finally:
            _orders_inflight -= 1
```

**2c. Repoint the three order-mutating call sites** to the wrapper. Three exact one-line edits (each `old` string is unique at HEAD):

- Place — `server.py:2277`:
  - old: `    result = await _run_ib_script_with_recovery("xenon-ib-place-order", ["--json", order_json], timeout=15)`
  - new: `    result = await _run_order_subprocess("xenon-ib-place-order", ["--json", order_json], timeout=15)` (if S2 has already merged, the call carries `runner=run_place_subprocess` — KEEP that kwarg; the wrapper forwards it)

- Cancel — `server.py:2431`:
  - old: `    result = await _run_ib_script_with_recovery("xenon-ib-order-manage", args, timeout=15)` (the one inside `_orders_cancel_from_body`, at line 2431)
  - new: `    result = await _run_order_subprocess("xenon-ib-order-manage", args, timeout=15)`

- Modify — `server.py:2604`:
  - old: `    result = await _run_ib_script_with_recovery("xenon-ib-order-manage", args, timeout=15)` (the one inside `_orders_modify_from_body`, at line 2604)
  - new: `    result = await _run_order_subprocess("xenon-ib-order-manage", args, timeout=15)`

> **NOTE for the executor:** lines 2431 and 2604 are byte-identical. `Edit` with `replace_all=false` will error on a non-unique match. Disambiguate by including the surrounding unique context in `old_string`:
>
> - For cancel (2431), use as `old_string`:
>   ```python
>       result = await _run_ib_script_with_recovery("xenon-ib-order-manage", args, timeout=15)
>       if not result.ok:
>           detail = {
>               "reason_code": ReasonCode.IB_CONNECTION.value,
>   ```
>   and replace only the first line with `_run_order_subprocess`.
> - For modify (2604), use as `old_string`:
>   ```python
>       result = await _run_ib_script_with_recovery("xenon-ib-order-manage", args, timeout=15)
>       if not result.ok:
>           # DB sequence is already advanced; don't roll back — prevents
>   ```
>   and replace only the first line with `_run_order_subprocess`.

**2d. Do NOT touch** the six non-mutating call sites (1450, 1486, 1565, 2969, 3028, 3070). They keep calling `_run_ib_script_with_recovery` directly.

### Step 3 — Add the gauge to `/health`

In `server.py::health` (line 1141), add one key to the returned dict. Anchor on the existing line inside the return dict (`server.py:1160`):

```python
        "realtime_subscribers": await asyncio.to_thread(_realtime_subscribers_health),
    }
```

Change to:

```python
        "realtime_subscribers": await asyncio.to_thread(_realtime_subscribers_health),
        "orders_inflight": _orders_inflight,
    }
```

(Chosen key name: `orders_inflight` — flat int, matches the health dict's flat snake_case style. No masking needed; it's a count, not an identifier.)

### Step 4 — Green

```bash
uv run pytest src/xenon/api/tests/test_order_subprocess_semaphore.py -xvs
```

All three tests must pass.

---

## 6. Verification matrix

| #   | Check                                                                                     | Exact command                                                                                                                  | Expected outcome                                                                                            |
| --- | ----------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| 1   | New concurrency unit test                                                                 | `uv run pytest src/xenon/api/tests/test_order_subprocess_semaphore.py -xvs`                                                    | 3 passed. `test_order_subprocess_bounded_to_two_under_burst` asserts `peak == 2` and all 5 `r.ok`.          |
| 2   | Existing order-route failure suite (regression — routes still map correctly)              | `uv run pytest src/xenon/api/tests/test_orders_routes_failures.py -x`                                                          | all pass (these patch `_run_ib_script_with_recovery`, which the wrapper still calls — behaviour unchanged). |
| 3   | Read-only mode routes unaffected                                                          | `uv run pytest src/xenon/api/tests/test_read_only_mode.py -x`                                                                  | all pass.                                                                                                   |
| 4   | Scoped affected suite                                                                     | `uv run python scripts/infra/dev/run_pytest_affected.py`                                                                       | 0 failures.                                                                                                 |
| 5   | Order-path CI guard (fallback reads)                                                      | `uv run python scripts/checks/no_json_fallback_on_order_path.py`                                                               | exit 0.                                                                                                     |
| 6   | Order-path CI guard (writes)                                                              | `uv run python scripts/checks/no_json_write_on_order_path.py`                                                                  | exit 0.                                                                                                     |
| 7   | Order-path CI guard (caller allowlist)                                                    | `uv run python scripts/checks/order_path_caller_allowlist.py`                                                                  | exit 0 (this PR adds no new importer of `ib_place_order`).                                                  |
| 8   | `/health` gauge present (route-level)                                                     | covered by test #1's `test_health_exposes_orders_inflight_gauge`                                                               | `"orders_inflight": 0` in JSON.                                                                             |
| 9   | Live paper probe (OPTIONAL — only if a paper stack is already running; do NOT start live) | `scripts/infra/dev.sh paper` then `curl -s http://localhost:8421/health \| uv run python -m json.tool \| grep orders_inflight` | line `"orders_inflight": 0` (or a small int during active order traffic).                                   |

**Web / typecheck / lint / E2E:** N/A — this change is Python-only, server-side, with **no** UI-visible surface (the gauge is an internal `/health` field the frontend does not render). No Vitest, no Playwright, no `tsc` needed. State this explicitly in the PR description so reviewers don't expect browser evidence.

**Negative direction:** `test_call_sites_repointed_statically` is the negative test — it proves by source inspection that the six read-only call sites do NOT go through the semaphore and that no mutating entry still calls the raw helper (route-level tests cannot catch a missed repoint because they monkeypatch the raw helper, which the wrapper also calls). Both directions covered: mutating path bounded (burst test), read-only path unbounded (static test).

---

## 7. Tripwires / abort criteria

- **If test #1 or #3 in Step 1 passes before Step 2** → STOP. A `_run_order_subprocess` / `_orders_inflight` / `orders_inflight` name already exists; reconcile before adding.
- **If `git grep -n "_run_ib_script_with_recovery(\"xenon-ib-place-order\"\|_run_ib_script_with_recovery(\"xenon-ib-order-manage\""` still returns any hit after Step 2c** → you missed a mutating call site (or repointed a non-mutating one). There must be exactly zero `_run_ib_script_with_recovery` calls for `xenon-ib-place-order` and `xenon-ib-order-manage` after the edit, and the six non-mutating sites must remain on `_run_ib_script_with_recovery`.
- **If more than these 3 files need edits** (`server.py` + the one new test file + the `docs/reference/order-path-incident-history.md` row append from §9) → STOP and report. The change is intentionally 2 files. Do not touch `wizard.py`, `session.py`, or `combo_wizard/*` — the wizard inherits the bound through `_orders_place_from_body` (Context §1).
- **If any step appears to require a live IB connection** → STOP. This entire change is verified with monkeypatched stubs; the optional live probe (matrix #9) is **paper only** (`scripts/infra/dev.sh paper`, IB port 4002). Never run against live (port 4001).
- **If the concurrency test is flaky** (peak observed as 1, never 2) → the `asyncio.sleep(0.05)` hold is too short relative to scheduling; raise it to `0.1`. Do not lower the assertion. If peak is ever observed > 2, the semaphore is not being acquired — re-check the wrapper wraps the `await` and that the three sites call `_run_order_subprocess`.
- **If `RuntimeError: ... bound to a different event loop`** appears in any test → the per-loop `WeakKeyDictionary` accessor was not used (someone reverted to a bare module-level `asyncio.Semaphore(2)`). Restore the accessor from Step 2b.

---

## 8. Rollback

Pure additive, no schema/migration. To revert:

```bash
git checkout master
git branch -D fix/order-subprocess-semaphore
```

If already merged: revert the single squash commit — removing the wrapper and repointing the three sites back to `_run_ib_script_with_recovery` restores exact prior behaviour. No data migration to undo.

---

## 9. Incident-history row

Append this row to `docs/reference/order-path-incident-history.md` (next number is **24**; keep the existing column order `# | Date / PR | Issue | Root cause | Solution | Prevention`):

```markdown
| 24 | 2026-07-05 #<PR> | Unbounded concurrent order subprocesses (OP-7): a burst of places/cancels/modifies spawned N simultaneous IB subprocesses, each on its own clientId (20-49), churning clientIds and loading the shared Gateway | No concurrency bound around the order-mutating `_run_ib_script_with_recovery` call sites in `server.py` (place 2277, cancel 2431, modify 2604) | Added `_run_order_subprocess` wrapper holding a per-event-loop `asyncio.Semaphore(2)` (WeakKeyDictionary-keyed to survive TestClient's per-instance portal loops); repointed the three mutating sites to it. Read-only CLIs (chain/depth/greeks) and portfolio/order pulls stay unbounded. `/health` exposes an `orders_inflight` gauge. Semaphore acquired AFTER `reserve_attempt` (place) / `apply_modify` (modify) so dedup + sequence gates still short-circuit queued duplicates | `test_order_subprocess_semaphore.py`: 5-way burst asserts peak concurrency == 2 + all complete; read-only path not throttled; `/health` gauge present. Wizard combo submit inherits the bound via `_orders_place_from_body` |
```

---

## 10. Commit

Commit message (no AI attribution trailer per global policy):

```
fix(orders): bound concurrent order subprocesses to 2 (OP-7)

Place/cancel/modify now go through _run_order_subprocess, a per-event-loop
asyncio.Semaphore(2) wrapper. Caps simultaneous IB order subprocesses under
burst to limit clientId churn + Gateway load. Read-only CLIs (chain/depth/
greeks) and portfolio/order pulls stay unbounded. /health exposes an
orders_inflight gauge. Semaphore acquired after idempotency reservation so
queued duplicates still dedup.
```

Then push the branch and open a PR (never `git push origin master`); wait for green CI before merge.
