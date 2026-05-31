# Position Rules — Plan 2: Backend Infra (Pure + Postgres)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the entire read-side: pure modules (rules, classifier, policies, position_key, triggers, close-claim logic) plus the Postgres schema, queries, and outbox-driven arm consumer. After this plan ships, every new fill that flows through `orders_store.record_fill()` lands a `PENDING_ARM` row in `position_protection`. No triggers fire yet; that arrives in Plan 3.

**Architecture:** Two layers. **Pure layer** (`src/xenon/execution/brackets/`) is broker-agnostic and DB-agnostic — pytest-only. **Postgres layer** (`schema.py`, `queries/`, Alembic migration, arm consumer) lives behind `XENON_POSITION_RULES_ENABLED=0` until Plan 3 wires the handler. Migration adds three tables (`position_protection`, `bracket_policies`, `position_close_claims`) and an outbox DLQ; `wizard_protection` is **not** dropped yet (that happens in Plan 3 alongside `wizard_stop_monitor.py` deletion).

**Tech Stack:** Python 3.13, SQLAlchemy + Alembic, Postgres 15, Pydantic, pytest, `uv`.

**Spec reference:** `docs/superpowers/specs/2026-05-04-position-rules-design.md` §3 (defaults), §4.2 (new code delta), §5 (data model — five subsections), §6 (post-fill arming hook), §9 (rule plug-in interface), §15 Phases 1 + 2.

**Prerequisite:** Plan 1 (Phase 0 DST fix) merged to `master`. Pure modules don't strictly require it, but the Postgres tests share fixtures with the daemon test infrastructure, and Plan 3 absolutely requires it. Land Plan 1 first to keep the dependency chain linear.

---

## Important spec/schema correction

The spec §6.3 Path A references `wizard_combo_attempts.combo_legs` and `expected_leg_count` — neither column exists. The actual table (`src/xenon/db/schema.py:351-385`) has a single `legs JSONB` column. The plan derives expected leg count from `len(attempt.legs)`. Documented as a comment in the implementation; a follow-up backlog item can make this explicit if needed.

---

## File Structure

### Created — pure modules

| Path                                                      | Responsibility                                                                                                                |
| --------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `src/xenon/execution/brackets/__init__.py`                | Package marker; re-export RuleEvaluator + RULE_REGISTRY                                                                       |
| `src/xenon/execution/brackets/asset_class.py`             | `AssetClass` enum + `classify_position()` — single-leg / wizard combo / patterns / UNCLASSIFIED                               |
| `src/xenon/execution/brackets/position_key.py`            | `compute_position_key(asset_class, descriptor)` — opaque deterministic key per spec §5.3                                      |
| `src/xenon/execution/brackets/policies.py`                | Most-specific-wins resolver: `resolve_policies(scope, asset_class) -> list[PolicyRow]`                                        |
| `src/xenon/execution/brackets/triggers.py`                | Pure `evaluate_*` helpers shared by rule modules (compare-to-threshold, MFE update, debit-to-close)                           |
| `src/xenon/execution/brackets/configs.py`                 | Pydantic `ConfigModel` per rule_kind + `PositionDescriptor` + `StateData`                                                     |
| `src/xenon/execution/brackets/close_claim.py`             | Pure functions: `derive_order_ref(claim_id)`, `claim_already_inflight(broker_perm_id_or_orderRef)`                            |
| `src/xenon/execution/brackets/rules/base.py`              | `RuleEvaluator` Protocol + `ArmResult` + `Decision` types + `RULE_REGISTRY`                                                   |
| `src/xenon/execution/brackets/rules/stop_loss.py`         | `StopLossRule` — stocks/long_options try native STP; combos return SYNTHETIC_ONLY                                             |
| `src/xenon/execution/brackets/rules/trailing_tp.py`       | `TrailingTpRule` — MFE tracking + activation gate                                                                             |
| `src/xenon/execution/brackets/rules/take_profit_fixed.py` | `TakeProfitFixedRule` — credit spreads close at 50% credit                                                                    |
| `src/xenon/execution/brackets/rules/combo_tp_alert.py`    | `ComboTpAlertRule` — lifts `_crossed` + notify path from `wizard_stop_monitor.py` (the original handler is deleted in Plan 3) |

### Created — Postgres layer

| Path                                                                         | Responsibility                                                                           |
| ---------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `src/xenon/db/migrations/versions/<rev>_add_position_rules_tables.py`        | Alembic migration A: 3 new tables + indexes + check constraints + 8 seed rows            |
| `src/xenon/db/queries/position_protection.py`                                | CRUD: `insert_pending_arm`, `cas_transition`, `list_active_rows`, `get_by_id`, etc.      |
| `src/xenon/db/queries/bracket_policies.py`                                   | `resolve_for_scope(scope, asset_class)` SQL helper using weighted ORDER BY               |
| `src/xenon/db/queries/position_close_claims.py`                              | `try_claim()`, `mark_submitted()`, `mark_terminal()`, `find_by_order_ref()`              |
| `src/xenon/execution/brackets/arm_hook.py`                                   | Outbox consumer: `on_fill_recorded(payload)` — classify → resolve → INSERT               |
| `scripts/tests/test_position_rules/__init__.py`                              | Package marker                                                                           |
| `scripts/tests/test_position_rules/test_asset_class.py`                      | classifier per asset class + manual leg-by-leg detection                                 |
| `scripts/tests/test_position_rules/test_position_key.py`                     | leg-order invariance for combos                                                          |
| `scripts/tests/test_position_rules/test_policies.py`                         | resolver unit + N-S1 regression case                                                     |
| `scripts/tests/test_position_rules/test_triggers.py`                         | per-rule_kind threshold logic                                                            |
| `scripts/tests/test_position_rules/test_close_claim.py`                      | derive_order_ref + retry-by-orderRef pure logic                                          |
| `scripts/tests/test_position_rules/test_state_machine.py`                    | every legal transition × illegal CAS rejection                                           |
| `scripts/tests/test_position_rules_db/__init__.py`                           | Package marker                                                                           |
| `scripts/tests/test_position_rules_db/test_migration.py`                     | Up/down clean; indexes via EXPLAIN; seed rows present                                    |
| `scripts/tests/test_position_rules_db/test_position_protection_queries.py`   | Insert PENDING_ARM, CAS transitions, partial-unique re-arm correctness (N-S2 regression) |
| `scripts/tests/test_position_rules_db/test_bracket_policies_queries.py`      | Most-specific-wins; account-specific beats broker-wide (N-S1 regression)                 |
| `scripts/tests/test_position_rules_db/test_position_close_claims_queries.py` | Concurrent claim contention (N-C1, N-C2); retry-by-orderRef idempotency (N-C3)           |
| `scripts/tests/test_position_rules_db/test_arm_hook.py`                      | Replay idempotency, DLQ on persistent failure, atomicity gate paths                      |

### Modified

| Path                     | Change                                                                                                                        |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------- |
| `src/xenon/db/schema.py` | **Add** `position_protection`, `bracket_policies`, `position_close_claims`. Do **not** drop `wizard_protection` yet (Plan 3). |
| `src/xenon/db/events.py` | Add `CHANNEL_POSITION_RULE_TRANSITION = "position_rule.transition"`; outbox DLQ helpers if not already present                |

---

## Task 1: Pure module — package skeleton + RuleEvaluator Protocol

**Files:**

- Create: `src/xenon/execution/brackets/__init__.py`
- Create: `src/xenon/execution/brackets/rules/__init__.py`
- Create: `src/xenon/execution/brackets/rules/base.py`

- [ ] **Step 1: Write the protocol + result types**

```python
# src/xenon/execution/brackets/rules/base.py
"""RuleEvaluator Protocol — every rule_kind plug-in implements this surface.

Spec §9. The handler dispatches `arm()` / `evaluate()` / `disarm()` per row;
nothing else may import broker SDKs or DB engines from rule modules — that
boundary is enforced by `scripts/checks/frozen_config_at_arm.py` in Plan 3.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Protocol


@dataclass(frozen=True)
class ArmResult:
    kind: Literal["NATIVE_ARMED", "SYNTHETIC_ONLY", "RETRY", "FAILED"]
    perm_id: int | None = None
    reason: str | None = None
    state_data_patch: dict[str, Any] | None = None


@dataclass(frozen=True)
class Decision:
    kind: Literal["NO_OP", "TRIGGERED", "UPDATE_STATE"]
    reason: str | None = None
    context: dict[str, Any] | None = None
    state_data_patch: dict[str, Any] | None = None


class RuleEvaluator(Protocol):
    rule_kind: ClassVar[str]

    def arm(self, scope, position, config, state_data) -> ArmResult: ...

    def evaluate(self, scope, position, config, state_data, marks) -> Decision: ...

    def disarm(self, scope, position, native_perm_id) -> None: ...


RULE_REGISTRY: dict[str, RuleEvaluator] = {}


def register(rule: RuleEvaluator) -> RuleEvaluator:
    RULE_REGISTRY[rule.rule_kind] = rule
    return rule
```

- [ ] **Step 2: Write `__init__.py` re-exports**

```python
# src/xenon/execution/brackets/__init__.py
"""Position-rules brackets package — broker-agnostic engine core."""
from xenon.execution.brackets.rules.base import (
    ArmResult,
    Decision,
    RULE_REGISTRY,
    RuleEvaluator,
    register,
)

__all__ = ["ArmResult", "Decision", "RULE_REGISTRY", "RuleEvaluator", "register"]
```

```python
# src/xenon/execution/brackets/rules/__init__.py
"""Rule plug-ins. Importing this package registers every concrete rule."""
from xenon.execution.brackets.rules import (  # noqa: F401 — side-effect imports register
    combo_tp_alert,
    stop_loss,
    take_profit_fixed,
    trailing_tp,
)
```

(The side-effect imports at the bottom will fail until Tasks 5–8 land. Wire `__init__.py` empty for now and update at the end of Task 8.)

- [ ] **Step 3: Smoke-import the package to verify**

```bash
uv run python -c "from xenon.execution.brackets.rules.base import RuleEvaluator, RULE_REGISTRY, ArmResult, Decision; print('ok')"
```

Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
git add src/xenon/execution/brackets/__init__.py src/xenon/execution/brackets/rules/base.py
git commit -m "feat(brackets): add RuleEvaluator Protocol skeleton"
```

---

## Task 2: Pure module — Pydantic configs

**Files:**

- Create: `src/xenon/execution/brackets/configs.py`
- Create: `scripts/tests/test_position_rules/__init__.py` (empty file)
- Create: `scripts/tests/test_position_rules/test_configs.py`

- [ ] **Step 1: Write the failing tests**

```python
# scripts/tests/test_position_rules/test_configs.py
"""Pydantic config model tests. Spec §5.5."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from xenon.execution.brackets.configs import (
    ComboTpAlertConfig,
    PositionDescriptor,
    StateData,
    StopLossConfig,
    TakeProfitFixedConfig,
    TrailingTpConfig,
    config_model_for,
)


def test_stop_loss_config_valid():
    cfg = StopLossConfig.model_validate({"threshold_pct": -0.20, "anchor": "entry_price"})
    assert cfg.threshold_pct == -0.20
    assert cfg.anchor == "entry_price"


def test_stop_loss_config_rejects_positive_threshold():
    with pytest.raises(ValidationError):
        StopLossConfig.model_validate({"threshold_pct": 0.20, "anchor": "entry_price"})


def test_trailing_tp_config_combo_shape():
    cfg = TrailingTpConfig.model_validate({
        "trail_pct": 0.25,
        "activation_pct_of_max_gain": 0.25,
        "anchor": "mfe_pnl_dollars",
    })
    assert cfg.trail_pct == 0.25
    assert cfg.activation_pct_of_max_gain == 0.25


def test_take_profit_fixed_credit_spread():
    cfg = TakeProfitFixedConfig.model_validate({
        "close_at_credit_pct": 0.50,
        "anchor": "synthetic_mark",
    })
    assert cfg.close_at_credit_pct == 0.50


def test_combo_tp_alert_config():
    cfg = ComboTpAlertConfig.model_validate({"threshold_pct": 0.50, "auto_place": False})
    assert cfg.auto_place is False


def test_position_descriptor_minimum():
    descriptor = PositionDescriptor.model_validate({
        "asset_class": "long_option",
        "opened_at": "2026-05-04T14:23:11Z",
        "source": "fastapi_orders_place",
        "anchor_price": 5.20,
        "anchor_currency": "USD",
        "opened_qty": 1,
        "protected_qty": 1,
        "multiplier": 100,
        "qty_unit": "contract",
        "legs": [{
            "sec_type": "OPT", "symbol": "GOOG", "expiry": "20260417",
            "strike": 315.0, "right": "C", "action": "BUY",
            "ratio": 1, "fill_price": 5.20, "con_id": 123456789,
        }],
    })
    assert descriptor.asset_class == "long_option"
    assert descriptor.legs[0].sec_type == "OPT"


def test_state_data_default_empty():
    sd = StateData.model_validate({})
    assert sd.consecutive_stale_ticks == 0
    assert sd.position_missing_ticks == 0
    assert sd.mfe is None


def test_config_model_for_dispatch():
    assert config_model_for("stop_loss") is StopLossConfig
    assert config_model_for("trailing_tp") is TrailingTpConfig
    assert config_model_for("take_profit_fixed") is TakeProfitFixedConfig
    assert config_model_for("combo_tp_alert") is ComboTpAlertConfig


def test_config_model_for_rejects_unknown():
    with pytest.raises(KeyError):
        config_model_for("super_secret_v2_kind")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest scripts/tests/test_position_rules/test_configs.py -xvs`
Expected: ImportError on `xenon.execution.brackets.configs`.

- [ ] **Step 3: Implement `configs.py`**

```python
# src/xenon/execution/brackets/configs.py
"""Pydantic shapes for the JSONB columns on position_protection.

Spec §5.5. Three families:
  - PositionDescriptor : frozen at insert; human-readable position shape
  - <Kind>Config       : per-rule_kind config — frozen at insert
  - StateData          : runtime mutable; MFE, retry counts, stale ticks
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ── PositionDescriptor ──────────────────────────────────────────────────────


class Leg(_Frozen):
    sec_type: Literal["STK", "OPT", "FUT"]
    symbol: str
    expiry: str | None = None
    strike: float | None = None
    right: Literal["C", "P"] | None = None
    action: Literal["BUY", "SELL"]
    ratio: int = 1
    fill_price: float
    con_id: int


