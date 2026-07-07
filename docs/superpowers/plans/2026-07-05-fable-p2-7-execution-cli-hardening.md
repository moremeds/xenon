# Plan: P2.7 — Execution-CLI hardening bundle (OP-12, OP-13, OP-14, OP-15)

**Date:** 2026-07-05
**Branch:** `fix/execution-cli-hardening`
**Findings:** OP-12 (Medium), OP-14 (Medium), OP-13 (Low), OP-15 (Low) — `docs/fable/03-findings-table.md` rows 22–25
**Severity:** Medium (bundle)
**Goal (one line):** Four small, related fixes on the execution-CLI surface — classify place-CLI
exceptions so connection-vs-reject map to the right HTTP class (OP-12); close the `ib_execute`
naked-short bypass and cover it with the caller-allowlist guard (OP-14); remove one dead import +
document the dormant pool cancel/modify module (OP-13); fix a false `ib_pool` docstring (OP-15).

This bundle touches the **order path**. Any live check is **PAPER only**
(`scripts/infra/dev.sh paper`, IB port 4002). Never test against live money. Nothing in this plan
_requires_ a live IB connection — every test uses fakes / pure functions.

---

## 1. Context — what exists today (verified at HEAD `fb5b6d81`)

Four independent surfaces, bundled because they all live in the execution-CLI layer:

- **OP-12** — `src/xenon/execution/ib_place_order.py::place_order` ends with a bare
  `except Exception as e: return {"status": "error", "message": str(e)}` (lines 197-198). The
  message is unclassified. The FastAPI place handler `_orders_place_from_body`
  (`src/xenon/api/server.py:2287-2324`) then treats every `status=="error"` result (that lacks an
  IB `code`) as a semantic reject → `REJECTED` / HTTP 502. A mid-flight _connection_ failure is
  therefore misreported as an IB reject. The cancel/modify CLI already solves this with
  `ib_order_manage.classify_failure` (`src/xenon/execution/ib_order_manage.py:32-57`) → the server
  maps `connection→503 / ownership→409 / ib_reject→4xx` via `_classify_to_http`
  (`server.py:2346-2357`). The place path never got the classifier.
- **OP-13** — `from xenon.api.pool_order_manage import pool_cancel_order, pool_modify_order`
  (`src/xenon/api/server.py:55`) is imported but **never called** anywhere in `server.py`. The
  module `src/xenon/api/pool_order_manage.py` is fully tested standalone
  (`scripts/tests/test_pool_order_manage.py` imports it directly) and the roadmap wants it
  **promoted later** (Option B — `docs/fable/09-target-architecture-options.md`). So the module
  stays; only the dead import is removed and a dormancy note is added to the module docstring.
- **OP-14** — `src/xenon/execution/ib_execute.py` (565 lines, console script `xenon-ib-execute`,
  `pyproject.toml:55`) is a parallel place+monitor+persist path. It builds a `LimitOrder` and calls
  `self.client.place_order(...)` **directly** (`OrderExecutor.place_order`, line 183-194) with **no
  Gate-4 naked-short check**. It is a documented operator tool (`docs/workflows/implement.md`,
  `README.md`), is tested (`scripts/tests/test_ib_execute_scope.py`), and was maintained recently
  (fractional-fill repair `db1febcc`, 2026-06). The caller-allowlist guard
  (`scripts/checks/order_path_caller_allowlist.py`) guards only `ib_place_order`, not `ib_execute`.
- **OP-15** — `src/xenon/api/ib_pool.py` docstring (lines 26-31) claims `acquire_owner` serializes
  "the naked-short audit (clientId 25) **and cancel subprocess** (20-49 range)." Grep proves the
  **only** production caller of `acquire_owner` is `naked_short_audit.py` (lines 382, 405). The
  cancel subprocess (`ib_order_manage.py`) never calls it. The docstring's second clause is false.

The executor does **NOT** need to understand: the combo wizard, the relay/quote stream, Futu, the
web frontend, or the fills-monitor internals of `ib_execute`. Do not touch them.

---

## 2. Drift from review (deltas vs the fable docs)

1. **OP-14 "wire preflight" is scoped to the naked-short Gate-4 guard, NOT the full
   `preflight.evaluate`.** The V1 universe is only **9 hedging tickers**
   (`GLD, IWM, NDX, QQQ, RUT, SIL, SPX, SPY, USO` — verified via
   `xenon.execution.universe.UNIVERSE`). `preflight.evaluate` universe-gates first and rejects any
   ticker outside those 9 with `UNIVERSE_UNKNOWN`. `ib_execute`'s own documented use is selling
   NFLX and buying GOOG options (`ib_execute.py:15-18`, `docs/workflows/implement.md`) — none of
   which are in the universe. Wiring the full evaluator would break the operator tool's primary use.
   The **mandatory, non-negotiable** invariant (root `CLAUDE.md` ⛔ Naked-Short Guard) is Gate 4,
   which the universe-agnostic function `naked_short_audit.find_naked_short_violations(orders,
positions)` enforces directly. So "wire preflight (minimum)" here == wire the naked-short Gate-4
   guard. This is the enforceable core of preflight; the universe gate is deliberately excluded and
   documented. **If a reviewer insists on the full evaluator, STOP** — that is a behavior change to
   the whole order path, out of scope for this bundle.
2. **OP-13 decision is fixed by this plan: keep the module, remove only the import.**
   `docs/fable/03-findings-table.md` row 23 says "Decide: wire it (Option B) or delete it." The
   roadmap (`docs/fable/09-target-architecture-options.md`, Option B) wants it promoted later, so
   this plan does neither delete nor wire — it removes the dead import and documents the module as a
   dormant Option-B seed. No judgment left to the executor.
3. **Line numbers in `03-findings-table.md` have drifted** (it cites `ib_place_order.py:197-198`,
   `ib_pool.py:26-31`, `server.py:55`). All anchors below use function names + unique snippets,
   re-verified at HEAD.
