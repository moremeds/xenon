"""GET /futu/reconciliation — daily + anomaly rows + current_delta."""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from xenon.api.server import app
from xenon.db.queries.futu_history import write_recon_anomaly, write_recon_daily
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_reconciliation_returns_latest_30(clean_scope, futu_pytest_app_scope):
    write_recon_daily(
        SCOPE,
        date(2024, 5, 1),
        Decimal("100"),
        Decimal("100"),
        Decimal("25"),
    )
    write_recon_anomaly(
        SCOPE,
        date(2024, 5, 1),
        notes={"anomaly_type": "missing_fx", "currency": "HKD"},
    )
    with TestClient(app) as client:
        r = client.get("/futu/reconciliation")
    assert r.status_code == 200
    body = r.json()
    assert "rows" in body and "current_delta" in body
    assert any(row["kind"] == "anomaly" for row in body["rows"])