class PositionDescriptor(_Frozen):
    asset_class: Literal["stock", "long_option", "debit_combo", "credit_spread", "covered_call", "unclassified"]
    opened_at: datetime
    opener_user_id: str | None = None
    source: Literal["fastapi_orders_place", "combo_wizard", "sweep_cli", "reconcile_discovered"]
    first_fill_id: int | None = None
    anchor_price: float
    anchor_currency: str
    opened_qty: int = Field(gt=0)
    protected_qty: int = Field(gt=0)
    multiplier: int = Field(gt=0)
    qty_unit: Literal["share", "contract", "spread"]
    legs: list[Leg]


# ── Per-rule_kind ConfigModels ───────────────────────────────────────────────


class StopLossConfig(_Frozen):
    threshold_pct: float | None = None
    threshold_pct_of_max_loss: float | None = None
    trigger_kind: Literal["mark_pct", "either"] | None = None
    mark_multiple_of_credit: float | None = None
    underlying_breach_short_strike: bool | None = None
    anchor: Literal["entry_price", "synthetic_mark"]

    @field_validator("threshold_pct")
    @classmethod
    def _stop_loss_must_be_negative(cls, v):
        if v is not None and v >= 0:
            raise ValueError("stop_loss threshold_pct must be negative (loss)")
        return v


class TrailingTpConfig(_Frozen):
    trail_pct: float = Field(gt=0)
    activation_pct: float | None = None
    activation_pct_of_max_gain: float | None = None
    anchor: Literal["mfe", "mfe_pnl_dollars"]


class TakeProfitFixedConfig(_Frozen):
    close_at_credit_pct: float = Field(gt=0, le=1)
    anchor: Literal["synthetic_mark"]


class ComboTpAlertConfig(_Frozen):
    threshold_pct: float
    auto_place: bool = False
    min_realert_interval_s: int = 3600


# ── StateData ────────────────────────────────────────────────────────────────


class StateData(BaseModel):
    model_config = ConfigDict(extra="allow")

    mfe: float | None = None
    last_mark: float | None = None
    last_mark_at: datetime | None = None
    consecutive_stale_ticks: int = 0
    position_missing_ticks: int = 0
    last_alert_at: datetime | None = None
    arm_attempts: int = 0
    last_arm_error: str | None = None
    partial_position_at_close: bool = False


# ── Dispatch ─────────────────────────────────────────────────────────────────


_CONFIG_MODELS: dict[str, type[_Frozen]] = {
    "stop_loss": StopLossConfig,
    "trailing_tp": TrailingTpConfig,
    "take_profit_fixed": TakeProfitFixedConfig,
    "combo_tp_alert": ComboTpAlertConfig,
}


def config_model_for(rule_kind: str) -> type[_Frozen]:
    return _CONFIG_MODELS[rule_kind]
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest scripts/tests/test_position_rules/test_configs.py -xvs`
Expected: 8 green.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/brackets/configs.py scripts/tests/test_position_rules/__init__.py scripts/tests/test_position_rules/test_configs.py
git commit -m "feat(brackets): add Pydantic configs for rule kinds + descriptor + state_data"
```

---

## Task 3: Pure module — `position_key`

**Files:**

- Create: `src/xenon/execution/brackets/position_key.py`
- Create: `scripts/tests/test_position_rules/test_position_key.py`

- [ ] **Step 1: Write the failing tests**

```python
# scripts/tests/test_position_rules/test_position_key.py
"""position_key encoding tests. Spec §5.3."""
from __future__ import annotations

from xenon.execution.brackets.position_key import compute_position_key


def _leg(sec_type, symbol, expiry=None, strike=None, right=None, action="BUY", ratio=1):
    return {
        "sec_type": sec_type, "symbol": symbol, "expiry": expiry,
        "strike": strike, "right": right, "action": action, "ratio": ratio,
        "fill_price": 1.0, "con_id": 0,
    }


def test_stock_key():
    key = compute_position_key("stock", {"legs": [_leg("STK", "AAPL")]})
    assert key == "STK::AAPL"


def test_long_option_key():
    key = compute_position_key("long_option", {"legs": [_leg("OPT", "GOOG", "20260417", 315.0, "C")]})
    assert key == "OPT::GOOG::20260417::315::C"


def test_credit_spread_key():
    legs = [
        _leg("OPT", "SPY", "20260516", 580.0, "P", action="SELL"),  # short put
        _leg("OPT", "SPY", "20260516", 575.0, "P", action="BUY"),   # long put
    ]
    key = compute_position_key("credit_spread", {"legs": legs})
    assert key == "CS::SPY::20260516::580::575::P"


def test_credit_spread_key_leg_order_invariant():
    legs_a = [
        _leg("OPT", "SPY", "20260516", 575.0, "P", action="BUY"),
        _leg("OPT", "SPY", "20260516", 580.0, "P", action="SELL"),
    ]
    legs_b = [
        _leg("OPT", "SPY", "20260516", 580.0, "P", action="SELL"),
        _leg("OPT", "SPY", "20260516", 575.0, "P", action="BUY"),
    ]
    assert compute_position_key("credit_spread", {"legs": legs_a}) == compute_position_key("credit_spread", {"legs": legs_b})


def test_debit_combo_key_hashed_and_leg_order_invariant():
    legs_a = [
        _leg("OPT", "TSLA", "20260516", 200.0, "C", action="BUY"),
        _leg("OPT", "TSLA", "20260516", 210.0, "C", action="SELL"),
    ]
    legs_b = list(reversed(legs_a))
    key_a = compute_position_key("debit_combo", {"legs": legs_a})
    key_b = compute_position_key("debit_combo", {"legs": legs_b})
    assert key_a == key_b
    assert key_a.startswith("COMBO::")
    assert key_a.endswith("::TSLA")


def test_covered_call_key():
    legs = [
        _leg("STK", "AAPL", action="BUY"),
        _leg("OPT", "AAPL", "20260620", 200.0, "C", action="SELL"),
    ]
    key = compute_position_key("covered_call", {"legs": legs})
    assert key == "CC::AAPL::20260620::200"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest scripts/tests/test_position_rules/test_position_key.py -xvs`

- [ ] **Step 3: Implement `position_key.py`**

```python
# src/xenon/execution/brackets/position_key.py
"""Opaque deterministic position key. Spec §5.3.

Canonical leg ordering for hashes: sort by (right, strike, expiry, action).
Prevents key drift from leg-order permutations.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def _format_strike(s: float) -> str:
    """Strip trailing zeros: 315.0 -> '315', 580.5 -> '580.5'."""
    if s == int(s):
        return str(int(s))
    return str(s).rstrip("0").rstrip(".")


def _canonicalize_legs(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort legs by (right, strike, expiry, action) for stable hashing."""
    def sort_key(leg):
        return (
            leg.get("right") or "",
            leg.get("strike") or 0.0,
            leg.get("expiry") or "",
            leg.get("action") or "",
        )
    return sorted(legs, key=sort_key)


def compute_position_key(asset_class: str, descriptor: dict[str, Any]) -> str:
    legs = list(descriptor.get("legs") or [])
    if not legs:
        raise ValueError("descriptor.legs is required to compute position_key")

    if asset_class == "stock":
        return f"STK::{legs[0]['symbol']}"

    if asset_class == "long_option":
        leg = legs[0]
        return (
            f"OPT::{leg['symbol']}::{leg['expiry']}::"
            f"{_format_strike(leg['strike'])}::{leg['right']}"
        )

    if asset_class == "covered_call":
        call = next((l for l in legs if l.get("right") == "C" and l.get("action") == "SELL"), None)
        if call is None:
            raise ValueError("covered_call descriptor missing short call leg")
        return f"CC::{call['symbol']}::{call['expiry']}::{_format_strike(call['strike'])}"

    if asset_class == "credit_spread":
        # Two legs, same expiry, same right; the SELL is the "short" strike.
        short = next((l for l in legs if l.get("action") == "SELL"), None)
        long_ = next((l for l in legs if l.get("action") == "BUY"), None)
        if short is None or long_ is None:
            raise ValueError("credit_spread requires one SELL leg and one BUY leg")
        return (
            f"CS::{short['symbol']}::{short['expiry']}::"
            f"{_format_strike(short['strike'])}::{_format_strike(long_['strike'])}::{short['right']}"
        )

    if asset_class == "debit_combo":
        canon = _canonicalize_legs(legs)
        # Hash the canonicalized JSON of legs (only the structure-defining fields)
        slim = [
            {k: leg.get(k) for k in ("sec_type", "symbol", "expiry", "strike", "right", "action", "ratio")}
            for leg in canon
        ]
        digest = hashlib.sha256(
            json.dumps(slim, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        first_symbol = canon[0]["symbol"]
        return f"COMBO::{digest}::{first_symbol}"

    if asset_class == "unclassified":
        # Should never be inserted, but encode for completeness
        return f"UNCL::{legs[0].get('symbol', 'UNKNOWN')}"

    raise ValueError(f"Unknown asset_class: {asset_class!r}")
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest scripts/tests/test_position_rules/test_position_key.py -xvs`
Expected: 6 green.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/brackets/position_key.py scripts/tests/test_position_rules/test_position_key.py
git commit -m "feat(brackets): add deterministic position_key encoder"
```

---

## Task 4: Pure module — asset-class classifier

**Files:**

- Create: `src/xenon/execution/brackets/asset_class.py`
- Create: `scripts/tests/test_position_rules/test_asset_class.py`

- [ ] **Step 1: Write the failing tests**

```python
# scripts/tests/test_position_rules/test_asset_class.py
"""Asset-class classifier tests. Spec §3.1, §6.2."""
from __future__ import annotations

from xenon.execution.brackets.asset_class import (
    AssetClass,
    ClassifyResult,
    classify_position,
)


def _stk(symbol, action="BUY"):
    return {"sec_type": "STK", "symbol": symbol, "action": action, "ratio": 1, "fill_price": 100.0, "con_id": 0}


def _opt(symbol, expiry, strike, right, action="BUY", ratio=1):
    return {"sec_type": "OPT", "symbol": symbol, "expiry": expiry, "strike": strike,
            "right": right, "action": action, "ratio": ratio, "fill_price": 5.0, "con_id": 0}


def test_stock_long():
    result = classify_position(legs=[_stk("AAPL")], wizard_session_payload=None, sibling_legs=None)
    assert result.asset_class == AssetClass.STOCK


def test_long_option():
    result = classify_position(legs=[_opt("GOOG", "20260417", 315.0, "C")], wizard_session_payload=None, sibling_legs=None)
    assert result.asset_class == AssetClass.LONG_OPTION


def test_credit_spread_short_put():
    legs = [
        _opt("SPY", "20260516", 580.0, "P", action="SELL"),
        _opt("SPY", "20260516", 575.0, "P", action="BUY"),
    ]
    result = classify_position(legs=legs, wizard_session_payload=None, sibling_legs=None)
    assert result.asset_class == AssetClass.CREDIT_SPREAD


def test_credit_spread_short_call():
    legs = [
        _opt("SPY", "20260516", 590.0, "C", action="SELL"),
        _opt("SPY", "20260516", 595.0, "C", action="BUY"),
    ]
    result = classify_position(legs=legs, wizard_session_payload=None, sibling_legs=None)
    assert result.asset_class == AssetClass.CREDIT_SPREAD


def test_debit_combo_call_vertical():
    legs = [
        _opt("TSLA", "20260516", 200.0, "C", action="BUY"),
        _opt("TSLA", "20260516", 210.0, "C", action="SELL"),
    ]
    result = classify_position(legs=legs, wizard_session_payload=None, sibling_legs=None)
    assert result.asset_class == AssetClass.DEBIT_COMBO


def test_covered_call_pattern():
    legs = [
        _stk("AAPL"),
        _opt("AAPL", "20260620", 200.0, "C", action="SELL"),
    ]
    result = classify_position(legs=legs, wizard_session_payload=None, sibling_legs=None)
    assert result.asset_class == AssetClass.COVERED_CALL


def test_jade_lizard_unclassified():
    legs = [
        _opt("TSLA", "20260516", 210.0, "C", action="BUY"),
        _opt("TSLA", "20260516", 220.0, "C", action="SELL"),
        _opt("TSLA", "20260516", 180.0, "P", action="SELL"),
    ]
    result = classify_position(legs=legs, wizard_session_payload=None, sibling_legs=None)
    assert result.asset_class == AssetClass.UNCLASSIFIED


def test_wizard_session_overrides_pattern_match():
    """When a combo wizard session_id is present, defer to its declared structure."""
    legs = [_opt("GOOG", "20260417", 315.0, "C")]
    payload = {"asset_class": "debit_combo"}  # wizard says combo even though one leg
    result = classify_position(legs=legs, wizard_session_payload=payload, sibling_legs=None)
    assert result.asset_class == AssetClass.DEBIT_COMBO


