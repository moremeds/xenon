"""Fixed take-profit for credit spreads. Spec §3.1, §9.1."""
from __future__ import annotations

from xenon.execution.brackets.rules.base import ArmResult, Decision, register
from xenon.execution.brackets.triggers import debit_to_close_at_credit_pct


class TakeProfitFixedRule:
    rule_kind = "take_profit_fixed"

    def arm(self, *, scope, position, config, state_data, executor=None) -> ArmResult:
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
            return Decision(
                kind="TRIGGERED",
                reason="credit_spread_tp_hit",
                context={"debit_to_close": debit, "credit": credit},
            )
        return Decision(kind="NO_OP")

    def disarm(self, *, scope, position, native_perm_id, executor=None) -> None:
        return None


register(TakeProfitFixedRule())
