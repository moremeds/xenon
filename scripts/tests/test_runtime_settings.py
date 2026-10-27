"""Runtime settings: registry, precedence, narrow-only rules, audit, auth,
read-only refusal, orders_paused on every place/modify path, and the
no-rows = env behavior of every api consumer."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from xenon import runtime_settings as rs
from xenon.api import guards
from xenon.api.routes import system as system_routes
from xenon.api.services import audit_enforce, order_entry
from xenon.db.engine import get_sync_engine
from xenon.db.schema import runtime_settings as settings_table
from xenon.db.schema import runtime_settings_audit as audit_table
from xenon.execution import broker_capabilities, futu_orders, gate4, quote_guard

_REAL_FETCH = system_routes._fetch_realtime_status_json

pytestmark = pytest.mark.usefixtures("pg_test_engine")

TOKEN = "rs-internal-token"
HDR = {"X-Internal-Token": TOKEN}
_API_ENV = (
    "XENON_FUTU_TRADING",
    "XENON_FUTU_TUPLES_LIVE",
    "XENON_FUTU_TUPLES_PAPER",
    "XENON_FUTU_MODIFY_TYPES",
    "XENON_FUTU_MKT_NOTIONAL_CAP",
    "XENON_GATE4_NETTING",
    "XENON_AUDIT_CANCEL",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _API_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)
    monkeypatch.setenv("XENON_INTERNAL_API_TOKEN", TOKEN)


@pytest.fixture(autouse=True)
def _no_relay(monkeypatch):
    """GET /admin/settings reads relay /status: no real network in tests."""
    from xenon.api.routes import system

    def down(url, timeout=0.5):
        raise OSError("relay down (test)")

    monkeypatch.setattr(system, "_fetch_realtime_status_json", down)


@pytest.fixture
def client():
    from xenon.api import server

    return TestClient(server.app)  # no lifespan: settings routes need no app.state


def _put(key, value, **kw):
    return rs.put(key, value, actor="test", note=kw.get("note"), confirm=kw.get("confirm"))


def _raw_row(key, value_json):
    with get_sync_engine().begin() as conn:
        conn.execute(
            text("INSERT INTO xenon.runtime_settings(key, value) VALUES (:k, CAST(:v AS jsonb))"),
            {"k": key, "v": value_json},
        )
    rs.invalidate()


def _audit_rows(key):
    with get_sync_engine().connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(audit_table).where(audit_table.c.key == key))]


# ---------- registry and validation ----------


def test_unknown_key_refused():
    with pytest.raises(rs.SettingError) as exc:
        _put("XENON_READ_ONLY", True)
    assert exc.value.reason_code == rs.UNKNOWN_KEY


@pytest.mark.parametrize(
    "key,value",
    [
        ("orders_paused", "true"),
        ("orders_paused", 1),
        ("IB_REALTIME_MAX_LINES", 9),
        ("IB_REALTIME_MAX_LINES", 101),
        ("IB_REALTIME_MAX_LINES", 50.5),
        ("IB_REALTIME_MAX_LINES", True),
        ("IB_REALTIME_STALE_TICK_MS", 0),
        ("XENON_QUOTES_DEFAULT", "futu"),
        ("FUTU_QUOTE_QUOTA", 0),
        ("FUTU_UNITS_PER_SYMBOL", 11),
        ("FUTU_BOOK_STALE_S", -1),
        ("FUTU_BOOK_STALE_S", float("nan")),
        ("FUTU_GREEK_FACTORS", {"rho": 1}),
        ("FUTU_GREEK_FACTORS", {"iv": "0.01"}),
        ("FUTU_GREEK_FACTORS", [1]),
        ("XENON_FUTU_TRADING", ["simulate", "1"]),
        ("XENON_FUTU_TRADING", "simulate"),
        ("XENON_FUTU_TUPLES_PAPER", ["STK:LMT:DAY"]),
        ("XENON_FUTU_MODIFY_TYPES", ["LIMIT"]),
        ("XENON_FUTU_MKT_NOTIONAL_CAP", 0),
        ("XENON_GATE4_NETTING", "on"),
    ],
)
def test_invalid_values_refused(key, value):
    with pytest.raises(rs.SettingError) as exc:
        _put(key, value)
    assert exc.value.reason_code == rs.INVALID_VALUE
    assert _audit_rows(key) == []


def test_every_registry_key_has_metadata():
    for s in rs.REGISTRY.values():
        assert s.category in {"toggle", "tuning", "safety"}
        assert s.apply in {"live", "restart"}
        assert s.owner in {"api", "relay", "futu-quotes"}
        assert s.consumers
        if s.owner == "api":
            assert s.env_value is not None


# ---------- precedence ----------


def test_no_row_effective_is_env(monkeypatch):
    monkeypatch.setenv("XENON_FUTU_MKT_NOTIONAL_CAP", "1000")
    assert rs.effective("XENON_FUTU_MKT_NOTIONAL_CAP") == 1000
    assert rs.effective("orders_paused") is False


def test_db_row_overrides_env_and_cache_invalidated_by_write():
    assert rs.orders_paused() is False
    _put("orders_paused", True)
    assert rs.orders_paused() is True
    rs.delete("orders_paused", actor="test", note=None, confirm=None)
    assert rs.orders_paused() is False


def test_invalid_db_row_is_ignored(monkeypatch):
    monkeypatch.setenv("XENON_FUTU_TRADING", "simulate")
    _raw_row("XENON_FUTU_TRADING", '"simulate"')
    _raw_row("NOT_A_KEY", "true")
    assert broker_capabilities.futu_trading_envs() == frozenset({"paper"})
    assert rs.overrides() == {}


def test_db_read_error_keeps_last_values(monkeypatch):
    _put("orders_paused", True)
    assert rs.orders_paused() is True
    rs.invalidate()
    monkeypatch.setattr(rs, "_load_rows", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert rs.orders_paused() is True


def test_cache_ttl_serves_without_db(monkeypatch):
    calls = []
    real = rs._load_rows
    monkeypatch.setattr(rs, "_load_rows", lambda: calls.append(1) or real())
    rs.overrides()
    rs.overrides()
    assert len(calls) == 1


# ---------- narrow-only safety keys ----------


def test_futu_trading_subset_only(monkeypatch):
    monkeypatch.setenv("XENON_FUTU_TRADING", "simulate")
    with pytest.raises(rs.SettingError) as exc:
        _put("XENON_FUTU_TRADING", ["simulate", "real"])
    assert exc.value.reason_code == rs.WIDENS_ENV
    monkeypatch.setenv("XENON_FUTU_TRADING", "simulate,real")
    _put("XENON_FUTU_TRADING", ["simulate"], confirm="XENON_FUTU_TRADING")
    assert broker_capabilities.futu_trading_envs() == frozenset({"paper"})
    # env narrowed later below the DB value: effective stays the intersection
    monkeypatch.setenv("XENON_FUTU_TRADING", "real")
    assert broker_capabilities.futu_trading_envs() == frozenset()


def test_real_scope_change_needs_confirm(monkeypatch):
    monkeypatch.setenv("XENON_FUTU_TRADING", "simulate,real")
    with pytest.raises(rs.SettingError) as exc:
        _put("XENON_FUTU_TRADING", ["simulate"])
    assert exc.value.reason_code == rs.CONFIRM_REQUIRED
    _put("XENON_FUTU_TRADING", ["simulate"], confirm="XENON_FUTU_TRADING")
    # revert re-enables real: confirm again
    with pytest.raises(rs.SettingError):
        rs.delete("XENON_FUTU_TRADING", actor="t", note=None, confirm=None)
    rs.delete("XENON_FUTU_TRADING", actor="t", note=None, confirm="XENON_FUTU_TRADING")
    assert broker_capabilities.futu_trading_envs() == frozenset({"paper", "live"})


def test_tuples_subset_only(monkeypatch):
    env_paper = futu_orders.env_allowed_tuples("paper")
    assert ("STK", "MKT", "DAY", "RTH") in env_paper
    with pytest.raises(rs.SettingError) as exc:
        _put("XENON_FUTU_TUPLES_PAPER", ["STK:LMT:GTC:RTH"])
    assert exc.value.reason_code == rs.WIDENS_ENV
    _put("XENON_FUTU_TUPLES_PAPER", ["stk:lmt:day:rth"])
    assert futu_orders.allowed_tuples("paper") == frozenset({("STK", "LMT", "DAY", "RTH")})
    assert futu_orders.allowed("paper", "STK", "MKT", "DAY", "RTH") is not None
    assert futu_orders.allowed_tuples("live") == futu_orders.env_allowed_tuples("live")


def test_live_tuples_change_needs_confirm():
    with pytest.raises(rs.SettingError) as exc:
        _put("XENON_FUTU_TUPLES_LIVE", ["STK:LMT:DAY:RTH"])
    assert exc.value.reason_code == rs.CONFIRM_REQUIRED
    _put("XENON_FUTU_TUPLES_LIVE", ["STK:LMT:DAY:RTH"], confirm="XENON_FUTU_TUPLES_LIVE")
    assert futu_orders.allowed_tuples("live") == frozenset({("STK", "LMT", "DAY", "RTH")})


def test_modify_types_subset_only(monkeypatch):
    with pytest.raises(rs.SettingError) as exc:
        _put("XENON_FUTU_MODIFY_TYPES", ["LMT", "MKT"])
    assert exc.value.reason_code == rs.WIDENS_ENV
    _put("XENON_FUTU_MODIFY_TYPES", [])
    assert futu_orders.modify_types() == frozenset()


def test_mkt_cap_min_only(monkeypatch):
    with pytest.raises(rs.SettingError) as exc:
        _put("XENON_FUTU_MKT_NOTIONAL_CAP", 30000)
    assert exc.value.reason_code == rs.WIDENS_ENV
    _put("XENON_FUTU_MKT_NOTIONAL_CAP", 5000.5)
    assert quote_guard.mkt_notional_cap() == Decimal("5000.5")
    monkeypatch.setenv("XENON_FUTU_MKT_NOTIONAL_CAP", "1000")
    assert quote_guard.mkt_notional_cap() == Decimal("1000")
    monkeypatch.setenv("XENON_FUTU_MKT_NOTIONAL_CAP", "bogus")
    assert quote_guard.mkt_notional_cap() is None
    with pytest.raises(rs.SettingError):
        _put("XENON_FUTU_MKT_NOTIONAL_CAP", 1)


def test_gate4_netting_can_only_turn_off(monkeypatch):
    with pytest.raises(rs.SettingError) as exc:
        _put("XENON_GATE4_NETTING", True)
    assert exc.value.reason_code == rs.WIDENS_ENV
    monkeypatch.setenv("XENON_GATE4_NETTING", "1")
    assert gate4.netting_enabled() is True
    _put("XENON_GATE4_NETTING", False)
    assert gate4.netting_enabled() is False
    _put("XENON_GATE4_NETTING", True)
    monkeypatch.setenv("XENON_GATE4_NETTING", "0")
    assert gate4.netting_enabled() is False


def test_audit_cancel_on_needs_confirm_off_does_not(monkeypatch):
    with pytest.raises(rs.SettingError) as exc:
        _put("XENON_AUDIT_CANCEL", True)
    assert exc.value.reason_code == rs.CONFIRM_REQUIRED
    _put("XENON_AUDIT_CANCEL", True, confirm="XENON_AUDIT_CANCEL")
    assert audit_enforce.enabled() is True
    monkeypatch.setenv("XENON_AUDIT_CANCEL", "1")
    _put("XENON_AUDIT_CANCEL", False)
    assert audit_enforce.enabled() is False


# ---------- no rows = today's behavior ----------


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"XENON_FUTU_TRADING": "simulate,real", "XENON_GATE4_NETTING": "yes", "XENON_AUDIT_CANCEL": "1"},
        {"XENON_FUTU_TRADING": "1", "XENON_FUTU_MKT_NOTIONAL_CAP": "-5", "XENON_FUTU_MODIFY_TYPES": "BAD"},
        {"XENON_FUTU_TUPLES_LIVE": "STK:LMT:DAY:RTH,STK:MKT:DAY:RTH", "XENON_FUTU_TUPLES_PAPER": "nope"},
    ],
)
def test_no_rows_consumers_equal_env_only(monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    flags = broker_capabilities.env_trading_flags()
    assert broker_capabilities.futu_trading_envs() == frozenset({"simulate": "paper", "real": "live"}[t] for t in flags)
    for acct in ("paper", "live", "other"):
        assert futu_orders.allowed_tuples(acct) == futu_orders.env_allowed_tuples(acct)
    assert futu_orders.modify_types() == futu_orders.env_modify_types()
    assert quote_guard.mkt_notional_cap() == quote_guard.env_mkt_notional_cap()
    assert gate4.netting_enabled() is gate4.env_netting_enabled()
    assert audit_enforce.enabled() is audit_enforce.env_enabled()
    assert rs.orders_paused() is False
    for key, s in rs.REGISTRY.items():
        if s.owner == "api":
            assert rs.effective(key) == rs.env_value(s)


# ---------- audit rows ----------


def test_put_and_delete_append_audit_rows():
    _put("IB_REALTIME_MAX_LINES", 60, note="shrink")
    _put("IB_REALTIME_MAX_LINES", 70)
    rs.delete("IB_REALTIME_MAX_LINES", actor="ops", note="revert", confirm=None)
    rows = sorted(_audit_rows("IB_REALTIME_MAX_LINES"), key=lambda r: r["id"])
    assert [(r["old_value"], r["new_value"]) for r in rows] == [(None, 60), (60, 70), (70, None)]
    assert rows[0]["note"] == "shrink" and rows[2]["changed_by"] == "ops"
    assert rs.delete("IB_REALTIME_MAX_LINES", actor="ops", note=None, confirm=None) == {
        "key": "IB_REALTIME_MAX_LINES",
        "changed": False,
    }
    assert len(_audit_rows("IB_REALTIME_MAX_LINES")) == 3


def test_refused_write_leaves_no_row(monkeypatch):
    with pytest.raises(rs.SettingError):
        _put("XENON_AUDIT_CANCEL", True)
    with get_sync_engine().connect() as conn:
        assert conn.execute(select(settings_table)).all() == []
    assert _audit_rows("XENON_AUDIT_CANCEL") == []


# ---------- API: auth, read-only, shape ----------


def test_get_settings_internal_ok(client, monkeypatch):
    monkeypatch.setenv("XENON_FUTU_MKT_NOTIONAL_CAP", "9000")
    _put("XENON_FUTU_MKT_NOTIONAL_CAP", 4000, note="tighten")
    resp = client.get("/admin/settings", headers=HDR)
    assert resp.status_code == 200
    rows = {r["key"]: r for r in resp.json()["settings"]}
    assert set(rows) == set(rs.REGISTRY)
    cap = rows["XENON_FUTU_MKT_NOTIONAL_CAP"]
    assert (cap["env_value"], cap["db_value"], cap["effective"], cap["narrow_only"]) == (9000, 4000, 4000, True)
    assert cap["last_change"]["new_value"] == 4000 and cap["note"] == "tighten"
    relay = rows["IB_REALTIME_MAX_LINES"]
    assert relay["env_known"] is False and relay["effective"] is None and relay["default"] == 85


def test_api_key_forbidden_even_with_internal_token(client):
    resp = client.get("/admin/settings", headers={**HDR, "X-API-Key": "k"})
    assert resp.status_code == 403
    assert resp.json()["reason_code"] == "API_KEY_FORBIDDEN"


def test_query_api_key_alone_is_refused(client, monkeypatch):
    monkeypatch.delenv("XENON_AUTH_ALLOW_DEV_OPEN", raising=False)
    monkeypatch.setenv("XENON_QUERY_API_KEY", "qk")
    for resp in (
        client.get("/admin/settings", headers={"X-API-Key": "qk"}),
        client.put("/admin/settings/orders_paused", json={"value": True}, headers={"X-API-Key": "qk"}),
        client.delete("/admin/settings/orders_paused", headers={"X-API-Key": "qk"}),
        client.get("/admin/settings", headers={"X-API-Key": "wrong"}),
    ):
        assert resp.status_code == 403
        assert resp.json()["reason_code"] == "API_KEY_FORBIDDEN"
    monkeypatch.setenv("XENON_AUTH_ALLOW_DEV_OPEN", "1")
    resp = client.get("/admin/settings", headers={"X-API-Key": "qk"})
    assert resp.status_code == 403 and resp.json()["reason_code"] == "API_KEY_FORBIDDEN"


def test_no_credential_401(client, monkeypatch):
    monkeypatch.delenv("XENON_AUTH_ALLOW_DEV_OPEN", raising=False)
    for resp in (
        client.get("/admin/settings"),
        client.put("/admin/settings/orders_paused", json={"value": True}),
        client.delete("/admin/settings/orders_paused"),
        client.get("/admin/settings", headers={"X-Internal-Token": "wrong"}),
    ):
        assert resp.status_code == 401
        assert resp.json()["reason_code"] == "INTERNAL_TOKEN_REQUIRED"


def test_dev_open_is_not_internal(client):
    resp = client.get("/admin/settings")  # root conftest sets XENON_AUTH_ALLOW_DEV_OPEN=1
    assert resp.status_code == 401
    assert resp.json()["reason_code"] == "INTERNAL_TOKEN_REQUIRED"


def test_put_and_delete_routes(client):
    resp = client.put("/admin/settings/orders_paused", json={"value": True, "note": "halt"}, headers=HDR)
    assert resp.status_code == 200 and resp.json()["changed"] is True
    assert rs.orders_paused() is True
    assert _audit_rows("orders_paused")[0]["changed_by"] == "internal-ui"
    resp = client.delete("/admin/settings/orders_paused?note=resume", headers=HDR)
    assert resp.status_code == 200 and rs.orders_paused() is False


@pytest.mark.parametrize(
    "key,body,status,code",
    [
        ("NOPE", {"value": 1}, 404, "SETTING_UNKNOWN"),
        ("orders_paused", {"value": "yes"}, 400, "SETTING_INVALID_VALUE"),
        ("orders_paused", {"nope": 1}, 400, "SETTING_INVALID_VALUE"),
        ("XENON_GATE4_NETTING", {"value": True}, 403, "SETTING_WIDENS_ENV"),
        ("XENON_AUDIT_CANCEL", {"value": True}, 428, "SETTING_CONFIRM_REQUIRED"),
        ("XENON_AUDIT_CANCEL", {"value": True, "confirm": "yes"}, 428, "SETTING_CONFIRM_REQUIRED"),
    ],
)
def test_put_errors_have_top_level_reason_code(client, key, body, status, code):
    resp = client.put(f"/admin/settings/{key}", json=body, headers=HDR)
    assert resp.status_code == status
    assert resp.json()["reason_code"] == code


def test_put_confirm_accepted(client):
    resp = client.put(
        "/admin/settings/XENON_AUDIT_CANCEL", json={"value": True, "confirm": "XENON_AUDIT_CANCEL"}, headers=HDR
    )
    assert resp.status_code == 200 and audit_enforce.enabled() is True


def test_read_only_refuses_writes_get_still_works(client, monkeypatch):
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    for resp in (
        client.put("/admin/settings/orders_paused", json={"value": True}, headers=HDR),
        client.put("/admin/settings/orders_paused", content=b"{bad json", headers=HDR),
        client.delete("/admin/settings/orders_paused", headers=HDR),
    ):
        assert resp.status_code == 403
        assert resp.json()["reason_code"] == "READ_ONLY_MODE"
    assert client.get("/admin/settings", headers=HDR).status_code == 200
    assert _audit_rows("orders_paused") == []


# ---------- orders_paused on every place / modify path ----------


@pytest.fixture
def paused():
    _put("orders_paused", True)


def _is_paused(resp) -> bool:
    body = resp.json() if hasattr(resp, "json") and callable(resp.json) else None
    if body is None:
        import json

        body = json.loads(resp.body)
    return resp.status_code == 403 and body.get("reason_code") == "ORDERS_PAUSED"


@pytest.mark.parametrize("path", ["/orders/place", "/orders/modify", "/orders/replace"])
def test_http_ib_place_modify_replace_paused(client, paused, path):
    assert _is_paused(client.post(path, json={}, headers=HDR))


@pytest.mark.parametrize("path", ["/orders/place", "/orders/modify"])
def test_http_futu_place_modify_paused(client, paused, monkeypatch, path):
    monkeypatch.setenv("XENON_FUTU_TRADING", "simulate")
    assert _is_paused(client.post(f"{path}?broker=FUTU&account_env=paper", json={}, headers=HDR))


@pytest.mark.parametrize(
    "action,body",
    [
        ("submit", {}),
        ("reprice", {"target_price": "1.00"}),
        ("protect", {"tp_target_price": "1.00", "alert_net_mid_threshold": "0.5"}),
    ],
)
def test_http_wizard_order_actions_paused(client, paused, action, body):
    assert _is_paused(client.post(f"/wizard/sessions/s-1/{action}", json=body, headers=HDR))


def test_http_cancel_and_abort_not_paused(client, paused):
    for resp in (
        client.post("/orders/cancel", json={}, headers=HDR),
        client.post("/orders/cancel?broker=FUTU&account_env=paper", json={}, headers=HDR),
        client.post("/wizard/sessions/s-1/abort", headers=HDR),
    ):
        assert resp.json().get("reason_code") != "ORDERS_PAUSED"


@pytest.mark.parametrize("fn", ["place", "modify", "replace"])
@pytest.mark.parametrize("broker", [None, "FUTU"])
def test_in_process_order_entry_paused(paused, fn, broker):
    """The wizard (submit_combo, reprice, protect) reaches these cores without a route."""
    result = asyncio.run(getattr(order_entry, fn)({}, broker=broker, account_env="paper"))
    assert _is_paused(result)


def test_in_process_cancel_not_paused(paused):
    result = asyncio.run(order_entry.cancel({}, broker="FUTU", account_env="paper"))
    import json

    assert json.loads(result.body).get("reason_code") != "ORDERS_PAUSED"


def test_guard_kinds(paused):
    from types import SimpleNamespace

    state = SimpleNamespace(broker="IB", mode_verified=True)
    for kind in guards._PAUSED_KINDS:
        assert _is_paused(guards.ensure_order_allowed(kind, state))
    for kind in ("order cancellation", "naked-short audit cancel", "wizard abort", "wizard plan"):
        resp = guards.ensure_order_allowed(kind, state)
        assert resp is None or not _is_paused(resp)


def test_read_only_precedes_pause(paused, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("XENON_READ_ONLY", "1")
    resp = guards.ensure_order_allowed("order placement", SimpleNamespace(broker="IB"))
    import json

    assert json.loads(resp.body)["reason_code"] == "READ_ONLY_MODE"


# ---------- task 1b: review findings ----------

HUGE = 10**1000


@pytest.mark.parametrize(
    "key,value",
    [
        pytest.param("FUTU_BOOK_STALE_S", HUGE, id="book-stale-huge"),
        pytest.param("XENON_FUTU_MKT_NOTIONAL_CAP", HUGE, id="cap-huge"),
        pytest.param("FUTU_GREEK_FACTORS", {"iv": HUGE}, id="greek-huge-int"),
        pytest.param("FUTU_GREEK_FACTORS", {"iv": 1e300}, id="greek-huge-float"),
        pytest.param("IB_REALTIME_MAX_LINES", HUGE, id="lines-huge"),
    ],
)
def test_huge_numbers_are_invalid_not_errors(key, value):
    with pytest.raises(rs.SettingError) as exc:
        _put(key, value)
    assert exc.value.reason_code == rs.INVALID_VALUE


def test_huge_number_put_is_400(client):
    resp = client.put(
        "/admin/settings/FUTU_BOOK_STALE_S",
        content=b'{"value": 1' + b"0" * 400 + b"}",
        headers={**HDR, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "SETTING_INVALID_VALUE"


def test_huge_number_db_row_is_ignored():
    _raw_row("FUTU_BOOK_STALE_S", "1" + "0" * 400)
    _put("orders_paused", True)
    assert rs.orders_paused() is True
    assert "FUTU_BOOK_STALE_S" not in rs.overrides()
    rows = {r["key"]: r for r in rs.describe()}
    assert rows["FUTU_BOOK_STALE_S"]["db_value_invalid"] is True


def _failing_load():
    raise RuntimeError("settings read failed")


@pytest.mark.parametrize("prime", [True, False])
def test_committed_write_survives_a_read_failure(monkeypatch, prime):
    from types import SimpleNamespace

    if prime:
        assert rs.orders_paused() is False  # cache primed with the empty set
    _put("orders_paused", True)
    real = rs._load_rows
    monkeypatch.setattr(rs, "_load_rows", _failing_load)
    assert rs.orders_paused() is True
    state = SimpleNamespace(broker="IB", mode_verified=True)
    assert _is_paused(guards.ensure_order_allowed("order placement", state))
    # never monkeypatch.undo(): it would also undo the Phase-2 engine binding
    monkeypatch.setattr(rs, "_load_rows", real)
    rs.delete("orders_paused", actor="t", note=None, confirm=None)
    monkeypatch.setattr(rs, "_load_rows", _failing_load)
    assert rs.orders_paused() is False


def test_load_started_before_a_write_cannot_overwrite_it(monkeypatch):
    real = rs._load_rows
    calls = []

    def racing_load():
        rows = real()  # snapshot taken before the write commits
        if not calls:
            calls.append(1)
            _put("orders_paused", True)
        return rows

    monkeypatch.setattr(rs, "_load_rows", racing_load)
    assert rs.orders_paused() is True
    assert rs.orders_paused() is True  # within the TTL too


def _hold_key_lock(key, value):
    """Second real transaction: take the key lock, write a row, wait for commit."""
    import json as _json

    conn = get_sync_engine().connect()
    tx = conn.begin()
    rs._lock_key(conn, key)
    conn.execute(
        text("INSERT INTO xenon.runtime_settings(key, value) VALUES (:k, CAST(:v AS jsonb))"),
        {"k": key, "v": _json.dumps(value)},
    )
    return conn, tx


def _run_in_thread(fn, name=None):
    import threading

    box = {}

    def run():
        try:
            box["result"] = fn()
        except Exception as exc:  # noqa: BLE001 — surfaced to the test
            box["error"] = exc

    t = threading.Thread(target=run, name=name)
    t.start()
    return t, box


@pytest.mark.committed_db
def test_concurrent_first_write_rechecks_confirm(monkeypatch):
    import time

    monkeypatch.setenv("XENON_AUDIT_CANCEL", "1")
    conn, tx = _hold_key_lock("XENON_AUDIT_CANCEL", False)
    t, box = _run_in_thread(lambda: _put("XENON_AUDIT_CANCEL", True))
    time.sleep(0.5)
    assert t.is_alive()  # waits on the key lock
    tx.commit()
    conn.close()
    t.join(10)
    assert isinstance(box.get("error"), rs.SettingError)
    assert box["error"].reason_code == rs.CONFIRM_REQUIRED
    rs.invalidate()
    assert audit_enforce.enabled() is False


@pytest.mark.committed_db
def test_concurrent_first_write_audits_true_predecessor():
    import time

    conn, tx = _hold_key_lock("IB_REALTIME_MAX_LINES", 50)
    t, box = _run_in_thread(lambda: _put("IB_REALTIME_MAX_LINES", 60))
    time.sleep(0.5)
    tx.commit()
    conn.close()
    t.join(10)
    assert "error" not in box
    rows = _audit_rows("IB_REALTIME_MAX_LINES")
    assert [(r["old_value"], r["new_value"]) for r in rows] == [(50, 60)]


@pytest.mark.committed_db
@pytest.mark.parametrize("older", ["put_false", "delete"])
def test_older_write_cannot_publish_over_a_newer_commit(monkeypatch, older):
    """Writer A commits first but is slow to publish; writer B commits after A.
    B's acknowledged pause must survive a later settings-read failure."""
    import threading
    from types import SimpleNamespace

    if older == "delete":
        _put("orders_paused", True)
    committed, release = threading.Event(), threading.Event()
    real_publish = rs._publish_write

    def slow_publish(key, value):
        if threading.current_thread().name == "older-writer":
            committed.set()
            assert release.wait(10)
        return real_publish(key, value)

    monkeypatch.setattr(rs, "_publish_write", slow_publish)

    def write_a():
        if older == "delete":
            return rs.delete("orders_paused", actor="a", note=None, confirm=None)
        return _put("orders_paused", False)

    a, a_box = _run_in_thread(write_a, name="older-writer")
    assert committed.wait(10)
    b, b_box = _run_in_thread(lambda: _put("orders_paused", True))
    b.join(1)  # either B returns (no ordering) or it waits for A's publish
    release.set()
    a.join(10)
    b.join(10)
    assert "error" not in a_box and "error" not in b_box
    with get_sync_engine().connect() as conn:
        stored = conn.execute(
            select(settings_table.c.value).where(settings_table.c.key == "orders_paused")
        ).scalar_one()
    assert stored is True
    monkeypatch.setattr(rs, "_load_rows", _failing_load)
    assert rs.orders_paused() is True
    state = SimpleNamespace(broker="IB", mode_verified=True)
    assert _is_paused(guards.ensure_order_allowed("order placement", state))