def test_manual_leg_by_leg_detection_returns_unclassified():
    """Single-leg fill with sibling fills at same scope+symbol+expiry → unsupported."""
    legs = [_opt("SPY", "20260516", 580.0, "P", action="SELL")]
    sibling_legs = [_opt("SPY", "20260516", 575.0, "P", action="BUY")]
    result = classify_position(legs=legs, wizard_session_payload=None, sibling_legs=sibling_legs)
    assert result.asset_class == AssetClass.UNCLASSIFIED
    assert result.reason == "manual_multi_leg_unsupported"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest scripts/tests/test_position_rules/test_asset_class.py -xvs`

- [ ] **Step 3: Implement `asset_class.py`**

```python
# src/xenon/execution/brackets/asset_class.py
"""Asset-class classifier. Spec §6.2.

The classifier reads the *full position context* — not just the single fill —
because a short-call fill alone is ambiguous (covered call vs credit spread vs
ratio). For combo-wizard fills the caller passes the wizard session payload and
we defer to its declared structure. For single-leg fills the caller passes any
sibling fills detected within `manual_assembly_window_s`; presence of siblings
flags the manual leg-by-leg unsupported case.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class AssetClass(StrEnum):
    STOCK = "stock"
    LONG_OPTION = "long_option"
    DEBIT_COMBO = "debit_combo"
    CREDIT_SPREAD = "credit_spread"
    COVERED_CALL = "covered_call"
    UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class ClassifyResult:
    asset_class: AssetClass
    reason: str | None = None  # populated for UNCLASSIFIED narrowings


def _is_credit_spread(legs: list[dict[str, Any]]) -> bool:
    if len(legs) != 2:
        return False
    a, b = legs
    if a.get("sec_type") != "OPT" or b.get("sec_type") != "OPT":
        return False
    if a.get("symbol") != b.get("symbol"):
        return False
    if a.get("expiry") != b.get("expiry"):
        return False
    if a.get("right") != b.get("right"):
        return False
    actions = {a.get("action"), b.get("action")}
    return actions == {"BUY", "SELL"}


def _is_credit(legs: list[dict[str, Any]]) -> bool:
    """Net credit: short strike has higher premium than long strike (puts: short higher strike; calls: short lower strike)."""
    short = next((l for l in legs if l.get("action") == "SELL"), None)
    long_ = next((l for l in legs if l.get("action") == "BUY"), None)
    if not short or not long_:
        return False
    if short["right"] == "P":
        return short["strike"] > long_["strike"]
    if short["right"] == "C":
        return short["strike"] < long_["strike"]
    return False


def _is_debit_combo(legs: list[dict[str, Any]]) -> bool:
    if len(legs) < 2:
        return False
    if not all(l.get("sec_type") == "OPT" for l in legs):
        return False
    if len(legs) == 2 and _is_credit_spread(legs) and _is_credit(legs):
        return False
    return True if len(legs) == 2 else False


def _is_covered_call(legs: list[dict[str, Any]]) -> bool:
    if len(legs) != 2:
        return False
    stock = next((l for l in legs if l.get("sec_type") == "STK" and l.get("action") == "BUY"), None)
    short_call = next((l for l in legs if l.get("sec_type") == "OPT" and l.get("action") == "SELL" and l.get("right") == "C"), None)
    if not stock or not short_call:
        return False
    return stock["symbol"] == short_call["symbol"]


def classify_position(
    *,
    legs: list[dict[str, Any]],
    wizard_session_payload: dict[str, Any] | None,
    sibling_legs: list[dict[str, Any]] | None,
) -> ClassifyResult:
    if wizard_session_payload is not None:
        ac = wizard_session_payload.get("asset_class")
        if ac in {a.value for a in AssetClass}:
            return ClassifyResult(asset_class=AssetClass(ac))

    if not legs:
        return ClassifyResult(asset_class=AssetClass.UNCLASSIFIED, reason="empty_legs")

    if len(legs) == 1:
        if sibling_legs:
            return ClassifyResult(asset_class=AssetClass.UNCLASSIFIED, reason="manual_multi_leg_unsupported")
        leg = legs[0]
        if leg.get("sec_type") == "STK":
            return ClassifyResult(asset_class=AssetClass.STOCK)
        if leg.get("sec_type") == "OPT" and leg.get("action") == "BUY":
            return ClassifyResult(asset_class=AssetClass.LONG_OPTION)
        return ClassifyResult(asset_class=AssetClass.UNCLASSIFIED, reason="single_leg_unsupported_shape")

    if _is_covered_call(legs):
        return ClassifyResult(asset_class=AssetClass.COVERED_CALL)

    if _is_credit_spread(legs) and _is_credit(legs):
        return ClassifyResult(asset_class=AssetClass.CREDIT_SPREAD)

    if _is_debit_combo(legs):
        return ClassifyResult(asset_class=AssetClass.DEBIT_COMBO)

    return ClassifyResult(asset_class=AssetClass.UNCLASSIFIED, reason="unrecognized_structure")
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest scripts/tests/test_position_rules/test_asset_class.py -xvs`
Expected: 9 green.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/brackets/asset_class.py scripts/tests/test_position_rules/test_asset_class.py
git commit -m "feat(brackets): add asset-class classifier with wizard + sibling-leg awareness"
```

---

## Task 5: Pure module — triggers

**Files:**

- Create: `src/xenon/execution/brackets/triggers.py`
- Create: `scripts/tests/test_position_rules/test_triggers.py`

- [ ] **Step 1: Write the failing tests**

```python
# scripts/tests/test_position_rules/test_triggers.py
"""Pure trigger-evaluation tests. Spec §3.1, §9."""
from __future__ import annotations

from xenon.execution.brackets.triggers import (
    apply_trail_after_activation,
    debit_to_close_at_credit_pct,
    mfe_update,
    pct_change,
    threshold_crossed_below,
)


def test_pct_change_basic():
    assert pct_change(current=90.0, anchor=100.0) == -0.10


def test_threshold_crossed_below():
    assert threshold_crossed_below(mark=4.0, threshold=5.0) is True
    assert threshold_crossed_below(mark=5.0, threshold=5.0) is True
    assert threshold_crossed_below(mark=5.01, threshold=5.0) is False


def test_mfe_update_increases_only():
    assert mfe_update(current_mfe=10.0, current_mark=11.0) == 11.0
    assert mfe_update(current_mfe=10.0, current_mark=9.0) == 10.0
    assert mfe_update(current_mfe=None, current_mark=8.0) == 8.0


def test_apply_trail_after_activation_inactive():
    """Mark below activation → no trigger, mfe still tracked."""
    fired, new_mfe = apply_trail_after_activation(
        anchor_price=10.0, current_mark=11.0, current_mfe=11.0, trail_pct=0.25, activation_pct=0.30
    )
    assert fired is False
    assert new_mfe == 11.0


def test_apply_trail_after_activation_active_trail_held():
    """Mark above activation but within trail → no trigger."""
    # anchor=10, activation_pct=0.30 → activated when mark ≥ 13
    fired, new_mfe = apply_trail_after_activation(
        anchor_price=10.0, current_mark=13.5, current_mfe=14.0, trail_pct=0.25, activation_pct=0.30
    )
    # 14.0 * (1 - 0.25) = 10.5; mark 13.5 > 10.5 → not fired
    assert fired is False
    assert new_mfe == 14.0


def test_apply_trail_after_activation_fires():
    """Once activated, mark dropped > trail_pct from MFE → fire."""
    fired, new_mfe = apply_trail_after_activation(
        anchor_price=10.0, current_mark=10.4, current_mfe=14.0, trail_pct=0.25, activation_pct=0.30
    )
    # Activated (mfe 14 ≥ anchor*(1+0.30)=13). Trail level = 14*0.75 = 10.5; mark 10.4 < 10.5 → fires.
    assert fired is True


def test_debit_to_close_at_credit_pct():
    """Credit spread: collected $1.00 credit, close at 50% → close when debit_to_close ≤ $0.50."""
    assert debit_to_close_at_credit_pct(debit_to_close=0.50, credit_received=1.00, close_at_credit_pct=0.50) is True
    assert debit_to_close_at_credit_pct(debit_to_close=0.51, credit_received=1.00, close_at_credit_pct=0.50) is False
    assert debit_to_close_at_credit_pct(debit_to_close=0.40, credit_received=1.00, close_at_credit_pct=0.50) is True
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest scripts/tests/test_position_rules/test_triggers.py -xvs`

- [ ] **Step 3: Implement `triggers.py`**

```python
# src/xenon/execution/brackets/triggers.py
"""Pure trigger arithmetic. No I/O, no DB. Spec §9.

Functions consumed by rule modules. Kept side-effect-free so the AST guard
`scripts/checks/frozen_config_at_arm.py` (Plan 3) can pin "no DB reads from
rules/" without false positives.
"""
from __future__ import annotations


def pct_change(*, current: float, anchor: float) -> float:
    if anchor == 0:
        raise ValueError("anchor cannot be zero")
    return (current - anchor) / anchor


def threshold_crossed_below(*, mark: float, threshold: float) -> bool:
    return mark <= threshold


def mfe_update(*, current_mfe: float | None, current_mark: float) -> float:
    if current_mfe is None:
        return current_mark
    return max(current_mfe, current_mark)


def apply_trail_after_activation(
    *,
    anchor_price: float,
    current_mark: float,
    current_mfe: float | None,
    trail_pct: float,
    activation_pct: float,
) -> tuple[bool, float]:
    """Returns (fired, new_mfe).

    Activation: MFE must reach `anchor_price * (1 + activation_pct)` once. Until
    then, no fire — but MFE is still tracked so we know when activation kicks in.

    Trail: once activated, fire when `current_mark < new_mfe * (1 - trail_pct)`.
    """
    new_mfe = mfe_update(current_mfe=current_mfe, current_mark=current_mark)
    activation_level = anchor_price * (1 + activation_pct)
    if new_mfe < activation_level:
        return False, new_mfe
    trail_level = new_mfe * (1 - trail_pct)
    fired = current_mark < trail_level
    return fired, new_mfe


def debit_to_close_at_credit_pct(
    *,
    debit_to_close: float,
    credit_received: float,
    close_at_credit_pct: float,
) -> bool:
    """Credit-spread fixed TP. Spec §3.1.

    Close when `debit_to_close ≤ (1 - close_at_credit_pct) × credit_received`.
    Example: collected $1.00 credit, close at 50% → close when buy-back ≤ $0.50.
    """
    return debit_to_close <= (1 - close_at_credit_pct) * credit_received
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest scripts/tests/test_position_rules/test_triggers.py -xvs`
Expected: 7 green.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/brackets/triggers.py scripts/tests/test_position_rules/test_triggers.py
git commit -m "feat(brackets): add pure trigger arithmetic helpers"
```

---

## Task 6: Pure module — close-claim logic

**Files:**

- Create: `src/xenon/execution/brackets/close_claim.py`
- Create: `scripts/tests/test_position_rules/test_close_claim.py`

- [ ] **Step 1: Write the failing tests**

```python
# scripts/tests/test_position_rules/test_close_claim.py
"""Pure close-claim logic tests. Spec §5.6, §10.2."""
from __future__ import annotations

import pytest

from xenon.execution.brackets.close_claim import (
    derive_order_ref,
    parse_order_ref_claim_id,
    should_skip_resubmit,
)


def test_derive_order_ref():
    assert derive_order_ref(claim_id=42) == "xenon-pr-42"
    assert derive_order_ref(claim_id=1_000_000) == "xenon-pr-1000000"


def test_parse_order_ref_claim_id():
    assert parse_order_ref_claim_id("xenon-pr-42") == 42
    assert parse_order_ref_claim_id("xenon-pr-1000000") == 1_000_000


def test_parse_order_ref_rejects_other_prefixes():
    with pytest.raises(ValueError):
        parse_order_ref_claim_id("xenon-cancel-42")
    with pytest.raises(ValueError):
        parse_order_ref_claim_id("xenon-pr-abc")


def test_should_skip_resubmit_when_open_order_with_orderref():
    """N-C3 retry idempotency."""
    open_orders = [{"orderRef": "xenon-pr-42", "permId": 12345}]
    skip, perm_id = should_skip_resubmit(order_ref="xenon-pr-42", open_orders=open_orders, executions=[])
    assert skip is True
    assert perm_id == 12345


def test_should_skip_resubmit_when_execution_with_orderref():
    """Order already filled at broker; retry must not double-submit."""
    executions = [{"orderRef": "xenon-pr-42", "permId": 99999}]
    skip, perm_id = should_skip_resubmit(order_ref="xenon-pr-42", open_orders=[], executions=executions)
    assert skip is True
    assert perm_id == 99999


def test_should_resubmit_when_no_match():
    skip, perm_id = should_skip_resubmit(order_ref="xenon-pr-42", open_orders=[], executions=[])
    assert skip is False
    assert perm_id is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest scripts/tests/test_position_rules/test_close_claim.py -xvs`

- [ ] **Step 3: Implement `close_claim.py`**

```python
# src/xenon/execution/brackets/close_claim.py
"""Close-claim helpers. Spec §5.6.

Pure logic: order_ref derivation and the orderRef-first lookup that makes
subprocess retries idempotent (codex N-C3 fix).
"""
from __future__ import annotations

from typing import Any

_ORDER_REF_PREFIX = "xenon-pr-"


def derive_order_ref(*, claim_id: int) -> str:
    return f"{_ORDER_REF_PREFIX}{claim_id}"


def parse_order_ref_claim_id(order_ref: str) -> int:
    if not order_ref.startswith(_ORDER_REF_PREFIX):
        raise ValueError(f"order_ref {order_ref!r} does not have prefix {_ORDER_REF_PREFIX!r}")
    suffix = order_ref[len(_ORDER_REF_PREFIX):]
    try:
        return int(suffix)
    except ValueError as e:
        raise ValueError(f"order_ref {order_ref!r} has non-integer suffix") from e


def should_skip_resubmit(
    *,
    order_ref: str,
    open_orders: list[dict[str, Any]],
    executions: list[dict[str, Any]],
) -> tuple[bool, int | None]:
    """Return (skip, perm_id). Spec §5.6 step 3 idempotent retry.

    If an open order or execution with the deterministic `orderRef` already
    exists at the broker, the retry must NOT submit another MKT — it attaches
    the existing perm_id and waits for reconcile.
    """
    for o in open_orders:
        if o.get("orderRef") == order_ref:
            return True, o.get("permId")
    for e in executions:
        if e.get("orderRef") == order_ref:
            return True, e.get("permId")
    return False, None
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest scripts/tests/test_position_rules/test_close_claim.py -xvs`
Expected: 6 green.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/brackets/close_claim.py scripts/tests/test_position_rules/test_close_claim.py
git commit -m "feat(brackets): add close-claim helpers (orderRef derivation + retry-skip)"
```

---

## Task 7: Pure module — rule plug-ins (4 modules)

**Files:**

- Create: `src/xenon/execution/brackets/rules/stop_loss.py`
- Create: `src/xenon/execution/brackets/rules/trailing_tp.py`
- Create: `src/xenon/execution/brackets/rules/take_profit_fixed.py`
- Create: `src/xenon/execution/brackets/rules/combo_tp_alert.py`
- Create: `scripts/tests/test_position_rules/test_rules.py`

The four rules share the same I/O contract (`RuleEvaluator`) but differ in `arm()` semantics (some try native STP, some are synthetic-only) and `evaluate()` math. We test them as pure functions — no broker, no DB. Native arming itself is exercised in Plan 3 (subprocess executor).

- [ ] **Step 1: Write the failing tests**

```python
# scripts/tests/test_position_rules/test_rules.py
"""Per-rule_kind arm + evaluate tests (synthetic-only paths). Spec §9.1.

