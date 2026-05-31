"""Most-specific-wins policy resolution. Spec §5.2, codex N-S1."""
from __future__ import annotations

from xenon.execution.brackets.policies import PolicyRow, deduplicate_by_specificity


def _row(rule_kind, broker=None, env=None, account=None, enabled=True, auto_place=True, config=None, policy_id=1):
    return PolicyRow(
        policy_id=policy_id,
        broker=broker,
        account_env=env,
        broker_account=account,
        asset_class="long_option",
        rule_kind=rule_kind,
        enabled=enabled,
        auto_place=auto_place,
        config=config or {"threshold_pct": -0.20, "anchor": "entry_price"},
    )


def test_account_specific_beats_broker_wide():
    """Codex N-S1 regression: account-specific ranks above broker-wide."""
    broker_wide = _row(
        "stop_loss",
        broker="IB",
        env=None,
        account=None,
        policy_id=1,
        config={"threshold_pct": -0.10, "anchor": "entry_price"},
    )
    account_specific = _row(
        "stop_loss",
        broker=None,
        env=None,
        account="DU1234567",
        policy_id=2,
        config={"threshold_pct": -0.20, "anchor": "entry_price"},
    )
    rows = [broker_wide, account_specific]
    rows.sort(
        key=lambda r: -(
            (4 if r.broker_account else 0)
            + (2 if r.account_env else 0)
            + (1 if r.broker else 0)
        )
    )
    deduped = deduplicate_by_specificity(rows)
    by_kind = {r.rule_kind: r for r in deduped}
    assert by_kind["stop_loss"].config["threshold_pct"] == -0.20


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
        _row(
            "trailing_tp",
            policy_id=2,
            config={"trail_pct": 0.25, "activation_pct": 0.30, "anchor": "mfe"},
        ),
    ]
    deduped = deduplicate_by_specificity(rows)
    kinds = sorted(r.rule_kind for r in deduped)
    assert kinds == ["stop_loss", "trailing_tp"]
