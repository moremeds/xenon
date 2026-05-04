"""Trailing take-profit. Spec §3.1, §9.1."""
from __future__ import annotations

from xenon.execution.brackets.rules.base import ArmResult, Decision, register
from xenon.execution.brackets.triggers import apply_trail_after_activation


class TrailingTpRule:
    rule_kind = "trailing_tp"

    def arm(self, *, scope, position, config, state_data) -> ArmResult:
        asset_class = position.get("asset_class")
        if asset_class in ("debit_combo", "credit_spread"):
            return ArmResult(kind="SYNTHETIC_ONLY")
        return ArmResult(kind="RETRY", reason="needs_subprocess_executor")

    def evaluate(self, *, scope, position, config, state_data, marks) -> Decision:
        mark = marks.get("mark")
        if mark is None:
            return Decision(kind="NO_OP", reason="missing_mark")

        activation_pct = config.get("activation_pct") or config.get("activation_pct_of_max_gain") or 0.0
        fired, new_mfe = apply_trail_after_activation(
            anchor_price=position["anchor_price"],
            current_mark=mark,
            current_mfe=state_data.get("mfe"),
            trail_pct=config["trail_pct"],
            activation_pct=activation_pct,
        )
        if fired:
            return Decision(
                kind="TRIGGERED",
                reason="trail_breached",
                context={"mark": mark, "mfe": new_mfe, "trail_pct": config["trail_pct"]},
            )
        if new_mfe != state_data.get("mfe"):
            return Decision(kind="UPDATE_STATE", state_data_patch={"mfe": new_mfe})
        return Decision(kind="NO_OP")

    def disarm(self, *, scope, position, native_perm_id) -> None:
        return None


register(TrailingTpRule())