4. **OP-12 and the S2 plan (`2026-07-05-fable-s2-uncertain-orderref.md`) both edit
   `ib_place_order.py` and `_orders_place_from_body`, but on non-overlapping anchors.** See §3
   "Merge independence" — this is load-bearing and stated explicitly so neither PR blocks the other.

---

## 3. Goal / Non-goals

### Goal

- OP-12: place CLI's final `except Exception` emits `classification` (via `classify_failure`); the
  server place handler maps `classification=="connection"` → 503 `IB_CONNECTION` instead of 502
  `IB_REJECT`.
- OP-14: (a) extend `order_path_caller_allowlist.py` to also guard `ib_execute` /
  `xenon-ib-execute`; (b) wire the naked-short Gate-4 guard into `ib_execute` so it refuses to place
  an order that would create/leave a naked short.
- OP-13: remove the dead `pool_cancel_order`/`pool_modify_order` import from `server.py`; add a
  dormancy note to `pool_order_manage.py`'s module docstring.
- OP-15: rewrite the `ib_pool.py` `acquire_owner` docstring to name its single real user
  (the naked-short audit).

### Non-goals (adjacent findings NOT fixed here — one change, one PR)

- **OP-1 / OP-11 / S2** (`UNCERTAIN` state, `orderRef`, event-driven ack) — separate PR
  (`2026-07-05-fable-s2-uncertain-orderref.md`). This plan must NOT touch the ack/blind-sleep block
  (`ib_place_order.py:143-153`) nor add `UNCERTAIN`.
- **Promoting `pool_order_manage` to the live cancel/modify path (Option B)** — deferred; this plan
  keeps it dormant.
- Making `ib_execute` call the FastAPI route (the heavier OP-14 option, explicitly rejected).
- The full `preflight.evaluate` universe gate in `ib_execute` (see Drift §1).
- Combo wizard, relay, Futu, web frontend.

### Merge independence vs the S2 plan (verified — read before touching either file)

- **`ib_place_order.py`:** S2 rewrites the _ack / blind-sleep_ block (the
  `wait_secs = 5 if order_type == "combo" else 2` region, lines 143-153). OP-12 edits the _final
  `except Exception as e:` handler_ (lines 197-198). **Different regions → no conflict.** Either PR
  may land first; git merges both cleanly.
- **`server.py::_orders_place_from_body`:** both plans edit the `if result.data and
result.data.get("status") == "error":` block. OP-12 inserts its connection-classification check
  **immediately before the line `ib_code = result.data.get("code")`.** That exact line is present in
  **both** HEAD and S2's rewritten handler (S2 plan §5, its rewritten block keeps
  `ib_code = result.data.get("code")` verbatim). So the OP-12 insert applies identically regardless
  of merge order. **Merge rule:** whichever lands second rebases; the OP-12 insert re-anchors to the
  same `ib_code = result.data.get("code")` line with no logic change. No coordination needed beyond
  a mechanical rebase.

---

## 4. Key facts (verified against the working tree / installed `.venv`)

- `ib_order_manage.classify_failure(error_code=None, exc=None) -> Literal["connection","ownership",
"ib_reject"]` — pure function, no IB connection, no side effects
  (`src/xenon/execution/ib_order_manage.py:32-57`). `classify_failure(exc=<generic Exception>)`
  returns `"connection"` (the fail-safe default → retryable 503); `exc` of type
  `ConnectionError/TimeoutError/OSError` → `"connection"`; a message containing "326" + "client id"
  → `"ownership"`.
- `server.py` already imports `ReasonCode` and `JSONResponse` and uses `HTTPException`
  (`_orders_place_from_body` uses all three). `ReasonCode.IB_CONNECTION == "IB_CONNECTION"`
  (`src/xenon/execution/preflight.py:48`). No new import needed for OP-12's server edit except
  `classify_failure` is **not** imported into `server.py` — the server only _reads_ the
  `classification` string the CLI already put in the JSON; do NOT import `classify_failure` into
  `server.py`.
- `orders_store.mark_terminal(*, submission_id, state, reason_code, filled_qty, avg_fill_price,
expected_states=None)` — `expected_states` is an existing optional optimistic-concurrency guard
  (`src/xenon/execution/orders_store.py:529-537`).
- `naked_short_audit.find_naked_short_violations(orders: list, positions: list) -> list`
  (`src/xenon/execution/naked_short_audit.py:122`) — **pure, universe-agnostic**. `orders` elements
  are dicts of shape `{"status": <in ACTIVE_STATUSES>, "action": "SELL", "totalQuantity": int,
"orderId": ..., "permId": ..., "contract": {"secType": "STK"|"OPT", "symbol": str, "right":
"C"|"P", "expiry": str, "strike": float}}`. `ACTIVE_STATUSES == {"Submitted", "PreSubmitted"}`
  (`naked_short_audit.py:24`). `positions` elements are portfolio.json-shaped dicts (`ticker`/
  `symbol`, `structure_type`, `contracts`, `expiry`, `legs:[{direction,type,contracts,strike}]`).
  BUY orders and BAG/`secType=="BAG"` are never violations (the function skips them).
- `account_snapshots` (`src/xenon/db/schema.py:63-82`) has `payload JSONB` (PortfolioView-shaped),
  scoped by `broker` / `account_env` / `broker_account`. The web place path loads it via
  `server.py::_load_portfolio_view_sync` (`SELECT payload ... ORDER BY snapshot_at DESC LIMIT 1`).
  `ib_execute` will replicate that ~12-line query locally (execution layer must NOT import
  `xenon.api.server`).
- `resolve_from_env() -> AccountScope` (`src/xenon/execution/account_scope.py:52`); `AccountScope`
  exposes `.broker` / `.account_env` / `.broker_account`. `ib_execute.log_trade` already uses it.
