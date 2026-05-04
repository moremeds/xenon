"""StopLoss rule. Spec §3.1, §9.1."""
from __future__ import annotations

from xenon.execution.brackets.rules.base import ArmResult, Decision, register
from xenon.execution.brackets.triggers import threshold_crossed_below


class StopLossRule:
    rule_kind = "stop_loss"

    def arm(self, *, scope, position, config, state_data) -> ArmResult:
        asset_class = position.get("asset_class")
        if asset_class in ("debit_combo", "credit_spread"):
            return ArmResult(kind="SYNTHETIC_ONLY", reason="bag_combo_no_native_bracket")
        return ArmResult(kind="RETRY", reason="needs_subprocess_executor")

    def evaluate(self, *, scope, position, config, state_data, marks) -> Decision:
        anchor = position["anchor_price"]
        asset_class = position.get("asset_class")
        mark = marks.get("mark")
        if mark is None:
            return Decision(kind="NO_OP", reason="missing_mark")

        if asset_class in ("stock", "long_option"):
            threshold = anchor * (1 + config["threshold_pct"])
            if threshold_crossed_below(mark=mark, threshold=threshold):
                return Decision(
                    kind="TRIGGERED",
                    reason="mark_below_threshold",
                    context={"mark": mark, "threshold": threshold},
                )
            return Decision(kind="NO_OP")

        if asset_class == "credit_spread":
            debit = marks.get("debit_to_close", 0.0)
            credit = position.get("credit_received", 0.0)
            multiplier = config.get("mark_multiple_of_credit", 2.0)
            spot = marks.get("underlying_spot")
            short_strike = position.get("short_strike")
            short_right = position.get("short_right")

            if credit and debit >= multiplier * credit:
                return Decision(
                    kind="TRIGGERED",
                    reason="debit_breach",
                    context={"debit_to_close": debit, "credit": credit, "multiple": multiplier},
                )

            if (
                config.get("underlying_breach_short_strike")
                and spot is not None
                and short_strike is not None
            ):
                breached = (spot <= short_strike) if short_right == "P" else (spot >= short_strike)
                if breached:
                    return Decision(
                        kind="TRIGGERED",
                        reason="underlying_breach_short_strike",
                        context={"spot": spot, "short_strike": short_strike},
                    )
            return Decision(kind="NO_OP")

        if asset_class == "debit_combo":
            threshold = anchor * (1 - config["threshold_pct_of_max_loss"])
            if mark <= threshold:
                return Decision(
                    kind="TRIGGERED",
                    reason="combo_synthetic_below_threshold",
                    context={"mark": mark, "threshold": threshold},
                )
            return Decision(kind="NO_OP")

        return Decision(kind="NO_OP", reason="unsupported_asset_class")

    def disarm(self, *, scope, position, native_perm_id) -> None:
        return None


register(StopLossRule())