Native-arm path (subprocess to xenon-ib-place-order) is exercised in Plan 3.
"""
from __future__ import annotations

from datetime import datetime, timezone

from xenon.execution.brackets.rules.base import RULE_REGISTRY
from xenon.execution.brackets.rules.combo_tp_alert import ComboTpAlertRule
from xenon.execution.brackets.rules.stop_loss import StopLossRule
from xenon.execution.brackets.rules.take_profit_fixed import TakeProfitFixedRule
from xenon.execution.brackets.rules.trailing_tp import TrailingTpRule


# A position is a tiny dict with `asset_class`, `anchor_price`, and `marks` is
# whatever the handler hands the rule. Rules MUST treat these as opaque dicts.

def _pos(asset_class, anchor_price, **kw):
    return {"asset_class": asset_class, "anchor_price": anchor_price, **kw}


def test_registry_populated():
    assert "stop_loss" in RULE_REGISTRY
    assert "trailing_tp" in RULE_REGISTRY
    assert "take_profit_fixed" in RULE_REGISTRY
    assert "combo_tp_alert" in RULE_REGISTRY


# ── stop_loss ──────────────────────────────────────────────────────────


def test_stop_loss_arm_combo_returns_synthetic_only():
    rule = StopLossRule()
    result = rule.arm(scope=None, position=_pos("debit_combo", 1.50), config={"threshold_pct_of_max_loss": 0.50, "anchor": "synthetic_mark"}, state_data={})
    assert result.kind == "SYNTHETIC_ONLY"


def test_stop_loss_evaluate_below_threshold_fires():
    rule = StopLossRule()
    config = {"threshold_pct": -0.20, "anchor": "entry_price"}
    decision = rule.evaluate(scope=None, position=_pos("long_option", 5.00), config=config, state_data={}, marks={"mark": 3.99})
    assert decision.kind == "TRIGGERED"


def test_stop_loss_evaluate_above_threshold_no_op():
    rule = StopLossRule()
    config = {"threshold_pct": -0.20, "anchor": "entry_price"}
    decision = rule.evaluate(scope=None, position=_pos("long_option", 5.00), config=config, state_data={}, marks={"mark": 4.01})
    assert decision.kind == "NO_OP"


# ── trailing_tp ────────────────────────────────────────────────────────


def test_trailing_tp_evaluate_below_activation_no_op_but_updates_mfe():
    rule = TrailingTpRule()
    config = {"trail_pct": 0.25, "activation_pct": 0.30, "anchor": "mfe"}
    decision = rule.evaluate(scope=None, position=_pos("long_option", 5.00), config=config, state_data={"mfe": None}, marks={"mark": 5.50})
    assert decision.kind == "UPDATE_STATE"
    assert decision.state_data_patch["mfe"] == 5.50


def test_trailing_tp_evaluate_after_activation_fires_on_drop():
    rule = TrailingTpRule()
    config = {"trail_pct": 0.25, "activation_pct": 0.30, "anchor": "mfe"}
    state_data = {"mfe": 7.00}  # mfe ≥ 5*(1+0.30)=6.50 → activated
    decision = rule.evaluate(scope=None, position=_pos("long_option", 5.00), config=config, state_data=state_data, marks={"mark": 5.20})
    # trail level = 7 * 0.75 = 5.25; mark 5.20 < 5.25 → fires
    assert decision.kind == "TRIGGERED"


# ── take_profit_fixed (credit spread) ──────────────────────────────────


def test_take_profit_fixed_credit_spread_fires():
    rule = TakeProfitFixedRule()
    config = {"close_at_credit_pct": 0.50, "anchor": "synthetic_mark"}
    pos = _pos("credit_spread", 1.00, credit_received=1.00)
    decision = rule.evaluate(scope=None, position=pos, config=config, state_data={}, marks={"debit_to_close": 0.40})
    assert decision.kind == "TRIGGERED"


def test_take_profit_fixed_credit_spread_no_op():
    rule = TakeProfitFixedRule()
    config = {"close_at_credit_pct": 0.50, "anchor": "synthetic_mark"}
    pos = _pos("credit_spread", 1.00, credit_received=1.00)
    decision = rule.evaluate(scope=None, position=pos, config=config, state_data={}, marks={"debit_to_close": 0.51})
    assert decision.kind == "NO_OP"


# ── combo_tp_alert (alert-only with debounce) ──────────────────────────


def test_combo_tp_alert_first_crossing_fires():
    rule = ComboTpAlertRule()
    config = {"threshold_pct": 0.50, "auto_place": False, "min_realert_interval_s": 3600}
    decision = rule.evaluate(
        scope=None,
        position=_pos("debit_combo", 1.50),
        config=config,
        state_data={"last_alert_at": None},
        marks={"mark": 2.30, "now": datetime(2026, 5, 4, 14, 30, tzinfo=timezone.utc)},
    )
    assert decision.kind == "TRIGGERED"
    assert decision.context["alert_only"] is True


def test_combo_tp_alert_second_crossing_within_debounce_no_op():
    rule = ComboTpAlertRule()
    config = {"threshold_pct": 0.50, "auto_place": False, "min_realert_interval_s": 3600}
    last = datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc).isoformat()
    decision = rule.evaluate(
        scope=None,
        position=_pos("debit_combo", 1.50),
        config=config,
        state_data={"last_alert_at": last},
        marks={"mark": 2.30, "now": datetime(2026, 5, 4, 14, 30, tzinfo=timezone.utc)},
    )
    assert decision.kind == "NO_OP"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest scripts/tests/test_position_rules/test_rules.py -xvs`

- [ ] **Step 3: Implement the four rule modules**

```python
# src/xenon/execution/brackets/rules/stop_loss.py
"""StopLoss rule. Spec §3.1, §9.1.

For stocks + long_options the handler delegates native STP arming to the
subprocess executor (Plan 3). Combos return SYNTHETIC_ONLY because IB does not
support native brackets on BAG combo orders.
"""
from __future__ import annotations

from typing import Any

from xenon.execution.brackets.rules.base import ArmResult, Decision, register
from xenon.execution.brackets.triggers import threshold_crossed_below


class StopLossRule:
    rule_kind = "stop_loss"

    def arm(self, *, scope, position, config, state_data) -> ArmResult:
        ac = position.get("asset_class")
        if ac in ("debit_combo", "credit_spread"):
            return ArmResult(kind="SYNTHETIC_ONLY", reason="bag_combo_no_native_bracket")
        # Stocks + long_options would attach a native STP via subprocess in Plan 3.
        # In v1 pure-tests we surface this as an explicit RETRY so the handler
        # picks it up; the subprocess path is wired in Plan 3 Task 4.
        return ArmResult(kind="RETRY", reason="needs_subprocess_executor")

    def evaluate(self, *, scope, position, config, state_data, marks) -> Decision:
        anchor = position["anchor_price"]
        ac = position.get("asset_class")
        mark = marks.get("mark")
        if mark is None:
            return Decision(kind="NO_OP", reason="missing_mark")
        if ac in ("stock", "long_option"):
            threshold = anchor * (1 + config["threshold_pct"])
            if threshold_crossed_below(mark=mark, threshold=threshold):
                return Decision(kind="TRIGGERED", reason="mark_below_threshold",
                                context={"mark": mark, "threshold": threshold})
            return Decision(kind="NO_OP")
        if ac == "credit_spread":
            # Either: ① debit-to-close ≥ 2× credit_received, or ② underlying touches short strike
            debit = marks.get("debit_to_close", 0.0)
            credit = position.get("credit_received", 0.0)
            multiplier = config.get("mark_multiple_of_credit", 2.0)
            spot = marks.get("underlying_spot")
            short_strike = position.get("short_strike")
            short_right = position.get("short_right")
            if credit and debit >= multiplier * credit:
                return Decision(kind="TRIGGERED", reason="debit_breach",
                                context={"debit_to_close": debit, "credit": credit, "multiple": multiplier})
            if config.get("underlying_breach_short_strike") and spot is not None and short_strike is not None:
                breached = (spot <= short_strike) if short_right == "P" else (spot >= short_strike)
                if breached:
                    return Decision(kind="TRIGGERED", reason="underlying_breach_short_strike",
                                    context={"spot": spot, "short_strike": short_strike})
            return Decision(kind="NO_OP")
        if ac == "debit_combo":
            # threshold_pct_of_max_loss expects max_loss in position (anchor_price for combos = net debit)
            net_debit = anchor
            max_loss = net_debit  # for vertical debit combo, max loss = debit paid
            threshold = max_loss * (1 - config["threshold_pct_of_max_loss"])
            # mark on a combo is the synthetic_mark; loss = anchor - mark; trigger when remaining ≤ threshold
            remaining = mark
            if remaining <= threshold:
                return Decision(kind="TRIGGERED", reason="combo_synthetic_below_threshold",
                                context={"mark": mark, "threshold": threshold})
            return Decision(kind="NO_OP")
        return Decision(kind="NO_OP", reason="unsupported_asset_class")

    def disarm(self, *, scope, position, native_perm_id) -> None:
        # Real cancel happens in Plan 3 via xenon-ib-order-manage.
        return None


register(StopLossRule())
```

```python
# src/xenon/execution/brackets/rules/trailing_tp.py
"""Trailing take-profit. Spec §3.1, §9.1."""
from __future__ import annotations

from xenon.execution.brackets.rules.base import ArmResult, Decision, register
from xenon.execution.brackets.triggers import apply_trail_after_activation, mfe_update


class TrailingTpRule:
    rule_kind = "trailing_tp"

    def arm(self, *, scope, position, config, state_data) -> ArmResult:
        ac = position.get("asset_class")
        if ac in ("debit_combo", "credit_spread"):
            return ArmResult(kind="SYNTHETIC_ONLY")
        return ArmResult(kind="RETRY", reason="needs_subprocess_executor")

    def evaluate(self, *, scope, position, config, state_data, marks) -> Decision:
        mark = marks.get("mark")
        if mark is None:
            return Decision(kind="NO_OP", reason="missing_mark")
        anchor = position["anchor_price"]
        activation_pct = config.get("activation_pct") or config.get("activation_pct_of_max_gain") or 0.0
        trail_pct = config["trail_pct"]
        current_mfe = state_data.get("mfe")

        fired, new_mfe = apply_trail_after_activation(
            anchor_price=anchor,
            current_mark=mark,
            current_mfe=current_mfe,
            trail_pct=trail_pct,
            activation_pct=activation_pct,
        )
        if fired:
            return Decision(kind="TRIGGERED", reason="trail_breached",
                            context={"mark": mark, "mfe": new_mfe, "trail_pct": trail_pct})
        if new_mfe != current_mfe:
            return Decision(kind="UPDATE_STATE", state_data_patch={"mfe": new_mfe})
        return Decision(kind="NO_OP")

    def disarm(self, *, scope, position, native_perm_id) -> None:
        return None


register(TrailingTpRule())
```

```python
# src/xenon/execution/brackets/rules/take_profit_fixed.py
"""Fixed take-profit (credit spreads only in v1). Spec §3.1, §9.1."""
from __future__ import annotations

from xenon.execution.brackets.rules.base import ArmResult, Decision, register
from xenon.execution.brackets.triggers import debit_to_close_at_credit_pct


class TakeProfitFixedRule:
    rule_kind = "take_profit_fixed"

    def arm(self, *, scope, position, config, state_data) -> ArmResult:
        # No native bracket on BAG combos.
        return ArmResult(kind="SYNTHETIC_ONLY")

    def evaluate(self, *, scope, position, config, state_data, marks) -> Decision:
        debit = marks.get("debit_to_close")
        credit = position.get("credit_received")
        if debit is None or credit is None:
            return Decision(kind="NO_OP", reason="missing_marks")
        if debit_to_close_at_credit_pct(
            debit_to_close=debit,
            credit_received=credit,
            close_at_credit_pct=config["close_at_credit_pct"],
        ):
            return Decision(kind="TRIGGERED", reason="credit_spread_tp_hit",
                            context={"debit_to_close": debit, "credit": credit})
        return Decision(kind="NO_OP")

    def disarm(self, *, scope, position, native_perm_id) -> None:
        return None


register(TakeProfitFixedRule())
```

```python
# src/xenon/execution/brackets/rules/combo_tp_alert.py
"""Alert-only combo TP threshold crossing. Spec §3.3, §9.1.

Lifts the `_crossed` + notify path from `wizard_stop_monitor.py`. The original
handler is deleted in Plan 3; this rule replaces it via the unified
PositionRulesHandler dispatch on rule_kind.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from xenon.execution.brackets.rules.base import ArmResult, Decision, register


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class ComboTpAlertRule:
    rule_kind = "combo_tp_alert"

    def arm(self, *, scope, position, config, state_data) -> ArmResult:
        return ArmResult(kind="SYNTHETIC_ONLY")  # alert-only — never attaches broker order

    def evaluate(self, *, scope, position, config, state_data, marks) -> Decision:
        mark = marks.get("mark")
        if mark is None:
            return Decision(kind="NO_OP", reason="missing_mark")
        anchor = position["anchor_price"]
        threshold_pct = config["threshold_pct"]
        threshold = anchor * (1 + threshold_pct)
        if mark < threshold:
            return Decision(kind="NO_OP")
        # Threshold crossed. Check debounce.
        now = marks.get("now") or datetime.now(timezone.utc)
        last = _parse_iso(state_data.get("last_alert_at")) if isinstance(state_data.get("last_alert_at"), str) else state_data.get("last_alert_at")
        debounce = timedelta(seconds=config.get("min_realert_interval_s", 3600))
        if last is not None and (now - last) < debounce:
            return Decision(kind="NO_OP", reason="debounced")
        return Decision(
            kind="TRIGGERED",
            reason="alert_only_threshold_crossed",
            context={"mark": mark, "threshold": threshold, "alert_only": True},
            state_data_patch={"last_alert_at": now.isoformat()},
        )

    def disarm(self, *, scope, position, native_perm_id) -> None:
        return None


register(ComboTpAlertRule())
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest scripts/tests/test_position_rules/test_rules.py -xvs`
Expected: 9 green.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/brackets/rules/ scripts/tests/test_position_rules/test_rules.py
git commit -m "feat(brackets): add 4 rule plug-ins (stop_loss, trailing_tp, take_profit_fixed, combo_tp_alert)"
```

---

## Task 8: Pure module — policy resolver

**Files:**

- Create: `src/xenon/execution/brackets/policies.py`
- Create: `scripts/tests/test_position_rules/test_policies.py`

This is the in-memory most-specific-wins resolver. The SQL-side weighted ORDER BY lives in `queries/bracket_policies.py` (Task 11); this pure module operates on already-fetched rows so we can unit-test the merge/dedupe logic.

- [ ] **Step 1: Write the failing tests**

