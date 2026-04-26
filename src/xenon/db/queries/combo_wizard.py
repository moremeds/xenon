"""Sync query functions for combo wizard tables (wizard_sessions,
wizard_combo_attempts, wizard_session_events, wizard_protection).

All functions take a sync sqlalchemy.Connection, not AsyncConnection.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Connection, insert, select, text, update

from xenon.db.schema import (
    wizard_combo_attempts,
    wizard_events,
    wizard_protection,
    wizard_sessions,
)


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
        )
    )


def get_session(conn: Connection, session_id: str) -> dict | None:
    row = conn.execute(select(wizard_sessions).where(wizard_sessions.c.session_id == session_id)).first()
    return dict(row._mapping) if row else None


def list_sessions(conn: Connection, *, limit: int = 50) -> list[dict]:
    result = conn.execute(select(wizard_sessions).order_by(wizard_sessions.c.updated_at.desc()).limit(limit))
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


def list_rehydratable(conn: Connection) -> list[dict]:
    result = conn.execute(
        select(wizard_sessions).where(
            wizard_sessions.c.state.in_(["submitting", "working", "reprice_pending", "protection_pending", "protected"])
        )
    )
    return [dict(r._mapping) for r in result]


# ── wizard_combo_attempts ──


def create_attempt(conn: Connection, **fields) -> None:
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


# ── wizard_protection ──


def upsert_protection(conn: Connection, session_id: str, **fields) -> None:
    existing = conn.execute(
        select(wizard_protection.c.protection_id).where(wizard_protection.c.session_id == session_id)
    ).first()
    now = datetime.now(timezone.utc)
    if existing is None:
        conn.execute(
            insert(wizard_protection).values(
                session_id=session_id,
                protection_type=fields.get("protection_type", "combo_tp_alert"),
                config=fields.get("config", {}),
                state=fields.get("state", "active"),
                created_at=now,
                **{k: v for k, v in fields.items() if k not in ("protection_type", "config", "state", "created_at")},
            )
        )
    else:
        fields.pop("created_at", None)
        conn.execute(update(wizard_protection).where(wizard_protection.c.session_id == session_id).values(**fields))


def list_protected_sessions(conn: Connection) -> list[dict]:
    """Join wizard_sessions + wizard_protection for PROTECTED state with alert enabled."""
    result = conn.execute(
        text("""
            SELECT s.session_id, s.ticker, s.payload, p.config, p.state as protection_state
            FROM xenon.wizard_sessions s
            JOIN xenon.wizard_protection p ON p.session_id = s.session_id
            WHERE UPPER(s.state) = 'PROTECTED'
              AND p.state = 'active'
        """)
    )
    return [dict(r._mapping) for r in result]


# ── orders_submissions helpers (for single_leg_rehydrate) ──


def list_unresolved_orders(conn: Connection) -> list[dict]:
    from xenon.db.schema import order_submissions

    result = conn.execute(
        select(order_submissions).where(order_submissions.c.state.in_(["PENDING", "WORKING", "PARTIALLY_FILLED"]))
    )
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
