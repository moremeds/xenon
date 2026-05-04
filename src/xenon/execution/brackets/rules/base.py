"""RuleEvaluator Protocol for position-rules plug-ins.

Spec §9. The handler dispatches arm/evaluate/disarm per row; rule modules stay
broker-agnostic unless a concrete rule explicitly receives an executor boundary.
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