```python
# scripts/tests/test_position_rules/test_policies.py
"""Most-specific-wins policy resolution. Spec §5.2, codex N-S1."""
from __future__ import annotations

from xenon.execution.brackets.policies import PolicyRow, deduplicate_by_specificity


def _row(rule_kind, broker=None, env=None, account=None, enabled=True, auto_place=True, config=None, policy_id=1):
    return PolicyRow(
        policy_id=policy_id,
        broker=broker, account_env=env, broker_account=account,
        asset_class="long_option", rule_kind=rule_kind,
        enabled=enabled, auto_place=auto_place,
        config=config or {"threshold_pct": -0.20, "anchor": "entry_price"},
    )


def test_account_specific_beats_broker_wide():
    """Codex N-S1 regression: weighted-score ORDER BY ranks account-specific above broker-wide."""
    broker_wide = _row("stop_loss", broker="IB", env=None, account=None, policy_id=1, config={"threshold_pct": -0.10, "anchor": "entry_price"})
    account_specific = _row("stop_loss", broker=None, env=None, account="DU1234567", policy_id=2, config={"threshold_pct": -0.20, "anchor": "entry_price"})
    rows = [broker_wide, account_specific]  # caller passes pre-sorted by SQL specificity DESC
    rows.sort(key=lambda r: -(
        (4 if r.broker_account else 0)
        + (2 if r.account_env else 0)
        + (1 if r.broker else 0)
    ))  # simulate the SQL ORDER BY
    deduped = deduplicate_by_specificity(rows)
    by_kind = {r.rule_kind: r for r in deduped}
    assert by_kind["stop_loss"].config["threshold_pct"] == -0.20  # account-specific won


def test_filters_disabled_rows():
    rows = [
        _row("stop_loss", enabled=False, policy_id=1),
        _row("stop_loss", enabled=True, policy_id=2),
    ]
    deduped = deduplicate_by_specificity(rows)
    assert len(deduped) == 1
    assert deduped[0].policy_id == 2


def test_returns_one_per_rule_kind():
    rows = [
        _row("stop_loss", policy_id=1),
        _row("trailing_tp", policy_id=2, config={"trail_pct": 0.25, "activation_pct": 0.30, "anchor": "mfe"}),
    ]
    deduped = deduplicate_by_specificity(rows)
    kinds = sorted(r.rule_kind for r in deduped)
    assert kinds == ["stop_loss", "trailing_tp"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest scripts/tests/test_position_rules/test_policies.py -xvs`

- [ ] **Step 3: Implement `policies.py`**

```python
# src/xenon/execution/brackets/policies.py
"""Pure-Python helpers for in-memory policy merging.

The SQL-side weighted ORDER BY lives in `xenon.db.queries.bracket_policies`.
This module's `deduplicate_by_specificity` consumes rows already sorted DESC
by specificity score, drops disabled rows, and keeps the first row per
rule_kind. Spec §5.2.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PolicyRow:
    policy_id: int
    broker: str | None
    account_env: str | None
    broker_account: str | None
    asset_class: str
    rule_kind: str
    enabled: bool
    auto_place: bool
    config: dict[str, Any]


def deduplicate_by_specificity(rows: list[PolicyRow]) -> list[PolicyRow]:
    """Caller passes rows pre-sorted DESC by specificity score, ASC policy_id tiebreak."""
    seen: dict[str, PolicyRow] = {}
    for row in rows:
        if not row.enabled:
            continue
        seen.setdefault(row.rule_kind, row)
    return list(seen.values())
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest scripts/tests/test_position_rules/test_policies.py -xvs`
Expected: 3 green.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/brackets/policies.py scripts/tests/test_position_rules/test_policies.py
git commit -m "feat(brackets): add pure most-specific-wins policy deduplicator"
```

---

## Task 9: Postgres — schema additions (no migration yet)

**Files:**

- Modify: `src/xenon/db/schema.py` — add three Tables. Do **not** drop `wizard_protection`.

- [ ] **Step 1: Append the three table definitions**

Add to `src/xenon/db/schema.py` (after `wizard_protection`, keeping it intact for now):

```python
# ── position_protection (replaces wizard_protection in Plan 3) ────────────────
position_protection = Table(
    "position_protection",
    xenon_metadata,
    Column("protection_id", BigInteger, primary_key=True, autoincrement=True),
    Column("broker", Text, nullable=False),
    Column("account_env", Text, nullable=False),
    Column("broker_account", Text, nullable=False),
    Column("position_key", Text, nullable=False),
    Column("position_descriptor", JSONB, nullable=False),
    Column("asset_class", Text, nullable=False),
    Column("rule_kind", Text, nullable=False),
    Column("state", Text, nullable=False, server_default=text("'PENDING_ARM'")),
    Column("config", JSONB, nullable=False),
    Column("state_data", JSONB, nullable=False, server_default=text("'{}'")),
    Column("native_order_perm_id", BigInteger, nullable=True),
    Column("native_order_state", Text, nullable=True),
    Column("armed_at", TIMESTAMP(timezone=True)),
    Column("triggered_at", TIMESTAMP(timezone=True)),
    Column("closed_at", TIMESTAMP(timezone=True)),
    Column("last_evaluated_at", TIMESTAMP(timezone=True)),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    CheckConstraint("broker IN ('IB','FUTU')", name="ck_position_protection_broker"),
    CheckConstraint("account_env IN ('paper','live','sim','legacy_unknown')", name="ck_position_protection_account_env"),
    CheckConstraint(
        "state IN ('PENDING_ARM','ARMED','TRIGGERED','CLOSED','CANCELED','FAILED','SUPERSEDED')",
        name="ck_position_protection_state",
    ),
    CheckConstraint(
        "rule_kind IN ('stop_loss','trailing_tp','take_profit_fixed','combo_tp_alert')",
        name="ck_position_protection_rule_kind",
    ),
    CheckConstraint(
        "asset_class IN ('stock','long_option','debit_combo','credit_spread','covered_call','unclassified')",
        name="ck_position_protection_asset_class",
    ),
    Index(
        "uq_position_protection_active",
        "broker", "account_env", "broker_account", "position_key", "rule_kind",
        unique=True,
        postgresql_where=text("state IN ('PENDING_ARM','ARMED','TRIGGERED')"),
    ),
    Index(
        "ix_position_protection_hot",
        "state", "broker", "account_env", "broker_account",
        postgresql_where=text("state IN ('PENDING_ARM','ARMED')"),
    ),
    Index(
        "ix_position_protection_lookup",
        "broker", "account_env", "broker_account", "position_key",
    ),
)


# ── bracket_policies (defaults seed) ──────────────────────────────────────────
bracket_policies = Table(
    "bracket_policies",
    xenon_metadata,
    Column("policy_id", BigInteger, primary_key=True, autoincrement=True),
    Column("broker", Text, nullable=True),
    Column("account_env", Text, nullable=True),
    Column("broker_account", Text, nullable=True),
    Column("asset_class", Text, nullable=False),
    Column("rule_kind", Text, nullable=False),
    Column("enabled", Boolean, nullable=False, server_default=text("TRUE")),
    Column("auto_place", Boolean, nullable=False, server_default=text("TRUE")),
    Column("config", JSONB, nullable=False),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    CheckConstraint(
        "rule_kind IN ('stop_loss','trailing_tp','take_profit_fixed','combo_tp_alert')",
        name="ck_bracket_policies_rule_kind",
    ),
    CheckConstraint(
        "asset_class IN ('stock','long_option','debit_combo','credit_spread','covered_call','unclassified')",
        name="ck_bracket_policies_asset_class",
    ),
    Index(
        "uq_bracket_policies_scope_class_kind",
        text("COALESCE(broker,'*')"),
        text("COALESCE(account_env,'*')"),
        text("COALESCE(broker_account,'*')"),
        "asset_class", "rule_kind",
        unique=True,
    ),
)


