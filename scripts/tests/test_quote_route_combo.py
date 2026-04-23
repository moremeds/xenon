import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from xenon.execution import orders_store, quote_tokens

SECRET = "b" * 64


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    monkeypatch.setenv("XENON_QUOTE_TOKEN_SECRET", SECRET)
    db = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(db))
    orders_store.init_store(db)
    # Force the market-hours gate to "open" so these tests are not flaky
    # outside RTH. The band/token logic under test is independent of it.
    from xenon.execution import quote_guard

    monkeypatch.setattr(quote_guard, "is_opt_tradeable", lambda _now: True)
    from xenon.api.server import app

    return TestClient(app)


def _mint(con_id: int, bid: str = "4.50", ask: str = "4.70") -> str:
    p = quote_tokens.QuotePayload(
        con_id=con_id,
        ticker="SPY",
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=100,
        ask_size=100,
        ts_server_ms=int(time.time() * 1000),
    )
    return quote_tokens.mint(p, SECRET)


def _combo_body(legs_tokens: dict[str, str] | None, limit_price: str = "2.70"):
    return {
        "type": "combo",
        "symbol": "SPY",
        "action": "BUY",
        "quantity": 1,
        "limitPrice": float(limit_price),
        "tif": "DAY",
        "client_attempt_id": f"attempt-{time.time_ns()}",
        "legs": [
            {"con_id": 1, "expiry": "2026-05-16", "strike": 500, "right": "C", "action": "BUY", "ratio": 1},
            {"con_id": 2, "expiry": "2026-05-16", "strike": 510, "right": "C", "action": "SELL", "ratio": 1},
        ],
        **({"quote_tokens": legs_tokens} if legs_tokens is not None else {}),
    }


def test_combo_missing_tokens_soft_fails_with_telemetry(client):
    """Missing combo quote_tokens soft-fails with telemetry (known gap).

    Several combo entry paths (ticker-detail OrderTab ComboOrderForm,
    OptionsChainTab, InstrumentDetailModal) do not yet mint quote_tokens.
    Hard-rejecting here would break those flows. Follow-up: wire tokens
    into every combo entry path, then flip to hard-reject.
    """
    body = _combo_body(None)
    r = client.post("/orders/place", json=body)
    assert r.status_code == 200, r.text
    from xenon.execution import orders_store

    con = orders_store._connect_utc(orders_store._resolve_path(None))
    try:
        rows = con.execute("SELECT kind FROM orders_events WHERE kind='QUOTE_TOKEN_MISSING_SOFT'").fetchall()
    finally:
        con.close()
    assert len(rows) == 1


def test_combo_in_band_tokens_pass(client):
    tokens = {"1": _mint(1, "4.50", "4.70"), "2": _mint(2, "2.00", "2.20")}
    body = _combo_body(tokens, limit_price="2.70")
    r = client.post("/orders/place", json=body)
    assert r.status_code == 200, r.text


def test_combo_out_of_band_rejects(client):
    tokens = {"1": _mint(1, "4.50", "4.70"), "2": _mint(2, "2.00", "2.20")}
    body = _combo_body(tokens, limit_price="3.50")
    r = client.post("/orders/place", json=body)
    assert r.status_code == 400
    assert r.json()["reason_code"] == "LIMIT_OUT_OF_BAND"


def test_combo_tampered_token_rejects(client):
    tokens = {"1": _mint(1, "4.50", "4.70") + "x", "2": _mint(2, "2.00", "2.20")}
    body = _combo_body(tokens)
    r = client.post("/orders/place", json=body)
    assert r.status_code == 400
    assert r.json()["reason_code"] == "STALE_QUOTE"


