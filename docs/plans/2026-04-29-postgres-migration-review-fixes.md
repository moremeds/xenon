# Postgres Migration Review Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix the four review findings from the loose-ends review of `feat/postgres-migration-completion` and close the remaining verification gaps before production rollout.

**Architecture:** Keep Postgres as the primary order/trade source and Flex as an audit overlay. Fix the one broken operator path, make journal outbox status use the new consumer id, make Flex overlay rows order-grain by stable identifiers, and enforce the intended session window in the nightly divergence job.

**Tech Stack:** Python 3.13, SQLAlchemy Core, FastAPI, Postgres/Alembic, IBClient/ib_insync, pytest, Vitest, Playwright/browser smoke, `uv` for all Python.

---

## Context

Source review findings to address:

1. `scripts.migrations._2026_04_28_replay_unknown_orders --apply` default IB factory imports `xenon.api.ib_pool`, which is a module, not the FastAPI `server.ib_pool` instance.
2. Flex trade grouping is still by `contract_desc`, so multiple round trips in the same contract can collapse into one row with the first `perm_id`.
3. `/journal/sync` status probe checks `consumed_by` for `"journal"`, while the new listener acks `"journal_auto_import"`.
4. `xenon.jobs.flex_divergence_check` computes yesterday's ET window but compares unfiltered PG/Flex payloads.

Non-code verification gap:

- Source pill visual verification was skipped. Run a browser smoke after the code fixes.

## Task 1: Fix UNKNOWN replay default IB factory

**Files:**
- Modify: `scripts/migrations/_2026_04_28_replay_unknown_orders.py`
- Modify: `scripts/tests/test_replay_unknown_orders.py`

**Step 1: Write failing tests for the default factory**

Add tests proving the script no longer imports `xenon.api.ib_pool` and that the default factory connects a real `IBClient` with `client_id="auto"` and disconnects it after replay.

```python
def test_replay_default_factory_uses_auto_allocated_ib_client(monkeypatch):
    from types import SimpleNamespace

    import scripts.migrations._2026_04_28_replay_unknown_orders as mod

    calls = {"connect": [], "disconnect": 0}

    class FakeClient:
        def connect(self, **kwargs):
            calls["connect"].append(kwargs)

        def disconnect(self):
            calls["disconnect"] += 1

        def get_open_orders(self):
            return []

        def get_executions(self):
            return []

        def get_positions(self):
            return []

    monkeypatch.setattr(mod, "IBClient", lambda: FakeClient())
    monkeypatch.setattr(mod, "_count_unknown", lambda scope: 1)

    def fake_rehydrate(ib_factory, store, **kwargs):
        ib = ib_factory()
        assert ib.get_open_orders() == []
        return [SimpleNamespace(noop=True, to_state="UNKNOWN")]

    monkeypatch.setattr(mod, "rehydrate_on_boot", fake_rehydrate)

    summary = mod.replay_unknown(scope=_SCOPE)

    assert calls["connect"]
    assert calls["connect"][0]["client_id"] == "auto"
    assert calls["disconnect"] == 1
    assert summary["scanned"] == 1
```

Add a second assertion-level regression:

```python
def test_replay_module_no_longer_imports_api_ib_pool():
    import inspect
    import scripts.migrations._2026_04_28_replay_unknown_orders as mod

    source = inspect.getsource(mod)
    assert "from xenon.api import ib_pool" not in source
    assert "ib_pool.get" not in source
```

**Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest scripts/tests/test_replay_unknown_orders.py -q
```

Expected: new default-factory tests fail because the current implementation imports `xenon.api.ib_pool`.

**Step 3: Implement a real CLI IB factory**

In `scripts/migrations/_2026_04_28_replay_unknown_orders.py`, import `IBClient` and connection defaults:

```python
from xenon.clients.ib_client import DEFAULT_GATEWAY_PORT, DEFAULT_HOST, IBClient
```

Add helper functions near `replay_unknown`:

```python
def _connect_replay_ib_client() -> IBClient:
    client = IBClient()
    client.connect(
        host=DEFAULT_HOST,
        port=DEFAULT_GATEWAY_PORT,
        client_id="auto",
        timeout=10,
    )
    return client
