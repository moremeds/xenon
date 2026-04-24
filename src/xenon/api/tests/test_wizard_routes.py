from __future__ import annotations

import json
import os
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from xenon.api import server as server_mod
from xenon.execution import orders_store


@pytest.fixture(autouse=True)
def _force_test_mode_on(monkeypatch):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    yield


@pytest.fixture
def client():
    return TestClient(server_mod.app)


def _plan_payload() -> dict:
    return {
        "ticker": "AAPL",
        "intent": "OPEN",
        "legs": [
            {
                "contract_id": "AAPL_20260417_200_C",
                "action": "BUY",
                "right": "C",
                "strike": "200",
                "expiry": "20260417",
                "quantity": 1,
            },
            {
                "contract_id": "AAPL_20260417_210_C",
                "action": "SELL",
                "right": "C",
                "strike": "210",
                "expiry": "20260417",
                "quantity": 1,
            },
        ],
        "quotes": {
            "AAPL_20260417_200_C": {"bid": "4.50", "ask": "4.70"},
            "AAPL_20260417_210_C": {"bid": "2.00", "ask": "2.20"},
        },
        "order_payload": {
            "symbol": "AAPL",
            "type": "combo",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": "2.50",
            "legs": [
                {
                    "conId": 1001,
                    "action": "BUY",
                    "ratio": 1,
                    "exchange": "SMART",
                },
                {
                    "conId": 1002,
                    "action": "SELL",
                    "ratio": 1,
                    "exchange": "SMART",
                },
            ],
        },
    }


def _db_path() -> Path:
    return Path(os.environ["XENON_ORDERS_DB_PATH"])


def _seed_session(session_id: str, order_payload: dict) -> None:
    db_path = _db_path()
    orders_store.init_store(db_path)
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            """
            INSERT INTO wizard_sessions (
                session_id, ticker, state, structure_name, intent, payload_json, created_at, updated_at
            ) VALUES (?, 'AAPL', 'planned', 'Bull Call Spread', 'OPEN', ?, NOW(), NOW())
            """,
            [session_id, json.dumps(order_payload)],
        )
    finally:
        con.close()


def test_plan_endpoint_returns_combo_mode_and_prices(client):
    resp = client.post("/wizard/plan", json=_plan_payload())

    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "COMBO"
    assert body["natural_price"] == "2.70"
    assert body["mid_price"] == "2.50"
    assert body["session_id"]


def test_submit_endpoint_reuses_shared_combo_submission_path(client):
    _seed_session("wiz-submit-1", _plan_payload()["order_payload"])

    resp = client.post(
        "/wizard/sessions/wiz-submit-1/submit",
        json={"target_price": "2.45", "price_basis": "MID"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["submission_id"]
    assert body["echo"]["type"] == "combo"
    assert body["echo"]["action"] == "BUY"

    con = duckdb.connect(str(_db_path()))
    try:
        count = con.execute(
            """
            SELECT COUNT(*)
              FROM orders_submissions
             WHERE client_attempt_id LIKE 'wiz:wiz-submit-1:combo:%'
            """
        ).fetchone()[0]
    finally:
        con.close()

    assert count == 1


def test_reprice_endpoint_modifies_live_combo_order_not_leg_orders(client):
    _seed_session("wiz-reprice-1", _plan_payload()["order_payload"])

    submit_resp = client.post(
        "/wizard/sessions/wiz-reprice-1/submit",
        json={"target_price": "2.45", "price_basis": "MID"},
    )
    assert submit_resp.status_code == 200
    submit_body = submit_resp.json()

    resp = client.post(
        "/wizard/sessions/wiz-reprice-1/reprice",
        json={"target_price": "2.35"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["echo"]["orderId"] == submit_body["orderId"]
    assert body["echo"]["newPrice"] == "2.35"
