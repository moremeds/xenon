# P1.1 — Order-path stage-timing log lines keyed by `client_attempt_id`

- **Date:** 2026-07-05
- **Proposed branch:** `feat/order-stage-timing-logs`
- **Finding IDs:** P1.1 (roadmap Phase 1, observability) — instrumentation prerequisite for the OP-1 / OP-7 latency work
- **Severity:** Low (purely additive observability; no behavior change)
- **Goal (one line):** Emit one structured, grep/jq-able log line per order-path stage (place: `gates_done`/`reserved`/`subprocess_spawned`/`persisted`; cancel & modify: `spawn`/`confirm`) carrying a correlation id and a monotonic `elapsed_ms`, plus a joinable route log on the Next.js place route — with zero change to order behavior.

---

## 1. Context (what exists today, verified at HEAD)

All Python line numbers below were read at HEAD; **anchor on the function names + quoted snippets**, not the numbers.

- **`src/xenon/api/server.py`**
  - Logger + logging config (verified around lines 89–95):
    ```python
    logger = logging.getLogger("xenon.api")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ```
    `import time` (line 18) and `import json` (line 14) are already present at module scope.
  - `async def _orders_place_from_body(body: dict):` — the place handler. Flow: broker guard → `_run_preflight` → `_validate_non_combo_quote` (non-combo) → extract `cid = body.get("client_attempt_id")` → `orders_store.reserve_attempt(...)` → `submission_id = outcome.submission_id` → test-mode short-circuit (`if _is_test_mode():` calls `orders_store.mark_submitted(...)` then returns) → `result = await _run_ib_script_with_recovery("xenon-ib-place-order", ...)` → on success `orders_store.mark_submitted(...)` inside `if result.data:` → `return result.data`.
  - `async def _orders_cancel_from_body(body: dict):` — test-mode short-circuit, then `order_id = body.get("orderId", 0)` / `perm_id = body.get("permId", 0)`, build `args`, `result = await _run_ib_script_with_recovery("xenon-ib-order-manage", args, timeout=15)`, on success `_mark_submission_cancelled(...)` then `return data`.
  - `async def _orders_modify_from_body(body: dict):` — test-mode short-circuit, sequence gate, build `args`, `result = await _run_ib_script_with_recovery("xenon-ib-order-manage", args, timeout=15)`, on success ends with `return {**data, "applied_sequence": modify_sequence}`.
- **`web/app/api/orders/place/route.ts`** — `export async function POST(request: Request)`. Calls `const requestId = getRequestId();`, validates the body, builds `orderPayload = buildFastApiPlaceOrderPayload(body)`, then `xenonFetch("/orders/place", ...)`. **It emits no `console.*` log today** — `requestId` is only threaded into response bodies/headers. `body.client_attempt_id` is a declared field on `PlaceBody`.

**What the executor does NOT need to understand:** IB pool internals, the subprocess CLIs, preflight/quote-gate logic, combo/naked-short guards, DB schema. This change only _reads_ `client_attempt_id` / `order_id` / `perm_id` that already exist and _writes log lines_. No SQL, no schema, no migration.

---

## 2. Drift from review

- **The S2 "early ack" line is NOT in code yet.** The brief says: include the `ack` stage only if `docs/superpowers/plans/2026-07-05-fable-s2-uncertain-orderref.md` has landed **in code**. Verified at HEAD: `grep -n "orderRef\|IB_ACK\|stage.*ack" src/xenon/execution/ib_place_order.py src/xenon/api/server.py` returns nothing. The S2 plan file exists but the code change (set `order.orderRef`, emit an early `{"stage":"ack"}` line, parse it in `_run_ib_script_with_recovery`) has **not** been implemented. **Therefore this plan OMITS the `ack` stage.** It is listed as an explicit follow-on in §4 Non-goals. Adding `ack` later is a one-line `_order_stage(...)` call in the place handler once S2 lands.
- **Logging formatter drops `extra=` fields.** `08 §8.3` and the `11-code-sketches.md §8` sketch use `logger.info("order_stage", extra={...})`. Verified: the repo's `logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s")` renders **only `%(message)s`** — so `extra=` fields would be silently invisible in production logs. **Decision (one approach): embed the structured fields as a JSON object inside the log _message string_.** No formatter change (a formatter change would alter every log line repo-wide and risk parsers/tests elsewhere — rejected). This keeps the change purely additive and both grep-able (`grep order_stage`) and jq-able. See §5 for the exact shape.
- **Cancel/modify have no `client_attempt_id`.** Their request bodies carry `orderId`/`permId`, not `client_attempt_id` (verified — `_orders_cancel_from_body` reads `body.get("orderId")`/`body.get("permId")`). So cancel/modify stage lines are keyed by an `order_ref` field (the order/perm id) with `op` set to `cancel`/`modify`. Place lines are keyed by `client_attempt_id` with `op=place`. The uniform `op` + `stage` + `elapsed_ms` shape lets one jq filter span all three.

