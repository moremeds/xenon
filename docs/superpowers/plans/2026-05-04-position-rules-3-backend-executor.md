# Position Rules — Plan 3: Backend Executor (Handler + Subprocess + Migration B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the actual engine. `PositionRulesHandler` ticks every 30s, evaluates `ARMED` rows, performs the close-claim protocol, and fires MKT-flatten via subprocess to `xenon-ib-place-order`. Native STP arming flows through the same subprocess. Combo-wizard rows are migrated onto `position_protection`; `wizard_stop_monitor.py` is deleted; `wizard_protection` is dropped. Feature flag-gated; ships off until Plan 4 acceptance.

**Architecture:** Three integration layers. **Handler layer** (`monitor_daemon/handlers/position_rules.py`) implements the per-tick loop in spec §8 — liveness check → mark read → evaluate → close-claim. **Executor layer** (`brackets/executor/`) abstracts native arming + flatten + cancel as a thin shim over `xenon-ib-place-order` and `xenon-ib-order-manage`. **Migration B** rebases combo-wizard rows from `wizard_protection` (already empty by user confirmation) onto `position_protection` with `rule_kind='combo_tp_alert'`, then deletes the legacy table and `wizard_stop_monitor.py`. The frozen-config CI guard locks the rule modules from importing `bracket_policies`.

**Tech Stack:** Python 3.13, ib_async (existing dep), SQLAlchemy + Alembic, Postgres advisory locks, asyncpg LISTEN/NOTIFY, pytest, `uv`.

**Spec reference:** `docs/superpowers/specs/2026-05-04-position-rules-design.md` §4.4 (broker symmetry), §6 (post-fill arming hook — runtime wiring), §8 (handler loop), §9 (rule plug-in interface, native paths), §10 (failure modes, all subsections), §14 (migration B), §15 Phases 3 + 4 + 5.

**Prerequisites:**

- Plan 1 merged (DST fix in `MonitorDaemon`).
- Plan 2 merged (schema + queries + arm consumer).

---

## File Structure

### Created

| Path                                                                        | Responsibility                                                                           |
| --------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | -------- | ----------- | ----------------------- |
| `src/xenon/monitor_daemon/handlers/position_rules.py`                       | `PositionRulesHandler(BaseHandler)` — per-tick loop; ~250 LOC                            |
| `src/xenon/monitor_daemon/handlers/arm_consumer.py`                         | LISTEN/NOTIFY harness wrapping `arm_hook.on_fill_event`; DLQ retry counter               |
| `src/xenon/execution/brackets/executor/__init__.py`                         | Package marker                                                                           |
| `src/xenon/execution/brackets/executor/ib_executor.py`                      | `IBExecutor.attach_native_stp(...)`, `flatten_mkt(...)`, `cancel(...)` — subprocess shim |
| `src/xenon/execution/brackets/executor/marks.py`                            | Per-tick mark cache + spot cache helpers                                                 |
| `src/xenon/execution/brackets/executor/native_liveness.py`                  | `verify_native_order_live(perm_id) -> ('Live'                                            | 'Filled' | 'Cancelled' | 'Inactive')` via IB API |
| `src/xenon/execution/brackets/executor/reconcile.py`                        | Boot reconcile + reconnect-triggered reconcile (§10.4 steps 1–4)                         |
| `scripts/checks/frozen_config_at_arm.py`                                    | AST-based CI guard — rule modules must not import `bracket_policies`                     |
| `scripts/tests/test_position_rules/test_handler_loop.py`                    | Unit tests for handler loop (mocked executor)                                            |
| `scripts/tests/test_position_rules/test_state_machine.py`                   | All legal transitions × CAS rejection                                                    |
| `scripts/tests/test_position_rules_subprocess/__init__.py`                  | Package marker                                                                           |
| `scripts/tests/test_position_rules_subprocess/test_ib_executor_contract.py` | JSON in/out contract for `xenon-ib-place-order` calls                                    |
| `scripts/tests/test_position_rules_db/test_handler_integration.py`          | End-to-end tick over a real Postgres + mocked IBExecutor                                 |
| `scripts/tests/test_position_rules_db/test_reconcile.py`                    | Boot reconcile snapping in-flight claims to terminal states                              |
| `scripts/tests/test_checks/test_frozen_config_at_arm.py`                    | CI guard's own unit tests                                                                |

### Modified

| Path                                                               | Change                                                                                                               |
| ------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------- |
| `src/xenon/monitor_daemon/run.py`                                  | Register `PositionRulesHandler` + `arm_consumer`; remove `WizardStopMonitorHandler`                                  |
| `src/xenon/monitor_daemon/handlers/__init__.py`                    | Export `PositionRulesHandler`; drop `WizardStopMonitorHandler` re-export                                             |
| `src/xenon/db/queries/combo_wizard.py`                             | INSERT/UPDATE/SELECT now hit `position_protection` filtered by `rule_kind='combo_tp_alert'`                          |
| `src/xenon/execution/combo_wizard/protect.py`                      | Writes `rule_kind='combo_tp_alert'` rows with `auto_place=FALSE`                                                     |
| `src/xenon/db/schema.py`                                           | **Drop** `wizard_protection` Table definition                                                                        |
| `src/xenon/db/migrations/versions/<rev>_drop_wizard_protection.py` | Migration B: ASSERT empty + DROP TABLE                                                                               |
| `scripts/migrations/migrate_to_postgres.py`                        | Remove `wizard_protection` block                                                                                     |
| `src/xenon/api/server.py`                                          | Add feature flag `XENON_POSITION_RULES_ENABLED` gate around handler registration                                     |
| `pyproject.toml`                                                   | Add `xenon-ib-place-order` extension flag (already present); add `xenon-position-rules-daemon` test entry if desired |

### Deleted

| Path                                                                    | Reason                                                                                               |
| ----------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `src/xenon/monitor_daemon/handlers/wizard_stop_monitor.py`              | Logic absorbed into `rules/combo_tp_alert.py` (Plan 2). Handler unified into `PositionRulesHandler`. |
| `src/xenon/db/tests/test_combo_wizard.py` `wizard_protection` test rows | Migrated onto `position_protection` shape                                                            |

---

## Task 1: Mark + spot cache helpers

**Files:**

- Create: `src/xenon/execution/brackets/executor/__init__.py` (empty)
- Create: `src/xenon/execution/brackets/executor/marks.py`
- Create: `scripts/tests/test_position_rules/test_marks.py`

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_position_rules/test_marks.py
"""Per-tick mark/spot cache. Spec §8 'Mark / spot coalescing'."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from xenon.execution.brackets.executor.marks import (
    MarkCache,
    Quote,
    SpotCache,
    is_quote_fresh,
)


def test_quote_fresh_within_window():
    now = datetime(2026, 5, 4, 14, 30, tzinfo=timezone.utc)
    q = Quote(symbol="AAPL", price=190.10, ts=now - timedelta(seconds=5))
    assert is_quote_fresh(q, now=now, max_age_s=60) is True


def test_quote_stale_after_window():
    now = datetime(2026, 5, 4, 14, 30, tzinfo=timezone.utc)
    q = Quote(symbol="AAPL", price=190.10, ts=now - timedelta(seconds=120))
    assert is_quote_fresh(q, now=now, max_age_s=60) is False


def test_mark_cache_coalesces_within_tick():
    """Two reads of the same con_id within one tick → underlying source called once."""
    calls = {"n": 0}

    def fetch(con_id):
        calls["n"] += 1
        return Quote(symbol="AAPL", price=190.10, ts=datetime.now(timezone.utc))

    cache = MarkCache(fetcher=fetch)
    cache.get(con_id=12345)
    cache.get(con_id=12345)
    assert calls["n"] == 1


def test_spot_cache_coalesces_per_symbol():
    calls = {"n": 0}

    def fetch(symbol):
        calls["n"] += 1
        return Quote(symbol=symbol, price=580.0, ts=datetime.now(timezone.utc))

    cache = SpotCache(fetcher=fetch)
    cache.get(symbol="SPY")
    cache.get(symbol="SPY")
    cache.get(symbol="QQQ")
    assert calls["n"] == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest scripts/tests/test_position_rules/test_marks.py -xvs`

- [ ] **Step 3: Implement `marks.py`**

```python
# src/xenon/execution/brackets/executor/marks.py
"""Per-tick mark/spot cache. Spec §8 'Mark / spot coalescing'.

Lives a single tick. Caller constructs a fresh MarkCache + SpotCache at the
start of `execute()` and discards them at the end. Coalesces multiple lookups
of the same contract / underlying to one IB API call per tick.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    ts: datetime


def is_quote_fresh(q: Quote, *, now: datetime | None = None, max_age_s: int = 60) -> bool:
    if now is None:
        now = datetime.now(timezone.utc)
    return (now - q.ts) <= timedelta(seconds=max_age_s)


class MarkCache:
    def __init__(self, fetcher: Callable[[int], Quote | None]):
        self._fetcher = fetcher
        self._cache: dict[int, Quote | None] = {}

    def get(self, *, con_id: int) -> Quote | None:
        if con_id not in self._cache:
            self._cache[con_id] = self._fetcher(con_id)
        return self._cache[con_id]


class SpotCache:
    def __init__(self, fetcher: Callable[[str], Quote | None]):
        self._fetcher = fetcher
        self._cache: dict[str, Quote | None] = {}

    def get(self, *, symbol: str) -> Quote | None:
        if symbol not in self._cache:
            self._cache[symbol] = self._fetcher(symbol)
        return self._cache[symbol]
```

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest scripts/tests/test_position_rules/test_marks.py -xvs`
Expected: 4 green.

```bash
git add src/xenon/execution/brackets/executor/__init__.py src/xenon/execution/brackets/executor/marks.py scripts/tests/test_position_rules/test_marks.py
git commit -m "feat(brackets): add per-tick mark/spot cache helpers"
```

---

## Task 2: IB executor — subprocess shim

**Files:**

- Create: `src/xenon/execution/brackets/executor/ib_executor.py`
- Create: `scripts/tests/test_position_rules_subprocess/__init__.py` (empty)
- Create: `scripts/tests/test_position_rules_subprocess/test_ib_executor_contract.py`

The executor wraps `xenon-ib-place-order` and `xenon-ib-order-manage` subprocess calls. It does **not** import `ib_async` directly — that boundary keeps Futu symmetry honest (spec §4.4) and the rule modules pure (spec §13.8 frozen-config guard).

- [ ] **Step 1: Write the contract test**