- `get_sync_engine()` is already imported in `ib_execute.py` (line 48).
- `ib_execute.OrderExecutor.place_order` (line 183) is the placement chokepoint; `main()` computes
  `limit_price` (~line 484-496), prints the order summary (~503-506), then hits the `--dry-run`
  exit (509), the confirm prompt (514-519), and finally `executor.place_order(...)` (line 522).
- The caller-allowlist guard scans `src/`, `scripts/`, `web/` for extensions
  `.py/.ts/.tsx/.js/.mjs/.sh/.toml/.yml/.yaml` (**not `.md`**); test files are exempt
  (`scripts/checks/order_path_caller_allowlist.py:55,65,69-77`). `docs/` is not scanned, so the
  `xenon-ib-execute` references in `README.md`/`docs/workflows/implement.md` will not trip it.
  `src/xenon/utils/ib_connection.py:25` and `src/xenon/clients/ib_client.py:101` contain the dict
  key `"ib_execute": <int>` — that is **not** `xenon-ib-execute` nor a
  `xenon.execution.ib_execute` import, so the new patterns will not match it (verify in Step 4c).
- No existing tests for `order_path_caller_allowlist.py` (checked `scripts/tests`) — Step 4 adds the
  first one.

---

## 5. Steps (strictly ordered — TDD: failing test first, then implement, then green)

> All Python via `uv run …`. Do not edit `.env`. Do not commit until the user says so.
> No new console-script entry point is added, so no `uv sync` is needed.

### Step 0 — Branch

```bash
cd /Users/chenxi/projects/xenon
git status --short          # STOP if dirty with unrelated changes; report and wait
git checkout -b fix/execution-cli-hardening
```

The two untracked probe scripts (`scripts/research/probe_*.py`) are pre-existing and unrelated —
leave them alone, do not stage them.

---

### Step 1 — OP-15: fix the `ib_pool.acquire_owner` docstring (docs-only, no test)

**1a.** Edit `src/xenon/api/ib_pool.py`. Replace the comment block (lines 23-31) that reads:

```python
# ---------------------------------------------------------------------------
# Owner-clientId registry (F5)
#
# Serializes short-lived IB connections that share a clientId slot — notably the
# naked-short audit (clientId 25) and cancel subprocess (20-49 range). Two
# concurrent connects on the same clientId would collide at the IB Gateway.
# This registry lets a caller claim a clientId slot in-process before
# attempting a connect, and blocks other callers from racing the same slot.
# ---------------------------------------------------------------------------
```

with (the ONLY real caller is the naked-short audit — verified: grep of `acquire_owner` finds a
single production callsite, `naked_short_audit.py:382,405`):

```python
# ---------------------------------------------------------------------------
# Owner-clientId registry (F5)
#
# Serializes short-lived, same-clientId IB connections so two concurrent
# connects on one clientId can't collide at the IB Gateway. A caller claims the
# slot in-process before connecting; other callers on the same id block until it
# frees. The only production user today is the post-sync naked-short audit
# (`xenon.execution.naked_short_audit`, which claims
# CLIENT_IDS["ib_order_manage"] — currently 20; do not hard-code the number
# here, it is resolved dynamically). The cancel/modify
# subprocess (`ib_order_manage`) does NOT use this registry — it reconnects as
# the order's original placing clientId and does not share a slot.
# ---------------------------------------------------------------------------
```

**Verify (grep-proof):**

```bash
grep -n "cancel/modify subprocess" src/xenon/api/ib_pool.py   # expect the new line
grep -rn "acquire_owner" src/xenon --include=*.py | grep -v "def acquire_owner" | grep -v tests
# expect ONLY naked_short_audit.py lines — confirms the docstring is now accurate
```

If the second grep shows any caller other than `naked_short_audit.py`, STOP — the docstring claim
you just wrote is wrong; report the extra caller.

---

### Step 2 — OP-13: remove dead import + document dormancy (no behavior change)

**2a.** Edit `src/xenon/api/server.py`. Delete line 55 exactly:

```python
from xenon.api.pool_order_manage import pool_cancel_order, pool_modify_order
```

**2b.** Edit `src/xenon/api/pool_order_manage.py`. Replace the module docstring (lines 1-6):

```python
"""Pool-based order cancel/modify — no subprocess, no extra connections.

Routes cancel/modify through the IBPool's sync connection (clientId=0, master).
The master client can manage ALL orders regardless of which clientId placed them,
eliminating the need to spawn subprocess scripts with their own IB connections.
"""
```

with:

```python
"""Pool-based order cancel/modify — no subprocess, no extra connections.

Routes cancel/modify through the IBPool's sync connection (clientId=0, master).
The master client can manage ALL orders regardless of which clientId placed them,
eliminating the need to spawn subprocess scripts with their own IB connections.

DORMANT — not wired into any live route. The production cancel/modify path is
the subprocess `ib_order_manage` (it reconnects as the order's *original*
placing clientId; the master client can SEE but not cancel/modify orders placed
by another clientId — see `src/xenon/api/CLAUDE.md` § Cancel / Modify Failure
Propagation). This module is kept as the Option-B seed: the target architecture
(`docs/fable/09-target-architecture-options.md`) proposes promoting it to replace
the subprocess. Fully unit-tested (`scripts/tests/test_pool_order_manage.py`).
Do not delete; do not import it into `server.py` until Option B is executed.
"""
```

**2c. Verify** the module's tests still run standalone (they import the module directly, so removing
the `server.py` import cannot affect them) and that nothing else imports the removed symbols:

```bash
grep -rn "pool_cancel_order\|pool_modify_order" src/xenon --include=*.py | grep -v tests
#   expect ONLY the two `def` lines in pool_order_manage.py — NO importer in server.py
uv run pytest scripts/tests/test_pool_order_manage.py -q     # expect all pass
uv run python -c "import xenon.api.server"                   # server still imports cleanly
```