---

## 3. Goal / 4. Non-goals

**Goal:** Add a `_order_stage(...)` helper and call it at each stage of the three handlers, plus one joinable `console.info` on the Next place route. Accept criteria (roadmap): one log line per stage, monotonic `elapsed_ms` deltas, a log-shape unit test.

**Non-goals (explicitly NOT in this PR — one change, one PR):**

- The `ack` stage / S2 early-ack line / UNCERTAIN transition (OP-1, separate plan `2026-07-05-fable-s2-uncertain-orderref.md`).
- Prometheus histograms / `order_subprocesses_inflight` gauge / event-loop-lag sampler (08 §8.3 — later Phase 1/2 items).
- The execution semaphore (OP-7, sketch §3).
- Any relay / quote-path instrumentation (P1.2, sketch §6/§7).
- Changing the logging formatter or introducing a JSON log handler repo-wide.
- Persisting stage timings to Postgres. These are _log lines_, not research/backtest output — the "Research & backtest persistence" rule does not apply (this is operational telemetry, not analytical results).

---

## 5. Key facts (verified)

| Fact                         | Value                                                                 | Verified against                                       |
| ---------------------------- | --------------------------------------------------------------------- | ------------------------------------------------------ |
| Logger name                  | `"xenon.api"`                                                         | `server.py:89`                                         |
| Log format renders           | only `%(message)s` (+ asctime/name/level prefix)                      | `server.py:90`                                         |
| `time` module imported       | yes (`import time`, line 18)                                          | `server.py`                                            |
| `json` module imported       | yes (`import json`, line 14)                                          | `server.py`                                            |
| Place correlation id         | `body.get("client_attempt_id")` (required; 400 if missing)            | `_orders_place_from_body`                              |
| Cancel/modify correlation id | `body.get("orderId")` / `body.get("permId")` (no `client_attempt_id`) | `_orders_cancel_from_body`, `_orders_modify_from_body` |
| Test-mode gate               | `_is_test_mode()` returns True when `XENON_API_TEST_MODE=1`           | used in all three handlers                             |
| Subprocess runner            | `await _run_ib_script_with_recovery(entry, args, timeout=…)`          | `server.py:3120`                                       |
| Next place route id          | `const requestId = getRequestId();`                                   | `web/app/api/orders/place/route.ts:49`                 |
| Next place body field        | `body.client_attempt_id` (typed on `PlaceBody`)                       | `route.ts:38`                                          |
| Test-mode place reaches      | `gates_done`, `reserved`, `persisted` (returns before subprocess)     | handler flow                                           |

**Log message shape (the contract):**

```
order_stage {"stage":"gates_done","elapsed_ms":12.3,"op":"place","client_attempt_id":"c-1"}
```

i.e. the literal token `order_stage`, a space, then a single JSON object. jq recipe:
`grep 'order_stage ' xenon-api.log | sed 's/^.*order_stage //' | jq -c 'select(.op=="place")'`.

---

## 6. Steps (strictly ordered, TDD)

> Repo invariants to honor: all Python via `uv run …`; no `git push origin master` (branch + PR); no AI-attribution commit trailers; no new JSON file read/write on the order path (this change writes **log lines**, not `data/*.json` — CI guards stay green); no `Math.abs()`/sign changes; real tickers at frozen prices in tests; no network at test runtime.

### Step 0 — Branch

```bash
git checkout -b feat/order-stage-timing-logs
```

### Step 1 — Failing unit test for the `_order_stage` helper (log shape + monotonicity)

Create `scripts/tests/test_order_stage_logging.py`:

