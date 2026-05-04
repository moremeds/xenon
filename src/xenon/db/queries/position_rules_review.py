"""Sidecar annotations for position-rules ops review."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from xenon.db.schema import position_rules_review

_VERDICTS = ("expected", "unexpected", "structural")


def add_annotation(
    engine: Engine,
    *,
    protection_id: int,
    event_id: int,
    reviewed_by: str,
    verdict: str,
    note: str | None = None,
) -> int | None:
    if verdict not in _VERDICTS:
        raise ValueError(f"Unsupported verdict: {verdict!r}")

    with engine.begin() as conn:
        stmt = (
            pg_insert(position_rules_review)
            .values(
                protection_id=protection_id,
                event_id=event_id,
                reviewed_by=reviewed_by,
                verdict=verdict,
                note=note,
            )
            .on_conflict_do_nothing(index_elements=["event_id"])
            .returning(position_rules_review.c.review_id)
        )
        row = conn.execute(stmt).first()
        return int(row.review_id) if row else None


def list_annotations(engine: Engine, *, since_event_id: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(position_rules_review)
            .where(position_rules_review.c.event_id > since_event_id)
            .order_by(position_rules_review.c.event_id.desc())
            .limit(limit)
        ).all()
    return [dict(row._mapping) for row in rows]
