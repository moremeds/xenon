"""/blotter surfaces a Flex failure as a structured payload, not a bare 502.

A Flex saved-query misconfiguration (e.g. IB ErrorCode 1001 — CSV format
on the XML-only legacy servlet) is an actionable operator error. The UI
needs the error text, not an opaque 502.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import xenon.api.server as server_mod
from xenon.api.guards import get_account_scope
from xenon.api.subprocess import ScriptResult
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DU0000000")


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def fake_run_module(module: str, args=None, timeout: float = 30.0) -> ScriptResult:
        return ScriptResult(
            ok=False,
            error="Flex Query request failed: Statement could not be generated at this time. (code: 1001)",
            exit_code=1,
        )

    monkeypatch.setattr(server_mod, "run_module", fake_run_module)
    monkeypatch.setattr(
        server_mod,
        "fetch_blotter_pg",
        lambda conn, scope, days: {"closed_trades": [], "open_trades": []},
    )
    monkeypatch.setattr(server_mod, "blotter_has_trades", lambda payload: False)
    server_mod.app.dependency_overrides[get_account_scope] = lambda: SCOPE
    try:
        yield TestClient(server_mod.app)
    finally:
        server_mod.app.dependency_overrides.pop(get_account_scope, None)


def test_flex_failure_returns_structured_payload(client: TestClient) -> None:
    resp = client.post("/blotter")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert "1001" in body["flex_error"]
    assert body["closed_trades"] == []
