"""Sync query functions for combo wizard tables.

All functions take a sync sqlalchemy.Connection, not AsyncConnection.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Connection, insert, select, text, update

from xenon.db.schema import (
    position_protection,
    wizard_combo_attempts,
    wizard_events,
    wizard_sessions,
)
from xenon.execution.brackets.asset_class import AssetClass, classify_position
from xenon.execution.brackets.position_key import compute_position_key


def create_session(
    conn: Connection,
    *,
    session_id: str,
    ticker: str,
    state: str,
    structure_name: str | None = None,
    intent: str | None = None,
    payload: dict | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    broker: str = "IB",
    account_env: str = "legacy_unknown",
    broker_account: str = "legacy_unknown",
) -> None:
    now = created_at or datetime.now(timezone.utc)
    conn.execute(
        insert(wizard_sessions).values(
            session_id=session_id,
            ticker=ticker,
            state=state,
            structure_name=structure_name,
            intent=intent,
            payload=payload,
            created_at=now,
            updated_at=updated_at or now,
            broker=broker,
            account_env=account_env,
            broker_account=broker_account,
        )
    )


def get_session(conn: Connection, session_id: str) -> dict | None:
    row = conn.execute(select(wizard_sessions).where(wizard_sessions.c.session_id == session_id)).first()
    return dict(row._mapping) if row else None


def list_sessions(
    conn: Connection,
    *,
    limit: int = 50,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> list[dict]:
    conditions = []
    if broker is not None:
        conditions.append(wizard_sessions.c.broker == broker)
    if account_env is not None:
        conditions.append(wizard_sessions.c.account_env == account_env)
    if broker_account is not None:
        conditions.append(wizard_sessions.c.broker_account == broker_account)
    stmt = select(wizard_sessions).order_by(wizard_sessions.c.updated_at.desc()).limit(limit)
    if conditions:
        stmt = stmt.where(*conditions)
    result = conn.execute(stmt)
    return [dict(r._mapping) for r in result]


def update_session(conn: Connection, session_id: str, **fields) -> int:
    if "updated_at" not in fields:
        fields["updated_at"] = datetime.now(timezone.utc)
    result = conn.execute(update(wizard_sessions).where(wizard_sessions.c.session_id == session_id).values(**fields))
    return result.rowcount


def claim_session_for_submit(conn: Connection, session_id: str, attempt_id: str) -> dict | None:
    """CAS: set state=submitting + current_attempt_id only if state=PLANNED and no current attempt."""
    now = datetime.now(timezone.utc)
    result = conn.execute(
        update(wizard_sessions)
        .where(
            wizard_sessions.c.session_id == session_id,
            wizard_sessions.c.state.ilike("planned"),
            wizard_sessions.c.current_attempt_id.is_(None),
        )
        .values(state="submitting", current_attempt_id=attempt_id, updated_at=now)
        .returning(*wizard_sessions.c)
    )
    row = result.first()
    return dict(row._mapping) if row else None


def release_submit_claim(conn: Connection, session_id: str, attempt_id: str) -> None:
    now = datetime.now(timezone.utc)
    conn.execute(
        update(wizard_sessions)
        .where(
            wizard_sessions.c.session_id == session_id,
            wizard_sessions.c.current_attempt_id == attempt_id,
            wizard_sessions.c.state.ilike("submitting"),
        )
        .values(state="planned", current_attempt_id=None, updated_at=now)
    )


def list_rehydratable(
    conn: Connection,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> list[dict]:
    conditions = [
        wizard_sessions.c.state.in_(["submitting", "working", "reprice_pending", "protection_pending", "protected"])
    ]
    if broker is not None:
        conditions.append(wizard_sessions.c.broker == broker)
    if account_env is not None:
        conditions.append(wizard_sessions.c.account_env == account_env)
    if broker_account is not None:
        conditions.append(wizard_sessions.c.broker_account == broker_account)
    result = conn.execute(select(wizard_sessions).where(*conditions))
    return [dict(r._mapping) for r in result]


# ── wizard_combo_attempts ──


def create_attempt(conn: Connection, **fields) -> None:
    fields.setdefault("broker", "IB")
    fields.setdefault("account_env", "legacy_unknown")
    fields.setdefault("broker_account", "legacy_unknown")
    conn.execute(insert(wizard_combo_attempts).values(**fields))


def get_latest_attempt(conn: Connection, session_id: str) -> dict | None:
    row = conn.execute(
        select(wizard_combo_attempts)
        .where(wizard_combo_attempts.c.session_id == session_id)
        .order_by(wizard_combo_attempts.c.updated_at.desc())
        .limit(1)
    ).first()
    return dict(row._mapping) if row else None


def update_attempt(conn: Connection, attempt_id: str, **fields) -> int:
    if "updated_at" not in fields:
        fields["updated_at"] = datetime.now(timezone.utc)
    result = conn.execute(
        update(wizard_combo_attempts).where(wizard_combo_attempts.c.attempt_id == attempt_id).values(**fields)
    )
    return result.rowcount


# ── wizard_session_events (mapped to wizard_events table) ──


def record_event(conn: Connection, *, session_id: str, kind: str, detail: dict | None = None) -> None:
    conn.execute(
        insert(wizard_events).values(
            session_id=session_id,
            kind=kind,
            detail=detail,
            at=datetime.now(timezone.utc),
        )
    )


# ── position_protection-backed combo TP alert ──


_ACTIVE_PROTECTION_STATES = ("PENDING_ARM", "ARMED", "TRIGGERED")


def _normalize_combo_legs(ticker: str, legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for leg in legs:
        out = dict(leg)
        out["symbol"] = str(out.get("symbol") or out.get("localSymbol") or ticker).upper()
        out["sec_type"] = str(out.get("sec_type") or out.get("secType") or "OPT").upper()
        out["action"] = str(out.get("action") or "").upper()
        if out.get("right") is not None:
            out["right"] = str(out["right"]).upper()
        if out.get("strike") is not None:
            out["strike"] = float(out["strike"])
        if "con_id" not in out and out.get("conId") is not None:
            out["con_id"] = int(out["conId"])
        if "ratio" not in out:
            out["ratio"] = 1
        if out.get("fill_price") is not None:
            out["fill_price"] = float(out["fill_price"])
        normalized.append(out)
    return normalized


def _combo_protection_context(conn: Connection, session_id: str) -> dict[str, Any]:
    session = get_session(conn, session_id)
    if session is None:
        raise ValueError(f"Unknown wizard session {session_id}")

    attempt = get_latest_attempt(conn, session_id)
    legs = list((attempt or {}).get("legs") or (session.get("payload") or {}).get("legs") or [])
    if not legs:
        raise ValueError(f"Wizard session {session_id} has no combo legs for protection")

    ticker = str((attempt or {}).get("ticker") or session["ticker"])
    normalized_legs = _normalize_combo_legs(ticker, legs)
    classified = classify_position(legs=normalized_legs, wizard_session_payload=None, sibling_legs=None)
    asset_class = classified.asset_class.value
    anchor_price = float((attempt or {}).get("limit_price") or (session.get("payload") or {}).get("limitPrice") or 0.0)
    quantity = int((session.get("payload") or {}).get("quantity") or 1)
    descriptor = {
        "asset_class": asset_class,
        "wizard_session_id": session_id,
        "wizard_attempt_id": (attempt or {}).get("attempt_id"),
        "ticker": ticker,
        "structure_name": (attempt or {}).get("structure_name") or session.get("structure_name"),
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "source": "combo_wizard",
        "anchor_price": anchor_price,
        "anchor_currency": "USD",
        "opened_qty": quantity,
        "protected_qty": quantity,
        "multiplier": 100,
        "qty_unit": "spread",
        "legs": normalized_legs,
    }
    descriptor.update(_asset_class_descriptor_fields(classified.asset_class, descriptor))
    return {
        "session": session,
        "attempt": attempt,
        "descriptor": descriptor,
        "asset_class": asset_class,
        "position_key": compute_position_key(asset_class, descriptor),
    }


def _asset_class_descriptor_fields(asset_class: AssetClass, descriptor: dict[str, Any]) -> dict[str, Any]:
    if asset_class != AssetClass.CREDIT_SPREAD:
        return {}
    legs = descriptor["legs"]
    short = next((leg for leg in legs if leg.get("action") == "SELL"), None)
    long_ = next((leg for leg in legs if leg.get("action") == "BUY"), None)
    if short is None or long_ is None:
        return {}

    anchor_price = abs(float(descriptor.get("anchor_price") or 0.0))
    leg_credit = float(short.get("fill_price") or 0.0) - float(long_.get("fill_price") or 0.0)
    credit_received = anchor_price if anchor_price > 0 else max(leg_credit, 0.0)
    return {
        "credit_received": credit_received,
        "short_strike": float(short["strike"]),
        "short_right": short["right"],
    }


def upsert_protection(conn: Connection, session_id: str, **fields) -> None:
    ctx = _combo_protection_context(conn, session_id)
    session = ctx["session"]
    descriptor = ctx["descriptor"]
    position_key = ctx["position_key"]
    asset_class = ctx["asset_class"]
    config = dict(fields.get("config") or {})
    config["auto_place"] = False
    now = datetime.now(timezone.utc)

    existing = conn.execute(
        select(position_protection)
        .where(
            position_protection.c.broker == session["broker"],
            position_protection.c.account_env == session["account_env"],
            position_protection.c.broker_account == session["broker_account"],
            position_protection.c.position_key == position_key,
            position_protection.c.rule_kind == "combo_tp_alert",
            position_protection.c.state.in_(_ACTIVE_PROTECTION_STATES),
        )
        .order_by(position_protection.c.protection_id.desc())
        .limit(1)
    ).first()

    if existing is not None and dict(existing._mapping)["config"] == config:
        return

    if existing is not None:
        conn.execute(
            update(position_protection)
            .where(position_protection.c.protection_id == existing.protection_id)
            .values(state="SUPERSEDED", closed_at=now, updated_at=now)
        )

    state = fields.get("state", "PENDING_ARM")
    if str(state).lower() == "active":
        state = "PENDING_ARM"

    conn.execute(
        insert(position_protection).values(
            broker=session["broker"],
            account_env=session["account_env"],
            broker_account=session["broker_account"],
            position_key=position_key,
            position_descriptor=descriptor,
            asset_class=asset_class,
            rule_kind="combo_tp_alert",
            state=state,
            config=config,
            state_data={},
            triggered_at=fields.get("triggered_at"),
            created_at=fields.get("created_at", now),
            updated_at=now,
        )
    )


def get_protection(conn: Connection, session_id: str) -> dict | None:
    ctx = _combo_protection_context(conn, session_id)
    session = ctx["session"]
    row = conn.execute(
        select(position_protection)
        .where(
            position_protection.c.broker == session["broker"],
            position_protection.c.account_env == session["account_env"],
            position_protection.c.broker_account == session["broker_account"],
            position_protection.c.position_key == ctx["position_key"],
            position_protection.c.rule_kind == "combo_tp_alert",
            position_protection.c.state.in_(_ACTIVE_PROTECTION_STATES),
        )
        .order_by(position_protection.c.protection_id.desc())
        .limit(1)
    ).first()
    return dict(row._mapping) if row else None


def list_protected_sessions(
    conn: Connection,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> list[dict]:
    """Join wizard_sessions + position_protection for active combo TP alerts."""
    base_sql = """
        SELECT s.session_id, s.ticker, s.payload, p.config, p.state as protection_state
        FROM xenon.wizard_sessions s
        JOIN xenon.position_protection p
          ON p.position_descriptor->>'wizard_session_id' = s.session_id
        WHERE UPPER(s.state) = 'PROTECTED'
          AND p.rule_kind = 'combo_tp_alert'
          AND p.state IN ('PENDING_ARM','ARMED','TRIGGERED')
    """
    params: dict = {}
    if broker is not None:
        base_sql += " AND s.broker = :broker"
        params["broker"] = broker
    if account_env is not None:
        base_sql += " AND s.account_env = :account_env"
        params["account_env"] = account_env
    if broker_account is not None:
        base_sql += " AND s.broker_account = :broker_account"
        params["broker_account"] = broker_account
    result = conn.execute(text(base_sql), params)
    return [dict(r._mapping) for r in result]


# ── orders_submissions helpers (for single_leg_rehydrate) ──


DEFAULT_UNRESOLVED_STATES: tuple[str, ...] = ("PENDING", "WORKING", "PARTIALLY_FILLED")


def list_unresolved_orders(
    conn: Connection,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
    states: tuple[str, ...] = DEFAULT_UNRESOLVED_STATES,
) -> list[dict]:
    from xenon.db.schema import order_submissions

    conditions = [order_submissions.c.state.in_(list(states))]
    if broker is not None:
        conditions.append(order_submissions.c.broker == broker)
    if account_env is not None:
        conditions.append(order_submissions.c.account_env == account_env)
    if broker_account is not None:
        conditions.append(order_submissions.c.broker_account == broker_account)
    result = conn.execute(select(order_submissions).where(*conditions))
    return [dict(r._mapping) for r in result]


def update_order_state(conn: Connection, submission_id: str, state: str) -> None:
    from xenon.db.schema import order_submissions

    conn.execute(
        update(order_submissions)
        .where(order_submissions.c.submission_id == submission_id)
        .values(state=state, updated_at=datetime.now(timezone.utc))
    )


def get_order_modify_sequence(conn: Connection, *, ib_order_id: str = "", perm_id: str = "") -> int | None:
    from xenon.db.schema import order_submissions

    conditions = []
    if ib_order_id:
        conditions.append(order_submissions.c.ib_order_id == ib_order_id)
    if perm_id:
        conditions.append(order_submissions.c.perm_id == perm_id)
    if not conditions:
        return None
    from sqlalchemy import or_

    row = conn.execute(
        select(order_submissions.c.modify_sequence)
        .where(or_(*conditions))
        .order_by(order_submissions.c.updated_at.desc())
        .limit(1)
    ).first()
    return int(row[0]) if row else None
