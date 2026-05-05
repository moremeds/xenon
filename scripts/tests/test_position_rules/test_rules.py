"""Per-rule_kind arm + evaluate tests. Spec §9.1."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from xenon.execution.brackets.executor.ib_executor import PlaceResult
from xenon.execution.brackets.rules.base import RULE_REGISTRY
from xenon.execution.brackets.rules.combo_tp_alert import ComboTpAlertRule
from xenon.execution.brackets.rules.stop_loss import StopLossRule
from xenon.execution.brackets.rules.take_profit_fixed import TakeProfitFixedRule
from xenon.execution.brackets.rules.trailing_tp import TrailingTpRule


def _pos(asset_class, anchor_price, **kw):
    return {"asset_class": asset_class, "anchor_price": anchor_price, **kw}


def test_registry_populated():
    assert "stop_loss" in RULE_REGISTRY
    assert "trailing_tp" in RULE_REGISTRY
    assert "take_profit_fixed" in RULE_REGISTRY
    assert "combo_tp_alert" in RULE_REGISTRY


def test_stop_loss_arm_combo_returns_synthetic_only():
    rule = StopLossRule()
    result = rule.arm(
        scope=None,
        position=_pos("debit_combo", 1.50),
        config={"threshold_pct_of_max_loss": 0.50, "anchor": "synthetic_mark"},
        state_data={},
    )
    assert result.kind == "SYNTHETIC_ONLY"


def test_stop_loss_evaluate_below_threshold_fires():
    rule = StopLossRule()
    config = {"threshold_pct": -0.20, "anchor": "entry_price"}
    decision = rule.evaluate(
        scope=None,
        position=_pos("long_option", 5.00),
        config=config,
        state_data={},
        marks={"mark": 3.99},
    )
    assert decision.kind == "TRIGGERED"


def test_stop_loss_evaluate_above_threshold_no_op():
    rule = StopLossRule()
    config = {"threshold_pct": -0.20, "anchor": "entry_price"}
    decision = rule.evaluate(
        scope=None,
        position=_pos("long_option", 5.00),
        config=config,
        state_data={},
        marks={"mark": 4.01},
    )
    assert decision.kind == "NO_OP"


def test_stop_loss_arm_native_path_calls_executor():
    rule = StopLossRule()
    executor = MagicMock()
    executor.attach_native_stp.return_value = PlaceResult(
        perm_id=999,
        ib_order_id=1,
        status="Submitted",
        raw={},
    )
    pos = {
        "asset_class": "long_option",
        "anchor_price": 5.00,
        "protected_qty": 1,
        "multiplier": 100,
        "legs": [
            {
                "sec_type": "OPT",
                "symbol": "GOOG",
                "expiry": "20260417",
                "strike": 315.0,
                "right": "C",
                "action": "BUY",
                "con_id": 12345,
                "fill_price": 5.00,
            }
        ],
    }
    result = rule.arm(
        scope=None,
        position=pos,
        config={"threshold_pct": -0.20, "anchor": "entry_price"},
        state_data={},
        executor=executor,
    )
    assert result.kind == "NATIVE_ARMED"
    assert result.perm_id == 999
    executor.attach_native_stp.assert_called_once()
    args = executor.attach_native_stp.call_args.kwargs
    assert args["stop_price"] == 4.00
    assert args["close_action"] == "SELL"


def test_trailing_tp_evaluate_below_activation_no_op_but_updates_mfe():
    rule = TrailingTpRule()
    config = {"trail_pct": 0.25, "activation_pct": 0.30, "anchor": "mfe"}
    decision = rule.evaluate(
        scope=None,
        position=_pos("long_option", 5.00),
        config=config,
        state_data={"mfe": None},
        marks={"mark": 5.50},
    )
    assert decision.kind == "UPDATE_STATE"
    assert decision.state_data_patch["mfe"] == 5.50


def test_trailing_tp_evaluate_after_activation_fires_on_drop():
    rule = TrailingTpRule()
    config = {"trail_pct": 0.25, "activation_pct": 0.30, "anchor": "mfe"}
    state_data = {"mfe": 7.00}
    decision = rule.evaluate(
        scope=None,
        position=_pos("long_option", 5.00),
        config=config,
        state_data=state_data,
        marks={"mark": 5.20},
    )
    assert decision.kind == "TRIGGERED"


def test_take_profit_fixed_credit_spread_fires():
    rule = TakeProfitFixedRule()
    config = {"close_at_credit_pct": 0.50, "anchor": "synthetic_mark"}
    pos = _pos("credit_spread", 1.00, credit_received=1.00)
    decision = rule.evaluate(
        scope=None,
        position=pos,
        config=config,
        state_data={},
        marks={"debit_to_close": 0.40},
    )
    assert decision.kind == "TRIGGERED"


def test_take_profit_fixed_credit_spread_no_op():
    rule = TakeProfitFixedRule()
    config = {"close_at_credit_pct": 0.50, "anchor": "synthetic_mark"}
    pos = _pos("credit_spread", 1.00, credit_received=1.00)
    decision = rule.evaluate(
        scope=None,
        position=pos,
        config=config,
        state_data={},
        marks={"debit_to_close": 0.51},
    )
    assert decision.kind == "NO_OP"


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


def test_combo_tp_alert_absolute_wizard_threshold_fires_on_drop():
    rule = ComboTpAlertRule()
    config = {"alert_net_mid_threshold": "1.25", "auto_place": False, "min_realert_interval_s": 3600}
    decision = rule.evaluate(
        scope=None,
        position=_pos("debit_combo", 2.50),
        config=config,
        state_data={"last_alert_at": None},
        marks={"mark": 1.20, "now": datetime(2026, 5, 4, 14, 30, tzinfo=timezone.utc)},
    )
    assert decision.kind == "TRIGGERED"
    assert decision.reason == "alert_only_threshold_crossed"
    assert decision.context["threshold"] == 1.25


def test_combo_tp_alert_debit_absolute_threshold_fires_on_rise():
    rule = ComboTpAlertRule()
    config = {
        "alert_net_mid_threshold": "3.00",
        "polarity": "DEBIT",
        "auto_place": False,
        "min_realert_interval_s": 3600,
    }
    decision = rule.evaluate(
        scope=None,
        position=_pos("debit_combo", 2.50),
        config=config,
        state_data={"last_alert_at": None},
        marks={"mark": 3.05, "now": datetime(2026, 5, 4, 14, 30, tzinfo=timezone.utc)},
    )
    assert decision.kind == "TRIGGERED"


def test_combo_tp_alert_credit_absolute_threshold_fires_on_drop():
    rule = ComboTpAlertRule()
    config = {
        "alert_net_mid_threshold": "-0.45",
        "polarity": "CREDIT",
        "auto_place": False,
        "min_realert_interval_s": 3600,
    }
    decision = rule.evaluate(
        scope=None,
        position=_pos("credit_spread", -1.00),
        config=config,
        state_data={"last_alert_at": None},
        marks={"mark": -0.50, "now": datetime(2026, 5, 4, 14, 30, tzinfo=timezone.utc)},
    )
    assert decision.kind == "TRIGGERED"


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
