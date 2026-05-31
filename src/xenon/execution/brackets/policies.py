"""Pure-Python helpers for in-memory policy merging. Spec §5.2."""
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
    """Keep the first enabled row per rule_kind from pre-sorted specificity rows."""
    seen: dict[str, PolicyRow] = {}
    for row in rows:
        if not row.enabled:
            continue
        seen.setdefault(row.rule_kind, row)
    return list(seen.values())
