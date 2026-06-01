"""POST /futu/history/sync — scope-gated, validates `since`, returns
sync result envelope.
"""

from unittest.mock import patch

from fastapi.testclient import TestClient

from xenon.api.server import app


def test_invalid_since_returns_422(futu_pytest_app_scope):
    with TestClient(app) as client:
        r = client.post("/futu/history/sync", json={"since": "yesterday"})
    assert r.status_code == 422
    assert r.json()["detail"]["reason_code"] == "since_unparseable"


def test_out_of_range_since_returns_422(futu_pytest_app_scope):
    with TestClient(app) as client:
        r = client.post("/futu/history/sync", json={"since": "2020-01-01"})
    assert r.status_code == 422
    assert r.json()["detail"]["reason_code"] == "since_out_of_range"


def test_auto_returns_envelope(futu_pytest_app_scope):
    with patch("xenon.api.routes.futu_history.futu_history_sync") as mock_run:
        mock_run.return_value = {
            "status": "ok",
            "fills_added": 0,
            "cash_flows_added": 0,
            "nav_rows_added": 0,
            "warnings": [],
        }
        with TestClient(app) as client:
            r = client.post("/futu/history/sync", json={"since": "auto"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "fills_added" in body
