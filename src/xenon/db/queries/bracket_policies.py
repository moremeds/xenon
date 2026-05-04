"""Most-specific-wins resolver for bracket_policies. Spec §5.2."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from xenon.execution.brackets.policies import PolicyRow


def resolve_for_scope(
    engine: Engine,
    *,
    broker: str,
    account_env: str,
    broker_account: str,
    asset_class: str,
) -> list[PolicyRow]:
    sql = text(
        """
        SELECT
          policy_id, broker, account_env, broker_account,
          asset_class, rule_kind, enabled, auto_place, config
        FROM xenon.bracket_policies
        WHERE asset_class = :asset_class
          AND (broker IS NULL OR broker = :broker)
          AND (account_env IS NULL OR account_env = :account_env)
          AND (broker_account IS NULL OR broker_account = :broker_account)
        ORDER BY
          (CASE WHEN broker_account IS NOT NULL THEN 4 ELSE 0 END
         + CASE WHEN account_env    IS NOT NULL THEN 2 ELSE 0 END
         + CASE WHEN broker         IS NOT NULL THEN 1 ELSE 0 END) DESC,
          policy_id ASC
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(
            sql,
            {
                "asset_class": asset_class,
                "broker": broker,
                "account_env": account_env,
                "broker_account": broker_account,
            },
        ).all()

    return [
        PolicyRow(
            policy_id=row.policy_id,
            broker=row.broker,
            account_env=row.account_env,
            broker_account=row.broker_account,
            asset_class=row.asset_class,
            rule_kind=row.rule_kind,
            enabled=row.enabled,
            auto_place=row.auto_place,
            config=row.config,
        )
        for row in rows
    ]
