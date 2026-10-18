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
from pathlib import Path

import pytest
from sqlalchemy import create_engine, insert, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from xenon.db.schema import order_submissions
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
# Old-writer gate (spec §2.11 adv-18): the REAL previous orders_store
# ---------------------------------------------------------------------------
# The package `src/xenon` is extracted at each pinned commit with
# `git archive` and imported in a child process (PYTHONPATH first, so no
# candidate module is loaded: the previous schema.py, engine.py and
# orders_store.py run as released). Its writers then run against the
# throwaway DB migrated to this PR's head.
#   2e1562b3 = v0.8.4, the release running in prod (lead inventory 2026-10-18)
#   14cf70e5 = origin/master when this gate was written (adds PR #192 writers)

_OLD_WRITER_SHAS = {
    "v0.8.4": "2e1562b3ca43f4b48872f84e68fcad0f1052b352",
    "origin-master": "14cf70e5fc3734d92dfd280f2e5ff1712c5b3a24",
}

_OLD_WRITER_SCRIPT = r"""
import json, os, sys
from datetime import datetime, timezone
from decimal import Decimal

import xenon
assert xenon.__file__.startswith(os.environ["OLD_SRC"]), xenon.__file__
from xenon.execution import orders_store as old

SCOPE = dict(broker="IB", account_env="paper", broker_account="DU_OLDW")
now = datetime.now(timezone.utc)

def req(qty=1):
    return old.RequestRow(ticker="SPY", security_type="STK", action="BUY", quantity=qty,
                          multiplier=1, limit_price=Decimal("1.00"))

out = {}
win = old.reserve_attempt("old", "ow-fill", req(), **SCOPE)
sid = win.submission_id
old.mark_submitted(submission_id=sid, ib_order_id="71", perm_id="7171", placing_client_id=0)
old.record_fill(exec_id="ow-exec-1", submission_id=sid, perm_id="7171", con_id=None, ticker="SPY",
                side="BUY", qty=Decimal(1), price=Decimal("1.00"), filled_at=now, **SCOPE)
out["fill_terminal"] = old.mark_terminal(submission_id=sid, state="FILLED", reason_code=None,
                                         filled_qty=1, avg_fill_price=Decimal("1.00"))
for st in ("REJECTED", "CANCELLED", "FAILED", "PARTIALLY_FILLED"):
    w = old.reserve_attempt("old", f"ow-{st}", req(), **SCOPE)
    out[st] = old.mark_terminal(submission_id=w.submission_id, state=st, reason_code="OLD",
                                filled_qty=0, avg_fill_price=None)
snap = dict(perm_id="8181", ib_order_id="81", ticker="SPY", security_type="STK", action="BUY",
            quantity=1, limit_price=1.0, **SCOPE)
out["snap_insert"] = old.register_from_snapshot(**snap)["action"]
old.mark_terminal(submission_id="snapshot-8181", state="CANCELLED", reason_code="OLD",
                  filled_qty=0, avg_fill_price=None)
out["snap_resurrect"] = old.register_from_snapshot(**snap)["action"]
print(json.dumps(out))
"""


def _extract_old_src(sha: str, dest: Path) -> Path:
    import shutil
    import subprocess
    import tarfile
    import io

    if shutil.which("git") is None:
        pytest.skip("git unavailable: cannot load the previous release's orders_store")
    probe = subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=_ROOT, capture_output=True)
    if probe.returncode != 0:
        pytest.skip(f"pinned commit {sha} not in this clone (fetch origin): old-writer gate cannot run")
    blob = subprocess.run(
        ["git", "archive", "--format=tar", sha, "src/xenon"], cwd=_ROOT, capture_output=True, check=True
    )
    with tarfile.open(fileobj=io.BytesIO(blob.stdout)) as tar:
        tar.extractall(dest, filter="data")
    return dest / "src"


@pytest.mark.parametrize("release", sorted(_OLD_WRITER_SHAS))
def test_old_writer_runs_against_migrated_schema(release, throwaway_db, tmp_path):
    import json
    import subprocess
    import sys

    from alembic import command

    command.upgrade(_cfg(), "head")
    assert _has_check(throwaway_db)
    src = _extract_old_src(_OLD_WRITER_SHAS[release], tmp_path / "old")
    script = tmp_path / "old_writers.py"
    script.write_text(_OLD_WRITER_SCRIPT)
    env = {k: v for k, v in os.environ.items() if not k.startswith("XENON_") and k != "PYTHONPATH"}
    env.update(PYTHONPATH=str(src), OLD_SRC=str(src), DATABASE_URL=os.environ["DATABASE_URL"])
    run = subprocess.run([sys.executable, str(script)], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr[-3000:]
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out == {
        "fill_terminal": 1,
        "REJECTED": 1,
        "CANCELLED": 1,
        "FAILED": 1,
        "PARTIALLY_FILLED": 1,
        "snap_insert": "INSERTED",
        "snap_resurrect": "RESURRECTED",
    }
    with throwaway_db.connect() as c:
        states = dict(
            c.execute(
                text("select client_attempt_id, state from xenon.order_submissions where broker_account='DU_OLDW'")
            ).all()
        )
        fills = c.execute(text("select count(*) from xenon.order_fills where exec_id='ow-exec-1'")).scalar()
    assert states["ow-fill"] == "FILLED" and states["snapshot-8181"] == "WORKING"
    assert fills == 1


def test_unlisted_state_is_rejected_after_upgrade(throwaway_db):
    from alembic import command

    command.upgrade(_cfg(), "head")
    with pytest.raises(IntegrityError, match="ck_order_sub_state"):
        with throwaway_db.begin() as conn:
            conn.execute(insert(order_submissions).values(**_row("bad-x", "EXPIRED")))


def test_migration_sets_lock_timeout(throwaway_db):
    """A long transaction holding order_submissions makes the upgrade fail
    fast (lock_timeout 5s) instead of queueing order writes behind the
    ACCESS EXCLUSIVE request; after it ends, a re-run succeeds."""
    import time

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
    assert not _has_check(throwaway_db)
    command.upgrade(cfg, _REV)
    assert _has_check(throwaway_db)
