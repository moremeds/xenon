"""W2 — Historical Trades panel must show friendly empty state, not a 502,
when IB Flex Query credentials are unset.

Plan: docs/plans/2026-04-28-postgres-migration-completion-IMPL.md § W2.1
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from sqlalchemy import insert

from xenon.db.engine import get_sync_engine
from xenon.db.schema import trades


BROKER_SCOPE = {
    "broker": "IB",
    "account_env": "paper",
    "broker_account": "DU0000000",
}


@pytest.fixture
def unconfigured_client(monkeypatch):
    """FastAPI client with IB_FLEX_TOKEN + IB_FLEX_QUERY_ID unset."""
    monkeypatch.delenv("IB_FLEX_TOKEN", raising=False)
    monkeypatch.delenv("IB_FLEX_QUERY_ID", raising=False)
    from xenon.api import server
    from xenon.api.subprocess import ScriptResult

    async def flex_not_configured(*args, **kwargs):
        return ScriptResult(ok=False, error="FLEX_NOT_CONFIGURED", exit_code=2)

    monkeypatch.setattr(server, "run_module", flex_not_configured)

    return TestClient(server.app)


def test_blotter_returns_200_with_configured_false_when_flex_creds_missing(
    unconfigured_client,
):
    """Codex finding #4 / W2.1 acceptance.

    Without IB_FLEX_TOKEN / IB_FLEX_QUERY_ID, the legacy code path returned
    a 502 with the leaked CLI hint 'Run with --setup for configuration guide.',
    which the UI displayed as a red error. The fixed contract:

      - HTTP 200
      - body.configured == False
      - empty closed_trades / open_trades arrays
      - human-readable message field naming the missing env vars

    so the frontend can render an actionable empty state instead of an error.
    """
    resp = unconfigured_client.post("/blotter")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("configured") is False, f"Expected configured:false when Flex creds missing; got body={body}"
    assert body.get("closed_trades") == []
    assert body.get("open_trades") == []
    assert body.get("as_of") is None or body.get("as_of") == ""
    msg = body.get("message") or ""
    # Message must mention both env vars so users know exactly what to set.
    assert "IB_FLEX_TOKEN" in msg, f"message should mention IB_FLEX_TOKEN, got: {msg!r}"
    assert "IB_FLEX_QUERY_ID" in msg, f"message should mention IB_FLEX_QUERY_ID, got: {msg!r}"


def test_blotter_response_has_no_502_legacy_setup_hint(unconfigured_client):
    """Regression guard: the leaked 'Run with --setup' string must never
    surface in a 200 response or a 502 detail when creds are unset.
    """
    resp = unconfigured_client.post("/blotter")
    # No matter what status, the leaked CLI hint must not be the UI-facing message.
    assert "Run with --setup" not in resp.text, (
        "Legacy CLI setup hint leaked to API response; should be replaced by structured configured:false payload."
    )


def test_blotter_returns_postgres_rows_before_flex(monkeypatch):
    """When the execution ledger has trades, the route is PG-first.

    Missing Flex credentials must not hide captured order-pipeline fills.
    """
    from xenon.api import server

    async def fail_if_flex_called(*args, **kwargs):  # pragma: no cover - assertion guard
        raise AssertionError("Flex subprocess should not run when PG blotter has rows")

    monkeypatch.setattr(server, "run_module", fail_if_flex_called)

    opened_at = datetime(2026, 4, 28, 14, 30, tzinfo=timezone.utc)
    closed_at = datetime(2026, 4, 28, 15, 30, tzinfo=timezone.utc)
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(trades).values(
                ticker="AAPL",
                structure="Stock",
                action="BUY",
                quantity=100,
                entry_cost=1000,
                exit_cost=1240,
                realized_pnl=240,
                opened_at=opened_at,
                closed_at=closed_at,
                state="CLOSED",
                metadata={
                    "legs": [
                        {
                            "exec_id": "exec-pg-1",
                            "side": "BUY",
                            "qty": 100,
                            "price": "10.00",
                            "commission": "1.25",
                            "filled_at": opened_at.isoformat(),
                        },
                        {
                            "exec_id": "exec-pg-2",
                            "side": "SELL",
                            "qty": 100,
                            "price": "12.40",
                            "commission": "1.25",
                            "filled_at": closed_at.isoformat(),
                        },
                    ]
                },
                **BROKER_SCOPE,
            )
        )

    client = TestClient(server.app)
    resp = client.post("/blotter")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["configured"] is True
    assert body["source"] == "postgres"
    assert body["summary"]["closed_trades"] == 1
    assert body["closed_trades"][0]["symbol"] == "AAPL"
    assert body["closed_trades"][0]["executions"][-1]["exec_id"] == "exec-pg-2"


def test_blotter_get_returns_postgres_rows_without_flex(monkeypatch):
    """The read path must not use data/blotter.json or trigger Flex."""
    from xenon.api import server

    async def fail_if_flex_called(*args, **kwargs):  # pragma: no cover - assertion guard
        raise AssertionError("GET /blotter should not run Flex")

    monkeypatch.setattr(server, "run_module", fail_if_flex_called)

    opened_at = datetime(2026, 4, 28, 14, 30, tzinfo=timezone.utc)
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(trades).values(
                ticker="MSFT",
                structure="Stock",
                action="BUY",
                quantity=50,
                entry_cost=500,
                opened_at=opened_at,
                state="OPEN",
                metadata={
                    "legs": [
                        {
                            "exec_id": "exec-pg-open",
                            "side": "BUY",
                            "qty": 50,
                            "price": "10.00",
                            "commission": "0.75",
                            "filled_at": opened_at.isoformat(),
                        }
                    ]
                },
                **BROKER_SCOPE,
            )
        )

    client = TestClient(server.app)
    resp = client.get("/blotter")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["configured"] is True
    assert body["source"] == "postgres"
    assert body["summary"]["open_trades"] == 1
    assert body["open_trades"][0]["symbol"] == "MSFT"
