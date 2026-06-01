"""Cross-broker /equity-curve route: IB + Futu lines + optional combined."""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import text

from xenon.api.server import app
from xenon.db.engine import get_sync_engine
from xenon.db.queries.futu_history import write_nav_row
from xenon.execution.account_scope import AccountScope


def _seed_ib_nav(d: date, nav: float):
    with get_sync_engine().begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO xenon.nav_history
                    (broker, account_env, broker_account, date, nav, nav_type,
                     total, cash, stock_value, options_value)
                VALUES ('IB', 'paper', 'pytest', :d, :nav, 'reported',
                        :nav, 0, :nav, 0)
                ON CONFLICT DO NOTHING
                """
            ),
            {"d": d, "nav": nav},
        )


def _cleanup_ib_nav():
    with get_sync_engine().begin() as conn:
        conn.execute(
            text(
                "DELETE FROM xenon.nav_history WHERE broker='IB' AND account_env='paper'   AND broker_account='pytest'"
            )
        )


def test_per_broker_keys(clean_scope, futu_pytest_app_scope):
    futu_scope = AccountScope("FUTU", "paper", "pytest")
    write_nav_row(
        futu_scope,
        date(2024, 5, 1),
        Decimal("1000"),
        "reconstructed",
        Decimal("1000"),
        Decimal("0"),
    )
    _seed_ib_nav(date(2024, 5, 1), 2000.0)
    try:
        with TestClient(app) as client:
            r = client.get(
                "/equity-curve",
                params={"brokers": "IB,FUTU", "combined": "true"},
            )
        assert r.status_code == 200
        body = r.json()
        assert set(body["per_broker"].keys()) == {"IB", "FUTU"}
        # Combined renders only for dates with both brokers populated.
        assert body["combined"][0]["nav"] == 3000.0
    finally:
        _cleanup_ib_nav()
