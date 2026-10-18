"""PR-08: `ck_order_sub_state` CHECK on order_submissions.state.

- The migration round-trips on a throwaway local DB that holds PENDING and
  UNKNOWN rows (no row is changed), rejects an unlisted state after the
  upgrade, and refuses to upgrade over an unlisted state (it never rewrites
  rows to make the constraint fit).
- Old-writer test (spec §2.11 adv-18): the previous release's
  `orders_store` writers (copied from origin/master, statement for
  statement) still succeed against the migrated schema.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, insert, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from xenon.db.schema import order_submissions, regime_overrides
from xenon.execution import orders_store

_ROOT = Path(__file__).resolve().parents[2]
_MIG_DIR = _ROOT / "src" / "xenon" / "db" / "migrations"
_REV = "2026_10_18_order_state_check"
_PARENT = "2026_10_16_scope_attempt_legs"


def _migration_module():
    path = _MIG_DIR / "versions" / f"{_REV}.py"
    spec = importlib.util.spec_from_file_location("_pr08_mig", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_state_list_matches_order_states():
    """The migration freezes its list (old revisions must not change when
    the code list grows); this pins it to the code's single source."""
    assert tuple(_migration_module().ORDER_STATES) == orders_store.ORDER_STATES


def test_order_states_cover_every_state_constant():
    """Every state set the code reads or writes is a subset of ORDER_STATES,
    and every state seen in prod (lead inventory 2026-10-18) is listed."""
    states = set(orders_store.ORDER_STATES)
    assert orders_store._TERMINAL_STATES <= states
    assert set(orders_store._ACTIVE_STATES) <= states
    assert set(orders_store.NON_TERMINAL_STATES) <= states
    assert {"CANCELLED", "FAILED", "FILLED", "REJECTED", "UNKNOWN", "WORKING"} <= states
    assert {"PENDING", "PARTIALLY_FILLED"} <= states


# ---------------------------------------------------------------------------
# Throwaway DB (local only; name contains "test")
# ---------------------------------------------------------------------------


@pytest.fixture
def throwaway_db():
    base_env = os.environ.get("DATABASE_URL_TEST")
    if not base_env:
        pytest.skip("DATABASE_URL_TEST not set")
    base = make_url(base_env)
    if base.host not in ("127.0.0.1", "localhost"):
        pytest.skip("migration round-trip runs on a local Postgres only")
    plain = make_url(base.render_as_string(hide_password=False)).set(drivername="postgresql+psycopg")
    admin = create_engine(plain.set(database="postgres"), isolation_level="AUTOCOMMIT")
    name = f"xe_a13_mig_test_{uuid.uuid4().hex[:10]}"
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # pragma: no cover — env without CREATEDB
        admin.dispose()
        pytest.skip(f"cannot create throwaway DB: {exc}")
    url = plain.set(database=name)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS xenon"))
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS events"))
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
    try:
        yield engine
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:d AND pid<>pg_backend_pid()"
                ),
                {"d": name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def _cfg():
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_MIG_DIR))
    return cfg


def _row(sid: str, state: str) -> dict:
    now = datetime.now(timezone.utc)
    return dict(
        submission_id=sid,
        user_id="u",
        client_attempt_id=f"att-{sid}",
        ticker="SPY",
        security_type="STK",
        action="BUY",
        quantity=1,
        state=state,
        submitted_at=now,
        updated_at=now,
        broker="IB",
        account_env="paper",
        broker_account="DU_MIG",
    )


def _states(engine) -> dict[str, str]:
    with engine.connect() as c:
        return dict(c.execute(text("select submission_id, state from xenon.order_submissions")).all())


def _has_check(engine) -> bool:
    with engine.connect() as c:
        return c.execute(text("select count(*) from pg_constraint where conname='ck_order_sub_state'")).scalar() == 1


def test_migration_upgrade_downgrade_with_pending_and_unknown_rows(throwaway_db):
    from alembic import command

    engine = throwaway_db
    cfg = _cfg()
    command.upgrade(cfg, _PARENT)
    seeded = {"m-pending": "PENDING", "m-unknown": "UNKNOWN", "m-working": "WORKING", "m-filled": "FILLED"}
    with engine.begin() as conn:
        for sid, state in seeded.items():
            conn.execute(insert(order_submissions).values(**_row(sid, state)))

    command.upgrade(cfg, _REV)
    assert _has_check(engine)
    assert _states(engine) == seeded  # no row changed, no terminalization
    with engine.connect() as c:
        assert c.execute(text("select convalidated from pg_constraint where conname='ck_order_sub_state'")).scalar()
    for state in orders_store.ORDER_STATES:
        with engine.begin() as conn:
            conn.execute(insert(order_submissions).values(**_row(f"ok-{state}", state)))
    with pytest.raises(IntegrityError, match="ck_order_sub_state"):
        with engine.begin() as conn:
            conn.execute(insert(order_submissions).values(**_row("bad-1", "SUBMITTED")))
    with pytest.raises(IntegrityError, match="ck_order_sub_state"):
        with engine.begin() as conn:
            conn.execute(
                update(order_submissions)
                .where(order_submissions.c.submission_id == "m-pending")
                .values(state="working")
            )

    command.downgrade(cfg, _PARENT)
    assert not _has_check(engine)
    assert {k: v for k, v in _states(engine).items() if k in seeded} == seeded

    # An unlisted state blocks the upgrade loudly; the row is never rewritten.
    with engine.begin() as conn:
        conn.execute(insert(order_submissions).values(**_row("bad-2", "SUBMITTED")))
    with pytest.raises(Exception, match="SUBMITTED"):
        command.upgrade(cfg, _REV)
    assert not _has_check(engine)
    assert _states(engine)["bad-2"] == "SUBMITTED"
    with engine.begin() as conn:
        conn.execute(text("delete from xenon.order_submissions where submission_id='bad-2'"))
    command.upgrade(cfg, "head")
    assert _has_check(engine)