```python
import json
import logging

import pytest

# The Step-5 integration test drives the real route through TestClient and
# writes via orders_store's own engine — same reason test_place_quote_gate.py
# is marked committed_db. (The Step-1 helper test doesn't need it but the
# module-level marker matches the exemplar and is harmless there.)
pytestmark = pytest.mark.committed_db


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # Import server without needing a live gateway; test-mode keeps lifespan cheap.
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    yield


def _parse_stage_records(caplog):
    """Return the parsed JSON payloads of every `order_stage` log record, in order."""
    out = []
    for rec in caplog.records:
        msg = rec.getMessage()
        if msg.startswith("order_stage "):
            out.append(json.loads(msg[len("order_stage "):]))
    return out


def test_order_stage_emits_one_json_line_per_stage_with_monotonic_elapsed(caplog, monkeypatch):
    from xenon.api import server

    # Deterministic clock: successive time.monotonic() calls advance by 10ms.
    ticks = iter([100.010, 100.020, 100.030, 100.040])
    monkeypatch.setattr(server.time, "monotonic", lambda: next(ticks))

    t0 = 100.000  # explicit anchor; helper computes (now - t0) * 1000
    with caplog.at_level(logging.INFO, logger="xenon.api"):
        server._order_stage("gates_done", t0, op="place", client_attempt_id="c-1")
        server._order_stage("reserved", t0, op="place", client_attempt_id="c-1", submission_id="s-1")
        server._order_stage("subprocess_spawned", t0, op="place", client_attempt_id="c-1")
        server._order_stage("persisted", t0, op="place", client_attempt_id="c-1")

    recs = _parse_stage_records(caplog)
    stages = [r["stage"] for r in recs]
    assert stages == ["gates_done", "reserved", "subprocess_spawned", "persisted"]

    # every record carries the correlation id, op, and a numeric elapsed_ms
    for r in recs:
        assert r["op"] == "place"
        assert r["client_attempt_id"] == "c-1"
        assert isinstance(r["elapsed_ms"], (int, float))
    assert recs[1]["submission_id"] == "s-1"

    # monotonic deltas: elapsed_ms strictly increases across stages
    elapsed = [r["elapsed_ms"] for r in recs]
    assert elapsed == [10.0, 20.0, 30.0, 40.0]
    assert all(b > a for a, b in zip(elapsed, elapsed[1:]))
```

Run it — it MUST fail on `AttributeError: module 'xenon.api.server' has no attribute '_order_stage'`:

```bash
uv run pytest scripts/tests/test_order_stage_logging.py -xvs
```

**Tripwire:** if this test PASSES before Step 2, STOP — `_order_stage` already exists and the anchor is wrong.

### Step 2 — Add the `_order_stage` helper

In `src/xenon/api/server.py`, insert the helper **immediately before** `async def _orders_place_from_body(body: dict):`. Anchor on that `def` line.

```python
def _order_stage(stage: str, t0: float, **fields) -> None:
    """Emit one structured stage-timing log line for the order path (P1.1).

    Format: the literal token ``order_stage`` followed by a single JSON object,
    so the line is greppable (``grep order_stage``) and jq-able. ``elapsed_ms``
    is milliseconds since ``t0`` (a ``time.monotonic()`` reading taken at handler
    entry), giving monotonic deltas within one request. Correlation key is
    ``client_attempt_id`` for place and ``order_ref`` for cancel/modify; ``op``
    disambiguates. Purely observational: any failure here must never affect the
    order path, so the whole body is guarded.
    """
    try:
        payload = {
            "stage": stage,
            "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
            **fields,
        }
        logger.info("order_stage %s", json.dumps(payload, default=str))
    except Exception:  # pragma: no cover — instrumentation must never break orders
        pass
```

Re-run Step 1 — it MUST now pass:

```bash
uv run pytest scripts/tests/test_order_stage_logging.py -xvs
```

### Step 3 — Wire the place handler stages

In `_orders_place_from_body`, make these five edits (anchor on the quoted snippets).

**3a. Capture `t0` + correlation id at entry.** Anchor: the first line of the function body,
`broker = str(getattr(app.state, "broker", "IB") or "IB").upper()`. Insert immediately **above** it:

```python
    _stage_t0 = time.monotonic()
    _stage_cid = str(body.get("client_attempt_id") or "")
```