```

Replace the current `if ib_client_factory is None:` block with a tracked default factory:

```python
    managed_clients: list[Any] = []
    if ib_client_factory is None:
        def ib_client_factory() -> Any:
            client = _connect_replay_ib_client()
            managed_clients.append(client)
            return client
```

Wrap the `rehydrate_on_boot(...)` call in a `try/finally` so default clients disconnect:

```python
    try:
        decisions = rehydrate_on_boot(...)
    except Exception as exc:
        ...
    finally:
        for client in managed_clients:
            try:
                client.disconnect()
            except Exception:
                logger.warning("failed to disconnect replay IB client", exc_info=True)
```

Do not disconnect injected test/live factories supplied by callers; only disconnect clients created by the default helper.

**Step 4: Run task tests**

Run:

```bash
uv run pytest scripts/tests/test_replay_unknown_orders.py scripts/tests/test_single_leg_rehydrate.py -q
```

Expected: all pass.

**Step 5: Commit**

```bash
git add scripts/migrations/_2026_04_28_replay_unknown_orders.py scripts/tests/test_replay_unknown_orders.py
git commit -m "fix(migration): make unknown replay use standalone ib client"
```

## Task 2: Align journal outbox pending probe with listener consumer id

**Files:**
- Modify: `src/xenon/db/queries/journal.py`
- Modify: `src/xenon/api/services/journal_auto_import.py`
- Modify: `src/xenon/api/tests/test_journal_sync_endpoint.py`
- Modify: `src/xenon/api/tests/test_journal_auto_import.py`

**Step 1: Write failing status-probe test**

In `src/xenon/api/tests/test_journal_sync_endpoint.py`, add an event consumed by the new listener id:

```python
def test_journal_sync_treats_auto_import_consumer_as_consumed():
    _emit_trade_closed(broker_account="DU0000000", consumed_by=["journal_auto_import"])

    response = TestClient(app).post("/journal/sync")
    body = response.json()

    assert response.status_code == 200
    assert body["pending_outbox"] == 0
```

Also adjust the existing test to include both legacy and new consumer ids:

```python
    _emit_trade_closed(broker_account="DU0000000", consumed_by=["journal"])
    _emit_trade_closed(broker_account="DU0000000", consumed_by=["journal_auto_import"])
```

Expected pending count should still only include the unconsumed row.

**Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest src/xenon/api/tests/test_journal_sync_endpoint.py -q
```

Expected: new test fails because `pending_journal_outbox_count()` checks only `["journal"]`.

**Step 3: Implement shared consumer constant and predicate**

In `src/xenon/db/queries/journal.py`, add constants near the imports:

```python
AUTO_IMPORT_CONSUMER_ID = "journal_auto_import"
LEGACY_JOURNAL_CONSUMER_ID = "journal"
```

Update `pending_journal_outbox_count()` to count rows where neither consumer has acked:

```python
            or_(
                outbox.c.consumed_by.is_(None),
                (
                    ~outbox.c.consumed_by.contains([AUTO_IMPORT_CONSUMER_ID])
                    & ~outbox.c.consumed_by.contains([LEGACY_JOURNAL_CONSUMER_ID])
                ),
            ),
```

In `src/xenon/api/services/journal_auto_import.py`, replace the local string:

```python
from xenon.db.queries.journal import AUTO_IMPORT_CONSUMER_ID, upsert_auto_import_entry

CONSUMER_ID = AUTO_IMPORT_CONSUMER_ID
```

This keeps existing tests importing `CONSUMER_ID` from the service working while removing string drift.

**Step 4: Run task tests**

Run:

