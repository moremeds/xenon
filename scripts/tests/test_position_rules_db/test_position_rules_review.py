from __future__ import annotations

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.db.queries.position_rules_review import add_annotation, list_annotations


@pytest.fixture
def engine():
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_rules_review WHERE event_id < 0"))
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_rules_review WHERE event_id < 0"))


def test_add_annotation_is_idempotent_by_event(engine):
    review_id = add_annotation(
        engine,
        protection_id=123,
        event_id=-1,
        reviewed_by="codex-test",
        verdict="expected",
        note="ok",
    )
    assert review_id is not None

    duplicate = add_annotation(
        engine,
        protection_id=123,
        event_id=-1,
        reviewed_by="codex-test",
        verdict="unexpected",
    )
    assert duplicate is None

    rows = list_annotations(engine, since_event_id=-2)
    row = next(row for row in rows if row["event_id"] == -1)
    assert row["verdict"] == "expected"
    assert row["note"] == "ok"
