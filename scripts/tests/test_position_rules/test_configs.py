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
    cfg = TrailingTpConfig.model_validate(
        {
            "trail_pct": 0.25,
            "activation_pct_of_max_gain": 0.25,
            "anchor": "mfe_pnl_dollars",
        }
    )
    assert cfg.trail_pct == 0.25
    assert cfg.activation_pct_of_max_gain == 0.25


def test_take_profit_fixed_credit_spread():
    cfg = TakeProfitFixedConfig.model_validate(
        {
            "close_at_credit_pct": 0.50,
            "anchor": "synthetic_mark",
        }
    )
    assert cfg.close_at_credit_pct == 0.50


def test_combo_tp_alert_config():
    cfg = ComboTpAlertConfig.model_validate({"threshold_pct": 0.50, "auto_place": False})
    assert cfg.auto_place is False


def test_combo_tp_alert_config_accepts_wizard_absolute_threshold():
    cfg = ComboTpAlertConfig.model_validate({"alert_net_mid_threshold": "1.25", "auto_place": False})
    assert cfg.alert_net_mid_threshold == 1.25


def test_position_descriptor_minimum():
    descriptor = PositionDescriptor.model_validate(
        {
            "asset_class": "long_option",
            "opened_at": "2026-05-04T14:23:11Z",
            "source": "fastapi_orders_place",
            "anchor_price": 5.20,
            "anchor_currency": "USD",
            "opened_qty": 1,
            "protected_qty": 1,
            "multiplier": 100,
            "qty_unit": "contract",
            "legs": [
                {
                    "sec_type": "OPT",
                    "symbol": "GOOG",
                    "expiry": "20260417",
                    "strike": 315.0,
                    "right": "C",
                    "action": "BUY",
                    "ratio": 1,
                    "fill_price": 5.20,
                    "con_id": 123456789,
                }
            ],
        }
    )
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
