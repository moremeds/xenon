"""Unit tests for the Operator console aggregate helpers in server.py."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import sqlalchemy as sa

from xenon.api.server import (
    EXPECTED_WRITERS,
    _ib_auth_verdict,
    _service_health_rows,
    _uw_api_health,
)
from xenon.db.engine import get_sync_engine
from xenon.db.schema import service_health, uw_api_stats
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


# --- _uw_api_health -----------------------------------------------------


def test_uw_health_latest_row(pg_session):
    with get_sync_engine().begin() as c:
        c.execute(
            sa.insert(uw_api_stats).values(
                bucket_hour=datetime(2026, 6, 15, 14, tzinfo=timezone.utc),
                requests=10,
                cache_hits=4,
                latency_sum=Decimal("300"),
                latency_count=3,
                status_2xx=10,
                status_4xx=0,
                status_5xx=0,
            )
        )
    h = _uw_api_health()
    assert h is not None
    assert h["requests"] == 10
    assert h["latency_avg_ms"] == 100.0


def test_uw_health_empty(pg_session):
    assert _uw_api_health() is None


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
