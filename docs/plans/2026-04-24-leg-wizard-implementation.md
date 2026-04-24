# Combo-First Spread Execution Wizard Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a combo-first popup wizard for defined-risk spreads so Xenon places and manages the spread as a single BAG order, reduces legging risk, preserves signed pricing, and keeps all execution and rehydrate evidence in the existing order-hardening stack.

**Architecture:** Reuse the current combo order path instead of inventing a parallel leg sequencer. Wizard session state lives in `wizard_*` tables inside `data/orders.duckdb`, while actual combo submissions and reprices still flow through the existing `orders_store`, `/orders/place`, `/orders/modify`, `ib_place_order.py`, and `ib_order_manage.py` stack. The UI ships as a popup modal launched from `OptionsChainTab > OrderBuilder` and `OrderTab > ComboOrderForm`, with a compact parent `Resume Wizard` strip.

**Tech Stack:** Python 3.13, FastAPI, DuckDB, Pydantic, ib_insync, Next.js App Router, React 19, Vitest, Playwright.

**UI Constraint:** The wizard must ship as an in-app popup modal dialog. It must not ship as a dedicated route, full-page takeover, or side drawer. Any persistent visibility outside the modal is limited to a compact parent-surface session strip with `Resume Wizard`.

**Execution Constraint:** For V1 supported defined-risk spreads, execute as a single combo/BAG order. Do not implement leg-by-leg execution as the default path. Preserve Xenon’s existing BAG action/leg-action/sign semantics.

**Risk Rationale:** Combo-first is the safer default for supported defined-risk
spreads because it avoids turning ordinary entry/exit flow into a legging
workflow that can leave the operator partially filled and temporarily
unhedged.

**Reuse Contract (do NOT modify hardened files):**

- `src/xenon/execution/ib_place_order.py` already handles combo/BAG placement
  with `NonGuaranteed=1` (see `ib_place_order.py:47-81, 126-130`).
- `src/xenon/execution/ib_order_manage.py` already re-applies
  `smartComboRoutingParams` on BAG modifies (`ib_order_manage.py:371-377`).
- Wizard submit calls `POST /orders/place` with `type: "combo"`.
- Wizard reprice calls `POST /orders/modify`.
- The new `combo_wizard/` module is session/planning state only. It MUST NOT
  edit the two files above. If a combo-path bug is discovered during wizard
  work, fix it in a separate PR with its own regression tests — do not bundle.

**Paper-First Gate:** Any task that exercises combo modify or combo abort
against a live IB connection MUST be validated against an IB paper account
before merging. Record the paper verification in the task's Step 4 notes
(see `feedback_broker_bugs_paper_first`).

---

### Task 1: Combo-First Session Storage And Planner Foundation

**Files:**

- Create: `src/xenon/execution/combo_wizard/__init__.py`
- Create: `src/xenon/execution/combo_wizard/models.py`
- Create: `src/xenon/execution/combo_wizard/store.py`
- Create: `src/xenon/execution/combo_wizard/planner.py`
- Create: `src/xenon/execution/combo_wizard/combo_quotes.py`
- Modify: `src/xenon/execution/orders_store.py`
- Create: `scripts/tests/test_combo_wizard_store.py`
- Create: `scripts/tests/test_combo_wizard_planner.py`
- Create: `scripts/tests/test_combo_wizard_quotes.py`

**Step 1: Write the failing tests**

```python
from decimal import Decimal

from xenon.execution.combo_wizard import planner, store


def test_init_store_creates_combo_wizard_tables(tmp_path):
    db = tmp_path / "orders.duckdb"
    store.init_store(db)
    assert store.list_tables(db) >= {
        "wizard_sessions",
        "wizard_combo_attempts",
        "wizard_session_events",
        "wizard_protection",
    }


def test_planner_returns_natural_mid_and_ladder_for_supported_vertical():
    plan = planner.build_plan(...)
    assert plan.structure_name == "Bull Call Spread"
    assert plan.mode == "COMBO"
    assert plan.natural_price == Decimal("2.70")
    assert plan.mid_price == Decimal("2.50")
    assert plan.ladder_step == Decimal("0.05")
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest scripts/tests/test_combo_wizard_store.py scripts/tests/test_combo_wizard_planner.py scripts/tests/test_combo_wizard_quotes.py -q`

