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
from xenon.api.services import audit_enforce, order_entry
from xenon.db.engine import get_sync_engine
from xenon.db.schema import runtime_settings as settings_table
from xenon.db.schema import runtime_settings_audit as audit_table
from xenon.execution import broker_capabilities, futu_orders, gate4, quote_guard

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
    assert client.get("/admin/settings", headers={"X-API-Key": "qk"}).status_code == 401
    monkeypatch.setenv("XENON_AUTH_ALLOW_DEV_OPEN", "1")
    resp = client.get("/admin/settings", headers={"X-API-Key": "qk"})
    assert resp.status_code == 403 and resp.json()["reason_code"] == "API_KEY_FORBIDDEN"


def test_no_credential_401(client, monkeypatch):
    monkeypatch.delenv("XENON_AUTH_ALLOW_DEV_OPEN", raising=False)
    assert client.get("/admin/settings").status_code == 401
    assert client.put("/admin/settings/orders_paused", json={"value": True}).status_code == 401


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
