"""Regression: order entry must not call the deleted RegimeGate.

The VCG-CRI "regime gate" (signal layer) was removed in the pure-portfolio
pivot (#104, cc568c3): `xenon.api.services.regime_gate` and
`regime_state` were deleted along with their imports. But the call sites
in `_orders_place_from_body` / `_orders_modify_from_body` were left
behind, so every live order placement raised

    NameError: name 'get_regime_state_for_scope' is not defined

→ HTTP 500. CI stayed green because the gate self-disables in test mode
(`_is_test_mode()` early-return), so the live path was never exercised.

These tests pin the regression:
- `test_place_stock_buy_does_not_500_with_gate_forced_on` exercises the
  exact path that 500'd, forcing the (now-removed) gate to run.
- `test_server_does_not_reference_deleted_regime_symbols` prevents the
  deleted symbols from being reintroduced.

See docs/reference/order-path-incident-history.md.
"""

from decimal import Decimal
from pathlib import Path

import pytest

# Phase 2 carve-out — mirrors test_place_quote_gate.py: the place path
# touches orders_store via its own engine, so stay on TRUNCATE isolation.
pytestmark = pytest.mark.committed_db

from fastapi.testclient import TestClient

SECRET = "d" * 64


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    monkeypatch.setenv("XENON_QUOTE_TOKEN_SECRET", SECRET)
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    monkeypatch.setenv("XENON_DATA_DIR", str(tmp_path))
    # Force any regime gate to run even under test mode. Before the fix
    # this drove the place path into the undefined gate symbols.
    monkeypatch.setenv("XENON_REGIME_GATE_IN_TESTS", "1")
    yield


@pytest.fixture
def client():
    from xenon.api import server
    from xenon.execution import orders_store, quote_guard

    orders_store.init_store()
    server._tick_rule_cache = quote_guard.TickRuleCache(
        source=lambda con_id: Decimal("0.01"),
        ttl_seconds=3600,
    )
    return TestClient(server.app)


def test_place_stock_buy_does_not_500_with_gate_forced_on(client, monkeypatch):
    from xenon.api import server

    async def _fresh_quote(ticker: str, con_id: int):
        return {
            "bid": Decimal("500.10"),
            "ask": Decimal("500.20"),
            "bid_size": 100,
            "ask_size": 120,
        }

    monkeypatch.setattr(server, "_fetch_quote_snapshot", _fresh_quote)

    resp = client.post(
        "/orders/place",
        json={
            "type": "stock",
            "symbol": "SPY",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": 500.20,
            "con_id": 756733,
            "client_attempt_id": "regime-regression-1",
        },
    )

    # The bug surfaced as a 500 NameError inside the regime gate. The
    # order may legitimately accept (200) or be blocked by a *real* gate
    # such as preflight/naked-short (4xx) — but it must never 500.
    assert resp.status_code != 500, resp.text


def test_server_does_not_reference_deleted_regime_symbols():
    """The deleted VCG-CRI symbols must not reappear in the order path."""
    server_src = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "xenon"
        / "api"
        / "server.py"
    ).read_text(encoding="utf-8")

    for symbol in (
        "get_regime_state_for_scope",
        "evaluate_order_gate",
        "GateDecision",
    ):
        assert symbol not in server_src, (
            f"{symbol!r} was deleted in the pure-portfolio pivot (#104) but "
            "is referenced again in server.py — this reintroduces the "
            "order-place 500 NameError."
        )