If `import xenon.api.server` raises `NameError`/`F821` for `pool_cancel_order`/`pool_modify_order`,
some code path _was_ using it — STOP and report (the finding said it was dead; a live use contradicts
the finding).

---

### Step 3 — OP-12: classify place-CLI exceptions → correct HTTP class

**3a. Failing test (CLI level)** — append to `scripts/tests/test_ib_place_order.py` (reuse its
`_FakeClient` style; a fresh subclass whose `qualify_contracts` raises drives the final
`except Exception`):

```python
def test_place_order_classifies_connection_exception(monkeypatch):
    """A mid-flight exception in the place body must carry a `classification`
    so the parent maps connection failures to 503, not a 502 IB reject (OP-12)."""

    class _RaisingClient(_FakeClient):
        def qualify_contracts(self, *contracts):
            raise ConnectionError("socket dropped mid-place")

    monkeypatch.setattr(ib_place_order, "IBClient", _RaisingClient)
    result = ib_place_order.place_order(
        {
            "type": "stock",
            "symbol": "QQQ",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": 1.0,
            "tif": "DAY",
        }
    )
    assert result["status"] == "error"
    assert result["classification"] == "connection"
    assert "socket dropped" in result["message"]
```

Run — MUST fail with `KeyError: 'classification'`:

```bash
uv run pytest scripts/tests/test_ib_place_order.py::test_place_order_classifies_connection_exception -xvs
```

If it passes, STOP — the anchor is wrong (the handler already classifies).

**3b. Implement (CLI)** — `src/xenon/execution/ib_place_order.py`. Replace the final handler
(lines 197-198):

```python
    except Exception as e:
        return {"status": "error", "message": str(e)}
```

with (import is function-local to avoid a module-level `server`→CLI cycle and keep the CLI's import
surface minimal — `ib_order_manage` is a sibling execution module, safe to import):

```python
    except Exception as e:
        # OP-12: classify so the FastAPI place handler maps connection failures
        # to 503 (retryable) instead of collapsing every exception into a 502
        # IB reject. Mirrors the cancel/modify CLI (ib_order_manage). A generic
        # exception with no IB error code falls back to "connection" (fail-safe).
        from xenon.execution.ib_order_manage import classify_failure

        return {
            "status": "error",
            "message": str(e),
            "classification": classify_failure(exc=e),
        }
```

> Do NOT touch the early connection-failure return at line 52-53 (`"Connection failed: {e}"`) — it
> is outside the finding's scope and S2 does not touch it either; leave it.

Green:

```bash
uv run pytest scripts/tests/test_ib_place_order.py scripts/tests/test_ib_place_order_contract.py -q
```

