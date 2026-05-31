"""Alert-only combo take-profit threshold crossing. Spec §3.3, §9.1."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from xenon.execution.brackets.rules.base import ArmResult, Decision, register


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ComboTpAlertRule:
    rule_kind = "combo_tp_alert"

    def arm(self, *, scope, position, config, state_data, executor=None) -> ArmResult:
        return ArmResult(kind="SYNTHETIC_ONLY")

    def evaluate(self, *, scope, position, config, state_data, marks) -> Decision:
        mark = marks.get("mark")
        if mark is None:
            return Decision(kind="NO_OP", reason="missing_mark")

        if config.get("alert_net_mid_threshold") is not None:
            threshold = float(config["alert_net_mid_threshold"])
            polarity = str(config.get("polarity") or "").upper()
            if polarity == "DEBIT":
                crossed = mark >= threshold
            else:
                crossed = mark <= threshold
            if not crossed:
                return Decision(kind="NO_OP")
        else:
            threshold = position["anchor_price"] * (1 + config["threshold_pct"])
            if mark < threshold:
                return Decision(kind="NO_OP")

        now = marks.get("now") or datetime.now(timezone.utc)
        raw_last = state_data.get("last_alert_at")
        last = _parse_iso(raw_last) if isinstance(raw_last, str) else raw_last
        debounce = timedelta(seconds=config.get("min_realert_interval_s", 3600))
        if last is not None and (now - last) < debounce:
            return Decision(kind="NO_OP", reason="debounced")

        return Decision(
            kind="TRIGGERED",
            reason="alert_only_threshold_crossed",
            context={"mark": mark, "threshold": threshold, "alert_only": True},
            state_data_patch={"last_alert_at": now.isoformat()},
        )

    def disarm(self, *, scope, position, native_perm_id, executor=None) -> None:
        return None


register(ComboTpAlertRule())