**3b. `gates_done`** — anchor on `outcome = orders_store.reserve_attempt(`. Insert immediately
**above** that call. This puts the line AFTER the `client_attempt_id` presence validation (the
400 branch under `# F4: atomic reservation`), so a malformed request without a cid never emits
an uncorrelated stage line, and BEFORE the reservation, so the gates→reserve delta is measured:

```python
    _order_stage("gates_done", _stage_t0, op="place", client_attempt_id=_stage_cid)
```

**3c. `reserved`** — anchor on `submission_id = outcome.submission_id`. Insert immediately **below** it:

```python
    _order_stage("reserved", _stage_t0, op="place", client_attempt_id=_stage_cid,
                 submission_id=submission_id)
```

**3d. `persisted` (test-mode path)** — anchor on the test-mode `mark_submitted` call:

```python
        orders_store.mark_submitted(
            submission_id=submission_id,
            ib_order_id=str(order_id),
            perm_id=str(perm_id),
            placing_client_id=26,
        )
```

Insert immediately **below** that call (before the `return { "status": "ok", ... }`):

```python
        _order_stage("persisted", _stage_t0, op="place", client_attempt_id=_stage_cid,
                     submission_id=submission_id)
```

**3e. `subprocess_spawned` + `persisted` (real path)** — anchor on
`result = await _run_ib_script_with_recovery("xenon-ib-place-order", ["--json", order_json], timeout=15)`.
Insert immediately **above** that line:

```python
    _order_stage("subprocess_spawned", _stage_t0, op="place", client_attempt_id=_stage_cid,
                 submission_id=submission_id)
```

Then anchor on the real-path `mark_submitted` inside `if result.data:`:

```python
        orders_store.mark_submitted(
            submission_id=submission_id,
            ib_order_id=str(result.data.get("orderId") or ""),
            perm_id=str(result.data.get("permId") or ""),
            placing_client_id=int(result.data.get("clientId") or 26),
        )
```

Insert immediately **below** it (before `return result.data`):

```python
        _order_stage("persisted", _stage_t0, op="place", client_attempt_id=_stage_cid,
                     submission_id=submission_id)
```

### Step 4 — Wire cancel & modify stages

**4a. Cancel.** In `_orders_cancel_from_body`, anchor on `order_id = body.get("orderId", 0)`. Insert immediately **above** it:

```python
    _stage_t0 = time.monotonic()
```

Then anchor on `result = await _run_ib_script_with_recovery("xenon-ib-order-manage", args, timeout=15)` (the one inside `_orders_cancel_from_body`). Insert immediately **above** it:

```python
    _order_stage("spawn", _stage_t0, op="cancel",
                 order_ref=str(order_id or perm_id or ""))
```

Then anchor on `_mark_submission_cancelled(str(order_id or ""), str(perm_id or ""))`. Insert immediately **below** it (before `return data`):

```python
    _order_stage("confirm", _stage_t0, op="cancel",
                 order_ref=str(order_id or perm_id or ""))
```

**4b. Modify.** In `_orders_modify_from_body`, anchor on `order_id = body.get("orderId", 0)`. Insert immediately **above** it:

```python
    _stage_t0 = time.monotonic()
```

Then anchor on `result = await _run_ib_script_with_recovery("xenon-ib-order-manage", args, timeout=15)` (the one inside `_orders_modify_from_body`). Insert immediately **above** it:

```python
    _order_stage("spawn", _stage_t0, op="modify",
                 order_ref=str(order_id or perm_id or ""))
```

Then anchor on `return {**data, "applied_sequence": modify_sequence}`. Insert immediately **above** it:

```python
    _order_stage("confirm", _stage_t0, op="modify",
                 order_ref=str(order_id or perm_id or ""))
```

> Note: the cancel/modify `spawn` stage is placed on the real path (after the test-mode early return), so `_is_test_mode()` short-circuits skip it — consistent with place, whose `subprocess_spawned` also only fires on the real path.

### Step 5 — Integration test: stages fire in the real place handler (test-mode)

Append to `scripts/tests/test_order_stage_logging.py`:

