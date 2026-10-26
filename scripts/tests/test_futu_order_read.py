"""X5a task 4: observed terms, GET contract fields, untriggered statuses, /health capabilities.

Orders go through the real routes, Gate 4, the Futu order worker and the
observation mirror over the autospec-bound fake Futu context (`FakeOrderCtx`);
the IB quote gate is stubbed (`futu` fixture). Ids, quantities and prices are
made-up test values, not market data.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scripts.tests import test_orders_futu_branch as _branch
from scripts.tests.test_futu_observation import ACC, SCOPE, _env, _order  # noqa: F401 — autouse fixture
from scripts.tests.test_futu_order_intent import LIVE, LIVE_TUPLES, STP, TRAIL, TRAIL_LIMIT, _observe, _set_legs, _typed
from scripts.tests.test_orders_futu_branch import (  # noqa: F401 — autouse fixture
    LIVE_ACC,
    _configured,
    _post,
    _reserved,
    _row,
)
from xenon.api.routes.orders import orders_payload_for_scope
from xenon.api.services import futu_observation as fo
from xenon.execution import orders_store

pytestmark = pytest.mark.committed_db

futu = _branch.futu
futu_ib_quotes = _branch.futu_ib_quotes

STP_LMT = {"orderType": "STP LMT", "auxPrice": 510.5, "limitPrice": 510.52}
MIT = {"orderType": "MIT", "auxPrice": 490.5}
LIT = {"orderType": "LIT", "auxPrice": 490.5, "limitPrice": 490.52}
NONE = {"auxPrice": None, "trailType": None, "trailValue": None, "trailSpread": None}

# label → (env, place fields, expected GET terms)
TYPES = {
    "LMT": ("live", {"limitPrice": 500.2}, {"limitPrice": 500.2, **NONE}),
    "MKT": ("paper", {"orderType": "MKT"}, {"limitPrice": None, **NONE}),
    "STP": ("live", STP, {"limitPrice": None, **NONE, "auxPrice": 510.5}),
    "STP LMT": ("live", STP_LMT, {"limitPrice": 510.52, **NONE, "auxPrice": 510.5}),
    "MIT": ("live", MIT, {"limitPrice": None, **NONE, "auxPrice": 490.5}),
    "LIT": ("live", LIT, {"limitPrice": 490.52, **NONE, "auxPrice": 490.5}),
    "TRAIL": ("live", TRAIL, {"limitPrice": None, **NONE, "trailType": "RATIO", "trailValue": 1.5}),
    "TRAIL LIMIT": (
        "live",
        TRAIL_LIMIT,
        {"limitPrice": None, **NONE, "trailType": "AMOUNT", "trailValue": 2.5, "trailSpread": 0.05},
    ),
}
TRIGGER_TYPES = ["STP", "STP LMT", "MIT", "LIT", "TRAIL", "TRAIL LIMIT"]


@pytest.fixture
def live_types(monkeypatch):
    monkeypatch.setenv("XENON_FUTU_TUPLES_LIVE", LIVE_TUPLES)


def _scope(env):
    return SCOPE if env == "paper" else LIVE


def _place(futu, label, cid=None):
    env, fields, _terms = TYPES[label]
    resp = _post(futu, "/orders/place", _typed(cid or f"read-{label}", **fields), env=env)
    assert resp.status_code == 200, resp.text
    return env, resp.json()["submission_id"], resp.json()["brokerOrderId"]


def _shown(env, oid):
    rows = [o for o in orders_payload_for_scope(_scope(env))["open_orders"] if o["brokerOrderId"] == oid]
    return rows[0] if rows else None


# -- Observation stores the observed terms -------------------------------------


@pytest.mark.parametrize(
    "cols, stored",
    [
        (
            {"order_type": "STOP_LIMIT", "price": 511.0, "aux_price": 510.5, "trail_type": "N/A", "session": "RTH"},
            {"label": "STP LMT", "aux_price": "510.5", "trail_type": None, "session": "RTH"},
        ),
        (
            {"order_type": "TRAILING_STOP_LIMIT", "price": 3.0, "aux_price": 0.0, "trail_type": "AMOUNT"}
            | {"trail_value": 2.5, "trail_spread": 0.05, "session": "ETH"},
            {"label": "TRAIL LIMIT", "aux_price": None, "trail_type": "AMOUNT", "trail_value": "2.5"}
            | {"trail_spread": "0.05", "session": "ETH"},
        ),
        (
            {"order_type": "NORMAL", "price": 500.2, "aux_price": 0.0, "trail_value": 0.0, "session": "N/A"},
            {"label": "LMT", "aux_price": None, "trail_value": None, "session": None},  # SDK placeholders dropped
        ),
        (
            {"order_type": "AUCTION_LIMIT", "price": 500.2, "aux_price": 1.0, "session": "OVERNIGHT"},
            {"label": None, "order_type": "AUCTION_LIMIT", "aux_price": "1.0", "session": "OVERNIGHT"},
        ),
    ],
)
def test_the_observation_stores_type_label_trigger_trail_and_session(cols, stored):
    row = {**_order(order_id="7801", code="US.SPY", side="BUY", qty=1.0), **cols}
    obs = fo.observation_from_row(row, LIVE, datetime.now(timezone.utc))

    info = orders_store._futu_order_info(obs)

    assert info["status"] == "SUBMITTED" and info["order_type"] == cols["order_type"]
    assert {k: info[k] for k in stored} == stored


def test_the_observed_terms_land_in_legs_futu_order_never_in_intent_or_terms(futu, live_types):
    env, sid, oid = _place(futu, "TRAIL LIMIT")
    before = _row(sid).legs
    futu.ctx.book[oid].update(trail_value=3.0, session="RTH")  # changed in the Futu app

    assert _observe(LIVE, futu.ctx)["complete"]

    legs = _row(sid).legs
    assert legs["futu_order"]["trail_value"] == "3.0" and legs["futu_order"]["label"] == "TRAIL LIMIT"
    assert legs["futu_intent"] == before["futu_intent"] and legs["futu_terms"] == before["futu_terms"]
    assert _shown(env, oid)["trailValue"] == 3.0  # GET shows what Futu reports


# -- GET /orders?broker=FUTU: the contract fields ------------------------------


@pytest.mark.parametrize("label", list(TYPES))
def test_get_returns_the_contract_fields_per_type(futu, live_types, label):
    env, sid, oid = _place(futu, label)
    assert _observe(_scope(env), futu.ctx)["complete"]

    shown = _shown(env, oid)

    assert shown["orderType"] == label and shown["tif"] == "DAY" and shown["session"] == "RTH"
    assert {k: shown[k] for k in TYPES[label][2]} == TYPES[label][2]
    assert (shown["brokerOrderId"], shown["orderId"], shown["permId"]) == (oid, 0, 0)
    assert shown["futuStatus"] == "SUBMITTED" and shown["modifySequence"] == 0
    assert (shown["broker"], shown["accountEnv"], shown["brokerAccount"]) == (
        "FUTU",
        env,
        ACC if env == "paper" else LIVE_ACC,
    )


@pytest.mark.parametrize("label", ["STP LMT", "TRAIL LIMIT"])
def test_get_falls_back_to_the_confirmed_terms_then_the_placement(futu, live_types, label):
    """An own order with no observed terms yet shows `futu_terms`, then `futu_intent`."""
    env, sid, oid = _place(futu, label)
    terms = dict(_row(sid).legs["futu_terms"])
    if label == "STP LMT":
        terms["aux_price"] = "511"
    else:
        terms["trail_value"] = "2.75"
    _set_legs(sid, futu_order={"order_type": None, "status": "SUBMITTED"}, futu_terms=terms)

    shown = _shown(env, oid)

    assert shown["orderType"] == label and shown["session"] == "RTH"
    if label == "STP LMT":
        assert (shown["auxPrice"], shown["limitPrice"]) == (511.0, 510.52)
    else:
        assert (shown["trailType"], shown["trailValue"], shown["trailSpread"]) == ("AMOUNT", 2.75, 0.05)
        assert shown["limitPrice"] is None


# -- Untriggered status, cancel and fill for trigger types ----------------------


@pytest.mark.parametrize("label", TRIGGER_TYPES)
def test_waiting_submit_stays_unknown_keeps_demand_and_cancels(futu, live_types, label):
    env, sid, oid = _place(futu, label)
    futu.ctx.book[oid]["order_status"] = "WAITING_SUBMIT"

    assert _observe(LIVE, futu.ctx)["complete"]

    row = _row(sid)
    assert row.state == "UNKNOWN" and row.legs["futu_order"]["status"] == "WAITING_SUBMIT"
    assert _reserved(sid) == 2  # demand kept
    shown = _shown(env, oid)
    assert shown is not None and shown["futuStatus"] == "WAITING_SUBMIT" and shown["orderType"] == label

    cancel = _post(futu, "/orders/cancel", {"brokerOrderId": oid}, env=env)

    assert cancel.status_code == 200 and cancel.json()["confirmed"] is True, cancel.text
    assert _row(sid).state == "CANCELLED"  # terminal: Gate 4 drops the demand
    assert _shown(env, oid) is None


@pytest.mark.parametrize("label", TRIGGER_TYPES)
def test_a_submitted_trigger_order_is_working_and_its_fill_is_recorded(futu, live_types, label):
    env, sid, oid = _place(futu, label)
    assert _observe(LIVE, futu.ctx)["complete"]
    assert _row(sid).state == "WORKING" and _reserved(sid) == 2  # untriggered SUBMITTED = working

    futu.ctx.book[oid].update(order_status="FILLED_ALL", dealt_qty=2.0, dealt_avg_price=505.0)
    assert _observe(LIVE, futu.ctx)["complete"]

    row = _row(sid)
    assert row.state == "FILLED" and int(row.filled_qty) == 2
    assert _shown(env, oid) is None


# -- /health futu.capabilities ---------------------------------------------------


def _capabilities():
    from xenon.api.routes import system

    health = system._compute_futu_health()
    assert ACC not in str(health) and LIVE_ACC not in str(health)
    return health["capabilities"]


def test_capabilities_default_to_the_v016_surface(monkeypatch):
    for name in ("XENON_FUTU_TUPLES_LIVE", "XENON_FUTU_TUPLES_PAPER", "XENON_FUTU_MODIFY_TYPES"):
        monkeypatch.delenv(name, raising=False)

    assert _capabilities() == {
        "paper": {"tuples": ["OPT:LMT:DAY:RTH", "STK:LMT:DAY:RTH", "STK:MKT:DAY:RTH"], "modify": ["LMT"]},
        "live": {
            "tuples": ["OPT:LMT:DAY:RTH", "OPT:LMT:GTC:RTH", "STK:LMT:DAY:RTH", "STK:LMT:GTC:RTH"],
            "modify": ["LMT"],
        },
    }


def test_one_added_tuple_shows_only_itself(monkeypatch):
    default = "STK:LMT:DAY:RTH,STK:LMT:GTC:RTH,OPT:LMT:DAY:RTH,OPT:LMT:GTC:RTH"
    monkeypatch.setenv("XENON_FUTU_TUPLES_LIVE", default + ",STK:STP:DAY:RTH")
    monkeypatch.setenv("XENON_FUTU_MODIFY_TYPES", "LMT,STP,TRAIL")

    caps = _capabilities()

    assert caps["live"]["tuples"] == sorted(default.split(",") + ["STK:STP:DAY:RTH"])
    assert caps["live"]["modify"] == ["LMT", "STP"]  # TRAIL has no allowed tuple
    assert caps["paper"]["modify"] == ["LMT"] and "STK:STP:DAY:RTH" not in caps["paper"]["tuples"]


@pytest.mark.parametrize(
    "env_vars",
    [
        {"XENON_FUTU_TUPLES_LIVE": "STK:LMT:DAY"},  # malformed: the whole list is off
        {"XENON_FUTU_TUPLES_LIVE": "OPT:MKT:DAY:RTH"},  # never a Futu tuple
    ],
)
def test_a_bad_allowlist_shows_no_live_tuple(monkeypatch, env_vars):
    for k, v in env_vars.items():
        monkeypatch.setenv(k, v)

    assert _capabilities()["live"] == {"tuples": [], "modify": []}


def test_a_bad_modify_list_turns_modify_off(monkeypatch):
    monkeypatch.setenv("XENON_FUTU_MODIFY_TYPES", "LMT,PEG")

    caps = _capabilities()

    assert caps["live"]["modify"] == [] and caps["paper"]["modify"] == []
    assert caps["live"]["tuples"]  # place tuples do not depend on it


def test_get_nulls_v016_placeholders_of_a_type_without_the_term(futu, live_types):
    """A row stored before X5 (raw `futu_order.aux_price` for any type, the SDK
    price as limit) shows no trigger for LMT and no limit for STP."""
    from sqlalchemy import update

    from xenon.db.engine import get_sync_engine
    from xenon.db.schema import order_submissions

    env, lmt_sid, lmt_oid = _place(futu, "LMT", cid="v016-lmt")
    _set_legs(lmt_sid, futu_order={"order_type": "NORMAL", "aux_price": "0.0", "status": "SUBMITTED"})
    env, stp_sid, stp_oid = _place(futu, "STP", cid="v016-stp")
    _set_legs(stp_sid, futu_order={"order_type": "STOP", "aux_price": "510.5", "status": "SUBMITTED"})
    with get_sync_engine().begin() as conn:
        conn.execute(
            update(order_submissions).where(order_submissions.c.submission_id == stp_sid).values(limit_price=9.99)
        )

    assert (_shown(env, lmt_oid)["auxPrice"], _shown(env, lmt_oid)["limitPrice"]) == (None, 500.2)
    assert (_shown(env, stp_oid)["auxPrice"], _shown(env, stp_oid)["limitPrice"]) == (510.5, None)