Expected: FAIL because `combo_wizard` does not exist yet.

**Step 3: Write minimal implementation**

```python
# src/xenon/execution/combo_wizard/combo_quotes.py
def compute_combo_quote(legs, prices):
    net_ask = Decimal("0")
    net_bid = Decimal("0")
    for leg in legs:
        if leg.action == "BUY":
            net_ask += leg.ask * leg.ratio
            net_bid += leg.bid * leg.ratio
        else:
            net_ask -= leg.bid * leg.ratio
            net_bid -= leg.ask * leg.ratio
    bid = min(net_bid, net_ask)
    ask = max(net_bid, net_ask)
    return ComboQuote(bid=bid, ask=ask, mid=(bid + ask) / 2)
```

Wire `store.init_store()` into `orders_store.init_store()` so wizard tables share `data/orders.duckdb`.

Add a **TS/Python parity test** in `test_combo_wizard_quotes.py` that feeds a
fixed fixture (bull call, bear put, iron condor, long butterfly) into
`compute_combo_quote()` and asserts outputs equal the canonical
`computeNetOptionQuote()` reference values exported from
`web/lib/optionsChainUtils.ts`. The TS reference is the source of truth;
if they disagree, fix the Python mirror.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest scripts/tests/test_combo_wizard_store.py scripts/tests/test_combo_wizard_planner.py scripts/tests/test_combo_wizard_quotes.py -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/xenon/execution/combo_wizard/__init__.py src/xenon/execution/combo_wizard/models.py src/xenon/execution/combo_wizard/store.py src/xenon/execution/combo_wizard/planner.py src/xenon/execution/combo_wizard/combo_quotes.py src/xenon/execution/orders_store.py scripts/tests/test_combo_wizard_store.py scripts/tests/test_combo_wizard_planner.py scripts/tests/test_combo_wizard_quotes.py
git commit -m "feat: add combo wizard planner and storage foundation"
```

### Task 2: FastAPI Wizard Router Wired To Shared Combo Submit/Modify

**Files:**

- Create: `src/xenon/api/routes/wizard.py`
- Create: `src/xenon/execution/combo_wizard/session.py`
- Modify: `src/xenon/api/server.py` (mount `/wizard` router only)
- Create: `src/xenon/api/tests/test_wizard_routes.py`
- Create: `scripts/tests/test_combo_wizard_session.py`

> **Do NOT modify** `src/xenon/execution/ib_place_order.py` or
> `src/xenon/execution/ib_order_manage.py`. Both already support BAG combos
> (`ib_place_order.py:47-81, 126-130`; `ib_order_manage.py:371-377`). Wizard
> submit calls `POST /orders/place` with `type: "combo"`; wizard reprice calls
> `POST /orders/modify`. If a combo-path defect surfaces during wizard work,
> file it as a separate fix PR with its own regression — do not bundle.

**Step 1: Write the failing tests**

```python
def test_plan_endpoint_returns_combo_mode_and_prices(client):
    resp = client.post("/wizard/plan", json={...})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "COMBO"
    assert "natural_price" in body
    assert "mid_price" in body


def test_submit_endpoint_reuses_shared_combo_submission_path(client):
    resp = client.post("/wizard/sessions/abc/submit", json={...})
    assert resp.status_code == 200
    assert resp.json()["submission_id"].startswith("sub-")