```python
# scripts/tests/test_position_rules_subprocess/test_ib_executor_contract.py
"""Subprocess JSON contract for IBExecutor. Spec §13.4.

Asserts the JSON payload we feed `xenon-ib-place-order` matches the CLI's
accepted schema, and that stdout-parsing maps subprocess output to perm_id.
Memory `[live E2E surfaces contract bugs]`: this is the FIRST defense; the
paper-smoke checklist in Plan 4 is the second.
"""
from __future__ import annotations

import json
import os
import subprocess
from unittest.mock import patch

import pytest

from xenon.execution.account_scope import AccountScope
from xenon.execution.brackets.executor.ib_executor import IBExecutor


@pytest.fixture
def scope():
    return AccountScope(broker="IB", account_env="paper", broker_account="DU1234567")


def test_flatten_mkt_subprocess_payload_shape(scope):
    """Flatten payload must include orderRef, action=opposite of opening, MKT, RTH-only."""
    fake_completed = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=json.dumps({"perm_id": 12345, "ib_order_id": 9999, "status": "Submitted"}),
        stderr="",
    )
    with patch("subprocess.run", return_value=fake_completed) as run:
        executor = IBExecutor()
        result = executor.flatten_mkt(
            scope=scope,
            con_id=12345,
            symbol="AAPL",
            sec_type="STK",
            close_action="SELL",
            qty=100,
            order_ref="xenon-pr-42",
        )
        cmd = run.call_args.args[0]
        assert "xenon-ib-place-order" in cmd[0] or cmd[0].endswith("xenon-ib-place-order")
        # The CLI receives JSON via stdin or --json arg
        passed_json = run.call_args.kwargs.get("input") or "".join(a for a in cmd if a.startswith("{"))
        payload = json.loads(passed_json)
        assert payload["orderRef"] == "xenon-pr-42"
        assert payload["orderType"] == "MKT"
        assert payload["outsideRth"] is False
        assert payload["tif"] == "DAY"
        assert payload["action"] == "SELL"
        assert payload["symbol"] == "AAPL"
        assert payload["qty"] == 100
    assert result.perm_id == 12345


def test_flatten_mkt_subprocess_error_surfaces(scope):
    fake_completed = subprocess.CompletedProcess(
        args=[], returncode=2,
        stdout="",
        stderr=json.dumps({"reason_code": "IB_API_ERROR", "message": "Pacing violation"}),
    )
    with patch("subprocess.run", return_value=fake_completed):
        executor = IBExecutor()
        with pytest.raises(RuntimeError) as exc:
            executor.flatten_mkt(
                scope=scope, con_id=1, symbol="X", sec_type="STK",
                close_action="SELL", qty=1, order_ref="xenon-pr-1",
            )
        assert "Pacing violation" in str(exc.value)


def test_attach_native_stp_payload_shape(scope):
    fake_completed = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=json.dumps({"perm_id": 555, "ib_order_id": 6, "status": "Submitted"}),
        stderr="",
    )
    with patch("subprocess.run", return_value=fake_completed) as run:
        executor = IBExecutor()
        result = executor.attach_native_stp(
            scope=scope,
            con_id=12345,
            symbol="AAPL",
            sec_type="STK",
            close_action="SELL",
            qty=100,
            stop_price=87.40,
            tif="GTC",
        )
        passed_json = run.call_args.kwargs.get("input") or "".join(a for a in run.call_args.args[0] if a.startswith("{"))
        payload = json.loads(passed_json)
        assert payload["orderType"] == "STP"
        assert payload["stopPrice"] == 87.40
        assert payload["tif"] == "GTC"
        assert payload["outsideRth"] is False
    assert result.perm_id == 555


def test_cancel_subprocess_uses_order_manage(scope):
    fake_completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps({"status": "Cancelled"}), stderr="",
    )
    with patch("subprocess.run", return_value=fake_completed) as run:
        executor = IBExecutor()
        executor.cancel(scope=scope, perm_id=12345)
        cmd = run.call_args.args[0]
        assert "xenon-ib-order-manage" in cmd[0] or cmd[0].endswith("xenon-ib-order-manage")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest scripts/tests/test_position_rules_subprocess/test_ib_executor_contract.py -xvs`

- [ ] **Step 3: Inspect existing CLI to confirm flag shape**

Read `src/xenon/execution/ib_place_order.py` and `src/xenon/execution/ib_order_manage.py` headers/argparse blocks to learn the exact CLI flag names and JSON shape they accept. The implementation below assumes `--json '<payload>'` for `xenon-ib-place-order` and `--perm-id <N>` for `xenon-ib-order-manage`. If your local CLI uses a different convention, mirror it in the executor.

- [ ] **Step 4: Implement `ib_executor.py`**

```python
# src/xenon/execution/brackets/executor/ib_executor.py
"""IB executor subprocess shim. Spec §4.3 EXECUTOR LAYER, §10.2.

Wraps `xenon-ib-place-order` and `xenon-ib-order-manage`. The handler invokes
this shim; rule modules never import it (frozen-config guard).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Any

from xenon.execution.account_scope import AccountScope

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlaceResult:
    perm_id: int | None
    ib_order_id: int | None
    status: str
    raw: dict[str, Any]


def _scope_env(scope: AccountScope) -> dict[str, str]:
    env = os.environ.copy()
    env["XENON_TRADING_MODE"] = scope.account_env
    env["XENON_BROKER_ACCOUNT"] = scope.broker_account
    env["XENON_BROKER"] = scope.broker
    return env


def _run_place_order(payload: dict[str, Any], scope: AccountScope) -> PlaceResult:
    cmd = ["xenon-ib-place-order", "--json", json.dumps(payload)]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=_scope_env(scope),
        timeout=30,
    )
    if result.returncode != 0:
        try:
            err = json.loads(result.stderr)
        except json.JSONDecodeError:
            err = {"reason_code": "subprocess_error", "message": result.stderr.strip()}
        raise RuntimeError(f"xenon-ib-place-order failed: {err.get('message') or err}")
    try:
        out = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"xenon-ib-place-order produced unparseable stdout: {result.stdout[:200]}") from e
    return PlaceResult(
        perm_id=out.get("perm_id"),
        ib_order_id=out.get("ib_order_id"),
        status=out.get("status", "Unknown"),
        raw=out,
    )


class IBExecutor:
    def attach_native_stp(
        self,
        *,
        scope: AccountScope,
        con_id: int,
        symbol: str,
        sec_type: str,
        close_action: str,        # 'SELL' for long → close; 'BUY' for short → close
        qty: int,
        stop_price: float,
        tif: str = "GTC",
    ) -> PlaceResult:
        payload = {
            "conId": con_id,
            "symbol": symbol,
            "secType": sec_type,
            "action": close_action,
            "qty": qty,
            "orderType": "STP",
            "stopPrice": stop_price,
            "tif": tif,
            "outsideRth": False,
        }
        return _run_place_order(payload, scope)

    def flatten_mkt(
        self,
        *,
        scope: AccountScope,
        con_id: int,
        symbol: str,
        sec_type: str,
        close_action: str,
        qty: int,
        order_ref: str,
    ) -> PlaceResult:
        payload = {
            "conId": con_id,
            "symbol": symbol,
            "secType": sec_type,
            "action": close_action,
            "qty": qty,
            "orderType": "MKT",
            "tif": "DAY",
            "outsideRth": False,
            "orderRef": order_ref,
        }
        return _run_place_order(payload, scope)

    def cancel(self, *, scope: AccountScope, perm_id: int) -> dict[str, Any]:
        cmd = ["xenon-ib-order-manage", "--cancel", "--perm-id", str(perm_id)]
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            env=_scope_env(scope), timeout=30,
        )
        if result.returncode != 0:
            try:
                err = json.loads(result.stderr)
            except json.JSONDecodeError:
                err = {"message": result.stderr.strip()}
            raise RuntimeError(f"xenon-ib-order-manage cancel failed: {err.get('message') or err}")
        return json.loads(result.stdout)
```

(Adjust `--json` ↔ stdin or `--perm-id` ↔ `--cancel-perm-id` based on what the existing CLIs actually accept; the test patches `subprocess.run` so the integration is verified by the contract test, not by re-running the CLI.)

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest scripts/tests/test_position_rules_subprocess/test_ib_executor_contract.py -xvs`
Expected: 4 green.

```bash
git add src/xenon/execution/brackets/executor/ib_executor.py scripts/tests/test_position_rules_subprocess/__init__.py scripts/tests/test_position_rules_subprocess/test_ib_executor_contract.py
git commit -m "feat(brackets): add IBExecutor subprocess shim (place + flatten + cancel)"
```

---

## Task 3: Native-liveness probe

**Files:**

- Create: `src/xenon/execution/brackets/executor/native_liveness.py`
- Create: `scripts/tests/test_position_rules_subprocess/test_native_liveness.py`

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_position_rules_subprocess/test_native_liveness.py
"""Native-order liveness probe. Spec §8, §10.3.

Tests the function as a pure shim over IBClient — IBClient itself is mocked.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from xenon.execution.brackets.executor.native_liveness import (
    NativeOrderState,
    verify_native_order_live,
)


def test_returns_filled_when_ib_reports_filled():
    ib = MagicMock()
    ib.get_order_state.return_value = {"status": "Filled", "permId": 12345}
    state = verify_native_order_live(ib_client=ib, perm_id=12345)
    assert state == NativeOrderState.FILLED


def test_returns_cancelled_when_ib_reports_cancelled():
    ib = MagicMock()
    ib.get_order_state.return_value = {"status": "Cancelled", "permId": 12345}
    state = verify_native_order_live(ib_client=ib, perm_id=12345)
    assert state == NativeOrderState.CANCELLED


def test_returns_unknown_on_disconnect():
    ib = MagicMock()
    ib.get_order_state.side_effect = ConnectionError("disconnected")
    state = verify_native_order_live(ib_client=ib, perm_id=12345)
    assert state == NativeOrderState.UNKNOWN
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest scripts/tests/test_position_rules_subprocess/test_native_liveness.py -xvs`

- [ ] **Step 3: Implement `native_liveness.py`**

```python
# src/xenon/execution/brackets/executor/native_liveness.py
"""Per-tick native-order liveness probe. Spec §8, §10.3.

Returns the broker's view of the order so the handler can detect manual
TWS cancels or Filled native brackets that haven't been reconciled yet.
"""
from __future__ import annotations

from enum import StrEnum


class NativeOrderState(StrEnum):
    LIVE = "LIVE"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    INACTIVE = "INACTIVE"
    UNKNOWN = "UNKNOWN"  # connection error, not enough info


_IB_TO_NATIVE = {
    "Submitted": NativeOrderState.LIVE,
    "PreSubmitted": NativeOrderState.LIVE,
    "Working": NativeOrderState.LIVE,
    "Filled": NativeOrderState.FILLED,
    "Cancelled": NativeOrderState.CANCELLED,
    "ApiCancelled": NativeOrderState.CANCELLED,
    "Inactive": NativeOrderState.INACTIVE,
}


def verify_native_order_live(*, ib_client, perm_id: int) -> NativeOrderState:
    try:
        state = ib_client.get_order_state(perm_id=perm_id)
    except Exception:  # noqa: BLE001 — coarse: any IB exception means we cannot conclude
        return NativeOrderState.UNKNOWN
    if state is None:
        return NativeOrderState.UNKNOWN
    raw = state.get("status")
    return _IB_TO_NATIVE.get(raw, NativeOrderState.UNKNOWN)
```

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest scripts/tests/test_position_rules_subprocess/test_native_liveness.py -xvs`
Expected: 3 green.

```bash
git add src/xenon/execution/brackets/executor/native_liveness.py scripts/tests/test_position_rules_subprocess/test_native_liveness.py
git commit -m "feat(brackets): add native-order liveness probe"
```

---

## Task 4: Wire native-arm path through StopLossRule + TrailingTpRule

**Files:**

- Modify: `src/xenon/execution/brackets/rules/stop_loss.py`
- Modify: `src/xenon/execution/brackets/rules/trailing_tp.py`

Plan 2's rule modules return `RETRY(reason="needs_subprocess_executor")` on `arm()` for stocks/long_options. Now wire `IBExecutor` so the same `arm()` returns `NATIVE_ARMED(perm_id=...)`. The executor is **passed in** at handler-call time — rule modules still don't import `bracket_policies` (the frozen-config guard locks that).

- [ ] **Step 1: Add executor parameter to `arm()`**

The `RuleEvaluator` Protocol stays signature-compatible by accepting the executor via a kwarg-only "context" dict. We extend the Protocol once and update all four rules.

Edit `src/xenon/execution/brackets/rules/base.py`:

```python
class RuleEvaluator(Protocol):
    rule_kind: ClassVar[str]

    def arm(self, scope, position, config, state_data, *, executor=None) -> ArmResult: ...

    def evaluate(self, scope, position, config, state_data, marks) -> Decision: ...

    def disarm(self, scope, position, native_perm_id, *, executor=None) -> None: ...