**3c. Failing test (server mapping)** — new file
`scripts/tests/test_orders_place_classification_route.py`. It monkeypatches
`_run_ib_script_with_recovery` to return a classified error `ScriptResult` (no fake CLI binary
needed) and asserts the route maps `connection` → 503. Mark `committed_db` (the place handler writes
via `orders_store`'s own engine).

```python
"""OP-12: a place result with classification=="connection" must map to HTTP 503
IB_CONNECTION (retryable), not 502 IB_REJECT."""
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.committed_db

REAL_QQQ_LIMIT = 480.0  # frozen real-ish QQQ level, authoring date 2026-07-05


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("XENON_API_TEST_MODE", raising=False)
    from xenon.api import server
    from xenon.api.subprocess import ScriptResult

    class _Accept:
        accept = True
        reason_code = None
        reason_detail = None

    async def _ok_preflight(body, cover_ratio=1.0):
        return _Accept()

    async def _ok_quote(body):
        return _Accept(), 200

    async def _fake_runner(entry, args, timeout=30, runner=None):
        # Simulate the place CLI returning a classified connection failure.
        return ScriptResult(
            ok=True,
            data={
                "status": "error",
                "message": "socket dropped mid-place",
                "classification": "connection",
            },
        )

    monkeypatch.setattr(server, "_run_preflight", _ok_preflight)
    monkeypatch.setattr(server, "_validate_non_combo_quote", _ok_quote)
    monkeypatch.setattr(server, "_run_ib_script_with_recovery", _fake_runner)
    return TestClient(server.app)


def _place_body(cid):
    return {
        "type": "stock", "symbol": "QQQ", "action": "BUY", "quantity": 1,
        "limitPrice": REAL_QQQ_LIMIT, "con_id": 320227571, "client_attempt_id": cid,
    }


def test_connection_classification_maps_503(client):
    resp = client.post("/orders/place", json=_place_body("cid-op12-conn-1"))
    assert resp.status_code == 503, resp.text
    assert resp.json()["reason_code"] == "IB_CONNECTION"


def test_unclassified_error_still_502(client, monkeypatch):
    # A status=="error" with NO classification must keep today's 502 IB_REJECT.
    from xenon.api import server
    from xenon.api.subprocess import ScriptResult

    async def _reject_runner(entry, args, timeout=30, runner=None):
        return ScriptResult(ok=True, data={"status": "error", "message": "generic reject"})

    monkeypatch.setattr(server, "_run_ib_script_with_recovery", _reject_runner)
    resp = client.post("/orders/place", json=_place_body("cid-op12-rej-1"))
    assert resp.status_code == 502, resp.text
```

Run — MUST fail (`test_connection_classification_maps_503` gets 502 today):

```bash
uv run pytest scripts/tests/test_orders_place_classification_route.py -x
```

If both tests already pass, STOP — the server already reads `classification` (it does not at HEAD).

> **Fixture-attribute check (do before finalizing):** read `_orders_place_from_body`
> `server.py:2168-2204` — `_run_preflight` returns a verdict object read as `.accept` /
> `.reason_code` / `.reason_detail`; the accept path never touches `.reason_code.value` because
> `verdict.accept` short-circuits. `_validate_non_combo_quote` returns `(verdict, status_int)`. The
> `_Accept` stub above satisfies both. If the real signatures differ, adapt the stub — never weaken
> production code for a stub.

**3d. Implement (server mapping)** — `src/xenon/api/server.py`, inside `_orders_place_from_body`, in
the `if result.data and result.data.get("status") == "error":` block. Insert the following
**immediately before the existing line `ib_code = result.data.get("code")`** (this anchor is present
in both HEAD and the S2-rewritten handler — see §3 Merge independence):

```python
        # OP-12: a classified *connection* failure is retryable and was NOT a
        # semantic reject — map it to 503 IB_CONNECTION (parity with the
        # cancel/modify path's _classify_to_http) instead of the REJECTED/502
        # fall-through below. Guard the terminal write so a concurrent WORKING
        # transition (e.g. a late ack under S2) is never clobbered.
        #
        # POST-S2 GUARD (merge-order invariant): if S2 has merged, an ack may
        # have been captured (`result.ack`) and the row already persisted as
        # WORKING — an ack means the order IS live at IB, so a "connection"
        # classification after it must NOT be presented as a retryable failure
        # (that invites a duplicate). Only take this branch when no ack exists:
        #     if getattr(result, "ack", None) is None and result.data.get("classification") == "connection":
        # Pre-S2 (ScriptResult has no `ack` attr) getattr returns None and the
        # condition reduces to the plain classification check.
        if getattr(result, "ack", None) is None and result.data.get("classification") == "connection":
            orders_store.mark_terminal(
                submission_id=submission_id,
                state="FAILED",
                reason_code="SUBPROCESS_ERROR",
                filled_qty=0,
                avg_fill_price=None,
                expected_states=("PENDING",),
            )
            return JSONResponse(
                status_code=503,
                content={
                    "detail": result.data.get("message", "IB connection failed"),
                    "reason_code": ReasonCode.IB_CONNECTION.value,
                    "reason_detail": result.data.get("message"),
                    "submission_id": submission_id,
                },
            )
```

Everything after this insert (the `ib_code = ...` / code-110 / `IB_REJECT` logic) is unchanged.

Green:

```bash
uv run pytest scripts/tests/test_orders_place_classification_route.py -x
```

---

### Step 4 — OP-14a: extend the caller-allowlist guard to cover `ib_execute`

**4a. Failing test** — new file `scripts/tests/test_order_path_caller_allowlist.py`. It builds a tiny
fake repo tree, drops a non-allowlisted file that imports `ib_execute`, and asserts the guard flags
it; then asserts the guard passes clean on the real repo.

```python
"""Guard-script coverage for ib_execute (OP-14a). The caller-allowlist must fail
the build if a non-allowlisted, non-test file imports/invokes the ib_execute
placement entry point — same threat model as ib_place_order."""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "scripts" / "checks" / "order_path_caller_allowlist.py"


def _run_guard(root: Path):
    return subprocess.run(
        [sys.executable, str(GUARD), "--repo-root", str(root)],
        capture_output=True, text=True,
    )


def test_guard_flags_unauthorized_ib_execute_import(tmp_path):
    src = tmp_path / "src" / "rogue"
    src.mkdir(parents=True)
    (src / "caller.py").write_text(
        "from xenon.execution.ib_execute import OrderExecutor\n"
    )
    res = _run_guard(tmp_path)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "ib_execute" in (res.stdout + res.stderr)


def test_guard_flags_unauthorized_ib_execute_cli(tmp_path):
    sh = tmp_path / "scripts"
    sh.mkdir(parents=True)
    (sh / "run.sh").write_text("xenon-ib-execute --type stock --symbol QQQ\n")
    res = _run_guard(tmp_path)
    assert res.returncode == 1, res.stdout + res.stderr


def test_guard_passes_on_real_repo():
    res = _run_guard(REPO)
    assert res.returncode == 0, res.stdout + res.stderr
```

Run — MUST fail on the first two cases (returncode 0 today because the guard ignores `ib_execute`):

```bash
uv run pytest scripts/tests/test_order_path_caller_allowlist.py -x
```

**4b. Implement** — `scripts/checks/order_path_caller_allowlist.py`.

(i) Extend the module docstring: after the "Forbidden references:" list (before the `Reference:`
line), add:

```python
This guard also covers the parallel operator placement entry
`xenon.execution.ib_execute` (CLI: `xenon-ib-execute`), which places orders
directly and must never be imported/invoked by another in-process caller.
```

(ii) Add `ib_execute` to `_ALLOWLIST` (after the `ib_place_order.py` self entry):

```python
        # The module itself (self-references in docstrings, __main__).
        "src/xenon/execution/ib_place_order.py",
        # Parallel operator placement entry — same guard (OP-14). Self-ref only.
        "src/xenon/execution/ib_execute.py",
```

(iii) Add the four `ib_execute` patterns to `_VIOLATION_PATTERNS`:

```python
_VIOLATION_PATTERNS = (
    re.compile(r"^\s*from\s+xenon\.execution\.ib_place_order\s+import\b"),
    re.compile(r"^\s*from\s+xenon\.execution\s+import\b[^#\n]*\bib_place_order\b"),
    re.compile(r"^\s*import\s+xenon\.execution\.ib_place_order\b"),
    re.compile(r"\bxenon-ib-place-order\b"),
    re.compile(r"^\s*from\s+xenon\.execution\.ib_execute\s+import\b"),
    re.compile(r"^\s*from\s+xenon\.execution\s+import\b[^#\n]*\bib_execute\b"),
    re.compile(r"^\s*import\s+xenon\.execution\.ib_execute\b"),
    re.compile(r"\bxenon-ib-execute\b"),
)
```

**4c. Verify no false positive on the real tree** (this is why the CLIENT_IDS dict-key form matters):

```bash
uv run python scripts/checks/order_path_caller_allowlist.py     # expect: exit 0, "OK — no unauthorized callers"
uv run pytest scripts/tests/test_order_path_caller_allowlist.py -x
```

If the guard now reports a violation in `src/xenon/utils/ib_connection.py`,
`src/xenon/clients/ib_client.py`, or any real file, STOP — a pattern is too broad (the dict key
`"ib_execute": 25` must NOT match; only `xenon.execution.ib_execute` imports and the `xenon-ib-execute`
console name should). Tighten the pattern or allowlist the declarative file, and report which.

> Note on "all four execution CLIs" (`docs/fable/10-roadmap.md` acceptance): this bundle covers the
> two _placement_ entries (`ib_place_order`, `ib_execute`). `ib_order_manage` is the legitimate
> cancel/modify subprocess (order-mutating, not a placement bypass) and `ib_reconcile`/`ib_orders`
> are read paths — none are placement bypasses, so they are intentionally not added. State this in
> the PR description.

---

### Step 5 — OP-14b: wire the naked-short Gate-4 guard into `ib_execute`

The guard reuses `naked_short_audit.find_naked_short_violations` (universe-agnostic; see Drift §1)
against the pending order + the scoped portfolio snapshot. Pure and testable without live IB.

**5a. Failing test** — new file `scripts/tests/test_ib_execute_naked_short_gate.py`.

```python
"""OP-14b: ib_execute must refuse to place an order that leaves a naked short."""
import pytest

from xenon.execution import ib_execute


def _portfolio(positions):
    # PortfolioView-shaped payload as stored in account_snapshots.payload.
    return {"positions": positions, "available_funds": "0"}


def test_gate_blocks_uncovered_short_call(monkeypatch):
    # No QQQ shares, no long calls → SELL 1 QQQ call is naked.
    monkeypatch.setattr(ib_execute, "_load_scoped_portfolio_view", lambda: _portfolio([]))
    violations = ib_execute._naked_short_gate(
        order_type="option", symbol="QQQ", side="SELL", qty=1,
        right="C", expiry="20260417", strike=480.0,
    )
    assert violations, "expected a naked-short violation"
    assert "QQQ" in violations[0]["reason"]


def test_gate_allows_covered_short_call(monkeypatch):
    # 100 QQQ shares cover 1 short call.
    covered = [{
        "ticker": "QQQ", "structure_type": "Stock", "contracts": 100, "legs": [],
    }]
    monkeypatch.setattr(ib_execute, "_load_scoped_portfolio_view", lambda: _portfolio(covered))
    violations = ib_execute._naked_short_gate(
        order_type="option", symbol="QQQ", side="SELL", qty=1,
        right="C", expiry="20260417", strike=480.0,
    )
    assert violations == []


def test_gate_allows_buy(monkeypatch):
    monkeypatch.setattr(ib_execute, "_load_scoped_portfolio_view",
                        lambda: (_ for _ in ()).throw(AssertionError("BUY must not load portfolio")))
    assert ib_execute._naked_short_gate(
        order_type="stock", symbol="NFLX", side="BUY", qty=100,
        right=None, expiry=None, strike=None,
    ) == []


def test_gate_blocks_sell_when_portfolio_unavailable(monkeypatch):
    # Fail-closed: a SELL with no loadable snapshot must be blocked, not fall open.
    monkeypatch.setattr(ib_execute, "_load_scoped_portfolio_view", lambda: None)
    violations = ib_execute._naked_short_gate(
        order_type="stock", symbol="QQQ", side="SELL", qty=100,
        right=None, expiry=None, strike=None,
    )
    assert violations and "portfolio snapshot unavailable" in violations[0]["reason"]
```

Run — MUST fail (`AttributeError: _naked_short_gate` / `_load_scoped_portfolio_view`):

```bash
uv run pytest scripts/tests/test_ib_execute_naked_short_gate.py -x
```

**5b. Implement** — `src/xenon/execution/ib_execute.py`.

(i) Add imports near the top (after the existing `from xenon.db.engine import get_sync_engine`,
line 48):

```python
from xenon.execution.naked_short_audit import ACTIVE_STATUSES, find_naked_short_violations
```

(ii) Add the two helpers at module scope (place them just above `class OrderExecutor:`, line 113):

```python
def _load_scoped_portfolio_view() -> Optional[dict]:
    """Load the latest scoped portfolio snapshot payload from Postgres.

    Mirrors server.py::_load_portfolio_view_sync but stays in the execution
    layer (a subprocess CLI must not import xenon.api.server). Returns the
    PortfolioView-shaped payload dict, or None when no snapshot / scope is
    resolvable — callers must fail closed on None for SELL orders.
    """
    try:
        from sqlalchemy import select

        from xenon.db.schema import account_snapshots
        from xenon.execution.account_scope import resolve_from_env

        scope = resolve_from_env()
        stmt = (
            select(account_snapshots.c.payload)
            .where(account_snapshots.c.broker == scope.broker)
            .where(account_snapshots.c.account_env == scope.account_env)
            .where(account_snapshots.c.broker_account == scope.broker_account)
            .order_by(account_snapshots.c.snapshot_at.desc())
            .limit(1)
        )
        with get_sync_engine().connect() as con:
            row = con.execute(stmt).first()
        if row is None or not row.payload:
            return None
        return dict(row.payload)
    except Exception as exc:  # scope unresolved / DB down / bad payload
        print(f"  Warning: could not load portfolio snapshot for gate: {exc}")
        return None


def _naked_short_gate(
    *,
    order_type: str,
    symbol: str,
    side: str,
    qty: int,
    right: Optional[str],
    expiry: Optional[str],
    strike: Optional[float],
) -> list:
    """Gate 4 (naked-short) check for a single pending ib_execute order.

    Reuses the same pure detector as the post-sync audit
    (naked_short_audit.find_naked_short_violations). BUY orders never create
    short exposure → allowed without loading the portfolio. A SELL with no
    loadable portfolio snapshot is BLOCKED (fail closed). Returns the list of
    violation dicts (empty == allowed).
    """
    if side.upper() != "SELL":
        return []

    positions = _load_scoped_portfolio_view()
    if positions is None:
        return [{
            "order_id": None,
            "perm_id": None,
            "symbol": symbol,
            "reason": (
                f"SELL {qty} {symbol}: portfolio snapshot unavailable — refusing to "
                "place to avoid an unguarded naked short (set XENON_TRADING_MODE + "
                "XENON_BROKER_ACCOUNT and run ib_sync first)"
            ),
        }]

    sec_type = "STK" if order_type == "stock" else "OPT"
    contract = {"secType": sec_type, "symbol": symbol.upper()}
    if sec_type == "OPT":
        contract.update({"right": (right or "").upper(), "expiry": expiry, "strike": strike})

    pending_order = {
        "status": "PreSubmitted",  # member of ACTIVE_STATUSES so the detector inspects it
        "action": "SELL",
        "totalQuantity": int(qty),
        "orderId": None,
        "permId": None,
        "contract": contract,
    }
    assert "PreSubmitted" in ACTIVE_STATUSES  # guard against a future rename
    return find_naked_short_violations([pending_order], positions.get("positions", []))
```

(iii) Call the gate in `main()` before placing. Find, in `main()`, the order-summary print block
that ends just before the dry-run exit (lines ~503-511):

```python
        # Show order summary
        print(f"\n💰 Order Summary:")
        print(f"   {args.side} {args.qty}x {args.symbol}")
        print(f"   @ ${limit_price:.2f}")
        print(f"   Total: ${total_value:,.2f}")

        # Dry run exit
        if args.dry_run:
```

Insert the gate check **between the summary block and the `# Dry run exit` comment**:

```python
        # Show order summary
        print(f"\n💰 Order Summary:")
        print(f"   {args.side} {args.qty}x {args.symbol}")
        print(f"   @ ${limit_price:.2f}")
        print(f"   Total: ${total_value:,.2f}")

        # Gate 4 (naked-short) — mandatory, no exceptions (root CLAUDE.md ⛔).
        # ib_execute historically bypassed this; OP-14 closes the bypass.
        violations = _naked_short_gate(
            order_type=args.type,
            symbol=args.symbol,
            side=args.side,
            qty=args.qty,
            right=args.right,
            expiry=args.expiry,
            strike=args.strike,
        )
        if violations:
            print("\n⛔ BLOCKED — naked-short guard:")
            for v in violations:
                print(f"   {v['reason']}")
            sys.exit(1)

        # Dry run exit
        if args.dry_run:
```

> Placement is BEFORE the `--dry-run` exit, so `--dry-run` also reports a block (correct: a blocked
> order should never be presented as placeable). `args.right/expiry/strike` are `None` for stock
> orders — the gate ignores them for `secType=="STK"`.

Green:

```bash
uv run pytest scripts/tests/test_ib_execute_naked_short_gate.py scripts/tests/test_ib_execute_scope.py -q
```

---

### Step 6 — Full-surface regression + guard sweep

```bash
uv run python scripts/infra/dev/run_pytest_affected.py
uv run python scripts/checks/no_json_fallback_on_order_path.py     # expect exit 0
uv run python scripts/checks/no_json_write_on_order_path.py        # expect exit 0
uv run python scripts/checks/order_path_caller_allowlist.py        # expect exit 0
```

No web files are touched → no Vitest/Playwright/tsc required for this bundle.

---

## 6. Verification matrix (exhaustive — exact commands + expected outcomes)

| #   | What                                   | Command                                                                                                                                                                                    | Expected                                                                     |
| --- | -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------- |
| 1   | OP-12 CLI classification (red→green)   | `uv run pytest scripts/tests/test_ib_place_order.py::test_place_order_classifies_connection_exception -xvs`                                                                                | `1 passed`; asserts `result["classification"]=="connection"`                 |
| 2   | OP-12 CLI no regression                | `uv run pytest scripts/tests/test_ib_place_order.py scripts/tests/test_ib_place_order_contract.py -q`                                                                                      | all pass                                                                     |
| 3   | OP-12 server 503 map (red→green)       | `uv run pytest scripts/tests/test_orders_place_classification_route.py -x`                                                                                                                 | 2 passed; `reason_code=="IB_CONNECTION"`, status 503; unclassified stays 502 |
| 4   | OP-13 dead-import removed, no live use | `grep -rn "pool_cancel_order\|pool_modify_order" src/xenon --include=*.py \| grep -v tests`                                                                                                | only the two `def` lines in `pool_order_manage.py`                           |
| 5   | OP-13 server still imports             | `uv run python -c "import xenon.api.server"`                                                                                                                                               | exit 0, no `NameError`                                                       |
| 6   | OP-13 dormant module tests             | `uv run pytest scripts/tests/test_pool_order_manage.py -q`                                                                                                                                 | all pass                                                                     |
| 7   | OP-14a guard test (red→green)          | `uv run pytest scripts/tests/test_order_path_caller_allowlist.py -x`                                                                                                                       | 3 passed                                                                     |
| 8   | OP-14a guard clean on real tree        | `uv run python scripts/checks/order_path_caller_allowlist.py`                                                                                                                              | exit 0, `OK — no unauthorized callers of ib_place_order.`                    |
| 9   | OP-14a shows both entries covered      | `uv run python scripts/checks/order_path_caller_allowlist.py --show-allowlist`                                                                                                             | lists `src/xenon/execution/ib_execute.py`                                    |
| 10  | OP-14b gate (red→green)                | `uv run pytest scripts/tests/test_ib_execute_naked_short_gate.py -x`                                                                                                                       | 4 passed                                                                     |
| 11  | OP-14b no ib_execute regression        | `uv run pytest scripts/tests/test_ib_execute_scope.py scripts/tests/test_utils.py -q`                                                                                                      | all pass                                                                     |
| 12  | OP-15 docstring accurate               | `grep -rn "acquire_owner" src/xenon --include=*.py \| grep -v "def acquire_owner" \| grep -v tests`                                                                                        | only `naked_short_audit.py` lines                                            |
| 13  | Order-path CI guards                   | `uv run python scripts/checks/no_json_fallback_on_order_path.py; uv run python scripts/checks/no_json_write_on_order_path.py; uv run python scripts/checks/order_path_caller_allowlist.py` | each exit 0                                                                  |
| 14  | Scoped affected suite                  | `uv run python scripts/infra/dev/run_pytest_affected.py`                                                                                                                                   | green                                                                        |

**Optional PAPER live smoke (only if IB paper is already up — NOT required):** with
`scripts/infra/dev.sh paper` running, `uv run xenon-ib-execute --type option --symbol QQQ
--expiry <near> --strike <OTM-call> --right C --qty 1 --side SELL --limit 0.05 --dry-run` against a
QQQ-shares-free paper account must print `⛔ BLOCKED — naked-short guard` and exit 1. Never run this
against live (port 4001).

---

## 7. Tripwires / abort criteria (STOP and report)

- Any red-first test (Steps 3a, 3c, 4a, 5a) **passes before** its implementation → the anchor is
  wrong; STOP.
- Step 2c: `import xenon.api.server` raises `NameError` for the removed symbols → the import was NOT
  dead; STOP (contradicts OP-13).
- Step 4c: the guard flags a real (non-test, non-rogue) file → a `_VIOLATION_PATTERN` is too broad;
  STOP, tighten, report which file.
- Step 5: `find_naked_short_violations` import fails or `ACTIVE_STATUSES` no longer contains
  `"PreSubmitted"` → the assert fires; STOP (upstream shape changed).
- OP-12 server edit: if the anchor line `ib_code = result.data.get("code")` is **not found** inside
  `_orders_place_from_body` (S2 landed and renamed it) → STOP, re-read the S2-merged handler and
  re-anchor the insert to the same line; do not guess a new location.
- More than the 7 files below need edits → STOP and report (scope creep): `ib_pool.py`, `server.py`,
  `pool_order_manage.py`, `ib_place_order.py`, `order_path_caller_allowlist.py`, `ib_execute.py`,
  plus the 4 new/edited test files.
- Any step appears to need a **live IB** connection → STOP; everything here is fakes/pure functions.
  If you think you need live, you have taken a wrong turn.
- If a reviewer asks to swap the naked-short gate for full `preflight.evaluate` (universe gate) →
  STOP (Drift §1: that breaks ib_execute's documented non-universe use; out of scope).

---

## 8. Rollback

Single branch, no migration, no schema change, no new console-script entry point, no data writes on
the happy path (the OP-12 `mark_terminal` FAILED write is guarded by `expected_states=("PENDING",)`
and only fires on a real connection failure). To revert everything:

```bash
git checkout master
git branch -D fix/execution-cli-hardening
```

No data cleanup needed. `pool_order_manage.py` is untouched behaviorally (docstring only), so the
dormant Option-B path is preserved intact.

---

## 9. Incident-history row (OP-14 closes a real order-path bypass — append on merge)

Append to `docs/reference/order-path-incident-history.md` (next sequential id; keep the table
format — copy an existing row's column set):

> | \<next id\> | 2026-07-05 (fix/execution-cli-hardening) | `xenon-ib-execute` (documented operator placement CLI) placed orders directly via `IBClient.place_order` with **no Gate-4 naked-short check** — a parallel path around the `/orders/place` preflight, and not covered by the caller-allowlist guard which only guarded `ib_place_order` | `ib_execute.OrderExecutor.place_order` bypassed the server preflight entirely; `scripts/checks/order_path_caller_allowlist.py` did not name `ib_execute`, so a new in-process importer would also have slipped through | Wired the pure `naked_short_audit.find_naked_short_violations` gate into `ib_execute.main()` before placement (BUY exempt; SELL with no loadable scoped snapshot fails closed); extended `order_path_caller_allowlist.py` to guard `xenon.execution.ib_execute` / `xenon-ib-execute` as a second placement entry. Full `preflight.evaluate` deliberately NOT used — its 9-ticker universe gate would break ib_execute's documented equity use; the naked-short guard is the mandatory invariant. Also (same PR) OP-12 place-CLI exception classification (connection→503), OP-13 dead pool-cancel/modify import removed + module documented dormant, OP-15 `ib_pool.acquire_owner` docstring corrected | `scripts/tests/test_ib_execute_naked_short_gate.py`, `scripts/tests/test_order_path_caller_allowlist.py`, `scripts/tests/test_orders_place_classification_route.py`, `scripts/tests/test_ib_place_order.py::test_place_order_classifies_connection_exception` |

---

## 10. Files touched (final scope)

Production: `src/xenon/api/ib_pool.py` (docstring), `src/xenon/api/server.py` (−1 import, +OP-12
insert), `src/xenon/api/pool_order_manage.py` (docstring), `src/xenon/execution/ib_place_order.py`
(except handler), `scripts/checks/order_path_caller_allowlist.py` (guard extension),
`src/xenon/execution/ib_execute.py` (gate + 2 helpers + 1 import).
Tests (new): `scripts/tests/test_orders_place_classification_route.py`,
`scripts/tests/test_order_path_caller_allowlist.py`,
`scripts/tests/test_ib_execute_naked_short_gate.py`.
Tests (appended): `scripts/tests/test_ib_place_order.py`.
Docs (on merge): `docs/reference/order-path-incident-history.md` (one row).