```bash
uv run pytest src/xenon/api/tests/test_journal_auto_import.py src/xenon/api/tests/test_journal_sync_endpoint.py -q
```

Expected: all pass.

**Step 5: Commit**

```bash
git add src/xenon/db/queries/journal.py src/xenon/api/services/journal_auto_import.py src/xenon/api/tests/test_journal_sync_endpoint.py src/xenon/api/tests/test_journal_auto_import.py
git commit -m "fix(journal): align auto import outbox consumer id"
```

## Task 3: Make Flex blotter grouping order-grain when identifiers exist

**Files:**
- Modify: `src/xenon/trade_blotter/flex_query.py`
- Modify: `scripts/tests/test_flex_query_perm_id.py`

**Step 1: Write failing grouping test**

In `scripts/tests/test_flex_query_perm_id.py`, add a test that creates two closed round trips for the same contract with different `perm_id` values.

```python
def test_flex_grouping_keeps_same_contract_distinct_by_perm_id():
    from xenon.trade_blotter.flex_query import group_executions_to_trades

    def ex(exec_id, side, qty, price, perm_id, hour):
        return Execution(
            exec_id=exec_id,
            time=datetime(2026, 4, 27, hour, 0),
            symbol="AAPL",
            sec_type=SecurityType.STOCK,
            side=side,
            quantity=Decimal(str(qty)),
            price=Decimal(str(price)),
            commission=Decimal("0.5"),
            perm_id=perm_id,
            ib_order_id=f"ORD-{perm_id}",
        )

    trades = group_executions_to_trades([
        ex("E1", Side.BUY, 1, 100, "PERM-1", 10),
        ex("E2", Side.SELL, 1, 110, "PERM-1", 11),
        ex("E3", Side.BUY, 1, 120, "PERM-2", 12),
        ex("E4", Side.SELL, 1, 130, "PERM-2", 13),
    ])

    payload = blotter_to_dict(TradeBlotter(trades=trades, as_of=datetime(2026, 4, 27, 16, 0)))
    assert [row["perm_id"] for row in payload["closed_trades"]] == ["PERM-1", "PERM-2"]
```

Add a fallback grouping test for missing `perm_id` but present `ib_order_id`:

```python
def test_flex_grouping_uses_ib_order_id_only_as_group_key_not_perm_id():
    ...
    assert payload["closed_trades"][0]["perm_id"] is None
```

The fallback can use `ib_order_id` to avoid collapsing rows, but it must not emit `ibOrderID` as `perm_id` because `permID` is the stable cross-day join key.

**Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest scripts/tests/test_flex_query_perm_id.py -q
```

Expected: new grouping test fails because current grouping uses `contract_desc` only.

**Step 3: Implement identifier-aware grouping**

In `src/xenon/trade_blotter/flex_query.py`, update `group_executions_to_trades()`:

```python
def _execution_group_key(exec: Execution) -> tuple[str, str]:
    identifier = exec.perm_id or exec.ib_order_id or exec.contract_desc
    return (str(identifier), exec.contract_desc)
```

Then replace:

```python
        key = exec.contract_desc
```

with:

```python
        key = _execution_group_key(exec)
```

Because `contract_desc` is now part of the key tuple, set the emitted trade description from the first execution:

```python
    trades_map = defaultdict(lambda: {"executions": [], "sec_type": None, "symbol": None, "contract_desc": None})
    ...
        trades_map[key]["contract_desc"] = exec.contract_desc
    ...
                contract_desc=data["contract_desc"] or str(key),