```

- [ ] **Step 2: Update `StopLossRule.arm()` for native attach**

Edit `src/xenon/execution/brackets/rules/stop_loss.py` `arm()`:

```python
def arm(self, *, scope, position, config, state_data, executor=None) -> ArmResult:
    ac = position.get("asset_class")
    if ac in ("debit_combo", "credit_spread"):
        return ArmResult(kind="SYNTHETIC_ONLY", reason="bag_combo_no_native_bracket")
    if executor is None:
        return ArmResult(kind="RETRY", reason="needs_subprocess_executor")
    anchor = position["anchor_price"]
    threshold_pct = config["threshold_pct"]
    stop_price = round(anchor * (1 + threshold_pct), 2)
    leg = position["legs"][0]
    close_action = "SELL" if leg["action"] == "BUY" else "BUY"
    try:
        result = executor.attach_native_stp(
            scope=scope,
            con_id=leg["con_id"],
            symbol=leg["symbol"],
            sec_type=leg["sec_type"],
            close_action=close_action,
            qty=position["protected_qty"] * (position.get("multiplier", 1) if leg["sec_type"] == "STK" else 1),
            stop_price=stop_price,
            tif="GTC",
        )
        return ArmResult(kind="NATIVE_ARMED", perm_id=result.perm_id,
                         state_data_patch={"native_stop_price": stop_price})
    except Exception as exc:  # noqa: BLE001
        return ArmResult(kind="RETRY", reason=str(exc))
```

(Symmetric edit for `trailing_tp.py` — but since IB's TRAIL bracket has known bugs per legacy IB client issue #216 referenced in spec §19, the v1 `trailing_tp` arm path returns `SYNTHETIC_ONLY` for everything. We track the trail in `state_data.mfe` and fire MKT-flatten at trigger time.)

- [ ] **Step 3: Update `TrailingTpRule.arm()` to remain synthetic**

Edit `src/xenon/execution/brackets/rules/trailing_tp.py` `arm()`:

```python
def arm(self, *, scope, position, config, state_data, executor=None) -> ArmResult:
    return ArmResult(kind="SYNTHETIC_ONLY", reason="trail_handled_by_synthetic_monitor")
```

- [ ] **Step 4: Update existing rule tests + add native-arm test**

Append to `scripts/tests/test_position_rules/test_rules.py`:

```python
from unittest.mock import MagicMock

from xenon.execution.brackets.executor.ib_executor import PlaceResult


def test_stop_loss_arm_native_path_calls_executor():
    rule = StopLossRule()
    executor = MagicMock()
    executor.attach_native_stp.return_value = PlaceResult(perm_id=999, ib_order_id=1, status="Submitted", raw={})
    pos = {
        "asset_class": "long_option",
        "anchor_price": 5.00,
        "protected_qty": 1,
        "multiplier": 100,
        "legs": [{
            "sec_type": "OPT", "symbol": "GOOG", "expiry": "20260417",
            "strike": 315.0, "right": "C", "action": "BUY",
            "con_id": 12345, "fill_price": 5.00,
        }],
    }
    result = rule.arm(scope=None, position=pos, config={"threshold_pct": -0.20, "anchor": "entry_price"}, state_data={}, executor=executor)
    assert result.kind == "NATIVE_ARMED"
    assert result.perm_id == 999
    executor.attach_native_stp.assert_called_once()
    args = executor.attach_native_stp.call_args.kwargs
    assert args["stop_price"] == 4.00  # 5.00 * (1 - 0.20)
    assert args["close_action"] == "SELL"
```

- [ ] **Step 5: Run all rule tests + commit**

Run: `uv run pytest scripts/tests/test_position_rules/test_rules.py -xvs`
Expected: all green (existing 9 + 1 new native-arm test).

```bash
git add src/xenon/execution/brackets/rules/base.py src/xenon/execution/brackets/rules/stop_loss.py src/xenon/execution/brackets/rules/trailing_tp.py scripts/tests/test_position_rules/test_rules.py
git commit -m "feat(brackets): wire native STP arm path via IBExecutor"
```

---

## Task 5: PositionRulesHandler — handler loop

**Files:**

- Create: `src/xenon/monitor_daemon/handlers/position_rules.py`
- Create: `scripts/tests/test_position_rules/test_handler_loop.py`

- [ ] **Step 1: Write the failing test (mocked executor + Postgres)**

```python
# scripts/tests/test_position_rules/test_handler_loop.py
"""PositionRulesHandler loop semantics. Spec §8.

Mocks IBClient + IBExecutor; uses real Postgres via fixtures so CAS + outbox
emission are exercised end-to-end without touching IB.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.db.queries.position_protection import insert_pending_arm
from xenon.execution.account_scope import AccountScope
from xenon.execution.brackets.executor.ib_executor import PlaceResult
from xenon.execution.brackets.executor.marks import Quote
from xenon.execution.brackets.executor.native_liveness import NativeOrderState
from xenon.monitor_daemon.handlers.position_rules import PositionRulesHandler


@pytest.fixture
def engine():
    e = get_sync_engine()
    with e.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key LIKE 'TEST::%'"))
        conn.execute(text("DELETE FROM xenon.position_close_claims WHERE position_key LIKE 'TEST::%'"))
    yield e
    with e.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key LIKE 'TEST::%'"))
        conn.execute(text("DELETE FROM xenon.position_close_claims WHERE position_key LIKE 'TEST::%'"))


def _stock_descriptor(symbol="AAPL", price=100.0, con_id=12345):
    return {
        "asset_class": "stock", "opened_at": "2026-05-04T14:00:00Z",
        "source": "fastapi_orders_place", "anchor_price": price, "anchor_currency": "USD",
        "opened_qty": 100, "protected_qty": 100, "multiplier": 1, "qty_unit": "share",
        "legs": [{"sec_type": "STK", "symbol": symbol, "action": "BUY", "ratio": 1, "fill_price": price, "con_id": con_id}],
    }


def test_pending_arm_transitions_to_armed(engine):
    descriptor = _stock_descriptor()
    pid = insert_pending_arm(
        engine, broker="IB", account_env="paper", broker_account="DU1234567",
        position_key="TEST::AAPL_HANDLER", position_descriptor=descriptor,
        asset_class="stock", rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    executor = MagicMock()
    executor.attach_native_stp.return_value = PlaceResult(perm_id=999, ib_order_id=1, status="Submitted", raw={})
    ib_client = MagicMock()
    ib_client.get_order_state.return_value = {"status": "Submitted", "permId": 999}
    ib_client.connected = True
    ib_client.get_quote.return_value = Quote(symbol="AAPL", price=100.0, ts=datetime.now(timezone.utc))

    handler = PositionRulesHandler(engine=engine, executor=executor, ib_client=ib_client,
                                   scope=AccountScope(broker="IB", account_env="paper", broker_account="DU1234567"))
    handler.execute()

    with engine.connect() as conn:
        row = conn.execute(text("SELECT state, native_order_perm_id FROM xenon.position_protection WHERE protection_id = :pid"), {"pid": pid}).first()
    assert row.state == "ARMED"
    assert row.native_order_perm_id == 999


def test_armed_below_threshold_triggers_and_claims(engine):
    descriptor = _stock_descriptor(symbol="MSFT", price=100.0, con_id=22222)
    pid = insert_pending_arm(
        engine, broker="IB", account_env="paper", broker_account="DU1234567",
        position_key="TEST::MSFT_HANDLER", position_descriptor=descriptor,
        asset_class="stock", rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    # Skip arm: directly transition to ARMED (no native order; pure synthetic for test).
    with engine.begin() as conn:
        conn.execute(text("UPDATE xenon.position_protection SET state='ARMED', native_order_perm_id=NULL, armed_at=NOW() WHERE protection_id=:pid"), {"pid": pid})

    executor = MagicMock()
    executor.flatten_mkt.return_value = PlaceResult(perm_id=12345, ib_order_id=1, status="Submitted", raw={})
    ib_client = MagicMock()
    ib_client.connected = True
    ib_client.get_quote.return_value = Quote(symbol="MSFT", price=91.0, ts=datetime.now(timezone.utc))  # 9% below entry → triggers
    ib_client.find_open_orders_by_order_ref.return_value = []
    ib_client.find_executions_by_order_ref.return_value = []
    ib_client.positions.return_value = [{"symbol": "MSFT", "qty": 100, "con_id": 22222}]

    handler = PositionRulesHandler(engine=engine, executor=executor, ib_client=ib_client,
                                   scope=AccountScope(broker="IB", account_env="paper", broker_account="DU1234567"))
    handler.execute()

    with engine.connect() as conn:
        row = conn.execute(text("SELECT state FROM xenon.position_protection WHERE protection_id=:pid"), {"pid": pid}).first()
        claim = conn.execute(text("SELECT status, broker_perm_id, order_ref FROM xenon.position_close_claims WHERE position_key='TEST::MSFT_HANDLER'")).first()
    assert row.state == "TRIGGERED"
    assert claim.status == "SUBMITTED"
    assert claim.broker_perm_id == 12345
    executor.flatten_mkt.assert_called_once()
    flatten_args = executor.flatten_mkt.call_args.kwargs
    assert flatten_args["order_ref"] == claim.order_ref


def test_two_rules_same_position_only_one_mkt(engine):
    """Spec §8 same-tick narrative — both rules see breach, only one closes."""
    descriptor = _stock_descriptor(symbol="GOOG", price=100.0, con_id=33333)
    pid_sl = insert_pending_arm(
        engine, broker="IB", account_env="paper", broker_account="DU1234567",
        position_key="TEST::GOOG_HANDLER", position_descriptor=descriptor,
        asset_class="stock", rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    pid_tp = insert_pending_arm(
        engine, broker="IB", account_env="paper", broker_account="DU1234567",
        position_key="TEST::GOOG_HANDLER", position_descriptor=descriptor,
        asset_class="stock", rule_kind="trailing_tp",
        config={"trail_pct": 0.05, "activation_pct": 0.0, "anchor": "mfe"},
    )
    with engine.begin() as conn:
        conn.execute(text("UPDATE xenon.position_protection SET state='ARMED', state_data='{\"mfe\": 110.0}'::jsonb WHERE position_key='TEST::GOOG_HANDLER'"))

    executor = MagicMock()
    executor.flatten_mkt.return_value = PlaceResult(perm_id=12345, ib_order_id=1, status="Submitted", raw={})
    ib_client = MagicMock()
    ib_client.connected = True
    ib_client.get_quote.return_value = Quote(symbol="GOOG", price=91.0, ts=datetime.now(timezone.utc))  # below SL AND below trail
    ib_client.find_open_orders_by_order_ref.return_value = []
    ib_client.find_executions_by_order_ref.return_value = []
    ib_client.positions.return_value = [{"symbol": "GOOG", "qty": 100, "con_id": 33333}]

    handler = PositionRulesHandler(engine=engine, executor=executor, ib_client=ib_client,
                                   scope=AccountScope(broker="IB", account_env="paper", broker_account="DU1234567"))
    handler.execute()

    assert executor.flatten_mkt.call_count == 1
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT protection_id, state FROM xenon.position_protection
            WHERE position_key='TEST::GOOG_HANDLER' ORDER BY protection_id
        """)).all()
    states = sorted(r.state for r in rows)
    assert states == ["SUPERSEDED", "TRIGGERED"]