# ─── D5: the api is the only settings DB reader (ib_sync child = env only) ──


@pytest.mark.parametrize(("db", "child"), [(None, "1"), (False, "0")])
def test_sync_spawn_passes_effective_netting_in_child_env(monkeypatch, db, child):
    from xenon.api import server as server_mod
    from xenon.api.routes import portfolio
    from xenon.api.subprocess import ScriptResult

    monkeypatch.setenv("XENON_GATE4_NETTING", "1")
    if db is not None:
        _put("XENON_GATE4_NETTING", db)
    seen = {}

    async def fake(entry, args, timeout=30, env=None):
        seen.update(entry=entry, env=env())
        return ScriptResult(ok=True, data={})

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake)
    assert asyncio.run(portfolio._run_sync_pass()).ok
    assert seen["entry"] == "xenon-ib-sync"
    assert seen["env"] == {"XENON_GATE4_NETTING": child}


def test_run_entry_point_merges_child_env(monkeypatch, tmp_path):
    from xenon.api import subprocess as sp

    (tmp_path / "xenon-x").write_text("")
    monkeypatch.setattr(sp, "VENV_BIN", tmp_path)
    monkeypatch.setenv("XENON_PARENT_ONLY", "kept")
    seen = {}

    async def fake_exec(*cmd, **kw):
        seen.update(kw)
        raise OSError("not started")

    monkeypatch.setattr(sp.asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(sp.run_entry_point("xenon-x", [], env={"XENON_GATE4_NETTING": "0"}))
    assert seen["env"]["XENON_GATE4_NETTING"] == "0"
    assert seen["env"]["XENON_PARENT_ONLY"] == "kept"


def test_ib_sync_child_reads_no_settings_from_the_db(monkeypatch):
    from xenon.execution import ib_sync

    monkeypatch.setenv("XENON_GATE4_NETTING", "1")
    _put("XENON_GATE4_NETTING", False)  # a DB value the child must not see
    calls = []
    monkeypatch.setattr(rs, "_load_rows", lambda: calls.append(1) or [])
    with pytest.raises(SystemExit):
        ib_sync.main(["--help"])  # env-only mode is set before argument parsing
    rs.invalidate()
    assert gate4.netting_enabled() is True  # env only
    assert calls == []


def _sync_spawn_harness(monkeypatch):
    from xenon.api import server as server_mod

    monkeypatch.setenv("XENON_GATE4_NETTING", "1")
    monkeypatch.setattr(server_mod, "_ib_last_failure", {"data": 0.0})
    monkeypatch.setattr(server_mod, "is_cloud_mode", lambda: False)
    monkeypatch.setattr(server_mod, "is_docker_mode", lambda: False)
    monkeypatch.setattr(server_mod, "ib_pool", None)
    return server_mod


def test_netting_narrowed_during_preflight_reaches_the_spawn(monkeypatch):
    from xenon.api.routes import portfolio
    from xenon.api.subprocess import ScriptResult

    server_mod = _sync_spawn_harness(monkeypatch)
    spawns = []

    async def preflight():
        _put("XENON_GATE4_NETTING", False)  # committed while the sync waits
        return {"port_listening": True, "upstream_dead": False}

    async def spawn(entry, args, timeout=30, env=None):
        spawns.append(env["XENON_GATE4_NETTING"])
        return ScriptResult(ok=True, data={})

    monkeypatch.setattr(server_mod, "_pool_has_any_connection", lambda: False)
    monkeypatch.setattr(server_mod, "check_ib_gateway", preflight)
    monkeypatch.setattr(server_mod, "run_entry_point", spawn)
    assert gate4.netting_enabled() is True
    assert asyncio.run(portfolio._run_sync_pass()).ok
    assert spawns == ["0"]


def test_netting_narrowed_during_recovery_reaches_the_retry(monkeypatch):
    from xenon.api.routes import portfolio
    from xenon.api.subprocess import ScriptResult

    server_mod = _sync_spawn_harness(monkeypatch)
    spawns = []

    async def spawn(entry, args, timeout=30, env=None):
        spawns.append(env["XENON_GATE4_NETTING"])
        return ScriptResult(ok=len(spawns) > 1, error=None if len(spawns) > 1 else "Connection refused")

    async def gateway_down():
        return {"port_listening": False, "upstream_dead": True}

    async def restart():
        _put("XENON_GATE4_NETTING", False)  # committed during recovery
        return {"restarted": True, "port_listening": True}

    monkeypatch.setattr(server_mod, "_pool_has_any_connection", lambda: True)
    monkeypatch.setattr(server_mod, "check_ib_gateway", gateway_down)
    monkeypatch.setattr(server_mod, "restart_ib_gateway", restart)
    monkeypatch.setattr(server_mod, "run_entry_point", spawn)
    assert asyncio.run(portfolio._run_sync_pass()).ok
    assert spawns == ["1", "0"]


# ---------- labels and relay live values (UX task 4) ----------


def test_every_key_has_a_label_and_a_plain_description():
    for s in rs.REGISTRY.values():
        assert s.label and s.label != s.key, s.key
        assert "Narrow-only" not in s.description and "DB value" not in s.description, s.key
    rows = {r["key"]: r for r in rs.describe()}
    cap = rows["XENON_FUTU_MKT_NOTIONAL_CAP"]
    assert (cap["label"], cap["unit"]) == ("Futu market-order cap", "USD")
    assert rows["IB_REALTIME_STALE_TICK_MS"]["unit"] == "ms"
    assert rows["XENON_AUDIT_CANCEL"]["unit"] is None


def test_get_settings_adds_relay_live_values(client, monkeypatch):
    from xenon.api.routes import system

    seen = {}

    def up(url, timeout=0.5):
        seen["timeout"] = timeout
        return {
            "runtime_settings": {
                "effective": {"IB_REALTIME_MAX_LINES": 60, "XENON_QUOTES_DEFAULT": "futu-first", "FUTU_QUOTE_QUOTA": 1}
            }
        }

    monkeypatch.setattr(system, "_fetch_realtime_status_json", up)
    rows = {r["key"]: r for r in client.get("/admin/settings", headers=HDR).json()["settings"]}
    assert rows["IB_REALTIME_MAX_LINES"]["live_value"] == 60
    assert rows["XENON_QUOTES_DEFAULT"]["live_value"] == "futu-first"
    assert "live_value" not in rows["FUTU_QUOTE_QUOTA"]  # futu-quotes key: relay value not used
    assert "live_value" not in rows["XENON_FUTU_MKT_NOTIONAL_CAP"]
    assert seen["timeout"] <= 1


def test_get_settings_relay_down_leaves_live_value_absent(client):
    resp = client.get("/admin/settings", headers=HDR)  # autouse: relay down
    assert resp.status_code == 200
    assert all("live_value" not in r for r in resp.json()["settings"])


def test_get_settings_relay_without_status_token(client, monkeypatch):
    import io
    import urllib.error
    import urllib.request

    from xenon.api.routes import system

    monkeypatch.setattr(system, "_fetch_realtime_status_json", _REAL_FETCH)  # the real helper
    monkeypatch.delenv("IB_REALTIME_STATUS_TOKEN", raising=False)
    monkeypatch.setenv("IB_REALTIME_STATUS_URL", "http://relay.test/status")
    sent = {}

    def urlopen(req, timeout=None):
        sent["headers"] = dict(req.header_items())
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    resp = client.get("/admin/settings", headers=HDR)
    assert resp.status_code == 200
    assert "X-status-token" not in sent["headers"]
    assert all("live_value" not in r for r in resp.json()["settings"])