```python
def test_place_handler_emits_stage_lines_in_test_mode(caplog, monkeypatch, tmp_path):
    from decimal import Decimal

    monkeypatch.setenv("XENON_QUOTE_TOKEN_SECRET", "e" * 64)
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    monkeypatch.setenv("XENON_DATA_DIR", str(tmp_path))

    from fastapi.testclient import TestClient
    from xenon.api import server
    from xenon.execution import orders_store, quote_guard, quote_tokens

    orders_store.init_store()
    server._tick_rule_cache = quote_guard.TickRuleCache(
        source=lambda con_id: Decimal("0.01"),
        ttl_seconds=3600,
    )

    # Real frozen SPY snapshot (authored 2026-07-05): con_id 756733.
    import time as _t
    token = quote_tokens.mint(
        quote_tokens.QuotePayload(
            con_id=756733,
            ticker="SPY",
            bid=Decimal("500.10"),
            ask=Decimal("500.20"),
            bid_size=100,
            ask_size=120,
            ts_server_ms=int(_t.time() * 1000),
        ),
        "e" * 64,
    )
    body = {
        "type": "stock",
        "symbol": "SPY",
        "action": "BUY",
        "quantity": 1,
        "limitPrice": 500.20,
        "client_attempt_id": "stage-int-1",
        "quote_token": token,
        "con_id": 756733,
    }

    client = TestClient(server.app)
    with caplog.at_level(logging.INFO, logger="xenon.api"):
        r = client.post("/orders/place", json=body)
    assert r.status_code == 200

    recs = [
        json.loads(m[len("order_stage "):])
        for m in (rec.getMessage() for rec in caplog.records)
        if m.startswith("order_stage ")
    ]
    place = [r for r in recs if r.get("op") == "place" and r.get("client_attempt_id") == "stage-int-1"]
    stages = [r["stage"] for r in place]
    # Test-mode returns before the subprocess, so subprocess_spawned does not fire.
    assert stages == ["gates_done", "reserved", "persisted"]
    elapsed = [r["elapsed_ms"] for r in place]
    assert all(b >= a for a, b in zip(elapsed, elapsed[1:]))  # non-decreasing (shared t0)
```

Run:

```bash
uv run pytest scripts/tests/test_order_stage_logging.py -xvs
```

**Tripwire:** if `stages` includes `subprocess_spawned`, the test-mode early return moved — STOP and re-verify handler flow. If `stages` is empty, the `at_level`/logger name is wrong (must be `"xenon.api"`).

### Step 5b — Real-path coverage: place `subprocess_spawned` + cancel/modify `spawn`/`confirm`

The test-mode test cannot reach `subprocess_spawned` or the cancel/modify stages. Add three
more tests to `scripts/tests/test_order_stage_logging.py`, modeled on the patching pattern of
`src/xenon/api/tests/test_orders_routes_failures.py` (READ IT FIRST — it drives the real
cancel/modify paths with `_run_ib_script_with_recovery` monkeypatched to return a
`ScriptResult`, and seeds WORKING rows; copy its seeding/patching helpers rather than
inventing new ones):

1. `test_place_real_path_emits_subprocess_spawned_and_persisted` — test mode OFF
   (`monkeypatch.delenv("XENON_API_TEST_MODE")`), quote/preflight stubbed as in
   `test_place_quote_gate.py`, and `server._run_ib_script_with_recovery` monkeypatched to
   return `ScriptResult(ok=True, data={"status": "ok", "orderId": 1, "permId": 2,
"clientId": 26, "initialStatus": "Submitted"})`. Assert the place stage sequence is exactly
   `["gates_done", "reserved", "subprocess_spawned", "persisted"]` with non-decreasing
   `elapsed_ms`.
2. `test_cancel_success_emits_spawn_and_confirm` — mirror the cancel-success case from
   `test_orders_routes_failures.py` (seed a WORKING row, patch the runner to a success
   payload); assert stages `["spawn", "confirm"]` with `op == "cancel"` and
   `order_ref == <the seeded orderId>`.
3. `test_modify_success_emits_spawn_and_confirm` — same for modify (respect the
   `modify_sequence` gate exactly as the existing modify-success test does); assert stages
   `["spawn", "confirm"]` with `op == "modify"`.

These three tests are the proof that every promised stage actually fires on the real path —
without them, a missed Step-3e/Step-4 edit would be invisible (the test-mode test passes
regardless).

### Step 6 — Next.js place route: joinable log line