```

Do not change `Execution.perm_id` extraction to fall back to `ibOrderID`; leave [flex_query.py] `perm_id = trade.get("permID") or None`.

**Step 4: Run task tests**

Run:

```bash
uv run pytest scripts/tests/test_flex_query_perm_id.py scripts/tests/test_blotter_merge.py -q
```

Expected: all pass.

**Step 5: Commit**

```bash
git add src/xenon/trade_blotter/flex_query.py scripts/tests/test_flex_query_perm_id.py
git commit -m "fix(blotter): keep flex rows distinct by order identifier"
```

## Task 4: Apply ET session window in divergence job

**Files:**
- Modify: `src/xenon/jobs/flex_divergence_check.py`
- Modify: `scripts/tests/test_flex_divergence_check.py`
- Modify if needed: `docs/runbooks/ops.md`

**Step 1: Write failing filter tests**

In `scripts/tests/test_flex_divergence_check.py`, import the new helper:

```python
from datetime import datetime, timezone
from xenon.jobs.flex_divergence_check import filter_payload_by_execution_window
```

Add:

```python
def test_filter_payload_by_execution_window_keeps_only_rows_with_in_window_execution():
    start = datetime(2026, 4, 28, 4, 0, tzinfo=timezone.utc)
    end = datetime(2026, 4, 29, 4, 0, tzinfo=timezone.utc)
    payload = {
        "closed_trades": [
            {"perm_id": "in", "executions": [{"time": "2026-04-28T15:00:00+00:00"}]},
            {"perm_id": "out", "executions": [{"time": "2026-04-29T05:00:00+00:00"}]},
            {"perm_id": "bad", "executions": [{"time": ""}]},
        ],
        "open_trades": [{"perm_id": "open", "executions": [{"time": "2026-04-28T15:00:00+00:00"}]}],
    }

    filtered = filter_payload_by_execution_window(payload, start, end)

    assert [row["perm_id"] for row in filtered["closed_trades"]] == ["in"]
    assert filtered["open_trades"] == []
```

Update `test_main_records_a_run_when_both_sides_present` so the fake PG and Flex rows include execution times. Add an out-of-window divergent row and assert it is not counted.

**Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest scripts/tests/test_flex_divergence_check.py -q
```

Expected: new helper import fails.

**Step 3: Implement filtering helpers**

In `src/xenon/jobs/flex_divergence_check.py`, add helpers above `_main()`:

```python
def _parse_execution_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _row_has_execution_in_window(row: dict[str, Any], start_utc: datetime, end_utc: datetime) -> bool:
    for execution in row.get("executions") or []:
        if not isinstance(execution, dict):
            continue
        executed_at = _parse_execution_time(execution.get("time"))
        if executed_at is not None and start_utc <= executed_at.astimezone(timezone.utc) < end_utc:
            return True
    return False


def filter_payload_by_execution_window(
    payload: dict[str, Any],
    start_utc: datetime,
    end_utc: datetime,
) -> dict[str, Any]:
    return {
        **payload,
        "closed_trades": [
            row
            for row in payload.get("closed_trades", [])
            if _row_has_execution_in_window(row, start_utc, end_utc)
        ],
        "open_trades": [],
    }
```

In `_main()`, keep `days` as a broad fetch bound, but filter both sides before comparing:

```python
    start_utc, end_utc = yesterday_session_window()
    ...
    pg_payload = filter_payload_by_execution_window(pg_payload, start_utc, end_utc)
    ...
    flex_payload = filter_payload_by_execution_window(flex_result.data or {}, start_utc, end_utc)
    summary = compute_divergence(pg_payload, flex_payload)
```

This makes the docs claim true: yesterday's ET session window is the compared data, not just an approximate fetch size.

**Step 4: Run task tests**

Run:

```bash
uv run pytest scripts/tests/test_flex_divergence_check.py src/xenon/api/tests/test_health_observability.py -q
```

Expected: all pass.

**Step 5: Commit**

```bash
git add src/xenon/jobs/flex_divergence_check.py scripts/tests/test_flex_divergence_check.py docs/runbooks/ops.md
git commit -m "fix(observability): filter flex divergence by session window"
```

## Task 5: Verification and UI smoke

**Files:**
- No required code files.
- Attach screenshot evidence to PR if possible.

**Step 1: Run focused Python suite**