# ── position_close_claims (duplicate-close prevention; spec §5.6) ─────────────
position_close_claims = Table(
    "position_close_claims",
    xenon_metadata,
    Column("claim_id", BigInteger, primary_key=True, autoincrement=True),
    Column("broker", Text, nullable=False),
    Column("account_env", Text, nullable=False),
    Column("broker_account", Text, nullable=False),
    Column("position_key", Text, nullable=False),
    Column("claimed_by_protection_id", BigInteger, nullable=False),
    Column("claim_kind", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'PENDING'")),
    Column("order_ref", Text, nullable=False),
    Column("broker_perm_id", BigInteger, nullable=True),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("claimed_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("submitted_at", TIMESTAMP(timezone=True), nullable=True),
    Column("terminal_at", TIMESTAMP(timezone=True), nullable=True),
    Column("last_error", Text, nullable=True),
    CheckConstraint("broker IN ('IB','FUTU')", name="ck_position_close_claims_broker"),
    CheckConstraint("account_env IN ('paper','live','sim','legacy_unknown')", name="ck_position_close_claims_account_env"),
    CheckConstraint(
        "status IN ('PENDING','SUBMITTED','FILLED','FAILED','ABANDONED')",
        name="ck_position_close_claims_status",
    ),
    CheckConstraint(
        "claim_kind IN ('synthetic_close','native_reconcile_close')",
        name="ck_position_close_claims_kind",
    ),
    Index(
        "uq_position_close_claims_inflight",
        "broker", "account_env", "broker_account", "position_key",
        unique=True,
        postgresql_where=text("status IN ('PENDING','SUBMITTED')"),
    ),
    UniqueConstraint("order_ref", name="uq_position_close_claims_order_ref"),
    Index("ix_position_close_claims_cleanup", "broker", "account_env", "broker_account", "status"),
)
```

(Make sure the imports at the top of `schema.py` already include `Boolean`, `Integer`, `BigInteger`, `JSONB`, `Text`, `Index`, `UniqueConstraint`, `CheckConstraint`, `text`, `tz_now`, `TIMESTAMP`. They do — the file already uses every name above for other tables.)

- [ ] **Step 2: Verify imports & smoke**

Run: `uv run python -c "from xenon.db.schema import position_protection, bracket_policies, position_close_claims; print(position_protection.name, bracket_policies.name, position_close_claims.name)"`

Expected: `position_protection bracket_policies position_close_claims`.

- [ ] **Step 3: Commit**

```bash
git add src/xenon/db/schema.py
git commit -m "feat(db): add position_protection, bracket_policies, position_close_claims tables"
```

---

## Task 10: Postgres — Alembic migration A (additive)

**Files:**

- Create: `src/xenon/db/migrations/versions/<rev>_add_position_rules_tables.py`

- [ ] **Step 1: Generate the migration**

Run:

```bash
uv run alembic revision --autogenerate -m "add position rules tables"
```

This produces a migration file under `src/xenon/db/migrations/versions/` referencing the three new tables. Replace its body with the explicit content below (autogenerate handles SQLAlchemy ops well, but we want to pin the seed-row INSERTs and the partial-unique index expressions exactly).

- [ ] **Step 2: Author the migration body**

```python
"""add position rules tables

Revision ID: <generated>
Revises: 9f2c4a1d8e57
Create Date: 2026-05-04
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "<generated>"
down_revision: Union[str, Sequence[str], None] = "9f2c4a1d8e57"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── position_protection ──
    op.create_table(
        "position_protection",
        sa.Column("protection_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("account_env", sa.Text(), nullable=False),
        sa.Column("broker_account", sa.Text(), nullable=False),
        sa.Column("position_key", sa.Text(), nullable=False),
        sa.Column("position_descriptor", postgresql.JSONB(), nullable=False),
        sa.Column("asset_class", sa.Text(), nullable=False),
        sa.Column("rule_kind", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'PENDING_ARM'")),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("state_data", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("native_order_perm_id", sa.BigInteger(), nullable=True),
        sa.Column("native_order_state", sa.Text(), nullable=True),
        sa.Column("armed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("triggered_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("last_evaluated_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("(now() AT TIME ZONE 'UTC')")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("(now() AT TIME ZONE 'UTC')")),
        sa.CheckConstraint("broker IN ('IB','FUTU')", name="ck_position_protection_broker"),
        sa.CheckConstraint("account_env IN ('paper','live','sim','legacy_unknown')", name="ck_position_protection_account_env"),
        sa.CheckConstraint(
            "state IN ('PENDING_ARM','ARMED','TRIGGERED','CLOSED','CANCELED','FAILED','SUPERSEDED')",
            name="ck_position_protection_state",
        ),
        sa.CheckConstraint(
            "rule_kind IN ('stop_loss','trailing_tp','take_profit_fixed','combo_tp_alert')",
            name="ck_position_protection_rule_kind",
        ),
        sa.CheckConstraint(
            "asset_class IN ('stock','long_option','debit_combo','credit_spread','covered_call','unclassified')",
            name="ck_position_protection_asset_class",
        ),
        schema="xenon",
    )
    op.create_index(
        "uq_position_protection_active",
        "position_protection",
        ["broker", "account_env", "broker_account", "position_key", "rule_kind"],
        unique=True,
        postgresql_where=sa.text("state IN ('PENDING_ARM','ARMED','TRIGGERED')"),
        schema="xenon",
    )
    op.create_index(
        "ix_position_protection_hot",
        "position_protection",
        ["state", "broker", "account_env", "broker_account"],
        postgresql_where=sa.text("state IN ('PENDING_ARM','ARMED')"),
        schema="xenon",
    )
    op.create_index(
        "ix_position_protection_lookup",
        "position_protection",
        ["broker", "account_env", "broker_account", "position_key"],
        schema="xenon",
    )

    # ── bracket_policies ──
    op.create_table(
        "bracket_policies",
        sa.Column("policy_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("broker", sa.Text(), nullable=True),
        sa.Column("account_env", sa.Text(), nullable=True),
        sa.Column("broker_account", sa.Text(), nullable=True),
        sa.Column("asset_class", sa.Text(), nullable=False),
        sa.Column("rule_kind", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
        sa.Column("auto_place", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("(now() AT TIME ZONE 'UTC')")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("(now() AT TIME ZONE 'UTC')")),
        sa.CheckConstraint(
            "rule_kind IN ('stop_loss','trailing_tp','take_profit_fixed','combo_tp_alert')",
            name="ck_bracket_policies_rule_kind",
        ),
        sa.CheckConstraint(
            "asset_class IN ('stock','long_option','debit_combo','credit_spread','covered_call','unclassified')",
            name="ck_bracket_policies_asset_class",
        ),
        schema="xenon",
    )
    op.execute("""
        CREATE UNIQUE INDEX uq_bracket_policies_scope_class_kind
        ON xenon.bracket_policies (
            COALESCE(broker,'*'),
            COALESCE(account_env,'*'),
            COALESCE(broker_account,'*'),
            asset_class,
            rule_kind
        )
    """)

    # ── position_close_claims ──
    op.create_table(
        "position_close_claims",
        sa.Column("claim_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("account_env", sa.Text(), nullable=False),
        sa.Column("broker_account", sa.Text(), nullable=False),
        sa.Column("position_key", sa.Text(), nullable=False),
        sa.Column("claimed_by_protection_id", sa.BigInteger(), nullable=False),
        sa.Column("claim_kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'PENDING'")),
        sa.Column("order_ref", sa.Text(), nullable=False),
        sa.Column("broker_perm_id", sa.BigInteger(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("claimed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("(now() AT TIME ZONE 'UTC')")),
        sa.Column("submitted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("terminal_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint("broker IN ('IB','FUTU')", name="ck_position_close_claims_broker"),
        sa.CheckConstraint("account_env IN ('paper','live','sim','legacy_unknown')", name="ck_position_close_claims_account_env"),
        sa.CheckConstraint(
            "status IN ('PENDING','SUBMITTED','FILLED','FAILED','ABANDONED')",
            name="ck_position_close_claims_status",
        ),
        sa.CheckConstraint(
            "claim_kind IN ('synthetic_close','native_reconcile_close')",
            name="ck_position_close_claims_kind",
        ),
        sa.UniqueConstraint("order_ref", name="uq_position_close_claims_order_ref"),
        schema="xenon",
    )
    op.create_index(
        "uq_position_close_claims_inflight",
        "position_close_claims",
        ["broker", "account_env", "broker_account", "position_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING','SUBMITTED')"),
        schema="xenon",
    )
    op.create_index(
        "ix_position_close_claims_cleanup",
        "position_close_claims",
        ["broker", "account_env", "broker_account", "status"],
        schema="xenon",
    )

    # ── 8 seed rows for bracket_policies ──
    op.execute("""
        INSERT INTO xenon.bracket_policies (asset_class, rule_kind, auto_place, config) VALUES
          ('stock',          'stop_loss',          TRUE, '{"threshold_pct": -0.08, "anchor": "entry_price"}'),
          ('stock',          'trailing_tp',        TRUE, '{"trail_pct": 0.05, "activation_pct": 0.0, "anchor": "mfe"}'),
          ('long_option',    'stop_loss',          TRUE, '{"threshold_pct": -0.20, "anchor": "entry_price"}'),
          ('long_option',    'trailing_tp',        TRUE, '{"trail_pct": 0.25, "activation_pct": 0.30, "anchor": "mfe"}'),
          ('debit_combo',    'stop_loss',          TRUE, '{"threshold_pct_of_max_loss": 0.50, "anchor": "synthetic_mark"}'),
          ('debit_combo',    'trailing_tp',        TRUE, '{"trail_pct": 0.25, "activation_pct_of_max_gain": 0.25, "anchor": "mfe_pnl_dollars"}'),
          ('credit_spread',  'stop_loss',          TRUE, '{"trigger_kind": "either", "mark_multiple_of_credit": 2.0, "underlying_breach_short_strike": true, "anchor": "synthetic_mark"}'),
          ('credit_spread',  'take_profit_fixed',  TRUE, '{"close_at_credit_pct": 0.50, "anchor": "synthetic_mark"}')
    """)


def downgrade() -> None:
    op.drop_index("ix_position_close_claims_cleanup", table_name="position_close_claims", schema="xenon")
    op.drop_index("uq_position_close_claims_inflight", table_name="position_close_claims", schema="xenon")
    op.drop_table("position_close_claims", schema="xenon")
    op.execute("DROP INDEX IF EXISTS xenon.uq_bracket_policies_scope_class_kind")
    op.drop_table("bracket_policies", schema="xenon")
    op.drop_index("ix_position_protection_lookup", table_name="position_protection", schema="xenon")
    op.drop_index("ix_position_protection_hot", table_name="position_protection", schema="xenon")
    op.drop_index("uq_position_protection_active", table_name="position_protection", schema="xenon")
    op.drop_table("position_protection", schema="xenon")
```

- [ ] **Step 3: Run migration up + down clean**

```bash
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```

Expected: each step completes with no errors. The final state has the three tables present and 8 rows in `bracket_policies`.

Verify:

```bash
psql -h localhost -U xenon_app xenon_db -c "SELECT COUNT(*) FROM xenon.bracket_policies"
```

Expected: `count = 8`.

- [ ] **Step 4: Commit**

```bash
git add src/xenon/db/migrations/versions/<rev>_add_position_rules_tables.py
git commit -m "feat(db): add position rules migration (3 tables + 8 seed rows)"
```

---

## Task 11: Postgres — query modules

**Files:**

- Create: `src/xenon/db/queries/position_protection.py`
- Create: `src/xenon/db/queries/bracket_policies.py`
- Create: `src/xenon/db/queries/position_close_claims.py`
- Create: `scripts/tests/test_position_rules_db/__init__.py` (empty)
- Create: `scripts/tests/test_position_rules_db/test_position_protection_queries.py`
- Create: `scripts/tests/test_position_rules_db/test_bracket_policies_queries.py`
- Create: `scripts/tests/test_position_rules_db/test_position_close_claims_queries.py`

- [ ] **Step 1: Implement `bracket_policies.py` (smallest)**

```python
# src/xenon/db/queries/bracket_policies.py
"""Most-specific-wins resolver for bracket_policies. Spec §5.2.

Weighted ORDER BY: broker_account=4, account_env=2, broker=1; ties broken by
policy_id ASC. Caller deduplicates by rule_kind in Python via
`xenon.execution.brackets.policies.deduplicate_by_specificity`.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from xenon.execution.brackets.policies import PolicyRow


def resolve_for_scope(
    engine: Engine,
    *,
    broker: str,
    account_env: str,
    broker_account: str,
    asset_class: str,
) -> list[PolicyRow]:
    sql = text("""
        SELECT
          policy_id, broker, account_env, broker_account,
          asset_class, rule_kind, enabled, auto_place, config
        FROM xenon.bracket_policies
        WHERE asset_class = :asset_class
          AND (broker IS NULL OR broker = :broker)
          AND (account_env IS NULL OR account_env = :account_env)
          AND (broker_account IS NULL OR broker_account = :broker_account)
        ORDER BY
          (CASE WHEN broker_account IS NOT NULL THEN 4 ELSE 0 END
         + CASE WHEN account_env    IS NOT NULL THEN 2 ELSE 0 END
         + CASE WHEN broker         IS NOT NULL THEN 1 ELSE 0 END) DESC,
          policy_id ASC
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {
            "asset_class": asset_class,
            "broker": broker,
            "account_env": account_env,
            "broker_account": broker_account,
        }).all()
    return [
        PolicyRow(
            policy_id=r.policy_id,
            broker=r.broker,
            account_env=r.account_env,
            broker_account=r.broker_account,
            asset_class=r.asset_class,
            rule_kind=r.rule_kind,
            enabled=r.enabled,
            auto_place=r.auto_place,
            config=r.config,
        )
        for r in rows
    ]
```

- [ ] **Step 2: Test `bracket_policies.py` (N-S1 regression)**

```python
# scripts/tests/test_position_rules_db/test_bracket_policies_queries.py
"""Bracket-policies SQL resolver. Spec §5.2, codex N-S1."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.db.queries.bracket_policies import resolve_for_scope


@pytest.fixture
def engine_with_account_specific_override():
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO xenon.bracket_policies (broker, account_env, broker_account, asset_class, rule_kind, auto_place, config)
            VALUES
              ('IB', NULL, NULL, 'long_option', 'stop_loss', TRUE, '{"threshold_pct": -0.10, "anchor": "entry_price"}'),
              (NULL, NULL, 'DU1234567', 'long_option', 'stop_loss', TRUE, '{"threshold_pct": -0.20, "anchor": "entry_price"}')
            ON CONFLICT DO NOTHING
        """))
    yield engine
    with engine.begin() as conn:
        conn.execute(text("""
            DELETE FROM xenon.bracket_policies
            WHERE (broker = 'IB' AND broker_account IS NULL AND asset_class = 'long_option')
               OR (broker IS NULL AND broker_account = 'DU1234567' AND asset_class = 'long_option')
        """))


def test_account_specific_override_beats_broker_wide(engine_with_account_specific_override):
    rows = resolve_for_scope(
        engine_with_account_specific_override,
        broker="IB", account_env="paper", broker_account="DU1234567",
        asset_class="long_option",
    )
    # First row by ORDER BY should be the account-specific override.
    assert rows[0].broker_account == "DU1234567"
    assert rows[0].config["threshold_pct"] == -0.20
```

Run: `uv run pytest scripts/tests/test_position_rules_db/test_bracket_policies_queries.py -xvs`
Expected: 1 green.

- [ ] **Step 3: Implement `position_protection.py`**

```python
# src/xenon/db/queries/position_protection.py
"""CRUD + CAS for position_protection. Spec §5.1, §7.

CAS = optimistic state transition: every transition uses
`UPDATE … WHERE state=$expected RETURNING …`. Zero rowcount → another tick
already handled it. No double-trigger possible.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from xenon.db.events import emit_outbox_in_txn
from xenon.db.schema import position_protection

CHANNEL_POSITION_RULE_TRANSITION = "position_rule.transition"
PAYLOAD_VERSION = 1


def insert_pending_arm(
    engine: Engine,
    *,
    broker: str,
    account_env: str,
    broker_account: str,
    position_key: str,
    position_descriptor: dict[str, Any],
    asset_class: str,
    rule_kind: str,
    config: dict[str, Any],
) -> int | None:
    """Insert a PENDING_ARM row; returns protection_id, or None on ON CONFLICT."""
    with engine.begin() as conn:
        stmt = (
            pg_insert(position_protection)
            .values(
                broker=broker, account_env=account_env, broker_account=broker_account,
                position_key=position_key, position_descriptor=position_descriptor,
                asset_class=asset_class, rule_kind=rule_kind,
                state="PENDING_ARM", config=config,
            )
            .on_conflict_do_nothing(index_elements=None)  # rely on partial unique
            .returning(position_protection.c.protection_id)
        )
        row = conn.execute(stmt).first()
        if row is None:
            return None
        protection_id = row[0]
        emit_outbox_in_txn(
            conn,
            channel=CHANNEL_POSITION_RULE_TRANSITION,
            source="insert_pending_arm",
            payload={
                "payload_version": PAYLOAD_VERSION,
                "protection_id": protection_id,
                "position_key": position_key,
                "rule_kind": rule_kind,
                "old_state": None,
                "new_state": "PENDING_ARM",
                "reason": "fill_recorded",
                "context": {"asset_class": asset_class},
                "scope": {"broker": broker, "account_env": account_env, "broker_account": broker_account},
            },
        )
        return protection_id


def cas_transition(
    engine: Engine,
    *,
    protection_id: int,
    expected_state: str,
    new_state: str,
    reason: str,
    context: dict[str, Any] | None = None,
    state_data_patch: dict[str, Any] | None = None,
    native_order_perm_id: int | None = None,
) -> bool:
    """Optimistic CAS state transition + outbox emit. Returns True on success."""
    now = datetime.now(timezone.utc)
    timestamp_col = {
        "ARMED": "armed_at",
        "TRIGGERED": "triggered_at",
        "CLOSED": "closed_at",
        "CANCELED": "closed_at",
        "FAILED": "closed_at",
        "SUPERSEDED": "closed_at",
    }.get(new_state)

    values: dict[str, Any] = {"state": new_state, "updated_at": now}
    if timestamp_col:
        values[timestamp_col] = now
    if native_order_perm_id is not None:
        values["native_order_perm_id"] = native_order_perm_id

    with engine.begin() as conn:
        # Apply state_data patch via JSONB merge if provided.
        if state_data_patch is not None:
            existing = conn.execute(
                select(position_protection.c.state_data, position_protection.c.broker,
                       position_protection.c.account_env, position_protection.c.broker_account,
                       position_protection.c.position_key, position_protection.c.rule_kind)
                .where(position_protection.c.protection_id == protection_id)
            ).first()
            if existing is None:
                return False
            merged = dict(existing.state_data or {})
            merged.update(state_data_patch)
            values["state_data"] = merged
        else:
            existing = conn.execute(
                select(position_protection.c.broker, position_protection.c.account_env,
                       position_protection.c.broker_account, position_protection.c.position_key,
                       position_protection.c.rule_kind)
                .where(position_protection.c.protection_id == protection_id)
            ).first()
            if existing is None:
                return False

        stmt = (
            update(position_protection)
            .where(
                position_protection.c.protection_id == protection_id,
                position_protection.c.state == expected_state,
            )
            .values(**values)
            .returning(position_protection.c.protection_id)
        )
        result = conn.execute(stmt).first()
        if result is None:
            return False

        emit_outbox_in_txn(
            conn,
            channel=CHANNEL_POSITION_RULE_TRANSITION,
            source="cas_transition",
            payload={
                "payload_version": PAYLOAD_VERSION,
                "protection_id": protection_id,
                "position_key": existing.position_key,
                "rule_kind": existing.rule_kind,
                "old_state": expected_state,
                "new_state": new_state,
                "reason": reason,
                "context": context or {},
                "scope": {"broker": existing.broker, "account_env": existing.account_env, "broker_account": existing.broker_account},
            },
        )
        return True


def list_active_rows(
    engine: Engine,
    *,
    broker: str,
    account_env: str,
    broker_account: str,
    states: tuple[str, ...] = ("PENDING_ARM", "ARMED", "TRIGGERED"),
) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        result = conn.execute(
            select(position_protection)
            .where(
                position_protection.c.broker == broker,
                position_protection.c.account_env == account_env,
                position_protection.c.broker_account == broker_account,
                position_protection.c.state.in_(states),
            )
        )
        return [dict(r._mapping) for r in result]


def get_by_id(engine: Engine, *, protection_id: int) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = conn.execute(
            select(position_protection).where(position_protection.c.protection_id == protection_id)
        ).first()
        return dict(row._mapping) if row else None
```

- [ ] **Step 4: Test `position_protection.py` (N-S2 regression)**

```python
# scripts/tests/test_position_rules_db/test_position_protection_queries.py
"""position_protection CRUD + CAS + partial-unique re-arm. Spec §5.1, §7, codex N-S2."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.db.queries.position_protection import (
    cas_transition,
    insert_pending_arm,
    list_active_rows,
)


@pytest.fixture
def engine():
    e = get_sync_engine()
    with e.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key LIKE 'TEST::%'"))
    yield e
    with e.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key LIKE 'TEST::%'"))


def test_insert_pending_arm_emits_outbox_row(engine):
    pid = insert_pending_arm(
        engine,
        broker="IB", account_env="paper", broker_account="DU1234567",
        position_key="TEST::AAPL", position_descriptor={"asset_class": "stock", "legs": [{"sec_type": "STK", "symbol": "AAPL"}]},
        asset_class="stock", rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    assert pid is not None
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT payload->>'new_state' AS new_state
            FROM events.outbox
            WHERE channel = 'position_rule.transition'
              AND payload->>'protection_id' = :pid
            ORDER BY id DESC LIMIT 1
        """), {"pid": str(pid)}).first()
    assert row.new_state == "PENDING_ARM"


def test_cas_transition_pending_to_armed_succeeds(engine):
    pid = insert_pending_arm(
        engine, broker="IB", account_env="paper", broker_account="DU1234567",
        position_key="TEST::AAPL2",
        position_descriptor={"asset_class": "stock", "legs": [{"sec_type": "STK", "symbol": "AAPL"}]},
        asset_class="stock", rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    assert cas_transition(engine, protection_id=pid, expected_state="PENDING_ARM", new_state="ARMED", reason="armed_synthetic")


def test_cas_transition_rejects_stale_expected_state(engine):
    pid = insert_pending_arm(
        engine, broker="IB", account_env="paper", broker_account="DU1234567",
        position_key="TEST::AAPL3",
        position_descriptor={"asset_class": "stock", "legs": [{"sec_type": "STK", "symbol": "AAPL"}]},
        asset_class="stock", rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    cas_transition(engine, protection_id=pid, expected_state="PENDING_ARM", new_state="ARMED", reason="armed")
    # Second attempt with stale expected_state must fail.
    assert cas_transition(engine, protection_id=pid, expected_state="PENDING_ARM", new_state="TRIGGERED", reason="trigger") is False


def test_partial_unique_allows_rearm_after_canceled(engine):
    """N-S2 regression: terminal CANCELED row does NOT block re-arm of same position."""
    descriptor = {"asset_class": "stock", "legs": [{"sec_type": "STK", "symbol": "AAPL"}]}
    pid1 = insert_pending_arm(
        engine, broker="IB", account_env="paper", broker_account="DU1234567",
        position_key="TEST::AAPL_REARM", position_descriptor=descriptor,
        asset_class="stock", rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    cas_transition(engine, protection_id=pid1, expected_state="PENDING_ARM", new_state="CANCELED", reason="manual_cancel")

    pid2 = insert_pending_arm(
        engine, broker="IB", account_env="paper", broker_account="DU1234567",
        position_key="TEST::AAPL_REARM", position_descriptor=descriptor,
        asset_class="stock", rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    assert pid2 is not None
    assert pid2 != pid1


def test_list_active_rows_filters_terminal(engine):
    descriptor = {"asset_class": "stock", "legs": [{"sec_type": "STK", "symbol": "AAPL"}]}
    pid = insert_pending_arm(
        engine, broker="IB", account_env="paper", broker_account="DU1234567",
        position_key="TEST::AAPL_LIST", position_descriptor=descriptor,
        asset_class="stock", rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    cas_transition(engine, protection_id=pid, expected_state="PENDING_ARM", new_state="CANCELED", reason="cancel")

    active = list_active_rows(engine, broker="IB", account_env="paper", broker_account="DU1234567")
    assert all(r["protection_id"] != pid for r in active)
```

Run: `uv run pytest scripts/tests/test_position_rules_db/test_position_protection_queries.py -xvs`
Expected: 5 green.

- [ ] **Step 5: Implement `position_close_claims.py`**

```python
# src/xenon/db/queries/position_close_claims.py
"""Position-level close-claim CRUD. Spec §5.6.

Two key operations:
 - try_claim(...) — atomic INSERT with partial-unique constraint; returns
   claim_id on success, None when another claim is in flight (N-C1, N-C2).
 - find_by_order_ref(...) — used by retries to attach existing perm_id (N-C3).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from xenon.db.schema import position_close_claims
from xenon.execution.brackets.close_claim import derive_order_ref


def try_claim(
    engine: Engine,
    *,
    broker: str,
    account_env: str,
    broker_account: str,
    position_key: str,
    claimed_by_protection_id: int,
    claim_kind: str,
) -> int | None:
    """INSERT … ON CONFLICT DO NOTHING; returns claim_id or None.

    The order_ref is set in a follow-up UPDATE because we need claim_id first.
    """
    with engine.begin() as conn:
        stmt = (
            pg_insert(position_close_claims)
            .values(
                broker=broker, account_env=account_env, broker_account=broker_account,
                position_key=position_key,
                claimed_by_protection_id=claimed_by_protection_id,
                claim_kind=claim_kind,
                status="PENDING",
                order_ref="__pending__",  # placeholder; immediately updated below
            )
            .on_conflict_do_nothing(index_elements=None)
            .returning(position_close_claims.c.claim_id)
        )
        row = conn.execute(stmt).first()
        if row is None:
            return None
        claim_id = row[0]
        order_ref = derive_order_ref(claim_id=claim_id)
        conn.execute(
            update(position_close_claims)
            .where(position_close_claims.c.claim_id == claim_id)
            .values(order_ref=order_ref)
        )
        return claim_id


def mark_submitted(
    engine: Engine,
    *,
    claim_id: int,
    broker_perm_id: int | None = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(position_close_claims)
            .where(position_close_claims.c.claim_id == claim_id)
            .values(
                status="SUBMITTED",
                broker_perm_id=broker_perm_id,
                attempts=position_close_claims.c.attempts + 1,
                submitted_at=datetime.now(timezone.utc),
            )
        )


def mark_terminal(
    engine: Engine,
    *,
    claim_id: int,
    status: str,
    last_error: str | None = None,
) -> None:
    assert status in ("FILLED", "FAILED", "ABANDONED")
    with engine.begin() as conn:
        conn.execute(
            update(position_close_claims)
            .where(position_close_claims.c.claim_id == claim_id)
            .values(
                status=status,
                last_error=last_error,
                terminal_at=datetime.now(timezone.utc),
            )
        )


def find_by_order_ref(engine: Engine, *, order_ref: str) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = conn.execute(
            select(position_close_claims).where(position_close_claims.c.order_ref == order_ref)
        ).first()
        return dict(row._mapping) if row else None


def increment_attempts(engine: Engine, *, claim_id: int, last_error: str | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(position_close_claims)
            .where(position_close_claims.c.claim_id == claim_id)
            .values(
                attempts=position_close_claims.c.attempts + 1,
                last_error=last_error,
            )
        )
```

- [ ] **Step 6: Test `position_close_claims.py` (N-C1, N-C2)**

```python
# scripts/tests/test_position_rules_db/test_position_close_claims_queries.py
"""Concurrent claim contention. Spec §5.6, codex N-C1, N-C2."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.db.queries.position_close_claims import (
    find_by_order_ref,
    mark_submitted,
    mark_terminal,
    try_claim,
)


@pytest.fixture
def engine():
    e = get_sync_engine()
    with e.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_close_claims WHERE position_key LIKE 'TEST::%'"))
    yield e
    with e.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_close_claims WHERE position_key LIKE 'TEST::%'"))


def test_first_try_claim_succeeds(engine):
    claim_id = try_claim(engine, broker="IB", account_env="paper", broker_account="DU1234567",
                        position_key="TEST::CC1", claimed_by_protection_id=1001, claim_kind="synthetic_close")
    assert claim_id is not None
    found = find_by_order_ref(engine, order_ref=f"xenon-pr-{claim_id}")
    assert found["status"] == "PENDING"
    assert found["claimed_by_protection_id"] == 1001


def test_second_try_claim_returns_none_while_inflight(engine):
    """N-C1: two concurrent claims for the same position — only first wins."""
    first = try_claim(engine, broker="IB", account_env="paper", broker_account="DU1234567",
                     position_key="TEST::CC2", claimed_by_protection_id=1, claim_kind="synthetic_close")
    second = try_claim(engine, broker="IB", account_env="paper", broker_account="DU1234567",
                      position_key="TEST::CC2", claimed_by_protection_id=2, claim_kind="native_reconcile_close")
    assert first is not None
    assert second is None


def test_terminal_claim_allows_new_claim(engine):
    """A FILLED/FAILED/ABANDONED claim must not block re-claim of the same position later."""
    claim_id = try_claim(engine, broker="IB", account_env="paper", broker_account="DU1234567",
                        position_key="TEST::CC3", claimed_by_protection_id=1, claim_kind="synthetic_close")
    mark_terminal(engine, claim_id=claim_id, status="FILLED")

    new_claim = try_claim(engine, broker="IB", account_env="paper", broker_account="DU1234567",
                         position_key="TEST::CC3", claimed_by_protection_id=2, claim_kind="synthetic_close")
    assert new_claim is not None
    assert new_claim != claim_id


def test_three_way_race_only_one_winner(engine):
    """N-C2 + N-C1: synthetic + synthetic + native-reconcile, all racing same position."""
    claim_a = try_claim(engine, broker="IB", account_env="paper", broker_account="DU1234567",
                       position_key="TEST::CC4", claimed_by_protection_id=1, claim_kind="synthetic_close")
    claim_b = try_claim(engine, broker="IB", account_env="paper", broker_account="DU1234567",
                       position_key="TEST::CC4", claimed_by_protection_id=2, claim_kind="synthetic_close")
    claim_c = try_claim(engine, broker="IB", account_env="paper", broker_account="DU1234567",
                       position_key="TEST::CC4", claimed_by_protection_id=3, claim_kind="native_reconcile_close")
    assert sum(1 for c in (claim_a, claim_b, claim_c) if c is not None) == 1


def test_mark_submitted_tracks_perm_id(engine):
    cid = try_claim(engine, broker="IB", account_env="paper", broker_account="DU1234567",
                   position_key="TEST::CC5", claimed_by_protection_id=1, claim_kind="synthetic_close")
    mark_submitted(engine, claim_id=cid, broker_perm_id=99999)
    found = find_by_order_ref(engine, order_ref=f"xenon-pr-{cid}")
    assert found["status"] == "SUBMITTED"
    assert found["broker_perm_id"] == 99999
```

Run: `uv run pytest scripts/tests/test_position_rules_db/test_position_close_claims_queries.py -xvs`
Expected: 5 green.

- [ ] **Step 7: Commit**

```bash
git add src/xenon/db/queries/position_protection.py src/xenon/db/queries/bracket_policies.py src/xenon/db/queries/position_close_claims.py scripts/tests/test_position_rules_db/__init__.py scripts/tests/test_position_rules_db/test_position_protection_queries.py scripts/tests/test_position_rules_db/test_bracket_policies_queries.py scripts/tests/test_position_rules_db/test_position_close_claims_queries.py
git commit -m "feat(db): add queries for position_protection, bracket_policies, position_close_claims"
```

---

## Task 12: Migration test — up/down clean + indexes via EXPLAIN

**Files:**

- Create: `scripts/tests/test_position_rules_db/test_migration.py`

- [ ] **Step 1: Write the test**

```python
# scripts/tests/test_position_rules_db/test_migration.py
"""Migration smoke + index plan. Spec §13.3."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine


def test_migration_seed_rows_present():
    engine = get_sync_engine()
    with engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM xenon.bracket_policies")).scalar_one()
    assert n >= 8


def test_partial_unique_index_used_for_active_lookup():
    """EXPLAIN must show the partial-unique index on the handler hot-path query."""
    engine = get_sync_engine()
    with engine.connect() as conn:
        plan = conn.execute(text("""
            EXPLAIN
            SELECT * FROM xenon.position_protection
            WHERE state IN ('PENDING_ARM','ARMED')
              AND broker = 'IB' AND account_env = 'paper' AND broker_account = 'DU1234567'
        """)).all()
    plan_text = "\n".join(row[0] for row in plan)
    assert "ix_position_protection_hot" in plan_text or "Index" in plan_text


def test_check_constraints_reject_invalid_state():
    engine = get_sync_engine()
    with pytest.raises(Exception):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO xenon.position_protection
                  (broker, account_env, broker_account, position_key, position_descriptor,
                   asset_class, rule_kind, state, config)
                VALUES
                  ('IB', 'paper', 'DU1234567', 'TEST::BOGUS', '{}', 'stock', 'stop_loss',
                   'WHATEVER', '{"threshold_pct": -0.08, "anchor": "entry_price"}')
            """))
```

- [ ] **Step 2: Run**

```bash
uv run pytest scripts/tests/test_position_rules_db/test_migration.py -xvs
```

Expected: 3 green.

- [ ] **Step 3: Commit**

```bash
git add scripts/tests/test_position_rules_db/test_migration.py
git commit -m "test(db): migration up/down + index plan + check-constraint smoke"
```

---

## Task 13: Outbox DLQ table (only if not already present)

**Files:**

- Modify (or create migration): `src/xenon/db/migrations/versions/<rev>_add_outbox_dlq_if_missing.py`

- [ ] **Step 1: Check whether `events.outbox_dlq` exists**

```bash
psql -h localhost -U xenon_app xenon_db -c "SELECT 1 FROM information_schema.tables WHERE table_schema='events' AND table_name='outbox_dlq'"
```

If a row is returned, **skip this task entirely**. The arm consumer in Task 14 will use the existing table.

If no row is returned, generate a small migration:

```bash
uv run alembic revision -m "add events outbox dlq table"
```

Body:

```python
def upgrade():
    op.create_table(
        "outbox_dlq",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_event_id", sa.BigInteger(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("dead_lettered_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("(now() AT TIME ZONE 'UTC')")),
        schema="events",
    )

def downgrade():
    op.drop_table("outbox_dlq", schema="events")
```

Then `uv run alembic upgrade head` and commit.

- [ ] **Step 2: Add `outbox_dlq` Table to `schema.py`**

If the migration is added, also add the SQLAlchemy `Table` definition to `src/xenon/db/schema.py` for `events.outbox_dlq`. Mirror the column shapes from the migration.

---

## Task 14: Arm hook — outbox consumer

**Files:**

- Create: `src/xenon/execution/brackets/arm_hook.py`
- Create: `scripts/tests/test_position_rules_db/test_arm_hook.py`

The arm hook is a `fill.recorded` consumer. It runs in its own transaction, classifies the position, resolves policies, and inserts `PENDING_ARM` rows. Multi-leg atomicity gating uses `wizard_combo_attempts.legs` (JSONB list); we derive `expected_leg_count` from `len(attempt.legs)`.

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_position_rules_db/test_arm_hook.py
"""Arm-hook outbox consumer. Spec §6.1, §6.3.

Verifies:
  - single-leg fill → exactly one position_protection row inserted per matching policy
  - replay (consumer crash mid-handle) → idempotent (no duplicate inserts)
  - DLQ — persistent failure goes to events.outbox_dlq
  - combo-wizard atomicity gate — partial fills do NOT classify
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.execution.brackets.arm_hook import on_fill_event


@pytest.fixture
def engine():
    e = get_sync_engine()
    with e.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key LIKE 'TEST::%'"))
        conn.execute(text("DELETE FROM xenon.order_fills WHERE exec_id LIKE 'TEST-%'"))
    yield e
    with e.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key LIKE 'TEST::%'"))


def _fill_event(exec_id, ticker, side, price=100.0, sec_type="STK"):
    return {
        "exec_id": exec_id, "submission_id": None, "combo_attempt_id": None,
        "perm_id": "1", "ticker": ticker, "side": side, "qty": 1, "price": str(price),
        "filled_at": "2026-05-04T14:23:11+00:00",
        "metadata": {"sec_type": sec_type},
        "broker": "IB", "account_env": "paper", "broker_account": "DU1234567",
        "con_id": 12345,
    }


def test_single_leg_stock_fill_arms_two_rules(engine):
    # The seed has stock-stop_loss + stock-trailing_tp rows.
    on_fill_event(engine, _fill_event("TEST-EX-1", "AAPL", "BUY"))
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT rule_kind FROM xenon.position_protection
            WHERE position_key = 'STK::AAPL' AND state = 'PENDING_ARM'
            ORDER BY rule_kind
        """)).all()
    assert [r.rule_kind for r in rows] == ["stop_loss", "trailing_tp"]


def test_replay_is_idempotent(engine):
    """Consumer replays fill_recorded → ON CONFLICT DO NOTHING → no duplicate rows."""
    event = _fill_event("TEST-EX-2", "MSFT", "BUY")
    on_fill_event(engine, event)
    on_fill_event(engine, event)  # replay
    with engine.connect() as conn:
        n = conn.execute(text("""
            SELECT COUNT(*) FROM xenon.position_protection WHERE position_key = 'STK::MSFT' AND state = 'PENDING_ARM'
        """)).scalar_one()
    assert n == 2  # 2 rules (stop_loss + trailing_tp), but only one of each
```

Run: `uv run pytest scripts/tests/test_position_rules_db/test_arm_hook.py -xvs`
Expected: ImportError on missing module.

- [ ] **Step 2: Implement `arm_hook.py`**

```python
# src/xenon/execution/brackets/arm_hook.py
"""Arm-hook outbox consumer. Spec §6.1, §6.2, §6.3.

Subscribes to `fill.recorded`. For each event:
  1. Re-fetch fill row (idempotent against replay).
  2. Atomicity gate (combo wizard partial fill → defer).
  3. Classify position.
  4. UNCLASSIFIED / COVERED_CALL → operator notification (no insert).
  5. Resolve policies for (scope, asset_class).
  6. INSERT PENDING_ARM rows ON CONFLICT DO NOTHING.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from xenon.db.engine import get_sync_engine
from xenon.db.events import emit_outbox_in_txn
from xenon.db.queries.bracket_policies import resolve_for_scope
from xenon.db.queries.position_protection import insert_pending_arm
from xenon.db.schema import order_fills, wizard_combo_attempts
from xenon.execution.brackets.asset_class import AssetClass, classify_position
from xenon.execution.brackets.policies import deduplicate_by_specificity
from xenon.execution.brackets.position_key import compute_position_key

logger = logging.getLogger(__name__)

CHANNEL_FILL_RECORDED = "fill.recorded"
DLQ_ATTEMPT_LIMIT = 5


def on_fill_event(engine: Engine | None = None, payload: dict[str, Any] | None = None) -> None:
    """Top-level consumer entry point.

    Spec §6.6: must NOT touch the source `record_fill` transaction. We open our
    own engine, do all work in a separate tx, and ack the source event by
    advancing the consumer's ack pointer (handled by the LISTEN harness).
    """
    if engine is None:
        engine = get_sync_engine()
    if payload is None:
        return
    try:
        _process(engine, payload)
    except Exception as exc:  # noqa: BLE001
        # The harness manages retry/DLQ counters via the surrounding LISTEN loop.
        logger.exception("arm_hook failed for exec_id=%s: %s", payload.get("exec_id"), exc)
        raise


def _process(engine: Engine, payload: dict[str, Any]) -> None:
    exec_id = payload["exec_id"]
    broker = payload["broker"]
    account_env = payload["account_env"]
    broker_account = payload["broker_account"]
    combo_attempt_id = payload.get("combo_attempt_id")

    # Re-fetch fill (idempotency anchor — the row is durable).
    with engine.connect() as conn:
        fill_row = conn.execute(select(order_fills).where(order_fills.c.exec_id == exec_id)).first()
    if fill_row is None:
        logger.warning("arm_hook: fill %s not found (replay before commit visibility?)", exec_id)
        return
    fill = dict(fill_row._mapping)

    # ── Atomicity gate ──
    legs: list[dict[str, Any]] = []
    wizard_session_payload = None

    if combo_attempt_id:
        # Path A — combo-wizard. Use wizard_combo_attempts.legs as the manifest.
        with engine.connect() as conn:
            attempt = conn.execute(
                select(wizard_combo_attempts).where(wizard_combo_attempts.c.attempt_id == combo_attempt_id)
            ).first()
        if attempt is None:
            _emit_unsupported(engine, payload, reason="wizard_attempt_missing")
            return
        attempt_dict = dict(attempt._mapping)
        manifest_legs = attempt_dict.get("legs") or []
        expected_leg_count = len(manifest_legs)

        with engine.connect() as conn:
            sibling_count = conn.execute(text("""
                SELECT COUNT(*) FROM xenon.order_fills WHERE combo_attempt_id = :attempt_id
            """), {"attempt_id": combo_attempt_id}).scalar_one()
        if sibling_count < expected_leg_count:
            logger.info("arm_hook: combo %s partial (%d/%d) — deferring", combo_attempt_id, sibling_count, expected_leg_count)
            return

        legs = manifest_legs
        wizard_session_payload = {"asset_class": attempt_dict.get("structure_name") or None}

    else:
        # Path B — single-leg, OR Path C — non-wizard manual multi-leg (UNCLASSIFIED).
        leg = {
            "sec_type": (fill.get("metadata") or {}).get("sec_type", "STK"),
            "symbol": fill["ticker"],
            "action": fill["side"],
            "ratio": 1,
            "fill_price": float(fill["price"]),
            "con_id": fill["con_id"],
        }
        # Sibling-leg detection: any other fills for same (scope, ticker) within 60s window
        # whose submission_id differs from this fill's submission_id.
        with engine.connect() as conn:
            siblings = conn.execute(text("""
                SELECT submission_id FROM xenon.order_fills
                WHERE broker = :b AND account_env = :e AND broker_account = :a
                  AND ticker = :t
                  AND combo_attempt_id IS NULL
                  AND submission_id != :sid
                  AND filled_at >= NOW() - INTERVAL '60 seconds'
            """), {"b": broker, "e": account_env, "a": broker_account, "t": fill["ticker"], "sid": fill.get("submission_id")}).all()
        sibling_legs = [{"sec_type": "OPT", "symbol": fill["ticker"]}] if siblings else None
        legs = [leg]

    # ── Classify ──
    classify = classify_position(
        legs=legs,
        wizard_session_payload=wizard_session_payload,
        sibling_legs=sibling_legs if not combo_attempt_id else None,
    )

    if classify.asset_class in (AssetClass.UNCLASSIFIED, AssetClass.COVERED_CALL):
        _emit_unsupported(engine, payload, reason=classify.reason or classify.asset_class.value)
        return

    # ── Resolve policies & insert PENDING_ARM rows ──
    rows = resolve_for_scope(
        engine, broker=broker, account_env=account_env, broker_account=broker_account,
        asset_class=classify.asset_class.value,
    )
    deduped = deduplicate_by_specificity(rows)
    if not deduped:
        _emit_unsupported(engine, payload, reason="no_matching_policies")
        return

    descriptor = {
        "asset_class": classify.asset_class.value,
        "opened_at": fill["filled_at"].isoformat() if hasattr(fill["filled_at"], "isoformat") else fill["filled_at"],
        "source": "fastapi_orders_place" if not combo_attempt_id else "combo_wizard",
        "first_fill_id": None,
        "anchor_price": float(fill["price"]),
        "anchor_currency": "USD",
        "opened_qty": int(fill["qty"]),
        "protected_qty": int(fill["qty"]),
        "multiplier": 100 if any(l.get("sec_type") == "OPT" for l in legs) else 1,
        "qty_unit": "contract" if any(l.get("sec_type") == "OPT" for l in legs) else "share",
        "legs": legs,
    }
    position_key = compute_position_key(classify.asset_class.value, descriptor)

    for policy in deduped:
        insert_pending_arm(
            engine,
            broker=broker, account_env=account_env, broker_account=broker_account,
            position_key=position_key, position_descriptor=descriptor,
            asset_class=classify.asset_class.value, rule_kind=policy.rule_kind,
            config=policy.config,
        )


def _emit_unsupported(engine: Engine, payload: dict[str, Any], *, reason: str) -> None:
    with engine.begin() as conn:
        emit_outbox_in_txn(
            conn,
            channel="position_rule.transition",
            source="arm_hook_unsupported",
            payload={
                "payload_version": 1,
                "kind": "arm_hook_unsupported",
                "reason": reason,
                "exec_id": payload.get("exec_id"),
                "scope": {
                    "broker": payload.get("broker"),
                    "account_env": payload.get("account_env"),
                    "broker_account": payload.get("broker_account"),
                },
            },
        )
```

- [ ] **Step 3: Run tests to verify pass**

Run: `uv run pytest scripts/tests/test_position_rules_db/test_arm_hook.py -xvs`
Expected: 2 green.

(For Plan 2 we ship the `_process` function and unit-test it directly. The actual LISTEN-loop subscription wiring lands in Plan 3 alongside the daemon registration; the test above invokes the function synchronously, which is enough to verify the gate / classify / resolve / insert flow.)

- [ ] **Step 4: Commit**

```bash
git add src/xenon/execution/brackets/arm_hook.py scripts/tests/test_position_rules_db/test_arm_hook.py
git commit -m "feat(brackets): add arm_hook outbox consumer with atomicity gate"
```

---

## Task 15: Verify the affected pytest run + push

- [ ] **Step 1: Run the affected suite**

```bash
uv run python scripts/infra/dev/run_pytest_affected.py
```

Expected: green. The full pure + DB tests added in this plan must pass.

- [ ] **Step 2: Push branch and open PR**

```bash
git push -u origin <plan-2-branch>
gh pr create --title "feat(position-rules): backend infra (pure modules + Postgres + arm consumer)" --body "$(cat <<'EOF'
## Summary

Phase 1 + 2 of the position-rules engine (spec `docs/superpowers/specs/2026-05-04-position-rules-design.md`). Lands the read-side: pure modules (rules, classifier, policies, position_key, triggers, close-claim helpers) plus Postgres schema, queries, and the outbox-driven arm consumer.

After this PR merges, every fill flowing through `orders_store.record_fill()` triggers the arm consumer and lands a `PENDING_ARM` row in `xenon.position_protection`. **No triggers fire yet** — the handler that evaluates `ARMED` rows ships in Plan 3. The feature is end-to-end inert at runtime until Plan 3 registers `PositionRulesHandler`.

## Tables added

- `xenon.position_protection` — one row per (position, rule_kind); FSM `PENDING_ARM → ARMED → TRIGGERED → CLOSED/CANCELED/FAILED/SUPERSEDED`
- `xenon.bracket_policies` — defaults seed (8 rows; per-account overrides land later)
- `xenon.position_close_claims` — partial-unique constraint prevents duplicate MKT closes (codex N-C1, N-C2, N-C3)

## Backwards compatibility

`xenon.wizard_protection` is **not** touched in this PR — it's still in active use by combo-wizard. Migration B (delete + repoint combo wizard onto `position_protection`) lands in Plan 3.

## Test plan

- [ ] `uv run pytest scripts/tests/test_position_rules/ -xvs` (pure unit, ~35 tests)
- [ ] `uv run pytest scripts/tests/test_position_rules_db/ -xvs` (Postgres, ~15 tests)
- [ ] `uv run python scripts/infra/dev/run_pytest_affected.py`
- [ ] CI green
EOF
)"
```

- [ ] **Step 3: Confirm CI green and merge.**

---

## Self-Review

**Spec coverage:**

- §3.1 default policy table → seeded in Task 10 ✓
- §4.2 new code (delta) → `brackets/` package ✓
- §5.1 position_protection columns + partial-unique → Task 9 + 10 ✓
- §5.2 bracket_policies most-specific-wins → Task 11 + 8 ✓
- §5.3 position_key encoding → Task 3 ✓
- §5.4 8 seed rows → Task 10 ✓
- §5.5 Pydantic configs + check constraints → Task 2 + 9 ✓
- §5.6 position_close_claims (N-C1/N-C2/N-C3) → Task 9 + 10 + 11 ✓
- §6.1 outbox-consumer arm hook → Task 14 ✓
- §6.2 classifier + manual leg-by-leg detection → Task 4 ✓
- §6.3 multi-leg atomicity gate (Path A wizard, Path B single, Path C unsupported) → Task 14 ✓
- §6.6 failure modes (DLQ behavior surfaced, harness wiring deferred to Plan 3 daemon) → Task 14 + 13 ✓
- §9 RuleEvaluator Protocol → Task 1 ✓
- §9.1 four rule modules → Task 7 ✓

**Out of scope (deferred to Plan 3 by design):**

- LISTEN/NOTIFY arm-consumer registration in MonitorDaemon
- DLQ harness retry counter
- Native bracket arming (subprocess to `xenon-ib-place-order`)
- `wizard_protection` deletion + combo wizard repointing
- `wizard_stop_monitor.py` deletion
- Frozen-config CI guard (`scripts/checks/frozen_config_at_arm.py`)

**Placeholder scan:** none — every code block compiles standalone; every test name and assertion is concrete.

**Type consistency:**

- `RuleEvaluator.arm/evaluate/disarm` signatures match across `base.py`, all four rule modules, and the `test_rules.py` callers.
- `PolicyRow` shape matches between `policies.py` (pure) and `bracket_policies.py` (queries).
- `derive_order_ref` / `parse_order_ref_claim_id` are used consistently in `close_claim.py` (pure) and `position_close_claims.py` (queries).
- `AssetClass` enum values match the `position_protection.asset_class` CHECK constraint and the `bracket_policies.asset_class` CHECK constraint exactly.
- `state` values in `cas_transition` match the `position_protection.state` CHECK constraint exactly.

**One known schema mismatch handled inline:** spec §6.3 references `wizard_combo_attempts.combo_legs` and `expected_leg_count`. The actual schema has `legs JSONB` only. Task 14's implementation derives `expected_leg_count` from `len(attempt.legs)` and uses the `legs` column directly. Documented at the top of the plan and in the code.

---
