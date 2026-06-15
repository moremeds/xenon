"""Unit tests for the Operator console aggregate helpers in server.py."""

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from xenon.api.server import (
    EXPECTED_WRITERS,
    _ib_auth_verdict,
    _order_submissions_health,
    _service_health_rows,
    _snapshotter_health,
)
from xenon.db.engine import get_sync_engine
from xenon.db.schema import account_snapshots, order_submissions, service_health
from xenon.db.service_health import record_service_health

# --- _ib_auth_verdict ---------------------------------------------------


def test_unreachable_when_port_closed():
    assert _ib_auth_verdict({"port_listening": False}, {}) == "unreachable"


def test_awaiting_when_upstream_dead():
    assert _ib_auth_verdict({"port_listening": True, "upstream_dead": True}, {}) == "awaiting"


def test_authenticated_when_any_role_connected():
    assert _ib_auth_verdict({"port_listening": True}, {"sync": {"connected": True}}) == "authenticated"


def test_unknown_when_listening_but_no_role():
    assert _ib_auth_verdict({"port_listening": True}, {"sync": {"connected": False}}) == "unknown"


# --- _snapshotter_health / _order_submissions_health scope filter ------


def test_snapshotter_health_scope_filter(pg_session):
    older = datetime(2026, 6, 15, 10, tzinfo=timezone.utc)
    newer = datetime(2026, 6, 15, 14, tzinfo=timezone.utc)
    with get_sync_engine().begin() as c:
        c.execute(
            sa.insert(account_snapshots).values(
                account="A",
                bankroll=0,
                snapshot_at=older,
                broker="IB",
                account_env="paper",
                broker_account="DU_A",
            )
        )
        c.execute(
            sa.insert(account_snapshots).values(
                account="B",
                bankroll=0,
                snapshot_at=newer,
                broker="IB",
                account_env="live",
                broker_account="U_B",
            )
        )
    # Unscoped → global max (the newer, other-scope row).
    assert _snapshotter_health()["last_write_at"].startswith("2026-06-15T14")
    # Scoped → only the active scope's row, not the newer other-scope one.
    scoped = _snapshotter_health({"broker": "IB", "account_env": "paper", "broker_account": "DU_A"})
    assert scoped["last_write_at"].startswith("2026-06-15T10")


def test_order_submissions_health_scope_filter(pg_session):
    now = datetime.now(timezone.utc)
    rows = [
        ("s1", "paper", "DU_A"),
        ("s2", "paper", "DU_A"),
        ("s3", "live", "U_B"),
    ]
    with get_sync_engine().begin() as c:
        for sid, env, acct in rows:
            c.execute(
                sa.insert(order_submissions).values(
                    submission_id=sid,
                    ticker="AAPL",
                    security_type="STK",
                    action="BUY",
                    quantity=1,
                    state="UNKNOWN",
                    submitted_at=now,
                    broker="IB",
                    account_env=env,
                    broker_account=acct,
                )
            )
    # Unscoped counts all 3 UNKNOWN rows.
    assert _order_submissions_health()["unknown_count"] == 3
    # Scoped counts only the active scope's 2 rows.
    scoped = _order_submissions_health({"broker": "IB", "account_env": "paper", "broker_account": "DU_A"})
    assert scoped["unknown_count"] == 2


# --- _service_health_rows ----------------------------------------------


def test_service_health_rows(pg_session):
    record_service_health("ib_activity_poller", "ok")
    record_service_health("naked_short_audit", "error", error={"m": 1})
    rows = _service_health_rows()
    by_name = {r["service"]: r for r in rows}

    assert by_name["ib_activity_poller"]["state"] == "ok"
    assert isinstance(by_name["ib_activity_poller"]["age_secs"], int)
    assert by_name["naked_short_audit"]["state"] == "error"

    # every expected writer appears (missing ones synthesized)
    assert set(EXPECTED_WRITERS) <= set(by_name)
    missing = [r for r in rows if r["state"] == "missing"]
    assert all(m["age_secs"] is None for m in missing)

    names = [r["service"] for r in rows]
    assert names == sorted(names)


def test_service_health_rows_handles_naive_timestamp(pg_session):
    naive = (datetime.now(timezone.utc) - timedelta(seconds=120)).replace(tzinfo=None)
    with get_sync_engine().begin() as c:
        c.execute(
            sa.insert(service_health).values(
                service="ib_activity_poller",
                broker="IB",
                account_env="paper",
                broker_account="DU0000000",
                state="ok",
                updated_at=naive,
            )
        )
    row = next(r for r in _service_health_rows() if r["service"] == "ib_activity_poller")
    assert row["age_secs"] is not None and row["age_secs"] >= 0


def test_service_health_rows_surfaces_futu_history_cross_scope(pg_session):
    # futu_history records under the FUTU account scope, which never matches the
    # IB-scoped reader. It must still surface (matched by service name across
    # scopes), not appear as the synthesized "missing" row.
    record_service_health(
        "futu_history",
        "ok",
        broker="FUTU",
        account_env="live",
        broker_account="999",
    )
    by_name = {r["service"]: r for r in _service_health_rows()}
    assert by_name["futu_history"]["state"] == "ok"
    assert isinstance(by_name["futu_history"]["age_secs"], int)
