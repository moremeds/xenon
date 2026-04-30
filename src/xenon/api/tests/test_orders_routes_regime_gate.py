"""Route-level coverage for RegimeGate wired into POST /orders/place.

Asserts the gate produces the correct HTTP shape:
- TIER_1 + non-hedge → 409 with override_required=True
- TIER_1 + non-hedge + valid override → submission proceeds, override row written
- TIER_2 + over-cap order → 422 resize_required
- NORMAL → no gate effect, order proceeds (smoke)

Tests pre-seed the regime_state cache via the in-process API rather
than inserting fixture rows, so they are fast and deterministic.
"""

from __future__ import annotations

import datetime as dt
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sa_text

from xenon.api import server as server_mod
from xenon.api.services import regime_state as regime_state_mod
from xenon.execution import quote_tokens

_QUOTE_SECRET = "e" * 64


@pytest.fixture(autouse=True)
def _enable_gate_in_tests(monkeypatch):
    from xenon.execution import quote_guard

    # Test mode is on (no IB subprocess) but the gate must still run.
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    monkeypatch.setenv("XENON_REGIME_GATE_IN_TESTS", "1")
    monkeypatch.setenv("XENON_REGIME_CACHE_TTL_S", "60")
    monkeypatch.setenv("XENON_REGIME_BANKROLL_USD_OVERRIDE", "100000")
    monkeypatch.setenv("XENON_QUOTE_TOKEN_SECRET", _QUOTE_SECRET)

    # Pin the clock to a fixed RTH timestamp so OPT orders don't trip
    # the market-hours gate. Tuesday 2026-06-09 18:30 UTC = 14:30 ET.
    fixed_now = dt.datetime(2026, 6, 9, 18, 30, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(server_mod, "_now", lambda: fixed_now)

    # In-process tick-rule cache so check_payload doesn't hit IB.
    server_mod._tick_rule_cache = quote_guard.TickRuleCache(
        source=lambda con_id: Decimal("0.01"),
        ttl_seconds=3600,
    )
    yield


def _mint_token(con_id: int, ticker: str, bid: str, ask: str) -> str:
    """Mint a fresh quote token signed by the test secret."""
    payload = quote_tokens.QuotePayload(
        con_id=con_id,
        ticker=ticker,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=100,
        ask_size=100,
        ts_server_ms=int(time.time() * 1000),
    )
    return quote_tokens.mint(payload, _QUOTE_SECRET)


@pytest.fixture
def client():
    return TestClient(server_mod.app)


def _seed_regime_state(*, vcg_tier: str, cri_tier: str, binding_side: str = "vcg") -> None:
    """Pre-seed the in-process regime cache so the gate sees a deterministic tier.

    Seeds across all cache keys the gate might look up (the actual
    app.state scope can vary between paper/legacy_unknown depending on
    whether the lifespan populated state before the test ran).
    """
    binding = max((vcg_tier, cri_tier), key=lambda t: regime_state_mod._TIER_ORDINAL[t])
    state = regime_state_mod.RegimeState(
        vcg_tier=vcg_tier,
        cri_tier=cri_tier,
        binding_tier="EDR" if binding == "UNKNOWN" else binding,
        binding_side=binding_side,
        vcg_scanned_at=dt.datetime.now(dt.timezone.utc),
        cri_scanned_at=dt.datetime.now(dt.timezone.utc),
        is_stale=False,
        panic_active=False,
    )
    regime_state_mod._cache_clear()
    # Seed both the legacy_unknown fallback and the actual app.state scope.
    regime_state_mod._cache_set(("legacy_unknown", "legacy_unknown"), state)
    try:
        scope = server_mod._resolve_scope_obj()
        regime_state_mod._cache_set((scope.account_env, scope.broker_account), state)
    except Exception:
        pass


def _stk_order_body(symbol: str, qty: int = 100, action: str = "BUY") -> dict:
    return {
        "type": "stock",
        "symbol": symbol,
        "action": action,
        "quantity": qty,
        "limitPrice": "200.00",
        "client_attempt_id": f"{symbol}-{qty}-{action}-test",
    }


def _opt_order_body(
    symbol: str = "QQQ",
    action: str = "BUY",
    right: str = "C",
    limit: str = "5.10",
    qty: int = 1,
) -> dict:
    """OPT order body with quote_token + con_id so F3 quote gate passes.

    bid/ask is centered around `limit` so check_payload accepts the
    limit price as in-band.
    """
    con_id = 99_900_000 + hash(f"{symbol}-{right}-{limit}") % 1000
    bid = str(Decimal(limit) - Decimal("0.05"))
    ask = str(Decimal(limit) + Decimal("0.05"))
    return {
        "type": "option",
        "symbol": symbol,
        "action": action,
        "right": right,
        "expiry": "2026-06-19",
        "strike": "200",
        "quantity": qty,
        "multiplier": 100,
        "limitPrice": limit,
        "client_attempt_id": f"{symbol}-{action}-{right}-{limit}-test",
        "con_id": con_id,
        "quote_token": _mint_token(con_id, symbol, bid, ask),
    }


def _delete_test_overrides(submission_ids: list[str]) -> None:
    eng = server_mod.get_sync_engine()
    with eng.begin() as conn:
        if submission_ids:
            conn.execute(
                sa_text("DELETE FROM xenon.regime_overrides WHERE submission_id = ANY(:ids)"),
                {"ids": submission_ids},
            )
            conn.execute(
                sa_text("DELETE FROM xenon.order_submissions WHERE submission_id = ANY(:ids)"),
                {"ids": submission_ids},
            )


def test_normal_regime_does_not_block_order(client):
    _seed_regime_state(vcg_tier="NORMAL", cri_tier="NORMAL")
    resp = client.post("/orders/place", json=_opt_order_body())
    # Test mode produces a synthetic order; status should be 200/test
    assert resp.status_code == 200, resp.text
    body = resp.json()
    sid = body.get("submission_id")
    if sid:
        _delete_test_overrides([sid])


def test_tier_1_non_hedge_blocks_with_override_required_409(client):
    _seed_regime_state(vcg_tier="NORMAL", cri_tier="TIER_1", binding_side="cri")
    resp = client.post("/orders/place", json=_opt_order_body(symbol="QQQ"))
    assert resp.status_code == 409
    body = resp.json()
    assert body["reason_code"] == "REGIME_BLOCK"
    assert body["decision"] == "block"
    assert body["binding_tier"] == "TIER_1"
    assert body["override_required"] is True
    assert body["override_min_reason_chars"] == 10


def test_tier_1_with_short_override_reason_still_409(client):
    _seed_regime_state(vcg_tier="NORMAL", cri_tier="TIER_1")
    body = _opt_order_body(symbol="QQQ")
    body["override"] = True
    body["override_reason"] = "too short"
    resp = client.post("/orders/place", json=body)
    assert resp.status_code == 409
    assert resp.json()["reason_code"] == "REGIME_BLOCK"


def test_tier_1_with_valid_override_writes_audit_row(client):
    _seed_regime_state(vcg_tier="NORMAL", cri_tier="TIER_1")
    body = _opt_order_body(symbol="QQQ")
    body["override"] = True
    body["override_reason"] = "earnings catalyst contrarian play"
    resp = client.post("/orders/place", json=body)
    assert resp.status_code == 200, resp.text
    response = resp.json()
    sid = response["submission_id"]
    eng = server_mod.get_sync_engine()
    try:
        with eng.connect() as conn:
            row = conn.execute(
                sa_text(
                    "SELECT submission_id, vcg_tier, cri_tier, user_reason, "
                    "binding_side, block_reason FROM xenon.regime_overrides "
                    "WHERE submission_id = :sid"
                ),
                {"sid": sid},
            ).first()
            assert row is not None, "override audit row missing"
            assert row.vcg_tier == "NORMAL"
            assert row.cri_tier == "TIER_1"
            assert row.user_reason == "earnings catalyst contrarian play"
    finally:
        _delete_test_overrides([sid])


def test_tier_1_hedge_passes_without_override(client):
    _seed_regime_state(vcg_tier="NORMAL", cri_tier="TIER_1")
    # SPY put = hedge per spec §4.5
    body = _opt_order_body(symbol="SPY", right="P", limit="3.00")
    resp = client.post("/orders/place", json=body)
    assert resp.status_code == 200, resp.text
    sid = resp.json().get("submission_id")
    if sid:
        _delete_test_overrides([sid])


def test_tier_2_over_cap_returns_422_resize_required(client, monkeypatch):
    # 1.25% of $10,000 bankroll = $125 cap. Long call at $5 × 100 mult = $500 max loss → over.
    monkeypatch.setenv("XENON_REGIME_BANKROLL_USD_OVERRIDE", "10000")
    _seed_regime_state(vcg_tier="TIER_2", cri_tier="NORMAL")
    resp = client.post("/orders/place", json=_opt_order_body(symbol="QQQ", limit="5.00"))
    assert resp.status_code == 422
    body = resp.json()
    assert body["reason_code"] == "REGIME_RESIZE_REQUIRED"
    assert body["decision"] == "resize_required"
    assert body["binding_tier"] == "TIER_2"
    assert body["max_loss_cap_usd"] == pytest.approx(125.0)
    assert body["max_loss_usd"] == pytest.approx(500.0)
    assert body["cover_ratio"] == 1.25


def test_tier_2_within_cap_proceeds_with_125_cover_ratio(client):
    _seed_regime_state(vcg_tier="TIER_2", cri_tier="NORMAL")
    # Small order: $1.00 × 100 = $100 max loss < $1,250 cap (1.25% of 100k)
    resp = client.post("/orders/place", json=_opt_order_body(symbol="QQQ", limit="1.00"))
    assert resp.status_code == 200, resp.text
    sid = resp.json().get("submission_id")
    if sid:
        _delete_test_overrides([sid])


def test_disabled_gate_skips_evaluation_entirely(client, monkeypatch):
    monkeypatch.setenv("XENON_REGIME_GATE_IN_TESTS", "0")
    monkeypatch.setenv("XENON_REGIME_GATE_DISABLED", "1")
    _seed_regime_state(vcg_tier="NORMAL", cri_tier="TIER_1")  # would normally block
    resp = client.post("/orders/place", json=_opt_order_body(symbol="QQQ"))
    # Gate disabled → order proceeds despite TIER_1
    assert resp.status_code == 200, resp.text
    sid = resp.json().get("submission_id")
    if sid:
        _delete_test_overrides([sid])