def test_combo_out_of_band_emits_quote_check_fail_telemetry(client):
    tokens = {"1": _mint(1, "4.50", "4.70"), "2": _mint(2, "2.00", "2.20")}
    body = _combo_body(tokens, limit_price="50.00")
    r = client.post("/orders/place", json=body)
    assert r.status_code == 400
    # Telemetry row exists with the reason code.
    con = orders_store._connect_utc(orders_store._resolve_path(None))
    try:
        rows = con.execute("SELECT kind, detail FROM orders_events WHERE kind='QUOTE_CHECK_FAIL'").fetchall()
    finally:
        con.close()
    assert len(rows) == 1
    import json as _json

    detail = _json.loads(rows[0][1])
    assert detail["reason_code"] == "LIMIT_OUT_OF_BAND"
    assert detail["leg_count"] == 2


def test_combo_tampered_token_emits_quote_check_fail_telemetry(client):
    tokens = {"1": _mint(1, "4.50", "4.70") + "x", "2": _mint(2, "2.00", "2.20")}
    body = _combo_body(tokens)
    r = client.post("/orders/place", json=body)
    assert r.status_code == 400
    con = orders_store._connect_utc(orders_store._resolve_path(None))
    try:
        rows = con.execute("SELECT kind FROM orders_events WHERE kind='QUOTE_CHECK_FAIL'").fetchall()
    finally:
        con.close()
    assert len(rows) == 1


# ---------- Shared short-credit-vertical fixture (cross-contract with TS) ----------
#
# This mirrors the exact fixture asserted in
# web/tests/position-order-seed-ticket.test.ts (ironCondorCreditPos) and
# web/tests/position-order-modal-quote-tokens.test.tsx — same legs, same
# envelope expectations — so the TS and Python sides cannot silently
# diverge on the IB BAG contract again.


def _short_credit_vertical_body(envelope: str, limit_price: str):
    """A SHORT credit vertical as the UI would submit for close/add.

    Structural legs are ALWAYS [SELL short_leg, BUY long_leg] per
    web/CLAUDE.md BAG convention (LONG→BUY, SHORT→SELL). The envelope
    toggles open/add (BUY) vs close (SELL); IB reverses legs on SELL.
    """
    tokens = {"1": _mint(1, "4.50", "4.70"), "2": _mint(2, "2.00", "2.20")}
    return {
        "type": "combo",
        "symbol": "SPY",
        "action": envelope,
        "quantity": 1,
        "limitPrice": float(limit_price),
        "tif": "DAY",
        "client_attempt_id": f"attempt-{time.time_ns()}",
        "legs": [
            # SHORT leg: sold 500C (received credit); structural action=SELL
            {"con_id": 1, "expiry": "2026-05-16", "strike": 500, "right": "C", "action": "SELL", "ratio": 1},
            # LONG leg: bought 510C hedge; structural action=BUY
            {"con_id": 2, "expiry": "2026-05-16", "strike": 510, "right": "C", "action": "BUY", "ratio": 1},
        ],
        "quote_tokens": tokens,
    }


def test_short_credit_vertical_close_envelope_sell_accepts(client):
    """Close of a SHORT credit vertical: envelope=SELL, limit near market debit.

    With envelope=SELL IB reverses to BUY 500C (pay ask 4.70) + SELL 510C
    (receive bid 2.00) → net debit 2.70 paid to close. A 2.80 limit is
    within band.
    """
    body = _short_credit_vertical_body(envelope="SELL", limit_price="2.80")
    r = client.post("/orders/place", json=body)
    assert r.status_code == 200, r.text


def test_short_credit_vertical_add_envelope_buy_accepts(client):
    """Add to a SHORT credit vertical: envelope=BUY, limit near market credit.

    With envelope=BUY IB executes legs as-labeled: SELL 500C (receive bid
    4.50) + BUY 510C (pay ask 2.20) → net credit 2.30 received. User
    submits a positive limit ≈ 2.30; sign-keyed band clears.
    """
    body = _short_credit_vertical_body(envelope="BUY", limit_price="2.30")
    r = client.post("/orders/place", json=body)
    assert r.status_code == 200, r.text