In `web/app/api/orders/place/route.ts`, anchor on:

```ts
const orderPayload = buildFastApiPlaceOrderPayload(body);
```

Insert immediately **above** that line:

```ts
// P1.1: correlation log so operators can join this request to FastAPI
// `order_stage` lines (op=place) by client_attempt_id value.
console.info(
  `order_place_route ${JSON.stringify({
    requestId,
    client_attempt_id: body.client_attempt_id,
  })}`,
);
```

### Step 7 — Web unit test for the route log

Create `web/tests/order-place-route-stage-log.test.ts`:

```ts
import { describe, test, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("@/lib/xenonApi", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/xenonApi")>("@/lib/xenonApi");
  return {
    ...actual,
    xenonFetch: vi.fn(async () => ({
      orderId: 1,
      permId: 2,
      initialStatus: "Submitted",
    })),
  };
});

import { POST } from "../app/api/orders/place/route";

describe("/api/orders/place — correlation log", () => {
  let infoSpy: ReturnType<typeof vi.spyOn>;
  beforeEach(() => {
    vi.clearAllMocks();
    infoSpy = vi.spyOn(console, "info").mockImplementation(() => {});
  });
  afterEach(() => {
    infoSpy.mockRestore();
  });

  test("logs order_place_route with the client_attempt_id", async () => {
    const req = new Request("http://localhost/api/orders/place", {
      method: "POST",
      body: JSON.stringify({
        type: "stock",
        symbol: "SPY",
        action: "BUY",
        quantity: 1,
        limitPrice: 500.2,
        client_attempt_id: "route-log-1",
      }),
    });
    const res = await POST(req);
    expect(res.status).toBe(200);
    const line = infoSpy.mock.calls
      .map((c) => String(c[0]))
      .find((s) => s.startsWith("order_place_route "));
    expect(line).toBeDefined();
    const payload = JSON.parse(line!.slice("order_place_route ".length));
    expect(payload.client_attempt_id).toBe("route-log-1");
    expect(typeof payload.requestId).toBe("string");
  });
});
```

Run:

```bash
cd web && npm test -- order-place-route-stage-log
```

---

## 7. Verification matrix (run every row; exact commands + expected outcomes)

| #   | Check                                    | Command                                                                                                                               | Expected                                                                                           |
| --- | ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| V1  | New helper unit test                     | `uv run pytest scripts/tests/test_order_stage_logging.py::test_order_stage_emits_one_json_line_per_stage_with_monotonic_elapsed -xvs` | `1 passed`; asserts `stages == [...]`, `elapsed == [10.0,20.0,30.0,40.0]`                          |
| V2  | Integration (test-mode place)            | `uv run pytest scripts/tests/test_order_stage_logging.py::test_place_handler_emits_stage_lines_in_test_mode -xvs`                     | `1 passed`; `stages == ["gates_done","reserved","persisted"]`                                      |
| V2b | Real-path stage coverage (Step 5b)       | `uv run pytest scripts/tests/test_order_stage_logging.py -xvs`                                                                        | 5 passed total — incl. real-path place `[gates_done,reserved,subprocess_spawned,persisted]`, cancel `[spawn,confirm]`, modify `[spawn,confirm]`                                      |
| V3  | Existing idempotency route suite green   | `uv run pytest scripts/tests/test_idempotency_route.py -xvs`                                                                          | `3 passed` (no regressions from the added log calls)                                               |
| V4  | Existing order-route failure suite green | `uv run pytest src/xenon/api/tests/test_orders_routes_failures.py -q`                                                                 | all pass                                                                                           |
| V5  | Scoped affected Python suite             | `uv run python scripts/infra/dev/run_pytest_affected.py`                                                                              | exit 0                                                                                             |
| V6  | CI guard — read side                     | `uv run python scripts/checks/no_json_fallback_on_order_path.py`                                                                      | exit 0                                                                                             |
| V7  | CI guard — write side                    | `uv run python scripts/checks/no_json_write_on_order_path.py`                                                                         | exit 0 (we add log lines, not `data/*.json` writes)                                                |
| V8  | CI guard — caller allowlist              | `uv run python scripts/checks/order_path_caller_allowlist.py`                                                                         | exit 0 (no new `ib_place_order` import)                                                            |
| V9  | Web route log test                       | `cd web && npm test -- order-place-route-stage-log`                                                                                   | `1 passed`                                                                                         |
| V10 | Existing place-route web suites green    | `cd web && npm test -- orders-place-idempotency-passthrough order-place-route-error-propagation orders-upstream-preserved`            | all pass                                                                                           |
| V11 | Web typecheck                            | `cd web && npx tsc --noEmit`                                                                                                          | exit 0, no errors                                                                                  |
| V12 | Web lint                                 | `cd web && npm run lint`                                                                                                              | exit 0 (the `console.info` is intentional; if lint flags `no-console`, keep it — check §Tripwires) |

