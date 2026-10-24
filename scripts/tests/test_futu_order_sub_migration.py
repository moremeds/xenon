"""X3a Task 2: additive migration `2026_10_24_futu_order_sub`.

order_submissions accepts broker FUTU (`ck_order_sub_broker`) and gains
`broker_order_id` with a partial unique index per scope. IB rows, the other
order_submissions constraints/indexes, trades, the wizard tables and
order_fills are unchanged. Downgrade refuses (no row change) while FUTU rows
or broker_order_id values exist. Runs on a throwaway local DB.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

from scripts.tests import test_order_state_check_migration as _mig
from scripts.tests.test_order_state_check_migration import _cfg, _row
from xenon.db.schema import order_fills, order_submissions

throwaway_db = _mig.throwaway_db  # shared fixture: throwaway local DB, dropped after the test

_REV = "2026_10_24_futu_order_sub"
_PARENT = "2026_10_20_drop_signal_tables"
_ACC = "281756479345015383"  # made-up Futu acc_id
_OLD = "ck_order_sub_broker_ib_only"


def _futu_row(sid: str, broker_order_id: str | None, *, env="paper", acc=_ACC) -> dict:
    row = _row(sid, "WORKING")
    row.update(broker="FUTU", account_env=env, broker_account=acc, broker_order_id=broker_order_id)
    return row


def _constraints(engine, table: str) -> dict[str, str]:
    with engine.connect() as c:
        return dict(
            c.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = to_regclass(:t) ORDER BY conname"
                ),
                {"t": f"xenon.{table}"},
            ).all()
        )


def _indexes(engine, table: str) -> dict[str, str]:
    with engine.connect() as c:
        return dict(
            c.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='xenon' AND tablename=:t"),
                {"t": table},
            ).all()
        )


def _rows(engine) -> dict[str, dict]:
    with engine.connect() as c:
        rows = c.execute(text("SELECT * FROM xenon.order_submissions ORDER BY submission_id")).mappings().all()
    return {r["submission_id"]: {k: v for k, v in r.items() if k != "broker_order_id"} for r in rows}


def _has_column(engine) -> bool:
    with engine.connect() as c:
        return (
            c.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns WHERE table_schema='xenon' "
                    "AND table_name='order_submissions' AND column_name='broker_order_id'"
                )
            ).scalar()
            == 1
        )


def _insert(engine, row: dict, table=order_submissions) -> None:
    with engine.begin() as conn:
        conn.execute(insert(table).values(**row))


def test_upgrade_allows_futu_keeps_ib_and_downgrade_round_trips(throwaway_db):
    from alembic import command

    engine = throwaway_db
    cfg = _cfg()
    command.upgrade(cfg, _PARENT)
    ib_rows = {"ib-1": "WORKING", "ib-2": "FILLED"}
    for sid, state in ib_rows.items():
        row = _row(sid, state)
        row.update(ib_order_id=f"9{sid[-1]}", perm_id=f"77{sid[-1]}")
        _insert(engine, row)
    with pytest.raises(IntegrityError, match="ck_order_sub_broker_ib_only"):
        _insert(engine, {k: v for k, v in _futu_row("futu-0", None).items() if k != "broker_order_id"})

    before_rows = _rows(engine)
    before_ck = _constraints(engine, "order_submissions")
    before_ix = _indexes(engine, "order_submissions")
    before_other = {t: _constraints(engine, t) for t in ("trades", "wizard_sessions", "wizard_combo_attempts", "order_fills")}
    assert before_ck[_OLD] == "CHECK ((broker = 'IB'::text))"

    command.upgrade(cfg, _REV)

    # IB rows unchanged; new column NULL on them.
    assert _rows(engine) == before_rows
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM xenon.order_submissions WHERE broker_order_id IS NOT NULL")).scalar() == 0
    # Only the broker CHECK changed; every other constraint and index is identical.
    after_ck = _constraints(engine, "order_submissions")
    assert _OLD not in after_ck
    assert after_ck["ck_order_sub_broker"] == "CHECK ((broker = ANY (ARRAY['IB'::text, 'FUTU'::text])))"
    assert {k: v for k, v in after_ck.items() if k != "ck_order_sub_broker"} == {
        k: v for k, v in before_ck.items() if k != _OLD
    }
    after_ix = _indexes(engine, "order_submissions")
    assert after_ix.pop("uq_order_sub_broker_order_id") == (
        "CREATE UNIQUE INDEX uq_order_sub_broker_order_id ON xenon.order_submissions "
        "USING btree (broker, account_env, broker_account, broker_order_id) WHERE (broker_order_id IS NOT NULL)"
    )
    assert after_ix == before_ix
    assert {t: _constraints(engine, t) for t in before_other} == before_other

    # FUTU accepted; the existing scope-attempt keys still apply to it.
    _insert(engine, _futu_row("futu-1", "FUT-1001"))
    _insert(engine, _futu_row("futu-2", None))
    _insert(engine, _futu_row("futu-3", None))  # many NULL ids
    _insert(engine, _futu_row("futu-4", "FUT-1001", env="live"))  # same id, other scope
    _insert(engine, _futu_row("futu-5", "FUT-1001", acc="281756479345015999"))
    ib = _row("ib-3", "WORKING")
    ib["broker_order_id"] = "FUT-1001"  # other broker, same id
    _insert(engine, ib)
    with pytest.raises(IntegrityError, match="uq_order_sub_broker_order_id"):
        _insert(engine, _futu_row("futu-dup", "FUT-1001"))
    dup_attempt = _futu_row("futu-dup2", "FUT-2002")
    dup_attempt["client_attempt_id"] = "att-futu-1"
    with pytest.raises(IntegrityError, match="uq_order_sub_scope_attempt|uq_order_sub_user_attempt"):
        _insert(engine, dup_attempt)
    bad = _futu_row("bad-broker", None)
    bad["broker"] = "SCHWAB"
    with pytest.raises(IntegrityError, match="ck_order_sub_broker"):
        _insert(engine, bad)
    # order_fills already accepts FUTU fills linked to a FUTU submission.
    _insert(
        engine,
        dict(
            exec_id=f"futu:{_ACC}:ord:FUT-1001:1",
            submission_id="futu-1",
            ticker="SPY",
            side="BUY",
            qty=1,
            price=1,
            filled_at=datetime.now(timezone.utc),
            broker="FUTU",
            account_env="paper",
            broker_account=_ACC,
        ),
        table=order_fills,
    )

    # Downgrade refuses while FUTU rows / broker_order_id values exist; nothing changes.
    snapshot = _rows(engine)
    with pytest.raises(Exception, match="non-IB row"):
        command.downgrade(cfg, _PARENT)
    assert _rows(engine) == snapshot and _has_column(engine)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM xenon.order_fills WHERE broker='FUTU'"))
        conn.execute(text("DELETE FROM xenon.order_submissions WHERE broker='FUTU'"))
    with pytest.raises(Exception, match="1 row\\(s\\) with broker_order_id"):
        command.downgrade(cfg, _PARENT)
    assert _has_column(engine)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM xenon.order_submissions WHERE submission_id='ib-3'"))

    command.downgrade(cfg, _PARENT)
    assert not _has_column(engine)
    assert _constraints(engine, "order_submissions") == before_ck
    assert _indexes(engine, "order_submissions") == before_ix
    assert _rows(engine) == before_rows
    command.upgrade(cfg, "head")
    assert _has_column(engine)


def test_schema_metadata_matches_migrated_table(throwaway_db):
    """schema.py declares exactly what the migration builds for order_submissions."""
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from xenon.db.schema import xenon_metadata

    command.upgrade(_cfg(), "head")
    ck = {c.name: str(c.sqltext) for c in order_submissions.constraints if c.name and c.name.startswith("ck_")}
    assert ck["ck_order_sub_broker"] == "broker IN ('IB','FUTU')"
    assert _OLD not in ck
    ix = {i.name: i for i in order_submissions.indexes}["uq_order_sub_broker_order_id"]
    assert ix.unique and [c.name for c in ix.columns] == ["broker", "account_env", "broker_account", "broker_order_id"]
    assert str(ix.dialect_options["postgresql"]["where"]) == "broker_order_id IS NOT NULL"

    with throwaway_db.connect() as conn:
        ctx = MigrationContext.configure(
            conn,
            opts={
                "include_schemas": True,
                "include_name": lambda name, type_, parent: type_ != "schema" or name == "xenon",
                "include_object": lambda obj, name, type_, reflected, compare_to: (
                    getattr(obj, "name", None) == "order_submissions"
                    if type_ == "table"
                    else getattr(getattr(obj, "table", None), "name", None) == "order_submissions"
                ),
            },
        )
        diffs = compare_metadata(ctx, xenon_metadata)
    assert diffs == []


def test_migration_sets_lock_timeout(throwaway_db):
    from alembic import command

    cfg = _cfg()
    command.upgrade(cfg, _PARENT)
    holder = throwaway_db.connect()
    tx = holder.begin()
    holder.execute(text("LOCK TABLE xenon.order_submissions IN ACCESS SHARE MODE"))
    try:
        started = time.monotonic()
        with pytest.raises(Exception, match="lock timeout"):
            command.upgrade(cfg, _REV)
        assert time.monotonic() - started < 30
    finally:
        tx.rollback()
        holder.close()
    assert not _has_column(throwaway_db)
    command.upgrade(cfg, _REV)
    assert _has_column(throwaway_db)