def test_reprice_endpoint_modifies_live_combo_order_not_leg_orders(client):
    resp = client.post("/wizard/sessions/abc/reprice", json={"target_price": "2.45"})
    assert resp.status_code == 200
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest src/xenon/api/tests/test_wizard_routes.py scripts/tests/test_combo_wizard_session.py -q`

Expected: FAIL because `/wizard/*` routes and combo session wiring do not exist.

**Step 3: Write minimal implementation**

```python
# src/xenon/api/routes/wizard.py
@router.post("/wizard/sessions/{session_id}/submit")
async def wizard_submit(session_id: str, body: SubmitRequest) -> dict:
    return await combo_session.submit_combo(session_id, body)


@router.post("/wizard/sessions/{session_id}/reprice")
async def wizard_reprice(session_id: str, body: RepriceRequest) -> dict:
    return await combo_session.reprice_combo(session_id, body)
```

Inside submit, derive a stable `client_attempt_id` such as:

```python
client_attempt_id = f"wiz:{session_id}:combo:{attempt_id}"
```

Inside `submit_combo()`, reuse the same place-order code path and semantics as
`/orders/place` with `type: "combo"` by calling the equivalent in-process
helper/service path already used by the orders route. Do not require loopback
HTTP inside FastAPI. This keeps `orders_submissions` and `orders_events`
authoritative and reuses the hardened BAG placement in `ib_place_order.py`
unchanged. `reprice_combo()` must reuse the same modify-order code path and
semantics as `/orders/modify` against the attempt's `ib_order_id` — do not
build a parallel modify path.

Assertion to add in `test_wizard_routes.py`: submit MUST produce exactly one
row in `orders_submissions` per attempt and use the existing combo code path
(verify by patching the real `ib_place_order.place_order` at its import site
in the orders route, not in the wizard module — see
`feedback_shim_vs_real_patching`).

**Step 4: Run tests to verify they pass**

Run: `uv run pytest src/xenon/api/tests/test_wizard_routes.py scripts/tests/test_combo_wizard_session.py -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/xenon/api/routes/wizard.py src/xenon/execution/combo_wizard/session.py src/xenon/api/server.py src/xenon/api/tests/test_wizard_routes.py scripts/tests/test_combo_wizard_session.py
git commit -m "feat: wire combo wizard through shared combo order stack"
```

### Task 3: Next.js Proxy Routes And Session Hook

**Files:**

- Create: `web/app/api/wizard/plan/route.ts`
- Create: `web/app/api/wizard/sessions/route.ts`
- Create: `web/app/api/wizard/sessions/[id]/submit/route.ts`
- Create: `web/app/api/wizard/sessions/[id]/reprice/route.ts`
- Create: `web/app/api/wizard/sessions/[id]/abort/route.ts`
- Create: `web/app/api/wizard/sessions/[id]/protect/route.ts`
- Create: `web/app/api/wizard/sessions/[id]/route.ts`
- Create: `web/app/api/wizard/stream/route.ts`
- Create: `web/lib/useWizardSession.ts`
- Create: `web/tests/wizard-routes.test.ts`
- Create: `web/tests/useWizardSession.test.ts`

**Step 1: Write the failing tests**

```ts
it("passes FastAPI wizard errors through without collapsing status", async () => {
  ...
});


it("streams wizard SSE state into the hook", async () => {
  const { result } = renderHook(() => useWizardSession("wiz-1"));
  ...
  expect(result.current.session?.state).toBe("WORKING");
});
```

**Step 2: Run tests to verify they fail**

Run: `cd web && npm test -- web/tests/wizard-routes.test.ts web/tests/useWizardSession.test.ts`

Expected: FAIL because the proxy routes and hook do not exist.

**Step 3: Write minimal implementation**

Use the same `xenonFetch()` / pass-through approach as current order routes and
the same streaming proxy pattern as `/api/uw-analyze/portfolio`.

**Step 4: Run tests to verify they pass**

Run: `cd web && npm test -- web/tests/wizard-routes.test.ts web/tests/useWizardSession.test.ts`

Expected: PASS.

**Step 5: Commit**

```bash
git add web/app/api/wizard web/lib/useWizardSession.ts web/tests/wizard-routes.test.ts web/tests/useWizardSession.test.ts
git commit -m "feat: add combo wizard proxy routes and session hook"
```

### Task 4: Popup Modal And Parent Session Strip

**Files:**

- Create: `web/components/ticker-detail/WizardModal.tsx`
- Create: `web/components/ticker-detail/WizardSessionStrip.tsx`
- Modify: `web/components/ticker-detail/OptionsChainTab.tsx`
- Modify: `web/components/ticker-detail/OrderTab.tsx`
- Modify: `web/app/globals.css`
- Create: `web/tests/wizard-modal.test.tsx`
- Create: `web/e2e/wizard-open.spec.ts`

**Delivery constraint:**

- `WizardModal` is a popup modal dialog over the current page.
- Do not implement a route-level wizard page.
- Do not implement a right-side drawer / slide-over instead of the modal.
- Add a compact parent-surface `WizardSessionStrip` in:
  - `OptionsChainTab > OrderBuilder`
  - `OrderTab > ComboOrderForm`
- The strip is monitor/resume UI only; the real workflow remains in the modal.
- Modal layout acceptance criteria:
  - width about `960px`
  - header telemetry rail
  - step strip
  - primary workflow pane + right telemetry rail on desktop
  - sticky footer action rail
  - mobile collapse to single column

**Step 1: Write the failing tests**

```ts
it("renders the wizard as a modal dialog, not a drawer landmark", async () => {
  render(<WizardModal open={true} ... />);
  expect(screen.getByRole("dialog", { name: /combo wizard/i })).toBeInTheDocument();
});


it("renders a compact wizard session strip in the parent surface when active", async () => {
  render(<OptionsChainTab ... />);
  expect(screen.getByText(/wizard session/i)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /resume wizard/i })).toBeInTheDocument();
});
```

Add browser coverage:

```ts
test("opens wizard from OrderBuilder, submits combo session, reprices toward natural, and resumes after refresh", async ({ page }) => {
  ...
});
```

**Step 2: Run tests to verify they fail**

Run: `cd web && npm test -- web/tests/wizard-modal.test.tsx && npm run test:e2e -- e2e/wizard-open.spec.ts`

Expected: FAIL because the modal and strip do not exist.

**Step 3: Write minimal implementation**

```tsx
export default function WizardModal({ open, session, ...props }: Props) {
  if (!open) return null;
  return (
    <Modal title="Combo Wizard" open={open} onClose={props.onClose}>
      <div className="wizard-panel">
        <div className="wizard-meta-rail">
          MODE COMBO · NATURAL/MID LADDER · SERVER AUTHORITY
        </div>
        <div className="wizard-step-strip">...</div>
        <div className="wizard-body">
          <section className="wizard-main-pane">...</section>
          <aside className="wizard-telemetry-rail">...</aside>
        </div>
        <div className="wizard-footer-rail">...</div>
      </div>
    </Modal>
  );
}
```

Use Xenon styling only:

- `var(--signal-core)` for active highlights
- `var(--panel)` / `var(--panel-raised)` surfaces
- `4px` max panel radius
- Inter + IBM Plex Mono only
- Keep the underlying ticker page visible beneath the modal scrim

**Step 4: Run tests to verify they pass**

Run: `cd web && npm test -- web/tests/wizard-modal.test.tsx && npm run test:e2e -- e2e/wizard-open.spec.ts`

Expected: PASS.

**Step 5: Commit**

```bash
git add web/components/ticker-detail/WizardModal.tsx web/components/ticker-detail/WizardSessionStrip.tsx web/components/ticker-detail/OptionsChainTab.tsx web/components/ticker-detail/OrderTab.tsx web/app/globals.css web/tests/wizard-modal.test.tsx web/e2e/wizard-open.spec.ts
git commit -m "feat: add combo wizard popup and session strip"
```

### Task 5: Protection, Rehydrate, And Daemon Integration

**Files:**

- Create: `src/xenon/execution/combo_wizard/protect.py`
- Create: `src/xenon/execution/combo_wizard/rehydrate.py`
- Create: `src/xenon/monitor_daemon/handlers/wizard_stop_monitor.py`
- Modify: `src/xenon/monitor_daemon/run.py`
- Modify: `src/xenon/execution/single_leg_rehydrate.py`
- Create: `scripts/tests/test_combo_wizard_protect.py`
- Create: `scripts/tests/test_combo_wizard_rehydrate.py`
- Create: `scripts/tests/test_wizard_stop_monitor.py`
- Create: `web/e2e/wizard-risk-alert.spec.ts`

**Step 1: Write the failing tests**

```python
def test_protection_pending_retries_then_fails_if_tp_attach_never_acks():
    ...


def test_rehydrate_uses_execution_truth_before_open_order_disappearance():
    ...
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest scripts/tests/test_combo_wizard_protect.py scripts/tests/test_combo_wizard_rehydrate.py scripts/tests/test_wizard_stop_monitor.py -q`

Expected: FAIL because protection/rehydrate/daemon pieces do not exist.

**Step 3: Write minimal implementation**

Reuse `_reconcile_from_three_sources` from `single_leg_rehydrate.py` where the
attempt/order-level logic overlaps.

**BAG-specific reconcile (critical):** IB reports combo fills as per-leg
execution rows sharing one parent `permId`. The combo rehydrate branch MUST
group executions by `permId`, sum each leg's `shares` against the expected
ratio, and only mark the attempt `FILLED` when every leg reached ratio ×
`totalQuantity`. A single-leg execution row is NOT a combo fill. Add a
regression test that feeds two leg executions (one partial, one full) and
asserts the session stays `PARTIALLY_FILLED`, not `FILLED`.

Register `WizardStopMonitorHandler` in `src/xenon/monitor_daemon/run.py`.

**Paper-first validation:** Before merging Task 5, run a manual paper-account
dry-run covering: combo submit → reprice (modify live BAG) → abort → fresh
submit → partial leg fill → restart/rehydrate. Record the paper session ID
and outcomes in the PR description.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest scripts/tests/test_combo_wizard_protect.py scripts/tests/test_combo_wizard_rehydrate.py scripts/tests/test_wizard_stop_monitor.py -q && cd web && npm run test:e2e -- e2e/wizard-risk-alert.spec.ts`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/xenon/execution/combo_wizard/protect.py src/xenon/execution/combo_wizard/rehydrate.py src/xenon/monitor_daemon/handlers/wizard_stop_monitor.py src/xenon/monitor_daemon/run.py src/xenon/execution/single_leg_rehydrate.py scripts/tests/test_combo_wizard_protect.py scripts/tests/test_combo_wizard_rehydrate.py scripts/tests/test_wizard_stop_monitor.py web/e2e/wizard-risk-alert.spec.ts
git commit -m "feat: add combo wizard protection and restart safety"
```

### Task 6: Full Verification And Release Readiness

**Files:**

- Modify: `docs/status.md`
- Modify: `docs/superpowers/specs/2026-04-20-leg-wizard-design.md`
- Test: `scripts/tests/test_combo_wizard_*.py`
- Test: `src/xenon/api/tests/test_wizard_routes.py`
- Test: `web/tests/wizard-*.test.tsx`
- Test: `web/e2e/wizard-*.spec.ts`

**Step 1: Write the release checklist**

```md
- no duplicate wizard submissions in orders_events
- combo submit/modify stayed BAG-first for supported spreads
- popup verified from both Order Builder and Order Tab
- Risk Alert copy says assisted exit, not stop-loss
- no leg-by-leg default flow was added to V1
```

**Step 2: Run all verification commands**

Run: `uv run pytest scripts/tests/test_combo_wizard_*.py src/xenon/api/tests/test_wizard_routes.py scripts/tests/test_naked_short_audit.py`

Expected: PASS.

Run: `cd web && npm test -- web/tests/wizard-routes.test.ts web/tests/useWizardSession.test.ts web/tests/wizard-modal.test.tsx`

Expected: PASS.

Run: `cd web && npm run test:e2e -- e2e/wizard-open.spec.ts e2e/wizard-risk-alert.spec.ts`

Expected: PASS.

**Step 3: Update status + spec notes**

```md
## Combo Wizard V1

- Supported defined-risk spreads now execute as BAG combos by default.
- Popup modal verified from both Order Builder and Order Tab.
```

**Step 4: Commit**

```bash
git add docs/status.md docs/superpowers/specs/2026-04-20-leg-wizard-design.md
git commit -m "docs: record combo wizard rollout state"
```

Plan complete and saved to `docs/plans/2026-04-24-leg-wizard-implementation.md`. Two execution options:

**1. Subagent-Driven (this session)** - I dispatch fresh subagent per task, review between tasks, fast iteration

**2. Parallel Session (separate)** - Open new session with executing-plans, batch execution with checkpoints

Which approach?
