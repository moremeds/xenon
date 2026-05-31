"""CRUD + CAS for position_protection. Spec §5.1, §7."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from xenon.db.events import emit_outbox_in_txn
from xenon.db.schema import position_protection

CHANNEL_POSITION_RULE_TRANSITION = "position_rule.transition"
PAYLOAD_VERSION = 1


def insert_pending_arm(
    engine: Engine,
    *,
    broker: str,
    account_env: str,
    broker_account: str,
    position_key: str,
    position_descriptor: dict[str, Any],
    asset_class: str,
    rule_kind: str,
    config: dict[str, Any],
) -> int | None:
    """Insert a PENDING_ARM row; return protection_id or None on conflict."""
    with engine.begin() as conn:
        stmt = (
            pg_insert(position_protection)
            .values(
                broker=broker,
                account_env=account_env,
                broker_account=broker_account,
                position_key=position_key,
                position_descriptor=position_descriptor,
                asset_class=asset_class,
                rule_kind=rule_kind,
                state="PENDING_ARM",
                config=config,
            )
            .on_conflict_do_nothing()
            .returning(position_protection.c.protection_id)
        )
        row = conn.execute(stmt).first()
        if row is None:
            return None

        protection_id = int(row[0])
        emit_outbox_in_txn(
            conn,
            channel=CHANNEL_POSITION_RULE_TRANSITION,
            source="insert_pending_arm",
            payload={
                "payload_version": PAYLOAD_VERSION,
                "protection_id": protection_id,
                "position_key": position_key,
                "rule_kind": rule_kind,
                "old_state": None,
                "new_state": "PENDING_ARM",
                "reason": "fill_recorded",
                "context": {"asset_class": asset_class},
                "scope": {
                    "broker": broker,
                    "account_env": account_env,
                    "broker_account": broker_account,
                },
            },
        )
        return protection_id


def _transition_timestamp_column(new_state: str) -> str | None:
    return {
        "ARMED": "armed_at",
        "TRIGGERED": "triggered_at",
        "CLOSED": "closed_at",
        "CANCELED": "closed_at",
        "FAILED": "closed_at",
        "SUPERSEDED": "closed_at",
    }.get(new_state)


def cas_transition(
    engine: Engine,
    *,
    protection_id: int,
    expected_state: str,
    new_state: str,
    reason: str,
    context: dict[str, Any] | None = None,
    state_data_patch: dict[str, Any] | None = None,
    native_order_perm_id: int | None = None,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> bool:
    """Optimistic state transition plus outbox emit. Return True on success."""
    now = datetime.now(timezone.utc)
    scope_filters = []
    if broker is not None:
        scope_filters.append(position_protection.c.broker == broker)
    if account_env is not None:
        scope_filters.append(position_protection.c.account_env == account_env)
    if broker_account is not None:
        scope_filters.append(position_protection.c.broker_account == broker_account)

    with engine.begin() as conn:
        existing = conn.execute(
            select(
                position_protection.c.state_data,
                position_protection.c.broker,
                position_protection.c.account_env,
                position_protection.c.broker_account,
                position_protection.c.position_key,
                position_protection.c.rule_kind,
            ).where(position_protection.c.protection_id == protection_id, *scope_filters)
        ).first()
        if existing is None:
            return False

        values: dict[str, Any] = {"state": new_state, "updated_at": now}
        timestamp_col = _transition_timestamp_column(new_state)
        if timestamp_col:
            values[timestamp_col] = now
        if native_order_perm_id is not None:
            values["native_order_perm_id"] = native_order_perm_id
        if state_data_patch is not None:
            merged = dict(existing.state_data or {})
            merged.update(state_data_patch)
            values["state_data"] = merged

        result = conn.execute(
            update(position_protection)
            .where(
                position_protection.c.protection_id == protection_id,
                position_protection.c.state == expected_state,
                *scope_filters,
            )
            .values(**values)
            .returning(position_protection.c.protection_id)
        ).first()
        if result is None:
            return False

        emit_outbox_in_txn(
            conn,
            channel=CHANNEL_POSITION_RULE_TRANSITION,
            source="cas_transition",
            payload={
                "payload_version": PAYLOAD_VERSION,
                "protection_id": protection_id,
                "position_key": existing.position_key,
                "rule_kind": existing.rule_kind,
                "old_state": expected_state,
                "new_state": new_state,
                "reason": reason,
                "context": context or {},
                "scope": {
                    "broker": existing.broker,
                    "account_env": existing.account_env,
                    "broker_account": existing.broker_account,
                },
            },
        )
        return True


def list_active_rows(
    engine: Engine,
    *,
    broker: str,
    account_env: str,
    broker_account: str,
    states: tuple[str, ...] = ("PENDING_ARM", "ARMED", "TRIGGERED"),
) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        result = conn.execute(
            select(position_protection).where(
                position_protection.c.broker == broker,
                position_protection.c.account_env == account_env,
                position_protection.c.broker_account == broker_account,
                position_protection.c.state.in_(states),
            )
        )
        return [dict(row._mapping) for row in result]


def get_by_id(
    engine: Engine,
    *,
    protection_id: int,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> dict[str, Any] | None:
    scope_filters = []
    if broker is not None:
        scope_filters.append(position_protection.c.broker == broker)
    if account_env is not None:
        scope_filters.append(position_protection.c.account_env == account_env)
    if broker_account is not None:
        scope_filters.append(position_protection.c.broker_account == broker_account)
    with engine.connect() as conn:
        row = conn.execute(
            select(position_protection).where(position_protection.c.protection_id == protection_id, *scope_filters)
        ).first()
        return dict(row._mapping) if row else None
