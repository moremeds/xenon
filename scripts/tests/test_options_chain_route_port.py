"""/options/chain must pass the resolved gateway port to the subprocess.

Discovered 2026-06-14 during live paper E2E: the route invoked
`xenon-ib-option-chain` with no `--port`, so the CLI fell back to its
default 4001 (live). In paper mode (gateway on 4002) every chain fetch
then failed with `Connect call failed ('127.0.0.1', 4001)` — the CHAIN
tab stayed 502 even after the Index-qualification fix. Mirror the
sync routes, which pass `--port DEFAULT_GATEWAY_PORT`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import xenon.api.server as server_mod
from xenon.api.subprocess import ScriptResult
from xenon.clients.ib_client import DEFAULT_GATEWAY_PORT


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict]:
    captured: dict = {}

    async def fake_recovery(entry: str, args: list, timeout: float = 30) -> ScriptResult:
        captured["entry"] = entry
        captured["args"] = list(args)
        return ScriptResult(
            ok=True,
            data={"symbol": "SPX", "expirations": ["20260116"], "exchanges": ["CBOE"]},
        )

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake_recovery)
    return TestClient(server_mod.app), captured


def test_options_chain_passes_gateway_port(client: tuple[TestClient, dict]) -> None:
    tc, captured = client
    resp = tc.get("/options/chain", params={"symbol": "spx"})
    assert resp.status_code == 200, resp.text
    assert captured["entry"] == "xenon-ib-option-chain"
    assert "--port" in captured["args"]
    assert str(DEFAULT_GATEWAY_PORT) in captured["args"]
    # symbol still upper-cased and forwarded
    assert "SPX" in captured["args"]
