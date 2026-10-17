"""XENON_READ_ONLY=1 disables write routes (orders/place, cancel, modify, journal).

Set by `scripts/infra/dev.sh live` — keeps dev-machine sessions from
persisting any live-IB-derived data into core_test. Real prod writes go
through the macmini Docker stack which never sets the flag.

Response shape: JSONResponse with top-level `reason_code: READ_ONLY_MODE`
so the web toast helper renders it directly (HTTPException(detail={...})
nests it under .detail and breaks the toast — see
CLAUDE.md memory feedback_httpexception_dict_detail_breaks_toast).
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def read_only_client(monkeypatch):
    """TestClient with XENON_READ_ONLY=1 and mode matched.

    Mode match is needed so the read-only check fires BEFORE the mode
    guard; otherwise a mode-mismatched test would 503 first and we'd
    never reach the 403.
    """
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    import xenon.api.trading_mode as tm

    importlib.reload(tm)
    import xenon.api.server as server

    importlib.reload(server)
    monkeypatch.setattr(server, "_get_managed_account_for_health", lambda: "DU1111111")
    with TestClient(server.app) as c:
        yield c


@pytest.fixture
def writable_client(monkeypatch):
    """Identical setup but without the read-only flag."""
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)
    import xenon.api.trading_mode as tm

    importlib.reload(tm)
    import xenon.api.server as server

    importlib.reload(server)
    monkeypatch.setattr(server, "_get_managed_account_for_health", lambda: "DU1111111")
    with TestClient(server.app) as c:
        yield c


@pytest.mark.parametrize(
    "path",
    [
        "/orders/place",
        "/orders/cancel",
        "/orders/modify",
    ],
)
def test_write_routes_return_403_in_read_only(read_only_client, path):
    """All three order-mutation routes return 403 with the standard payload."""
    r = read_only_client.post(path, json={})
    assert r.status_code == 403, r.text
    body = r.json()
    assert body.get("reason_code") == "READ_ONLY_MODE", body
    # Top-level reason_code is the toast contract — detail is a human string,
    # NOT a dict containing reason_code (which would nest it one level deep).
    assert isinstance(body.get("detail"), str)
    assert "dev.sh live" in body["detail"] or "XENON_READ_ONLY" in body["detail"]


@pytest.mark.parametrize(
    "path",
    [
        "/orders/place",
        "/orders/cancel",
        "/orders/modify",
    ],
)
def test_write_routes_return_403_in_read_only_even_when_mode_unverified(read_only_client, monkeypatch, path):
    """Read-only outranks mode verification on the HTTP routes too.

    A `dev.sh live` session with an unverified IB mode handshake must still
    get 403 READ_ONLY_MODE — a route-level Depends(require_mode_verified)
    that runs before the handler would answer 503 instead, hiding the real
    reason the write was refused.
    """
    import xenon.api.server as server

    monkeypatch.setattr(server.app.state, "mode_verified", False, raising=False)
    r = read_only_client.post(path, json={})
    assert r.status_code == 403, r.text
    assert r.json().get("reason_code") == "READ_ONLY_MODE", r.json()


def test_journal_post_read_only_precedes_scope_resolution(read_only_client, monkeypatch):
    """403 before scope resolution — an unresolvable account scope must not
    surface a 5xx ahead of the read-only refusal."""
    import xenon.api.server as server

    monkeypatch.setattr(server.app.state, "account", "", raising=False)
    monkeypatch.setattr(server.app.state, "trading_mode", "", raising=False)
    r = read_only_client.post("/journal", json={"ticker": "SPY", "note": "x"})
    assert r.status_code == 403, r.text
    assert r.json().get("reason_code") == "READ_ONLY_MODE", r.json()


@pytest.mark.parametrize(
    "path",
    [
        "/wizard/plan",
        "/wizard/sessions",
        "/wizard/sessions/wiz-ro/protect",
    ],
)
def test_wizard_post_read_only_precedes_body_validation(read_only_client, path):
    """403 before request-body validation — a malformed body must get the
    read-only refusal, not a 422."""
    r = read_only_client.post(path, json={"bogus": True})
    assert r.status_code == 403, r.text
    assert r.json().get("reason_code") == "READ_ONLY_MODE", r.json()


# Every write route the read-only ASGI guard covers — POST/PUT/PATCH/DELETE
# only; /orders/refresh stays open under read-only (it serves the PG view).
_WRITE_PATHS = [
    "/orders/place",
    "/orders/cancel",
    "/orders/modify",
    "/journal",
    "/wizard/plan",
    "/wizard/sessions",
    "/wizard/sessions/wiz-ro/submit",
    "/wizard/sessions/wiz-ro/reprice",
    "/wizard/sessions/wiz-ro/abort",
    "/wizard/sessions/wiz-ro/protect",
]


_WRITE_PATH_FORMS = [p + suffix for p in _WRITE_PATHS for suffix in ("", "/")]


@pytest.mark.parametrize("path", _WRITE_PATH_FORMS)
def test_read_only_refuses_malformed_json_body(read_only_client, path):
    """Malformed JSON gets 403 READ_ONLY_MODE, not 422 or a 307 trailing-slash
    redirect — the ASGI guard normalizes the path and runs before FastAPI
    decodes the request body."""
    r = read_only_client.post(
        path,
        content="{",
        headers={"Content-Type": "application/json"},
        follow_redirects=False,
    )
    assert r.status_code == 403, r.text
    assert r.json().get("reason_code") == "READ_ONLY_MODE", r.json()


@pytest.mark.parametrize("path", _WRITE_PATH_FORMS)
def test_read_only_refuses_schema_invalid_body(read_only_client, path):
    """Schema-invalid bodies get the 403 refusal ahead of validation — in
    canonical and trailing-slash form alike."""
    r = read_only_client.post(path, json={"bogus": True}, follow_redirects=False)
    assert r.status_code == 403, r.text
    assert r.json().get("reason_code") == "READ_ONLY_MODE", r.json()


@pytest.mark.parametrize("path", _WRITE_PATH_FORMS)
def test_unauthenticated_still_401_in_read_only(read_only_client, monkeypatch, path):
    """Auth stays outermost: a denied request gets 401 even under read-only —
    the write-guard middleware must not preempt authentication."""
    import xenon.api.server as server
    from xenon.api.auth import AuthDecision

    monkeypatch.setattr(server, "classify_auth", lambda request: AuthDecision("deny"))
    r = read_only_client.post(path, json={}, follow_redirects=False)
    assert r.status_code == 401, r.text


def test_journal_post_returns_403_in_read_only(read_only_client):
    """POST /journal also blocks — journal writes are user-authored state."""
    r = read_only_client.post(
        "/journal",
        json={"ticker": "SPY", "note": "test"},
    )
    assert r.status_code == 403, r.text
    body = r.json()
    assert body.get("reason_code") == "READ_ONLY_MODE", body


def test_journal_get_succeeds_in_read_only(read_only_client):
    """Reads stay open — the flag only blocks writes."""
    r = read_only_client.get("/journal?days=1&limit=1")
    # In test_mode the journal table is per-test-txn empty; the route
    # itself should still return 200 with an empty trades list.
    assert r.status_code == 200, r.text
    assert "trades" in r.json()


def test_orders_place_succeeds_when_flag_unset(writable_client):
    """Sanity: without XENON_READ_ONLY, the route advances past the guard.

    /orders/place still needs IB plumbing; in test_mode it short-circuits
    earlier paths. We only assert it does NOT return 403/READ_ONLY_MODE.
    """
    r = writable_client.post("/orders/place", json={})
    if r.status_code == 403:
        body = r.json()
        assert body.get("reason_code") != "READ_ONLY_MODE", (
            "Flag-unset client got 403 READ_ONLY_MODE — guard fired on the wrong side. Check monkeypatch ordering."
        )


# ---------------------------------------------------------------------------
# Wizard order-mutating POSTs — they bypass the /orders/* route guards by
# calling server._orders_*_from_body and combo_session writers in-process,
# so each route must apply the shared order guard itself.
# ---------------------------------------------------------------------------

_WIZARD_PLAN_BODY = {
    "ticker": "SPY",
    "intent": "OPEN",
    "legs": [
        {
            "contract_id": "SPY_20260417_200_C",
            "action": "BUY",
            "right": "C",
            "strike": "200",
            "expiry": "20260417",
            "quantity": 1,
        },
    ],
    "quotes": {"SPY_20260417_200_C": {"bid": "4.50", "ask": "4.70"}},
    "order_payload": {
        "symbol": "SPY",
        "type": "combo",
        "action": "BUY",
        "quantity": 1,
        "limitPrice": "4.60",
        "legs": [{"conId": 1001, "action": "BUY", "ratio": 1, "exchange": "SMART"}],
    },
}


def _wizard_sessions_count() -> int | None:
    """Row count via the bound test engine; None when PG is unreachable."""
    try:
        from xenon._test_db import is_pg_reachable

        if not is_pg_reachable():
            return None
        from sqlalchemy import text

        from xenon.db.engine import get_sync_engine

        with get_sync_engine().connect() as conn:
            return int(conn.execute(text("SELECT COUNT(*) FROM xenon.wizard_sessions")).scalar())
    except Exception:
        return None


@pytest.mark.parametrize(
    "path,body",
    [
        ("/wizard/plan", _WIZARD_PLAN_BODY),
        ("/wizard/sessions", _WIZARD_PLAN_BODY),
        ("/wizard/sessions/wiz-ro/submit", {}),
        ("/wizard/sessions/wiz-ro/reprice", {"target_price": "1.00"}),
        ("/wizard/sessions/wiz-ro/abort", {}),
        (
            "/wizard/sessions/wiz-ro/protect",
            {"tp_target_price": "1.00", "alert_net_mid_threshold": "0.50", "polarity": "DEBIT"},
        ),
    ],
)
def test_wizard_post_routes_return_403_in_read_only(read_only_client, monkeypatch, path, body):
    """Every wizard POST returns 403 READ_ONLY_MODE and writes nothing.

    The combo_session entry points are replaced with raising spies so any
    guard bypass surfaces as a 500 instead of silently persisting a
    wizard_sessions / wizard_combo_attempts row.
    """
    from xenon.execution.combo_wizard import session as combo_session

    called: list[str] = []
    for name in ("plan_session", "submit_combo", "reprice_combo", "abort_session", "get_session"):
        original = getattr(combo_session, name)
        if inspect.iscoroutinefunction(original):

            async def _spy(*args, _name=name, **kwargs):
                called.append(_name)
                raise AssertionError(f"combo_session.{_name} reached under read-only")

        else:

            def _spy(*args, _name=name, **kwargs):
                called.append(_name)
                raise AssertionError(f"combo_session.{_name} reached under read-only")

        monkeypatch.setattr(combo_session, name, _spy)

    rows_before = _wizard_sessions_count()
    r = read_only_client.post(path, json=body)
    assert r.status_code == 403, r.text
    resp = r.json()
    assert resp.get("reason_code") == "READ_ONLY_MODE", resp
    assert isinstance(resp.get("detail"), str)
    assert called == [], f"guard bypassed: {called}"
    rows_after = _wizard_sessions_count()
    if rows_before is not None and rows_after is not None:
        assert rows_after == rows_before, "wizard POST persisted rows under read-only"


def test_in_process_order_helpers_blocked_in_read_only(read_only_client):
    """_orders_{place,cancel,modify}_from_body self-guard.

    The wizard submit path reaches these without going through the
    /orders/* routes, so the read-only denial must live inside them.
    """
    import xenon.api.server as server

    for helper in (
        server._orders_place_from_body,
        server._orders_cancel_from_body,
        server._orders_modify_from_body,
    ):
        result = asyncio.run(helper({}))
        assert result.status_code == 403, f"{helper.__name__} did not deny in read-only"
        payload = json.loads(result.body)
        assert payload["reason_code"] == "READ_ONLY_MODE", payload


def test_orders_refresh_serves_pg_view_without_sync_in_read_only(read_only_client, monkeypatch):
    """Under read-only, /orders/refresh returns the PG view and never runs --sync.

    The sentinel payload proves the route consulted the PG view function
    rather than the test-mode short-circuit or the subprocess sync.
    """
    import xenon.api.server as server

    spawned: list[tuple] = []

    async def _no_spawn(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("read-only refresh spawned an IB sync subprocess")

    monkeypatch.setattr(server, "_run_ib_script_with_recovery", _no_spawn)
    pg_view = {"open_orders": [], "executed_orders": [], "open_count": 0, "executed_count": 0}
    monkeypatch.setattr(server, "orders_payload_for_scope", lambda scope: pg_view)

    r = read_only_client.post("/orders/refresh")
    assert r.status_code == 200, r.text
    assert r.json() == pg_view
    assert spawned == []


# ---------------------------------------------------------------------------
# orders_store choke point — every writer raises under XENON_READ_ONLY=1 so
# subprocess / in-process callers cannot persist order state in a live
# debugging session.
# ---------------------------------------------------------------------------

_ORDERS_STORE_WRITERS = [
    (
        "reserve_attempt",
        {
            "user_id": "u",
            "client_attempt_id": "c-1",
            "request": None,
        },
    ),
    ("apply_modify", {"order_id": "1", "sequence": 1}),
    (
        "register_from_snapshot",
        {
            "perm_id": "1",
            "ib_order_id": "1",
            "ticker": "SPY",
            "security_type": "STK",
            "action": "BUY",
            "quantity": 1,
            "limit_price": 1.0,
        },
    ),
    ("apply_modify_by_perm_id", {"perm_id": "1", "sequence": 1}),
    (
        "mark_submitted",
        {"submission_id": "s", "ib_order_id": "1", "perm_id": "1", "placing_client_id": 26},
    ),
    (
        "mark_terminal",
        {
            "submission_id": "s",
            "state": "CANCELLED",
            "reason_code": "USER_CANCEL",
            "filled_qty": 0,
            "avg_fill_price": None,
        },
    ),
    (
        "record_fill",
        {
            "exec_id": "e-1",
            "submission_id": None,
            "perm_id": None,
            "con_id": None,
            "ticker": "SPY",
            "side": "BUY",
            "qty": Decimal("1"),
            "price": Decimal("1.00"),
            "filled_at": datetime.now(timezone.utc),
            "broker": "IB",
            "account_env": "paper",
            "broker_account": "DU1",
        },
    ),
    ("update_fill_commission", {"exec_id": "e-1", "commission": Decimal("1.00"), "realized_pnl": None}),
    ("record_event", {"submission_id": "s", "kind": "K", "detail": {}}),
]


@pytest.mark.parametrize("writer,kwargs", _ORDERS_STORE_WRITERS)
def test_orders_store_writers_raise_under_read_only(monkeypatch, writer, kwargs):
    """Each orders_store write function raises before touching the engine."""
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    from xenon.execution import orders_store

    with pytest.raises(orders_store.ReadOnlyWriteError, match="XENON_READ_ONLY"):
        getattr(orders_store, writer)(**kwargs)


def test_ensure_writable_is_noop_when_flag_unset(monkeypatch):
    """Sanity: the choke point only fires when the flag is set."""
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)
    from xenon.execution import orders_store

    assert orders_store.ensure_writable() is None


# ---------------------------------------------------------------------------
# combo_wizard.session in-process mutators — scripts/services calling the
# module directly must get the same READ_ONLY_MODE refusal before any
# session read/claim/write, not just callers that pass through the HTTP
# routes.
# ---------------------------------------------------------------------------


def test_combo_session_mutators_refuse_before_state_work(monkeypatch):
    """Direct calls under XENON_READ_ONLY get the 403 refusal object without
    reaching any engine/session helper — every touchpoint is stubbed to fail."""
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    from xenon.execution.combo_wizard import session as combo_session

    def _boom(name):
        def _f(*args, **kwargs):
            raise AssertionError(f"combo_session.{name} reached under read-only")

        return _f

    monkeypatch.setattr(combo_session, "get_sync_engine", _boom("get_sync_engine"))
    monkeypatch.setattr(combo_session.combo_planner, "build_plan", _boom("build_plan"))
    for name in (
        "_claim_session_for_submit",
        "_load_session",
        "_load_current_attempt",
    ):
        monkeypatch.setattr(combo_session, name, _boom(name))

    def _assert_read_only(resp):
        assert getattr(resp, "status_code", None) == 403, resp
        assert json.loads(resp.body)["reason_code"] == "READ_ONLY_MODE"

    _assert_read_only(
        combo_session.create_session(ticker="SPY", intent="OPEN", structure_name="s", payload={})
    )
    _assert_read_only(
        combo_session.plan_session(ticker="SPY", intent="OPEN", legs=[], quotes={}, order_payload={})
    )
    _assert_read_only(asyncio.run(combo_session.submit_combo("wiz-x", {})))
    _assert_read_only(asyncio.run(combo_session.reprice_combo("wiz-x", {"target_price": "1.00"})))
    _assert_read_only(asyncio.run(combo_session.abort_session("wiz-x")))


def test_attach_protection_refuses_in_read_only(monkeypatch):
    """attach_protection under XENON_READ_ONLY returns the 403 refusal before
    any session read or injected-broker call."""
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    from xenon.execution.combo_wizard import protect as combo_protect

    def _boom(*args, **kwargs):
        raise AssertionError("_load_session reached under read-only")

    monkeypatch.setattr(combo_protect, "_load_session", _boom)

    class _BoomIB:
        def __getattr__(self, name):
            raise AssertionError(f"ib.{name} reached under read-only")

    resp = combo_protect.attach_protection(
        "wiz-x",
        ib=_BoomIB(),
        tp_target_price=Decimal("1.00"),
        alert_net_mid_threshold=Decimal("0.50"),
    )
    assert getattr(resp, "status_code", None) == 403, resp
    assert json.loads(resp.body)["reason_code"] == "READ_ONLY_MODE"
