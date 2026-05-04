"""Pydantic shapes for the JSONB columns on position_protection.

Spec §5.5. PositionDescriptor and per-kind configs are frozen at insert time;
StateData is runtime mutable state.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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
    asset_class: Literal[
        "stock",
        "long_option",
        "debit_combo",
        "credit_spread",
        "covered_call",
        "unclassified",
    ]
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
            raise ValueError("stop_loss threshold_pct must be negative")
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


_CONFIG_MODELS: dict[str, type[_Frozen]] = {
    "stop_loss": StopLossConfig,
    "trailing_tp": TrailingTpConfig,
    "take_profit_fixed": TakeProfitFixedConfig,
    "combo_tp_alert": ComboTpAlertConfig,
}


def config_model_for(rule_kind: str) -> type[_Frozen]:
    return _CONFIG_MODELS[rule_kind]