# ---------------------------------------------------------------------------
# Old-writer test: origin/master orders_store writers vs the migrated schema
# ---------------------------------------------------------------------------
# Copied from origin/master (2e1562b3) src/xenon/execution/orders_store.py:
# reserve_attempt INSERT (state PENDING), mark_submitted (WORKING),
# mark_terminal (any target state, optional expected_states),
# register_from_snapshot INSERT + resurrect (WORKING), and
# db/queries/combo_wizard.update_order_state (raw state).


def _old_reserve(conn, sid: str) -> None:
    now = datetime.now(timezone.utc)
    conn.execute(
        pg_insert(order_submissions)
        .values(
            submission_id=sid,
            user_id="old",
            client_attempt_id=f"old-{sid}",
            ticker="SPY",
            security_type="STK",
            action="BUY",
            quantity=1,
            expiry=None,
            strike=None,
            right=None,
            multiplier=1,
            con_id=None,
            limit_price=Decimal("1.00"),
            state="PENDING",
            submitted_at=now,
            updated_at=now,
            broker="IB",
            account_env="paper",
            broker_account="DU_OLD",
            legs=None,
        )
        .on_conflict_do_nothing()
    )


def _old_mark_submitted(conn, sid: str) -> None:
    conn.execute(
        update(order_submissions)
        .where(order_submissions.c.submission_id == sid)
        .values(
            ib_order_id="7", perm_id="7007", placing_client_id=0, state="WORKING", updated_at=datetime.now(timezone.utc)
        )
    )
    conn.execute(
        update(regime_overrides).where(regime_overrides.c.submission_id == sid).values(perm_id=7007, ib_order_id=7)
    )


def _old_mark_terminal(conn, sid: str, state: str, expected_states=None) -> int:
    stmt = update(order_submissions).where(order_submissions.c.submission_id == sid)
    if expected_states is not None:
        stmt = stmt.where(order_submissions.c.state.in_(expected_states))
    return conn.execute(
        stmt.values(
            state=state, reason_code=None, filled_qty=0, avg_fill_price=None, updated_at=datetime.now(timezone.utc)
        )
    ).rowcount


def _old_snapshot_insert(conn, perm: str) -> None:
    now = datetime.now(timezone.utc)
    conn.execute(
        pg_insert(order_submissions)
        .values(
            submission_id=f"snapshot-{perm}",
            user_id="snapshot",
            client_attempt_id=f"snapshot-{perm}",
            ticker="SPY",
            security_type="STK",
            action="BUY",
            quantity=1,
            multiplier=1,
            ib_order_id="9",
            perm_id=perm,
            limit_price=1.0,
            state="WORKING",
            submitted_at=now,
            updated_at=now,
            modify_sequence=0,
            broker="IB",
            account_env="paper",
            broker_account="DU_OLD",
            tif="DAY",
        )
        .on_conflict_do_nothing()
    )


def test_old_writer_lifecycle_against_migrated_schema(pg_test_engine):
    """Every state transition the previous release writes passes the CHECK."""
    with pg_test_engine.connect() as c:
        assert c.execute(text("select count(*) from pg_constraint where conname='ck_order_sub_state'")).scalar() == 1
    targets = ("FILLED", "REJECTED", "CANCELLED", "FAILED", "PARTIALLY_FILLED", "UNKNOWN")
    with pg_test_engine.begin() as conn:
        for state in targets:
            sid = f"old-{state}"
            _old_reserve(conn, sid)
            _old_mark_submitted(conn, sid)
            assert _old_mark_terminal(conn, sid, state, expected_states=("WORKING",)) == 1
        _old_reserve(conn, "old-reject-pending")
        assert _old_mark_terminal(conn, "old-reject-pending", "REJECTED") == 1
        _old_snapshot_insert(conn, "880088")
        # register_from_snapshot resurrect + combo_wizard.update_order_state
        conn.execute(
            update(order_submissions)
            .where(order_submissions.c.submission_id == "old-CANCELLED")
            .values(state="WORKING", reason_code=None)
        )
        conn.execute(
            update(order_submissions).where(order_submissions.c.submission_id == "old-UNKNOWN").values(state="UNKNOWN")
        )
        got = dict(
            conn.execute(
                text("select submission_id, state from xenon.order_submissions where broker_account='DU_OLD'")
            ).all()
        )
        conn.rollback()
    assert got["old-FILLED"] == "FILLED"
    assert got["old-CANCELLED"] == "WORKING"
    assert got["old-reject-pending"] == "REJECTED"
    assert got["snapshot-880088"] == "WORKING"


def test_old_writer_unlisted_state_is_rejected(pg_test_engine):
    with pytest.raises(IntegrityError, match="ck_order_sub_state"):
        with pg_test_engine.begin() as conn:
            _old_reserve(conn, "old-bad")
            _old_mark_terminal(conn, "old-bad", "EXPIRED")