**Live paper probe (OPTIONAL, PAPER ONLY — not required for merge; no live IB):**
Only if manually smoke-testing. Start `scripts/infra/dev.sh paper` (IB port 4002, Next :3200, FastAPI :8421). Place one paper stock order via the UI, then:

```bash
grep 'order_stage ' logs/*.log | sed 's/^.*order_stage //' | jq -c 'select(.op=="place")'
```

Expected: four JSON objects with `stage` in `gates_done`, `reserved`, `subprocess_spawned`, `persisted`, the same `client_attempt_id`, and strictly increasing `elapsed_ms`.
**Never run this against live IB (port 4001).**

**E2E browser:** Not required. This change produces **no UI-visible output** — it only writes server/route logs. The mandatory-E2E rule applies to UI changes; there is no rendered surface here. (Stated explicitly so the executor does not fabricate a Playwright run.)

---

## 8. Tripwires / abort criteria

- **STOP if the Step-1 test passes before Step 2** — `_order_stage` already exists; the anchor/design is stale. Re-read HEAD.
- **STOP if any anchor snippet in §6 is not found verbatim** — the file drifted since 2026-07-05. Re-locate by function name (`_orders_place_from_body` / `_orders_cancel_from_body` / `_orders_modify_from_body`) and the nearest `mark_submitted` / `_run_ib_script_with_recovery` / `return` line; do not guess line numbers.
- **STOP if edits touch more than 2 files of source** (`src/xenon/api/server.py`, `web/app/api/orders/place/route.ts`) plus the 2 new test files. If a third source file needs changing, the design is wrong — report.
- **STOP if any existing test in V3–V5, V10 goes from green to red** — the log calls must be purely additive. A red test means a stage call raised or was mis-placed (e.g. referenced a variable before assignment). Fix placement; do not weaken the test.
- **If `npm run lint` (V12) fails on `no-console`:** the repo has no other `console.*` in `web/app/api/orders/` (verified), so a rule may fire. Resolution: add an inline `// eslint-disable-next-line no-console` directly above the `console.info` line — do NOT delete the log and do NOT change the eslint config globally. Re-run V12.
- **Never run any live-IB step.** If manual verification is wanted, use `scripts/infra/dev.sh paper` (port 4002) only.
- **Do NOT add the `ack` stage** — S2 has not landed (see §2). If, and only if, `grep -n "orderRef" src/xenon/execution/ib_place_order.py` shows the early-ack line has since merged, STOP and report so this plan can be re-scoped; do not improvise the ack parsing.

---

## 9. Rollback

Pure additive, no schema/migration. To revert:

```bash
git checkout master
git branch -D feat/order-stage-timing-logs
```

If already committed on the branch and only part is bad: `git revert <sha>` — the four edit sites and two test files are independent; reverting the whole commit removes all log lines with zero residual state (no DB rows, no files written).

---

## 10. Incident-history row

This is an **observability addition**, not an order-path _bug fix_, so it does not require a `docs/reference/order-path-incident-history.md` row (that log is for bugs with root-cause/regression-test lineage). If the reviewer prefers a trace-forward breadcrumb, append this row under the existing table:

```
| 2026-07-05 | P1.1 stage-timing logs | Added `_order_stage()` structured log lines (place: gates_done/reserved/subprocess_spawned/persisted; cancel/modify: spawn/confirm) keyed by client_attempt_id/order_ref, plus a joinable order_place_route log on the Next place route. Purely additive; `ack` stage deferred to S2. | N/A (observability, no behavior change) | scripts/tests/test_order_stage_logging.py; web/tests/order-place-route-stage-log.test.ts |
```