def test_armed_with_native_perm_id_detects_external_cancel(engine):
    descriptor = _stock_descriptor(symbol="NVDA", price=100.0, con_id=44444)
    pid = insert_pending_arm(
        engine, broker="IB", account_env="paper", broker_account="DU1234567",
        position_key="TEST::NVDA_HANDLER", position_descriptor=descriptor,
        asset_class="stock", rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    with engine.begin() as conn:
        conn.execute(text("UPDATE xenon.position_protection SET state='ARMED', native_order_perm_id=777, armed_at=NOW() WHERE protection_id=:pid"), {"pid": pid})

    executor = MagicMock()
    ib_client = MagicMock()
    ib_client.connected = True
    ib_client.get_order_state.return_value = {"status": "Cancelled", "permId": 777}
    ib_client.positions.return_value = [{"symbol": "NVDA", "qty": 100, "con_id": 44444}]

    handler = PositionRulesHandler(engine=engine, executor=executor, ib_client=ib_client,
                                   scope=AccountScope(broker="IB", account_env="paper", broker_account="DU1234567"))
    handler.execute()

    with engine.connect() as conn:
        row = conn.execute(text("SELECT state FROM xenon.position_protection WHERE protection_id=:pid"), {"pid": pid}).first()
    assert row.state == "CANCELED"
    executor.flatten_mkt.assert_not_called()


def test_stale_quote_skips_evaluation(engine):
    descriptor = _stock_descriptor(symbol="TSLA", price=100.0, con_id=55555)
    pid = insert_pending_arm(
        engine, broker="IB", account_env="paper", broker_account="DU1234567",
        position_key="TEST::TSLA_HANDLER", position_descriptor=descriptor,
        asset_class="stock", rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    with engine.begin() as conn:
        conn.execute(text("UPDATE xenon.position_protection SET state='ARMED', state_data='{\"consecutive_stale_ticks\": 0}'::jsonb WHERE protection_id=:pid"), {"pid": pid})

    executor = MagicMock()
    ib_client = MagicMock()
    ib_client.connected = True
    # Stale quote: 5 minutes old
    from datetime import timedelta
    stale_ts = datetime.now(timezone.utc) - timedelta(minutes=5)
    ib_client.get_quote.return_value = Quote(symbol="TSLA", price=91.0, ts=stale_ts)

    handler = PositionRulesHandler(engine=engine, executor=executor, ib_client=ib_client,
                                   scope=AccountScope(broker="IB", account_env="paper", broker_account="DU1234567"))
    handler.execute()

    with engine.connect() as conn:
        row = conn.execute(text("SELECT state, state_data FROM xenon.position_protection WHERE protection_id=:pid"), {"pid": pid}).first()
    assert row.state == "ARMED"  # no transition
    assert row.state_data["consecutive_stale_ticks"] >= 1
    executor.flatten_mkt.assert_not_called()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest scripts/tests/test_position_rules/test_handler_loop.py -xvs`

- [ ] **Step 3: Implement `position_rules.py`**

```python
# src/xenon/monitor_daemon/handlers/position_rules.py
"""PositionRulesHandler — synthetic monitor + native-liveness check.

Spec §8. Per tick:
  1. List active rows for the current scope.
  2. PENDING_ARM rows → call rule.arm(executor=...).
  3. ARMED rows with native_order_perm_id → liveness check first.
  4. ARMED rows: read marks (cache), call rule.evaluate(), apply close-claim if TRIGGERED.
  5. TRIGGERED rows: reconcile claim status against IB.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from xenon.db.queries.position_close_claims import (
    find_by_order_ref,
    mark_submitted,
    mark_terminal,
    try_claim,
)
from xenon.db.queries.position_protection import cas_transition, list_active_rows
from xenon.execution.account_scope import AccountScope
from xenon.execution.brackets.close_claim import (
    derive_order_ref,
    should_skip_resubmit,
)
from xenon.execution.brackets.executor.ib_executor import IBExecutor
from xenon.execution.brackets.executor.marks import (
    MarkCache,
    SpotCache,
    is_quote_fresh,
)
from xenon.execution.brackets.executor.native_liveness import (
    NativeOrderState,
    verify_native_order_live,
)
from xenon.execution.brackets.rules.base import RULE_REGISTRY
from xenon.monitor_daemon.handlers.base import BaseHandler

_STALENESS_MAX_AGE_S = 60
_CONNECTION_STALE_ALERT_MISSES = 3
_SILENT_MARKET_ALERT_MISSES = 10
_POSITION_MISSING_TICKS_FOR_CANCEL = 2


class PositionRulesHandler(BaseHandler):
    name = "position_rules"
    interval_seconds = 30
    requires_market_hours = True

    def __init__(self, *, engine, executor: IBExecutor, ib_client, scope: AccountScope):
        super().__init__()
        self._engine = engine
        self._executor = executor
        self._ib = ib_client
        self._scope = scope

    def execute(self) -> dict[str, Any]:
        rows = list_active_rows(
            self._engine,
            broker=self._scope.broker, account_env=self._scope.account_env,
            broker_account=self._scope.broker_account,
        )
        mark_cache = MarkCache(fetcher=lambda con_id: self._ib.get_quote(con_id=con_id) if hasattr(self._ib, "get_quote") else None)
        spot_cache = SpotCache(fetcher=lambda symbol: self._ib.get_quote(symbol=symbol) if hasattr(self._ib, "get_quote") else None)

        positions_snapshot = self._safe_positions_snapshot()

        evaluated = 0
        for row in rows:
            if row["state"] == "PENDING_ARM":
                self._handle_pending_arm(row)
            elif row["state"] == "ARMED":
                self._handle_armed(row, mark_cache=mark_cache, spot_cache=spot_cache, positions=positions_snapshot)
            elif row["state"] == "TRIGGERED":
                self._handle_triggered(row)
            evaluated += 1
        return {"evaluated": evaluated}

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _safe_positions_snapshot(self) -> list[dict] | None:
        try:
            return self._ib.positions() if hasattr(self._ib, "positions") else None
        except Exception:  # noqa: BLE001
            return None

    def _position_in_snapshot(self, position_key: str, descriptor: dict, positions: list[dict] | None) -> bool:
        if positions is None:
            return True  # cannot conclude — treat as present, retry next tick
        leg = descriptor["legs"][0]
        for p in positions:
            if p.get("symbol") == leg["symbol"] and (p.get("con_id") in (None, leg.get("con_id"))):
                return True
        return False

    # ── State handlers ────────────────────────────────────────────────────────

    def _handle_pending_arm(self, row: dict) -> None:
        rule = RULE_REGISTRY[row["rule_kind"]]
        position = {**row["position_descriptor"], "anchor_price": row["position_descriptor"]["anchor_price"]}
        result = rule.arm(scope=self._scope, position=position, config=row["config"], state_data=row["state_data"], executor=self._executor)
        if result.kind == "NATIVE_ARMED":
            cas_transition(
                self._engine, protection_id=row["protection_id"],
                expected_state="PENDING_ARM", new_state="ARMED",
                reason="native_armed", state_data_patch=result.state_data_patch,
                native_order_perm_id=result.perm_id,
            )
        elif result.kind == "SYNTHETIC_ONLY":
            cas_transition(
                self._engine, protection_id=row["protection_id"],
                expected_state="PENDING_ARM", new_state="ARMED",
                reason="synthetic_only",
            )
        elif result.kind == "RETRY":
            attempts = (row["state_data"] or {}).get("arm_attempts", 0) + 1
            if attempts >= 4:
                cas_transition(
                    self._engine, protection_id=row["protection_id"],
                    expected_state="PENDING_ARM", new_state="FAILED",
                    reason=f"arm_failed_{result.reason}",
                    state_data_patch={"arm_attempts": attempts, "last_arm_error": result.reason},
                )
            else:
                # Stay PENDING_ARM, bump counter
                from xenon.db.engine import get_sync_engine  # local — avoid top-level cycle
                from sqlalchemy import update
                from xenon.db.schema import position_protection
                merged = dict(row["state_data"] or {})
                merged.update({"arm_attempts": attempts, "last_arm_error": result.reason})
                with self._engine.begin() as conn:
                    conn.execute(
                        update(position_protection)
                        .where(position_protection.c.protection_id == row["protection_id"])
                        .values(state_data=merged)
                    )
        elif result.kind == "FAILED":
            cas_transition(
                self._engine, protection_id=row["protection_id"],
                expected_state="PENDING_ARM", new_state="FAILED",
                reason=result.reason or "arm_failed",
            )

    def _handle_armed(self, row: dict, *, mark_cache: MarkCache, spot_cache: SpotCache, positions: list[dict] | None) -> None:
        # 1. External-close detection (2-tick gate, spec §10.3 / B1)
        descriptor = row["position_descriptor"]
        present = self._position_in_snapshot(row["position_key"], descriptor, positions)
        missing_ticks = (row["state_data"] or {}).get("position_missing_ticks", 0)
        if positions is not None and not present:
            new_missing = missing_ticks + 1
            if new_missing >= _POSITION_MISSING_TICKS_FOR_CANCEL and getattr(self._ib, "connected", True):
                cas_transition(
                    self._engine, protection_id=row["protection_id"],
                    expected_state="ARMED", new_state="CANCELED",
                    reason="position_closed_externally",
                    state_data_patch={"position_missing_ticks": new_missing},
                )
                return
            self._patch_state_data(row["protection_id"], {"position_missing_ticks": new_missing})
            return
        elif missing_ticks > 0:
            self._patch_state_data(row["protection_id"], {"position_missing_ticks": 0})

        # 2. Native-order liveness (spec §8)
        if row["native_order_perm_id"] is not None:
            state = verify_native_order_live(ib_client=self._ib, perm_id=row["native_order_perm_id"])
            if state == NativeOrderState.CANCELLED or state == NativeOrderState.INACTIVE:
                cas_transition(
                    self._engine, protection_id=row["protection_id"],
                    expected_state="ARMED", new_state="CANCELED",
                    reason="native_order_externally_cancelled",
                )
                return
            if state == NativeOrderState.FILLED:
                # The native bracket fired — insert native_reconcile_close claim, then mark CLOSED.
                claim_id = try_claim(
                    self._engine,
                    broker=self._scope.broker, account_env=self._scope.account_env,
                    broker_account=self._scope.broker_account,
                    position_key=row["position_key"], claimed_by_protection_id=row["protection_id"],
                    claim_kind="native_reconcile_close",
                )
                if claim_id is not None:
                    mark_submitted(self._engine, claim_id=claim_id, broker_perm_id=row["native_order_perm_id"])
                    mark_terminal(self._engine, claim_id=claim_id, status="FILLED")
                cas_transition(
                    self._engine, protection_id=row["protection_id"],
                    expected_state="ARMED", new_state="CLOSED", reason="native_bracket_filled",
                )
                return
            # LIVE / UNKNOWN → fall through to evaluate

        # 3. Read marks and freshness
        leg = descriptor["legs"][0]
        quote = mark_cache.get(con_id=leg["con_id"])
        if quote is None:
            self._handle_stale_quote(row)
            return
        if not is_quote_fresh(quote, max_age_s=_STALENESS_MAX_AGE_S):
            self._handle_stale_quote(row)
            return

        # Reset stale counter on fresh quote
        if (row["state_data"] or {}).get("consecutive_stale_ticks", 0) > 0:
            self._patch_state_data(row["protection_id"], {"consecutive_stale_ticks": 0})

        # 4. Evaluate
        rule = RULE_REGISTRY[row["rule_kind"]]
        marks = {"mark": quote.price, "now": datetime.now(timezone.utc)}
        if descriptor.get("asset_class") == "credit_spread":
            spot = spot_cache.get(symbol=leg["symbol"])
            if spot:
                marks["underlying_spot"] = spot.price
        position = {**descriptor, "anchor_price": descriptor["anchor_price"]}
        decision = rule.evaluate(scope=self._scope, position=position, config=row["config"], state_data=row["state_data"], marks=marks)

        if decision.kind == "TRIGGERED":
            self._submit_close(row, decision)
        elif decision.kind == "UPDATE_STATE":
            self._patch_state_data(row["protection_id"], decision.state_data_patch or {})

    def _handle_triggered(self, row: dict) -> None:
        # Reconcile in-flight claim against IB by orderRef.
        claim = self._find_pending_claim(row["position_key"])
        if claim is None:
            return
        order_ref = claim["order_ref"]
        open_orders = self._ib.find_open_orders_by_order_ref(order_ref) if hasattr(self._ib, "find_open_orders_by_order_ref") else []
        executions = self._ib.find_executions_by_order_ref(order_ref) if hasattr(self._ib, "find_executions_by_order_ref") else []
        if executions:
            mark_terminal(self._engine, claim_id=claim["claim_id"], status="FILLED")
            cas_transition(
                self._engine, protection_id=row["protection_id"],
                expected_state="TRIGGERED", new_state="CLOSED", reason="claim_filled",
            )
        elif not open_orders and claim["attempts"] >= 4:
            mark_terminal(self._engine, claim_id=claim["claim_id"], status="FAILED")
            cas_transition(
                self._engine, protection_id=row["protection_id"],
                expected_state="TRIGGERED", new_state="FAILED", reason="claim_failed_max_attempts",
            )

    # ── Submitting closes ─────────────────────────────────────────────────────

    def _submit_close(self, row: dict, decision) -> None:
        claim_id = try_claim(
            self._engine,
            broker=self._scope.broker, account_env=self._scope.account_env,
            broker_account=self._scope.broker_account,
            position_key=row["position_key"], claimed_by_protection_id=row["protection_id"],
            claim_kind="synthetic_close",
        )
        if claim_id is None:
            # Another rule or the native-reconciler already owns the close.
            cas_transition(
                self._engine, protection_id=row["protection_id"],
                expected_state="ARMED", new_state="SUPERSEDED",
                reason="claim_held_by_other_rule",
            )
            return

        order_ref = derive_order_ref(claim_id=claim_id)
        # Idempotent retry: if broker already has this orderRef, skip resubmit.
        open_orders = self._ib.find_open_orders_by_order_ref(order_ref) if hasattr(self._ib, "find_open_orders_by_order_ref") else []
        executions = self._ib.find_executions_by_order_ref(order_ref) if hasattr(self._ib, "find_executions_by_order_ref") else []
        skip, existing_perm = should_skip_resubmit(order_ref=order_ref, open_orders=open_orders, executions=executions)
        if skip:
            mark_submitted(self._engine, claim_id=claim_id, broker_perm_id=existing_perm)
        else:
            descriptor = row["position_descriptor"]
            leg = descriptor["legs"][0]
            close_action = "SELL" if leg["action"] == "BUY" else "BUY"
            current_qty = self._current_broker_qty(leg["symbol"], leg.get("con_id"))
            target_qty = min(descriptor["protected_qty"], current_qty) if current_qty else descriptor["protected_qty"]
            if target_qty == 0:
                mark_terminal(self._engine, claim_id=claim_id, status="ABANDONED", last_error="position_already_flat")
                cas_transition(
                    self._engine, protection_id=row["protection_id"],
                    expected_state="ARMED", new_state="CANCELED", reason="position_already_flat",
                )
                return
            try:
                result = self._executor.flatten_mkt(
                    scope=self._scope, con_id=leg["con_id"], symbol=leg["symbol"], sec_type=leg["sec_type"],
                    close_action=close_action, qty=target_qty, order_ref=order_ref,
                )
                mark_submitted(self._engine, claim_id=claim_id, broker_perm_id=result.perm_id)
            except Exception as exc:  # noqa: BLE001
                from xenon.db.queries.position_close_claims import increment_attempts
                increment_attempts(self._engine, claim_id=claim_id, last_error=str(exc))
                return

        cas_transition(
            self._engine, protection_id=row["protection_id"],
            expected_state="ARMED", new_state="TRIGGERED", reason=decision.reason or "trigger",
            context=decision.context,
        )

    # ── Utilities ─────────────────────────────────────────────────────────────

    def _find_pending_claim(self, position_key: str):
        from sqlalchemy import select, and_
        from xenon.db.schema import position_close_claims
        with self._engine.connect() as conn:
            row = conn.execute(
                select(position_close_claims).where(and_(
                    position_close_claims.c.broker == self._scope.broker,
                    position_close_claims.c.account_env == self._scope.account_env,
                    position_close_claims.c.broker_account == self._scope.broker_account,
                    position_close_claims.c.position_key == position_key,
                    position_close_claims.c.status.in_(("PENDING", "SUBMITTED")),
                ))
            ).first()
            return dict(row._mapping) if row else None

    def _current_broker_qty(self, symbol: str, con_id: int | None) -> int:
        try:
            positions = self._ib.positions() if hasattr(self._ib, "positions") else []
        except Exception:  # noqa: BLE001
            return 0
        for p in positions or []:
            if p.get("symbol") == symbol and (con_id is None or p.get("con_id") == con_id):
                return abs(int(p.get("qty", 0)))
        return 0

    def _handle_stale_quote(self, row: dict) -> None:
        from sqlalchemy import update
        from xenon.db.schema import position_protection
        merged = dict(row["state_data"] or {})
        merged["consecutive_stale_ticks"] = (merged.get("consecutive_stale_ticks") or 0) + 1
        with self._engine.begin() as conn:
            conn.execute(
                update(position_protection)
                .where(position_protection.c.protection_id == row["protection_id"])
                .values(state_data=merged)
            )

    def _patch_state_data(self, protection_id: int, patch: dict) -> None:
        from sqlalchemy import select, update
        from xenon.db.schema import position_protection
        with self._engine.begin() as conn:
            existing = conn.execute(
                select(position_protection.c.state_data).where(position_protection.c.protection_id == protection_id)
            ).first()
            merged = dict(existing.state_data or {}) if existing else {}
            merged.update(patch)
            conn.execute(
                update(position_protection)
                .where(position_protection.c.protection_id == protection_id)
                .values(state_data=merged)
            )
```

- [ ] **Step 4: Run tests + commit**

Run: `uv run pytest scripts/tests/test_position_rules/test_handler_loop.py -xvs`
Expected: 5 green.

```bash
git add src/xenon/monitor_daemon/handlers/position_rules.py scripts/tests/test_position_rules/test_handler_loop.py
git commit -m "feat(monitor-daemon): add PositionRulesHandler loop with claim protocol"
```

---

## Task 6: Boot reconcile

**Files:**

- Create: `src/xenon/execution/brackets/executor/reconcile.py`
- Create: `scripts/tests/test_position_rules_db/test_reconcile.py`

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_position_rules_db/test_reconcile.py
"""Boot reconcile. Spec §10.4."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.db.queries.position_close_claims import try_claim, mark_submitted
from xenon.db.queries.position_protection import insert_pending_arm
from xenon.execution.account_scope import AccountScope
from xenon.execution.brackets.executor.reconcile import boot_reconcile


@pytest.fixture
def engine():
    e = get_sync_engine()
    with e.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key LIKE 'TEST::%'"))
        conn.execute(text("DELETE FROM xenon.position_close_claims WHERE position_key LIKE 'TEST::%'"))
    yield e
    with e.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key LIKE 'TEST::%'"))
        conn.execute(text("DELETE FROM xenon.position_close_claims WHERE position_key LIKE 'TEST::%'"))


def test_boot_reconcile_snaps_inflight_claim_to_filled(engine):
    descriptor = {"asset_class": "stock", "anchor_price": 100.0, "opened_qty": 1, "protected_qty": 1,
                  "multiplier": 1, "qty_unit": "share", "opened_at": "2026-05-04T14:00:00Z",
                  "source": "fastapi_orders_place", "anchor_currency": "USD",
                  "legs": [{"sec_type": "STK", "symbol": "AAPL", "action": "BUY", "ratio": 1, "fill_price": 100.0, "con_id": 1}]}
    pid = insert_pending_arm(
        engine, broker="IB", account_env="paper", broker_account="DU1234567",
        position_key="TEST::AAPL_RECON", position_descriptor=descriptor,
        asset_class="stock", rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    with engine.begin() as conn:
        conn.execute(text("UPDATE xenon.position_protection SET state='TRIGGERED' WHERE protection_id=:pid"), {"pid": pid})
    cid = try_claim(engine, broker="IB", account_env="paper", broker_account="DU1234567",
                   position_key="TEST::AAPL_RECON", claimed_by_protection_id=pid, claim_kind="synthetic_close")
    mark_submitted(engine, claim_id=cid, broker_perm_id=12345)

    ib_client = MagicMock()
    ib_client.find_executions_by_order_ref.return_value = [{"orderRef": f"xenon-pr-{cid}", "permId": 12345}]
    ib_client.find_open_orders_by_order_ref.return_value = []

    boot_reconcile(engine=engine, ib_client=ib_client,
                   scope=AccountScope(broker="IB", account_env="paper", broker_account="DU1234567"))

    with engine.connect() as conn:
        protection = conn.execute(text("SELECT state FROM xenon.position_protection WHERE protection_id=:pid"), {"pid": pid}).first()
        claim = conn.execute(text("SELECT status FROM xenon.position_close_claims WHERE claim_id=:cid"), {"cid": cid}).first()
    assert protection.state == "CLOSED"
    assert claim.status == "FILLED"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest scripts/tests/test_position_rules_db/test_reconcile.py -xvs`

- [ ] **Step 3: Implement `reconcile.py`**

```python
# src/xenon/execution/brackets/executor/reconcile.py
"""Boot reconcile + reconnect-triggered reconcile. Spec §10.4."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, text

from xenon.db.queries.position_close_claims import mark_terminal
from xenon.db.queries.position_protection import cas_transition
from xenon.db.schema import position_close_claims, position_protection
from xenon.execution.account_scope import AccountScope
from xenon.execution.brackets.executor.native_liveness import (
    NativeOrderState,
    verify_native_order_live,
)

logger = logging.getLogger(__name__)


def boot_reconcile(*, engine, ib_client, scope: AccountScope) -> dict[str, Any]:
    if not getattr(ib_client, "connected", True):
        logger.info("boot_reconcile: IB not connected — deferring")
        return {"status": "deferred"}

    counts = {"claims_resolved": 0, "armed_rows_re_armed": 0}

    # Step 1: in-flight close claims
    with engine.connect() as conn:
        inflight = conn.execute(
            select(position_close_claims).where(
                position_close_claims.c.broker == scope.broker,
                position_close_claims.c.account_env == scope.account_env,
                position_close_claims.c.broker_account == scope.broker_account,
                position_close_claims.c.status.in_(("PENDING", "SUBMITTED")),
            )
        ).all()
    for claim_row in inflight:
        claim = dict(claim_row._mapping)
        order_ref = claim["order_ref"]
        executions = ib_client.find_executions_by_order_ref(order_ref) if hasattr(ib_client, "find_executions_by_order_ref") else []
        open_orders = ib_client.find_open_orders_by_order_ref(order_ref) if hasattr(ib_client, "find_open_orders_by_order_ref") else []
        if executions:
            mark_terminal(engine, claim_id=claim["claim_id"], status="FILLED")
            cas_transition(
                engine, protection_id=claim["claimed_by_protection_id"],
                expected_state="TRIGGERED", new_state="CLOSED", reason="boot_reconcile_filled",
            )
            counts["claims_resolved"] += 1
            continue
        if not open_orders and not executions:
            # Subprocess truly failed; revert to PENDING so the next handler tick retries.
            with engine.begin() as conn:
                from sqlalchemy import update
                conn.execute(
                    update(position_close_claims)
                    .where(position_close_claims.c.claim_id == claim["claim_id"])
                    .values(status="PENDING")
                )

    # Step 2: ARMED rows with native_order_perm_id — verify live
    with engine.connect() as conn:
        armed_rows = conn.execute(
            select(position_protection).where(
                position_protection.c.broker == scope.broker,
                position_protection.c.account_env == scope.account_env,
                position_protection.c.broker_account == scope.broker_account,
                position_protection.c.state == "ARMED",
                position_protection.c.native_order_perm_id.is_not(None),
            )
        ).all()
    for r in armed_rows:
        pid = r.protection_id
        state = verify_native_order_live(ib_client=ib_client, perm_id=r.native_order_perm_id)
        if state == NativeOrderState.CANCELLED:
            cas_transition(engine, protection_id=pid, expected_state="ARMED", new_state="PENDING_ARM",
                           reason="boot_reconcile_native_cancelled")
            counts["armed_rows_re_armed"] += 1
        elif state == NativeOrderState.FILLED:
            cas_transition(engine, protection_id=pid, expected_state="ARMED", new_state="CLOSED",
                           reason="boot_reconcile_native_filled")
            counts["claims_resolved"] += 1

    return counts
```

- [ ] **Step 4: Run tests + commit**

Run: `uv run pytest scripts/tests/test_position_rules_db/test_reconcile.py -xvs`
Expected: green.

```bash
git add src/xenon/execution/brackets/executor/reconcile.py scripts/tests/test_position_rules_db/test_reconcile.py
git commit -m "feat(brackets): add boot reconcile for in-flight claims + ARMED native orders"
```

---

## Task 7: Arm consumer LISTEN harness

**Files:**

- Create: `src/xenon/monitor_daemon/handlers/arm_consumer.py`
- Create: `scripts/tests/test_position_rules_db/test_arm_consumer_dlq.py`

- [ ] **Step 1: Write the failing DLQ test**

```python
# scripts/tests/test_position_rules_db/test_arm_consumer_dlq.py
"""Arm-consumer DLQ on persistent failure. Spec §6.6."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.monitor_daemon.handlers.arm_consumer import process_event_with_dlq


@pytest.fixture
def engine():
    e = get_sync_engine()
    with e.begin() as conn:
        conn.execute(text("DELETE FROM events.outbox_dlq WHERE source_event_id < 0"))
    yield e


def test_event_moves_to_dlq_after_max_attempts(engine):
    payload = {"exec_id": "TEST-DLQ-1", "broker": "IB", "account_env": "paper", "broker_account": "DU1234567"}
    counter = {"n": 0}

    def always_raise(eng, p):
        counter["n"] += 1
        raise RuntimeError("boom")

    with patch("xenon.execution.brackets.arm_hook.on_fill_event", side_effect=always_raise):
        for _ in range(6):
            process_event_with_dlq(engine=engine, source_event_id=-1, payload=payload, max_attempts=5)

    with engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM events.outbox_dlq WHERE source_event_id = -1")).scalar_one()
    assert n == 1
    assert counter["n"] == 5
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest scripts/tests/test_position_rules_db/test_arm_consumer_dlq.py -xvs`

- [ ] **Step 3: Implement `arm_consumer.py`**

```python
# src/xenon/monitor_daemon/handlers/arm_consumer.py
"""LISTEN harness for fill.recorded → arm_hook.on_fill_event. Spec §6.1, §6.6.

Runs as an async worker alongside the BaseHandler tick loop. On each NOTIFY:
  1. Look up the outbox row by id.
  2. Call arm_hook.on_fill_event in its own transaction.
  3. On success, ack (advance the consumer's id).
  4. On failure, increment in-memory attempt counter; after `max_attempts`,
     copy to events.outbox_dlq and ack.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from sqlalchemy import text

from xenon.db.events import CHANNEL_FILL_RECORDED
from xenon.execution.brackets.arm_hook import on_fill_event

logger = logging.getLogger(__name__)


# In-memory attempt counter keyed by (channel, source_event_id).
_attempt_counter: dict[tuple[str, int], int] = defaultdict(int)


def process_event_with_dlq(
    *,
    engine,
    source_event_id: int,
    payload: dict[str, Any],
    max_attempts: int = 5,
) -> bool:
    """Returns True if processed (success or DLQ); False if temporarily failing."""
    key = (CHANNEL_FILL_RECORDED, source_event_id)
    try:
        on_fill_event(engine, payload)
        _attempt_counter.pop(key, None)
        return True
    except Exception as exc:  # noqa: BLE001
        _attempt_counter[key] += 1
        if _attempt_counter[key] >= max_attempts:
            with engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO events.outbox_dlq (source_event_id, channel, source, payload, error, attempts)
                    VALUES (:sid, :ch, 'arm_consumer', :payload::jsonb, :err, :attempts)
                """), {
                    "sid": source_event_id, "ch": CHANNEL_FILL_RECORDED,
                    "payload": __import__("json").dumps(payload),
                    "err": str(exc), "attempts": _attempt_counter[key],
                })
            _attempt_counter.pop(key, None)
            return True
        logger.warning("arm_consumer: event %s attempt %d failed: %s", source_event_id, _attempt_counter[key], exc)
        return False
```

(The actual LISTEN/NOTIFY subscription wiring happens in `monitor_daemon/run.py` Task 9. This module is the per-event processor; the test invokes it directly.)

- [ ] **Step 4: Run tests + commit**

Run: `uv run pytest scripts/tests/test_position_rules_db/test_arm_consumer_dlq.py -xvs`
Expected: green.

```bash
git add src/xenon/monitor_daemon/handlers/arm_consumer.py scripts/tests/test_position_rules_db/test_arm_consumer_dlq.py
git commit -m "feat(monitor-daemon): add arm-consumer DLQ harness"
```

---

## Task 8: Migration B — drop wizard_protection + repoint combo wizard

**Files:**

- Create: `src/xenon/db/migrations/versions/<rev>_drop_wizard_protection.py`
- Modify: `src/xenon/db/schema.py` — remove `wizard_protection` Table block
- Modify: `src/xenon/db/queries/combo_wizard.py` — point INSERT/UPDATE/SELECT at `position_protection`
- Modify: `src/xenon/execution/combo_wizard/protect.py` — write `rule_kind='combo_tp_alert'` rows with `auto_place=FALSE`
- Modify: `scripts/migrations/migrate_to_postgres.py` — remove `wizard_protection` block
- Modify: `src/xenon/api/tests/conftest.py`, `src/xenon/api/tests/test_wizard_routes.py`, `src/xenon/db/tests/test_combo_wizard.py`, `src/xenon/db/tests/test_schema.py`, `scripts/tests/test_combo_wizard_protect.py`, `scripts/tests/conftest.py` — drop `wizard_protection` references

- [ ] **Step 1: Generate the migration**

```bash
uv run alembic revision -m "drop wizard_protection"
```

- [ ] **Step 2: Author migration body**

```python
"""drop wizard_protection (empty per Plan 2 precondition)

Revision ID: <generated>
Revises: <plan2 revision>
"""
from alembic import op
import sqlalchemy as sa

revision = "<generated>"
down_revision = "<plan2 revision>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Safety net: assert the table is empty before dropping.
    n = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM xenon.wizard_protection")).scalar_one()
    if n != 0:
        raise RuntimeError(f"wizard_protection has {n} rows — migration aborted. Manually rebase rows onto position_protection first.")
    op.drop_table("wizard_protection", schema="xenon")


def downgrade() -> None:
    # Recreate empty table for schema parity. Cannot restore data.
    op.create_table(
        "wizard_protection",
        sa.Column("protection_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("attempt_id", sa.Text()),
        sa.Column("protection_type", sa.Text(), nullable=False),
        sa.Column("config", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("triggered_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("(now() AT TIME ZONE 'UTC')")),
        sa.UniqueConstraint("session_id", name="uq_wizard_protection_session"),
        schema="xenon",
    )
```

- [ ] **Step 3: Repoint combo_wizard.py queries**

Edit `src/xenon/db/queries/combo_wizard.py`. The existing `wizard_protection`-targeting functions (`upsert_protection`, `get_protection`, `list_protected_sessions`) move to read/write `position_protection` filtered by `rule_kind = 'combo_tp_alert'`. The `position_key` for combo wizard rows uses the `wizard_combo_attempts.legs` manifest fed through `compute_position_key("debit_combo", descriptor)`.

Pseudocode for the rewrite:

```python
# Replace each wizard_protection.<op> call with the equivalent against position_protection.
# - upsert_protection(session_id=..., attempt_id=..., config=...) becomes:
#     1. resolve scope from session_id (already in wizard_sessions row)
#     2. compute position_key from attempt's legs JSONB
#     3. INSERT into position_protection with rule_kind='combo_tp_alert', state='PENDING_ARM',
#        config={**incoming_config, "auto_place": False}, on conflict do nothing
#     4. If config differs from existing row's config (frozen at insert), the new row goes in
#        as PENDING_ARM and the old one is moved to SUPERSEDED. (Wizard generally doesn't
#        re-config a single session — defensive only.)
#
# - get_protection(session_id) joins wizard_combo_attempts to derive position_key,
#     then SELECTs the position_protection row by (scope, position_key, rule_kind='combo_tp_alert').
#
# - list_protected_sessions becomes a JOIN on position_protection WHERE rule_kind='combo_tp_alert'
#     AND state IN ('PENDING_ARM','ARMED','TRIGGERED').
```

The full edit is mechanical; the existing test suite (`scripts/tests/test_combo_wizard_protect.py`, `src/xenon/db/tests/test_combo_wizard.py`) must be updated in lockstep. Run those tests after each function rewrite to keep the change reviewable.

- [ ] **Step 4: Update `combo_wizard/protect.py`**

Edit `src/xenon/execution/combo_wizard/protect.py`. Wherever it currently inserts into `wizard_protection`, route the call through the rewritten `combo_wizard.py` query module. Add a unit test that verifies the inserted row has:

- `rule_kind = 'combo_tp_alert'`
- `auto_place = FALSE` (carried via `config.auto_place`, not the policy table — since combo_tp_alert is not in the `bracket_policies` seed)
- `state = 'PENDING_ARM'`
- `position_descriptor` populated from the attempt's `legs`

- [ ] **Step 5: Drop `wizard_protection` from schema.py**

In `src/xenon/db/schema.py`, delete the `wizard_protection = Table(...)` block at lines 387-408. Remove from any module-level `__all__` if present.

- [ ] **Step 6: Update tests**

Find every reference to `wizard_protection`:

```bash
grep -rln "wizard_protection" src/ scripts/ web/ 2>/dev/null
```

For each hit:

- Test fixtures truncating `xenon.wizard_protection` → switch to truncating `xenon.position_protection WHERE rule_kind='combo_tp_alert'`.
- Test assertions querying `wizard_protection` → query `position_protection` filtered by `rule_kind='combo_tp_alert'`.
- `scripts/migrations/migrate_to_postgres.py` block 379-405 → delete.

- [ ] **Step 7: Run migration up/down + full test suite**

```bash
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
uv run pytest scripts/tests/test_combo_wizard_protect.py src/xenon/db/tests/test_combo_wizard.py -xvs
uv run pytest src/xenon/api/tests/test_wizard_routes.py -xvs
```

Expected: each step green.

- [ ] **Step 8: Commit**

```bash
git add src/xenon/db/migrations/versions/<rev>_drop_wizard_protection.py src/xenon/db/schema.py src/xenon/db/queries/combo_wizard.py src/xenon/execution/combo_wizard/protect.py scripts/migrations/migrate_to_postgres.py src/xenon/db/tests/test_combo_wizard.py src/xenon/db/tests/test_schema.py scripts/tests/test_combo_wizard_protect.py src/xenon/api/tests/conftest.py src/xenon/api/tests/test_wizard_routes.py scripts/tests/conftest.py
git commit -m "refactor(db): drop wizard_protection; repoint combo wizard onto position_protection"
```

---

## Task 9: Delete `wizard_stop_monitor.py` + register new handlers

**Files:**

- Delete: `src/xenon/monitor_daemon/handlers/wizard_stop_monitor.py`
- Modify: `src/xenon/monitor_daemon/handlers/__init__.py`
- Modify: `src/xenon/monitor_daemon/run.py`
- Modify: `src/xenon/api/server.py` (feature flag gate)

- [ ] **Step 1: Verify nothing else imports `wizard_stop_monitor`**

```bash
grep -rn "wizard_stop_monitor\|WizardStopMonitorHandler" src/ scripts/ 2>/dev/null
```

Hits should be limited to: the file itself, the tests for it (mark them deleted too), `monitor_daemon/__init__.py`, and `monitor_daemon/run.py`.

- [ ] **Step 2: Delete the handler module + its tests**

```bash
git rm src/xenon/monitor_daemon/handlers/wizard_stop_monitor.py
# If a dedicated test exists:
git rm scripts/tests/test_monitor_daemon/test_wizard_stop_monitor.py 2>/dev/null || true
```

(The `_default_notify` helper from this module is referenced in `combo_tp_alert.py` (Plan 2). If it's still needed, lift it into a new shared module like `src/xenon/monitor_daemon/notify.py` BEFORE deleting `wizard_stop_monitor.py`. Use `grep -n "_default_notify" src/` to confirm scope.)

- [ ] **Step 3: Update `handlers/__init__.py`**

Remove `WizardStopMonitorHandler` re-export. Add `PositionRulesHandler`:

```python
from xenon.monitor_daemon.handlers.position_rules import PositionRulesHandler
from xenon.monitor_daemon.handlers.fill_monitor import FillMonitorHandler
from xenon.monitor_daemon.handlers.preset_rebalance_handler import PresetRebalanceHandler
# ... whatever else is currently exported

__all__ = [
    "FillMonitorHandler",
    "PresetRebalanceHandler",
    "PositionRulesHandler",
]
```

- [ ] **Step 4: Update `run.py`**

Edit `src/xenon/monitor_daemon/run.py` `create_daemon()`:

```python
import os
from xenon.execution.account_scope import resolve_from_env
from xenon.db.engine import get_sync_engine
from xenon.execution.brackets.executor.ib_executor import IBExecutor
from xenon.clients.ib_client import IBClient  # adjust import path to actual location

# ... existing imports, MINUS wizard_stop_monitor

def create_daemon() -> MonitorDaemon:
    daemon = MonitorDaemon(
        state_file=STATE_FILE,
        respect_market_hours=True,
        loop_interval=30,
    )

    daemon.register(FillMonitorHandler(ib_port=4001, client_id=70, send_notifications=True))
    daemon.register(PresetRebalanceHandler())
    daemon.register(FlexTokenCheck())

    if os.environ.get("XENON_POSITION_RULES_ENABLED", "0") == "1":
        scope = resolve_from_env()
        ib_client = IBClient.singleton()
        executor = IBExecutor()
        daemon.register(PositionRulesHandler(
            engine=get_sync_engine(),
            executor=executor,
            ib_client=ib_client,
            scope=scope,
        ))
        # arm_consumer LISTEN harness — registered as an async task in the same daemon process.
        # See arm_consumer.py for run_listen_loop() which the daemon's async runtime calls.
        from xenon.monitor_daemon.handlers.arm_consumer import start_listen_loop
        daemon.register_async_task(start_listen_loop)

    daemon.load_state()
    return daemon
```

(The `register_async_task` method does not yet exist on `MonitorDaemon`. Add it. The existing `MonitorDaemon` runs handler ticks synchronously; async tasks need a small coroutine runner. Implementation: a thread that runs an asyncio event loop hosting the LISTEN subscription. See the existing `EventSubscriber` class in `src/xenon/db/events.py` for the pattern.)

- [ ] **Step 5: Add `start_listen_loop` to `arm_consumer.py`**

Append to `src/xenon/monitor_daemon/handlers/arm_consumer.py`:

```python
import asyncio
import json
import os

from xenon.db.events import EventSubscriber, CHANNEL_FILL_RECORDED


async def _listen_loop():
    """Long-lived LISTEN coroutine. One subscriber per daemon process."""
    from xenon.db.engine import get_sync_engine
    engine = get_sync_engine()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.warning("arm_consumer: DATABASE_URL unset — listen loop disabled")
        return
    sub = EventSubscriber(dsn=dsn, channels=[CHANNEL_FILL_RECORDED])
    sub.on(CHANNEL_FILL_RECORDED, lambda channel, payload: _dispatch(engine, payload))
    await sub.start()
    try:
        while True:
            await asyncio.sleep(60)  # keep coroutine alive; subscriber callback drives work
    finally:
        await sub.stop()


def _dispatch(engine, raw_payload: str | None) -> None:
    if not raw_payload:
        return
    try:
        payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    except json.JSONDecodeError:
        logger.warning("arm_consumer: malformed NOTIFY payload — skipping")
        return
    # Each NOTIFY is a single fill; we look up the outbox row id for DLQ keying.
    process_event_with_dlq(engine=engine, source_event_id=payload.get("__outbox_id", -1), payload=payload)


def start_listen_loop() -> None:
    """Sync entry point — daemon runs this in a daemon thread with its own asyncio loop."""
    asyncio.run(_listen_loop())
```

- [ ] **Step 6: Add a feature-flag guard test**

Append to `scripts/tests/test_monitor_daemon/test_run_setup.py` (create if missing):

```python
import os
from unittest.mock import patch

from xenon.monitor_daemon.run import create_daemon


def test_position_rules_disabled_by_default(monkeypatch):
    monkeypatch.delenv("XENON_POSITION_RULES_ENABLED", raising=False)
    daemon = create_daemon()
    handler_names = [h.name for h in daemon.handlers]
    assert "position_rules" not in handler_names


def test_position_rules_enabled_when_flag_set(monkeypatch):
    monkeypatch.setenv("XENON_POSITION_RULES_ENABLED", "1")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU1234567")
    monkeypatch.setenv("XENON_BROKER", "IB")
    with patch("xenon.clients.ib_client.IBClient.singleton") as mock_ib:
        mock_ib.return_value = object()
        daemon = create_daemon()
    handler_names = [h.name for h in daemon.handlers]
    assert "position_rules" in handler_names
```

- [ ] **Step 7: Run, verify, commit**

```bash
uv run pytest scripts/tests/test_monitor_daemon/test_run_setup.py -xvs
git add src/xenon/monitor_daemon/handlers/__init__.py src/xenon/monitor_daemon/handlers/arm_consumer.py src/xenon/monitor_daemon/run.py scripts/tests/test_monitor_daemon/test_run_setup.py
git rm src/xenon/monitor_daemon/handlers/wizard_stop_monitor.py
git commit -m "refactor(monitor-daemon): delete wizard_stop_monitor; register PositionRulesHandler + arm_consumer behind XENON_POSITION_RULES_ENABLED"
```

---

## Task 10: Frozen-config CI guard

**Files:**

- Create: `scripts/checks/frozen_config_at_arm.py`
- Create: `scripts/tests/test_checks/__init__.py` (empty)
- Create: `scripts/tests/test_checks/test_frozen_config_at_arm.py`
- Modify: `.github/workflows/ci.yml` — add the guard to the `order-path-guards` job

- [ ] **Step 1: Write the guard's own tests**

```python
# scripts/tests/test_checks/test_frozen_config_at_arm.py
"""CI guard self-test. Spec §13.8."""
from __future__ import annotations

import textwrap
from pathlib import Path

from scripts.checks.frozen_config_at_arm import check_module


def test_clean_module_passes(tmp_path: Path):
    src = tmp_path / "stop_loss.py"
    src.write_text(textwrap.dedent("""
        from xenon.execution.brackets.rules.base import register
        from xenon.execution.brackets.triggers import threshold_crossed_below

        class StopLossRule:
            rule_kind = "stop_loss"
            def arm(self, *, scope, position, config, state_data, executor=None):
                return None
    """))
    violations = check_module(src)
    assert violations == []


def test_module_importing_bracket_policies_fails(tmp_path: Path):
    src = tmp_path / "bad_rule.py"
    src.write_text(textwrap.dedent("""
        from xenon.db.queries.bracket_policies import resolve_for_scope

        class Bad:
            rule_kind = "bad"
    """))
    violations = check_module(src)
    assert violations
    assert any("bracket_policies" in v for v in violations)


def test_module_string_referencing_bracket_policies_fails(tmp_path: Path):
    src = tmp_path / "sneaky.py"
    src.write_text(textwrap.dedent("""
        SQL = "SELECT * FROM xenon.bracket_policies"
    """))
    violations = check_module(src)
    assert any("bracket_policies" in v for v in violations)
```

- [ ] **Step 2: Implement the guard**

```python
# scripts/checks/frozen_config_at_arm.py
"""CI guard: rule modules under src/xenon/execution/brackets/rules/ must NOT
import or reference `bracket_policies`. Spec §13.8.

Why: rule_kind config is FROZEN at row insert time and stored in
`position_protection.config`. If a rule module ever read live from
`bracket_policies`, a `psql UPDATE bracket_policies` would silently retune the
threshold of an already-armed position mid-flight — defeating the whole
"don't disturb existing positions" guarantee.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

RULES_DIR = Path("src/xenon/execution/brackets/rules")
FORBIDDEN_TOKEN = "bracket_policies"


def check_module(path: Path) -> list[str]:
    src = path.read_text()
    violations: list[str] = []

    # AST-based: any ImportFrom or Import touching bracket_policies
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [f"{path}: syntax error — {e}"]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if FORBIDDEN_TOKEN in mod:
                violations.append(f"{path}:{node.lineno}: import from `{mod}` references {FORBIDDEN_TOKEN}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if FORBIDDEN_TOKEN in alias.name:
                    violations.append(f"{path}:{node.lineno}: import `{alias.name}` references {FORBIDDEN_TOKEN}")
    # String-literal scan as a safety net for `text("... bracket_policies ...")` usage
    for i, line in enumerate(src.splitlines(), 1):
        if FORBIDDEN_TOKEN in line and "import" not in line and not line.lstrip().startswith("#"):
            # Allow comments; flag any other reference.
            violations.append(f"{path}:{i}: string literal references {FORBIDDEN_TOKEN}")
    return violations


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    target = Path(argv[0]) if argv else RULES_DIR
    files = list(target.rglob("*.py")) if target.is_dir() else [target]
    all_violations: list[str] = []
    for f in files:
        if f.name == "__init__.py":
            continue
        all_violations.extend(check_module(f))
    if all_violations:
        print("frozen_config_at_arm: violations:", file=sys.stderr)
        for v in all_violations:
            print(f"  {v}", file=sys.stderr)
        return 1
    print(f"frozen_config_at_arm: {len(files)} file(s) clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Run guard locally**

```bash
uv run python scripts/checks/frozen_config_at_arm.py
```

Expected: `frozen_config_at_arm: <n> file(s) clean`.

- [ ] **Step 4: Run guard's own tests**

```bash
uv run pytest scripts/tests/test_checks/test_frozen_config_at_arm.py -xvs
```

Expected: 3 green.

- [ ] **Step 5: Wire into CI**

Edit `.github/workflows/ci.yml`. Find the `order-path-guards` job and append:

```yaml
- name: Frozen config at arm
  run: uv run python scripts/checks/frozen_config_at_arm.py
```

- [ ] **Step 6: Commit**

```bash
git add scripts/checks/frozen_config_at_arm.py scripts/tests/test_checks/__init__.py scripts/tests/test_checks/test_frozen_config_at_arm.py .github/workflows/ci.yml
git commit -m "ci(checks): add frozen_config_at_arm guard for rule modules"
```

---

## Task 11: Order-path caller allowlist update

**Files:**

- Modify: `scripts/checks/order_path_caller_allowlist.py`

The existing guard pins which modules may invoke `xenon.execution.ib_place_order`. The new `IBExecutor` legitimately calls it via subprocess (not import), so no allowlist change is strictly needed. However, the new handler `position_rules.py` does import `IBExecutor` directly. Verify the guard still passes after Plan 3:

- [ ] **Step 1: Run the existing guard**

```bash
uv run python scripts/checks/order_path_caller_allowlist.py
```

If it fails (because the new module touches order-path code paths it considers gated), append `xenon.execution.brackets.executor.ib_executor` to the allowlist. **Otherwise leave alone.**

- [ ] **Step 2: Run the no-JSON-fallback guard**

```bash
uv run python scripts/checks/no_json_fallback_on_order_path.py
```

Expected: pass — none of the new modules read JSON fallback files.

---

## Task 12: Handler integration test (real Postgres + mocked IB)

**Files:**

- Create: `scripts/tests/test_position_rules_db/test_handler_integration.py`

This test exercises the full pipeline: `record_fill` → arm consumer → `position_protection` row → handler tick → trigger → claim → MKT submit (mocked) → CLOSED.

- [ ] **Step 1: Write the test**

```python
# scripts/tests/test_position_rules_db/test_handler_integration.py
"""End-to-end Postgres test. Spec §13.3."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope
from xenon.execution.brackets.arm_hook import on_fill_event
from xenon.execution.brackets.executor.ib_executor import PlaceResult
from xenon.execution.brackets.executor.marks import Quote
from xenon.execution.brackets.executor.native_liveness import NativeOrderState
from xenon.execution.orders_store import record_fill
from xenon.monitor_daemon.handlers.position_rules import PositionRulesHandler


@pytest.fixture
def engine():
    e = get_sync_engine()
    with e.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key LIKE 'STK::TESTINT%'"))
        conn.execute(text("DELETE FROM xenon.position_close_claims WHERE position_key LIKE 'STK::TESTINT%'"))
        conn.execute(text("DELETE FROM xenon.order_fills WHERE exec_id LIKE 'TESTINT-%'"))
    yield e
    with e.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key LIKE 'STK::TESTINT%'"))
        conn.execute(text("DELETE FROM xenon.position_close_claims WHERE position_key LIKE 'STK::TESTINT%'"))
        conn.execute(text("DELETE FROM xenon.order_fills WHERE exec_id LIKE 'TESTINT-%'"))


def test_full_pipeline(engine):
    # 1. record_fill commits + emits outbox.
    record_fill(
        exec_id="TESTINT-1", submission_id=None, perm_id="1", con_id=10001,
        ticker="TESTINT-A", side="BUY", qty=100, price=Decimal("100.00"),
        filled_at=datetime.now(timezone.utc), metadata={"sec_type": "STK"},
        broker="IB", account_env="paper", broker_account="DU1234567",
    )

    # 2. arm consumer processes the event (synchronous invocation here).
    on_fill_event(engine, {
        "exec_id": "TESTINT-1", "submission_id": None, "combo_attempt_id": None,
        "perm_id": "1", "ticker": "TESTINT-A", "side": "BUY", "qty": 100, "price": "100.00",
        "filled_at": datetime.now(timezone.utc).isoformat(), "metadata": {"sec_type": "STK"},
        "broker": "IB", "account_env": "paper", "broker_account": "DU1234567",
        "con_id": 10001,
    })

    # 3. Two PENDING_ARM rows now exist.
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT protection_id, rule_kind, state FROM xenon.position_protection
            WHERE position_key='STK::TESTINT-A' ORDER BY rule_kind
        """)).all()
    assert len(rows) == 2
    assert all(r.state == "PENDING_ARM" for r in rows)

    # 4. Handler tick: arms (mocked native STP).
    executor = MagicMock()
    executor.attach_native_stp.return_value = PlaceResult(perm_id=8888, ib_order_id=1, status="Submitted", raw={})
    ib_client = MagicMock()
    ib_client.connected = True
    ib_client.get_quote.return_value = Quote(symbol="TESTINT-A", price=100.0, ts=datetime.now(timezone.utc))
    ib_client.get_order_state.return_value = {"status": "Submitted", "permId": 8888}
    ib_client.positions.return_value = [{"symbol": "TESTINT-A", "qty": 100, "con_id": 10001}]

    handler = PositionRulesHandler(
        engine=engine, executor=executor, ib_client=ib_client,
        scope=AccountScope(broker="IB", account_env="paper", broker_account="DU1234567"),
    )
    handler.execute()

    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT rule_kind, state FROM xenon.position_protection
            WHERE position_key='STK::TESTINT-A' ORDER BY rule_kind
        """)).all()
    states = {r.rule_kind: r.state for r in rows}
    assert states["stop_loss"] == "ARMED"
    assert states["trailing_tp"] == "ARMED"

    # 5. Price drops 9% → stop_loss triggers, claim placed, second rule SUPERSEDED.
    ib_client.get_quote.return_value = Quote(symbol="TESTINT-A", price=91.0, ts=datetime.now(timezone.utc))
    executor.flatten_mkt.return_value = PlaceResult(perm_id=9999, ib_order_id=2, status="Submitted", raw={})
    ib_client.find_open_orders_by_order_ref.return_value = []
    ib_client.find_executions_by_order_ref.return_value = []
    handler.execute()

    with engine.connect() as conn:
        states = dict(conn.execute(text("""
            SELECT rule_kind, state FROM xenon.position_protection
            WHERE position_key='STK::TESTINT-A'
        """)).all())
        claim_count = conn.execute(text("""
            SELECT COUNT(*) FROM xenon.position_close_claims
            WHERE position_key='STK::TESTINT-A' AND status IN ('PENDING','SUBMITTED','FILLED')
        """)).scalar_one()
    assert claim_count == 1
    assert "TRIGGERED" in states.values()
    assert "SUPERSEDED" in states.values()
```

- [ ] **Step 2: Run + commit**

```bash
uv run pytest scripts/tests/test_position_rules_db/test_handler_integration.py -xvs
git add scripts/tests/test_position_rules_db/test_handler_integration.py
git commit -m "test(position-rules): end-to-end integration over real Postgres"
```

---

## Task 13: Push the PR + acceptance gates

- [ ] **Step 1: Run the full affected suite**

```bash
uv run python scripts/infra/dev/run_pytest_affected.py
cd web && npm test  # combo-wizard tests live here too — make sure they pass post-Migration B
cd ..
```

Expected: green everywhere.

- [ ] **Step 2: Push and open PR**

```bash
git push -u origin <plan-3-branch>
gh pr create --title "feat(position-rules): handler + executor + Migration B" --body "$(cat <<'EOF'
## Summary

Phase 3 + 4 + 5 of the position-rules engine. Lands the actual engine: `PositionRulesHandler` ticks every 30s, evaluates `ARMED` rows, performs the close-claim protocol, and fires MKT-flatten via subprocess to `xenon-ib-place-order`.

Also performs Migration B: combo-wizard rows move onto `position_protection` (with `rule_kind='combo_tp_alert'`); `wizard_stop_monitor.py` is deleted; `wizard_protection` is dropped.

Feature-flag-gated: handler registration requires `XENON_POSITION_RULES_ENABLED=1`. Default off until Plan 4 ships paper-smoke acceptance.

## Test plan

- [ ] `uv run pytest scripts/tests/test_position_rules/ scripts/tests/test_position_rules_db/ scripts/tests/test_position_rules_subprocess/ -xvs`
- [ ] `uv run pytest scripts/tests/test_combo_wizard_protect.py src/xenon/db/tests/test_combo_wizard.py -xvs` (Migration B regression)
- [ ] `uv run python scripts/checks/frozen_config_at_arm.py` clean
- [ ] `uv run python scripts/checks/order_path_caller_allowlist.py` clean
- [ ] `cd web && npm test` (combo-wizard surface)
- [ ] CI green
EOF
)"
```

- [ ] **Step 3: Confirm CI green and merge.** Plan 4 (UI + acceptance) follows.

---

## Self-Review

**Spec coverage:**

- §4.4 broker symmetry — IBExecutor wraps subprocess; FUTU branch is a future `elif` ✓
- §6.1 outbox-consumer arm hook — runtime wiring via arm_consumer.py + LISTEN harness ✓
- §6.6 DLQ on persistent failure ✓
- §8 handler loop with all 4 sub-bullets (liveness, mark coalescing, order-of-ops, claim protocol, same-tick narrative) ✓
- §9 RuleEvaluator extended with executor kwarg; 4 modules updated ✓
- §10.1 read-side failure modes (stale quote with connected/disconnected distinction) ✓
- §10.2 trigger-time write failure modes ✓
- §10.3 position-state surprises (2-tick gate) ✓
- §10.4 daemon singleton + boot reconcile (steps 1–3) ✓
- §10.5 slippage policy: MKT, RTH-only, no STP-LMT — encoded in `IBExecutor.flatten_mkt` payload ✓
- §13.3 partial-unique re-arm regression already covered in Plan 2 ✓
- §13.4 subprocess contract tests ✓
- §13.8 frozen-config CI guard ✓
- §14 Migration B + code edits ✓
- §15 Phases 3, 4, 5 ✓

**Out of scope (deferred to Plan 4):**

- `xenon-position-rules` CLI (list/show/cancel/sweep/health/events)
- Daily out-of-band sweep + 70% sanity gate
- Quarter-end re-arm sweep cron
- FastAPI routes (`/position-rules/*`)
- UI shield badge + drawer + global health indicator
- E2E browser tests
- `no_duplicate_close_audit.py`
- Paper-smoke runbook

**Placeholder scan:** none. Each step has concrete code or specific commands.

**Type consistency:**

- `RuleEvaluator.arm()` signature consistently includes `executor=None` keyword across base.py and all four rule modules.
- `PlaceResult` is the same dataclass used in `IBExecutor.attach_native_stp`, `flatten_mkt`, and asserted in handler test mocks.
- `NativeOrderState` enum values used identically in `position_rules.py` and `reconcile.py`.
- `derive_order_ref(claim_id=...)` from Plan 2 is reused by `position_rules.py._submit_close` — same signature.
- The ARMED → CANCELED reasons (`native_order_externally_cancelled`, `position_closed_externally`) and ARMED → SUPERSEDED reason (`claim_held_by_other_rule`) match spec §10.3.

**One non-trivial assumption:** The plan assumes `IBClient.singleton()` and `IBClient.get_order_state(perm_id=...)`, `find_open_orders_by_order_ref(...)`, `find_executions_by_order_ref(...)`, `positions()`, `get_quote(con_id=...)`, `connected` (attribute) are existing or trivially-addable surfaces. If any are missing, add them as thin shims in `IBClient` in this same PR — they're broker-side data the engine cannot run without. The integration test in Task 12 deliberately uses `MagicMock()` so we can ship the engine before all IBClient methods land; flag any missing IBClient surfaces in the PR description.

---