Run:

```bash
uv run pytest src/xenon/db/tests src/xenon/api/tests \
              scripts/tests/test_blotter_merge.py \
              scripts/tests/test_blotter_pg_perm_id.py \
              scripts/tests/test_flex_query_perm_id.py \
              scripts/tests/test_replay_unknown_orders.py \
              scripts/tests/test_flex_divergence_check.py \
              scripts/tests/test_blotter_query.py \
              scripts/tests/test_blotter_unconfigured.py -q
```

Expected: all pass.

**Step 2: Run web tests**

Run:

```bash
npx vitest run web/tests/blotter-route-pg.test.ts \
                web/tests/historical-trades-source-pill.test.tsx
```

Expected: all pass.

**Step 3: Run order-path guards**

Run:

```bash
uv run python scripts/checks/no_json_fallback_on_order_path.py
uv run python scripts/checks/order_path_caller_allowlist.py
```

Expected:

```text
OK - no new JSON fallbacks on order/migrated routes.
OK - no unauthorized callers of ib_place_order.
```

**Step 4: Run Alembic checks**

Run:

```bash
uv run alembic upgrade head
uv run alembic check
```

Expected: no new upgrade operations detected.

**Step 5: Run dry-runs with a concrete paper account**

Do not rely on an unset shell variable. First verify:

```bash
test -n "$IB_PAPER_ACCOUNT" && echo "$IB_PAPER_ACCOUNT"
```

Then run:

```bash
XENON_TRADING_MODE=paper XENON_BROKER_ACCOUNT=$IB_PAPER_ACCOUNT \
  uv run python -m scripts.migrations._2026_04_28_replay_unknown_orders

XENON_TRADING_MODE=paper XENON_BROKER_ACCOUNT=$IB_PAPER_ACCOUNT \
  uv run python -m xenon.jobs.flex_divergence_check
```

Expected:

- UNKNOWN replay dry-run prints `dry_run: true` and `unknown_count`.
- Divergence dry-run either prints `dry_run: true` with counts or `skipped: true` when Flex is unavailable.

**Step 6: Browser smoke for source pill**

Run the dev server from `web/`:

```bash
cd web && npm run dev
```

Open the dashboard, scroll to `Historical Trades (30 Days)`, click refresh if needed, and verify:

- Source pill renders as `PG`, `FLEX`, or `PG+FLEX` when `data.source` is present.
- Empty state does not show a source pill when `source="none"`.
- Pill uses existing `.pill neutral` styling and no raw hex.
- Header content does not overlap at desktop width.

Capture a screenshot for PR evidence.

**Step 7: Commit verification-only doc updates if any**

If only the PR body changes, no commit is needed. If runbook text changed:

```bash
git add docs/runbooks/ops.md
git commit -m "docs(runbook): clarify postgres migration verification"
```

## Final PR Addendum

Add this to the PR after implementation:

```markdown
Review fixes applied after loose-ends review:

- Fixed UNKNOWN replay `--apply` to use a standalone auto-allocated IB client instead of importing the FastAPI pool module.
- Aligned `/journal/sync` pending probe with the `journal_auto_import` outbox consumer id while preserving legacy `journal` acks.
- Made Flex blotter grouping identifier-aware so same-contract round trips with different `permID`s do not collapse before PG/Flex overlay.
- Applied the ET session window to both PG and Flex payloads in the nightly divergence job before comparison.
- Re-ran focused Python, Vitest, order-path guards, Alembic checks, paper dry-runs, and source-pill browser smoke.
```

## Execution Order

1. Task 1 - fixes the only broken deploy/operator path.
2. Task 2 - low-risk observability correctness.
3. Task 3 - fixes overlay correctness before Task 4 relies on it.
4. Task 4 - fixes nightly divergence precision.
5. Task 5 - proves the branch is ready for review/update.

Each task should land as its own commit on `feat/postgres-migration-completion`.
