"""GET /futu/equity-curve — NAV + recon merged by date, with C33 range
validation."""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from xenon.api.server import app
from xenon.db.queries.futu_history import write_nav_row, write_recon_daily
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_equity_curve_returns_rows(clean_scope, futu_pytest_app_scope):
    write_nav_row(
        SCOPE,
        date(2024, 5, 1),
        Decimal("10000"),
        "reconstructed",
        Decimal("10000"),
        Decimal("0"),
    )
    write_nav_row(
        SCOPE,
        date(2024, 5, 2),
        Decimal("10100"),
        "reported",
        Decimal("10000"),
        Decimal("100"),
        is_today=True,
    )
    write_recon_daily(
        SCOPE,
        date(2024, 5, 2),
        Decimal("10100"),
        Decimal("10000"),
        Decimal("25"),
    )
    with TestClient(app) as client:
        r = client.get(
            "/futu/equity-curve",
            params={"from": "2024-05-01", "to": "2024-05-02"},
        )
    assert r.status_code == 200
    rows = r.json()
    by_d = {row["date"]: row for row in rows}
    assert by_d["2024-05-01"]["source"] == "reconstructed"
    assert by_d["2024-05-02"]["recon"]["delta"] == 100.0


def test_equity_curve_validates_range(futu_pytest_app_scope):
    with TestClient(app) as client:
        r = client.get(
            "/futu/equity-curve",
            params={"from": "2020-01-01", "to": "2024-05-01"},
        )
    assert r.status_code == 422


def test_equity_curve_rejects_inverted_range(futu_pytest_app_scope):
    # C33: from > to.
    with TestClient(app) as client:
        r = client.get(
            "/futu/equity-curve",
            params={"from": "2024-12-01", "to": "2024-06-01"},
        )
    assert r.status_code == 422
    assert r.json()["detail"]["reason_code"] == "date_range_inverted"
