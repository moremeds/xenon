"""Gate-4 coverage module tests.

Part 1: parametrized over every case in the frozen fixture
`fixtures/gate4_coverage_cases.json` (lead-authored, spec §2.3 v3.2). Do not edit
the fixture; a disagreement means a deviation report, not a patch.

Part 2: focused unit tests for the helpers the module exposes.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.engine import make_url

import xenon._test_db as _test_db
from xenon.db.schema import order_submissions
from xenon.execution.coverage import (
    Decision,
    Holding,
    Leg,
    Modify,
    Order,
    View,
    evaluate,
    short_stock,
    uncovered,
)
from xenon.execution.orders_store import (
    ATTEMPT_BODY_MISMATCH,
    RequestRow,
    reserve_attempt,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "gate4_coverage_cases.json").read_text())
SCOPE = FIXTURE["scope_account"]


def _leg(d: dict) -> Leg:
    return Leg(
        action=d.get("action"),
        sym=d.get("sym"),
        sec=d.get("sec"),
        ratio=d.get("ratio", 1),
        right=d.get("right"),
        exp=d.get("exp"),
        mult=d.get("mult"),
        tc=d.get("tc"),
        strike=d.get("strike"),
        con_id=d.get("con_id"),
        und_sec=d.get("und_sec"),
    )


def _order(d: dict) -> Order:
    legs = d.get("legs")
    return Order(
        id=d.get("id"),
        account=d.get("account"),
        envelope=d.get("envelope"),
        qty=d.get("qty"),
        legs=None if legs is None else tuple(_leg(x) for x in legs),
        state=d.get("state", ""),
        filled_after_snapshot=d.get("filled_after_snapshot", 0),
        remaining=d.get("remaining", 0),
        guaranteed=d.get("guaranteed"),
        pending_amend_qty=d.get("pending_amend_qty"),
    )


def _holding(d: dict) -> Holding:
    return Holding(
        sym=d.get("sym"),
        sec=d.get("sec"),
        qty=d.get("qty"),
        account=d.get("account"),
        right=d.get("right"),
        exp=d.get("exp"),
        mult=d.get("mult"),
        tc=d.get("tc"),
        und_sec=d.get("und_sec"),
    )


def _candidate(d: dict):
    if "modify" in d:
        return Modify(order_id=d["modify"], new_qty=d["new_qty"])
    return _order(d)


def _view(case: dict) -> View:
    return View(**case.get("view", {}))


def _run(case: dict) -> Decision:
    return evaluate(
        [_holding(h) for h in case.get("holdings", [])],
        [_order(o) for o in case.get("orders", [])],
        _candidate(case["candidate"]),
        _view(case),
        SCOPE,
        netting_enabled=case.get("netting_enabled", False),
    )


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=[c["id"] for c in FIXTURE["cases"]])
def test_fixture_case(case):
    d = _run(case)
    assert d.accept == (case["expect"] == "accept"), (
        f"{case['id']}: expected {case['expect']} ({case.get('why', '')}) "
        f"— got accept={d.accept}, reasons={d.reasons}"
    )
    if case["expect"] == "reject":
        assert d.reasons, f"{case['id']}: reject without reasons"


# ---------------------------------------------------------------------------
# Focused unit tests
# ---------------------------------------------------------------------------

_EXP = "20261218"
_KEY = ("AAPL", "AAPL", _EXP, 100)


def _opt_leg(
    action="BUY", right="C", sym="AAPL", exp=_EXP, mult=100, tc="AAPL",
    ratio=1, strike=200, und_sec="STK",
):
    return Leg(action=action, sym=sym, sec="OPT", ratio=ratio, right=right, exp=exp, mult=mult, tc=tc, strike=strike, und_sec=und_sec)


def _call_holding(qty, sym="AAPL", exp=_EXP, mult=100, tc="AAPL", strike=200, account=SCOPE, und_sec="STK"):
    return Holding(sym=sym, sec="OPT", qty=qty, account=account, right="C", exp=exp, mult=mult, tc=tc, und_sec=und_sec)


def _sell_call_order(id="o1", account=SCOPE, qty=1, remaining=1, filled_after=0, **kw):
    return Order(
        id=id,
        account=account,
        envelope="SELL",
        qty=qty,
        remaining=remaining,
        filled_after_snapshot=filled_after,
        legs=(_opt_leg("BUY"),),
        **kw,
    )


def _sell_call_cand(
    strike=210, sym="AAPL", exp=_EXP, mult=100, tc="AAPL", qty=1,
    account=SCOPE, und_sec="STK",
):
    return Order(
        id="cand",
        account=account,
        envelope="SELL",
        qty=qty,
        remaining=qty,
        legs=(_opt_leg("BUY", strike=strike, sym=sym, exp=exp, mult=mult, tc=tc, und_sec=und_sec),),
    )


def test_sell_envelope_flips_leg_actions():
    # A SELL envelope on a BUY leg creates effective short-call demand.
    d = evaluate(
        [],
        [],
        Order(id="c", account=SCOPE, envelope="SELL", qty=1, remaining=1, legs=(_opt_leg("BUY"),)),
        View(),
        SCOPE,
    )
    assert not d.accept
    assert any("uncovered_call_increase" in r for r in d.reasons)


def test_buy_leg_never_supplies_cover():
    # Resting BUY call does not cover a new short call.
    holdings = [Holding(account=SCOPE, sym="AAPL", sec="STK", qty=100)]
    orders = [
        Order(id="o1", account=SCOPE, envelope="BUY", qty=1, remaining=1, legs=(_opt_leg("BUY", strike=200),))
    ]
    d = evaluate(holdings, orders, _sell_call_cand(), View(), SCOPE)
    assert d.accept  # 100 shares cover the single new short call
    holdings = []
    d = evaluate(holdings, orders, _sell_call_cand(), View(), SCOPE)
    assert not d.accept


def test_modify_max_semantics_direct():
    # max(old_total, new_qty) - filled_total, evaluated straight through _project math.
    order = Order(
        id="o1",
        account=SCOPE,
        envelope="SELL",
        qty=4,
        remaining=2,  # filled_total = 2
        filled_after_snapshot=1,
        legs=(_opt_leg("BUY"),),
    )
    # demand = filled_after(1) + max(4, 5) - 2 = 1 + 3 = 4 short calls
    d = evaluate([Holding(account=SCOPE, sym="AAPL", sec="STK", qty=300)], [order], Modify("o1", 5), View(), SCOPE)
    assert not d.accept  # 4 calls need 400 shares
    d = evaluate([Holding(account=SCOPE, sym="AAPL", sec="STK", qty=400)], [order], Modify("o1", 5), View(), SCOPE)
    assert d.accept


def test_pending_amend_keeps_max():
    order = _sell_call_order(qty=1, remaining=1, pending_amend_qty=3)
    # demand = max(1, 3) - 0 = 3
    d = evaluate([Holding(account=SCOPE, sym="AAPL", sec="STK", qty=200)], [order], _sell_call_cand(), View(), SCOPE)
    assert not d.accept
    d = evaluate([Holding(account=SCOPE, sym="AAPL", sec="STK", qty=400)], [order], _sell_call_cand(), View(), SCOPE)
    assert d.accept


def test_modify_unknown_target_fails_closed():
    d = evaluate([], [], Modify(order_id="nope", new_qty=1), View(), SCOPE)
    assert not d.accept
    assert any("modify_target_not_found" in r for r in d.reasons)


def test_guaranteed_nets_per_key_only():
    # guaranteed BAG, SELL env: legs flip -> SELL C200 / BUY C210, net zero —
    # but only when the netting rollout flag is on.
    cand = Order(
        id="c",
        account=SCOPE,
        envelope="SELL",
        qty=1,
        remaining=1,
        guaranteed=True,
        legs=(_opt_leg("BUY", strike=200), _opt_leg("SELL", strike=210)),
    )
    d = evaluate([], [], cand, View(), SCOPE, netting_enabled=True)
    assert d.accept
    # Flag off: legs are independent even though the bag is guaranteed.
    d_off = evaluate([], [], cand, View(), SCOPE)
    assert not d_off.accept
    # Same bag without the guarantee -> one independent SELL call leg.
    cand2 = Order(**{**cand.__dict__, "guaranteed": False})
    d2 = evaluate([], [], cand2, View(), SCOPE, netting_enabled=True)
    assert not d2.accept
    # Guaranteed but not every leg is OPT -> independent legs, no netting.
    cand3 = Order(
        id="c",
        account=SCOPE,
        envelope="SELL",
        qty=1,
        remaining=1,
        guaranteed=True,
        legs=(
            _opt_leg("BUY", strike=200),
            Leg(action="SELL", sym="AAPL", sec="STK", ratio=1),
        ),
    )
    assert not evaluate([], [], cand3, View(), SCOPE, netting_enabled=True).accept


def test_guaranteed_net_long_never_supplies_cover():
    # Net long call delta inside a guaranteed bag must not cover other orders.
    holdings = []
    orders = [_sell_call_order()]  # 1 uncovered short call outstanding
    cand = Order(
        id="c",
        account=SCOPE,
        envelope="BUY",
        qty=1,
        remaining=1,
        guaranteed=True,
        legs=(_opt_leg("SELL", strike=210), _opt_leg("BUY", strike=200, ratio=2)),
    )
    # Bag nets +1 long call at the key: the candidate adds no demand, but the
    # net long is discarded rather than lent to o1's existing uncovered short.
    d = evaluate(holdings, orders, cand, View(), SCOPE, netting_enabled=True)
    assert d.accept
    assert d.uncovered[_KEY] == 1
    # A net-short bag still rejects: net -1 call delta at the key.
    cand_short = Order(
        id="c",
        account=SCOPE,
        envelope="BUY",
        qty=1,
        remaining=1,
        guaranteed=True,
        legs=(_opt_leg("BUY", strike=200), _opt_leg("SELL", strike=210, ratio=2)),
    )
    assert not evaluate(holdings, orders, cand_short, View(), SCOPE, netting_enabled=True).accept


def test_stock_cover_requires_standard_deliverable():
    holdings = [Holding(account=SCOPE, sym="AAPL", sec="STK", qty=100)]
    # standard: und_sec STK, mult 100, tc == sym -> shares cover
    assert evaluate(holdings, [], _sell_call_cand(), View(), SCOPE).accept
    # mini: mult 10 -> no share cover
    assert not evaluate(holdings, [], _sell_call_cand(mult=10), View(), SCOPE).accept
    # adjusted: tc != sym -> no share cover
    assert not evaluate(holdings, [], _sell_call_cand(tc="AAPL1"), View(), SCOPE).accept
    # index underlying (e.g. SPX): shares never cover
    assert not evaluate(
        [Holding(account=SCOPE, sym="SPX", sec="STK", qty=100)], [],
        _sell_call_cand(sym="SPX", tc="SPX", und_sec="IND"), View(), SCOPE,
    ).accept
    # missing und_sec -> shares never cover that call
    assert not evaluate(holdings, [], _sell_call_cand(und_sec=None), View(), SCOPE).accept
    # ...but long-call cover for the same key is unaffected by und_sec.
    long_call = _call_holding(1)
    assert evaluate(
        [long_call], [],
        _sell_call_cand(strike=200, und_sec=None), View(), SCOPE,
    ).accept


def test_order_quantity_validation_rejects():
    def cand(**kw):
        base = dict(
            id="c", account=SCOPE, envelope="SELL", qty=1, remaining=1,
            legs=(_opt_leg("BUY"),),
        )
        base.update(kw)
        return Order(**base)

    good = [Holding(account=SCOPE, sym="AAPL", sec="STK", qty=200)]
    assert evaluate(good, [], cand(), View(), SCOPE).accept
    for kw in (
        {"qty": 0}, {"qty": True}, {"qty": 1.5},
        {"remaining": -1}, {"remaining": True}, {"remaining": 0.5},
        {"filled_after_snapshot": -1}, {"filled_after_snapshot": True},
        {"filled_after_snapshot": 1, "remaining": 1},  # sum > qty
        {"envelope": "HOLD"}, {"envelope": ""},
        {"pending_amend_qty": 0}, {"pending_amend_qty": True},
    ):
        d = evaluate(good, [], cand(**kw), View(), SCOPE)
        assert not d.accept, f"{kw} should reject"
        assert d.reasons, f"{kw} rejected without a reason"
    # An invalid existing order also rejects (strict gate path).
    bad_order = cand(id="o1", qty=-3)
    assert not evaluate(good, [bad_order], _sell_call_cand(), View(), SCOPE).accept
    # Modify candidates validate new_qty (int >= 1).
    order = _sell_call_order(qty=2, remaining=2)
    for new_qty in (0, -1, True, 2.5):
        assert not evaluate(good, [order], Modify("o1", new_qty), View(), SCOPE).accept
    assert evaluate(good, [order], Modify("o1", 2), View(), SCOPE).accept


def test_holding_qty_must_be_int():
    # bool / float / NaN / inf / string supply must not reach cover arithmetic.
    cand = _sell_call_cand()
    for bad_qty in (True, 1.5, float("nan"), float("inf"), -float("inf"), "100"):
        d = evaluate(
            [Holding(account=SCOPE, sym="AAPL", sec="STK", qty=bad_qty)],
            [], cand, View(), SCOPE,
        )
        assert not d.accept, f"holding qty {bad_qty!r} should reject"
        assert d.reasons
    # NaN/float long-call supply cannot silently cover either.
    for bad_qty in (float("nan"), 2.5, False):
        d = evaluate(
            [_call_holding(bad_qty)],
            [], _sell_call_cand(strike=200), View(), SCOPE,
        )
        assert not d.accept, f"OPT holding qty {bad_qty!r} should reject"


def test_holding_account_scoping():
    cand = _sell_call_cand()
    # Missing account on a holding -> incomplete view -> reject.
    no_acct = Holding(sym="AAPL", sec="STK", qty=200, account=None)
    d = evaluate([no_acct], [], cand, View(), SCOPE)
    assert not d.accept
    assert any("holding_missing_account" in r for r in d.reasons)
    # Foreign-account supply is ignored — shares there never cover.
    foreign = Holding(sym="AAPL", sec="STK", qty=200, account="OTHER")
    d = evaluate([foreign], [], cand, View(), SCOPE)
    assert not d.accept
    assert any("uncovered_call_increase" in r for r in d.reasons)
    assert not any("holding_missing_account" in r for r in d.reasons)


def test_uncovered_held_spx_call_not_share_covered():
    # A held SPX short call (und_sec IND) is never share-covered: the SPX
    # "shares" cannot be delivered. Direct unit check on the audit helper.
    holdings = [
        Holding(account=SCOPE, sym="SPX", sec="STK", qty=100),
        _call_holding(-1, sym="SPX", tc="SPX", und_sec="IND"),
    ]
    unc = uncovered(holdings, [])
    assert unc[("SPX", "SPX", _EXP, 100)] == 1
    # Same held call with und_sec STK -> the 100 shares do cover it.
    holdings[1] = _call_holding(-1, sym="SPX", tc="SPX", und_sec="STK")
    assert uncovered(holdings, []) == {}


def test_long_call_first_then_shares_assignment():
    # long call covers the same-key short so shares remain for the new key.
    holdings = [
        Holding(account=SCOPE, sym="AAPL", sec="STK", qty=100),
        _call_holding(1, strike=200),
        _call_holding(-1, strike=210),
    ]
    d = evaluate(holdings, [], _sell_call_cand(strike=220, exp="20270115"), View(), SCOPE)
    assert d.accept


def test_uncovered_and_short_stock_helpers():
    holdings = [Holding(account=SCOPE, sym="AAPL", sec="STK", qty=100)]
    orders = [_sell_call_order(qty=3, remaining=3)]
    unc = uncovered(holdings, orders)
    assert unc[_KEY] == 2
    ss = short_stock(holdings, orders)
    assert ss == {}
    # live SELL stock order beyond holdings -> short stock
    orders2 = [
        Order(
            id="s1",
            account=SCOPE,
            envelope="SELL",
            qty=150,
            remaining=150,
            legs=(Leg(action="BUY", sym="AAPL", sec="STK", ratio=1),),
        )
    ]
    assert short_stock(holdings, orders2)["AAPL"] == 50
    # lenient: an unresolvable order is skipped, not raised
    orders3 = [Order(id="u1", account=SCOPE, envelope="BUY", qty=1, legs=None)]
    assert uncovered(holdings, orders3) == {}


def test_uncovered_helper_with_candidate():
    cand = _sell_call_cand()
    assert uncovered([], [], cand)[_KEY] == 1
    assert uncovered([_call_holding(1)], [], cand) == {}


def test_state_irrelevant_to_demand():
    # Demand comes from filled_after + remaining regardless of order state.
    for state in ("FILLED", "CANCELLED", "UNKNOWN", "WORKING", "PENDING"):
        o = _sell_call_order(state=state, qty=1, remaining=0, filled_after=1)
        assert uncovered([], [o])[_KEY] == 1


def test_candidate_account_must_match_scope():
    cand = _sell_call_cand(account="U2")
    d = evaluate([Holding(account=SCOPE, sym="AAPL", sec="STK", qty=100)], [], cand, View(), SCOPE)
    assert not d.accept
    assert any("candidate_account_mismatch" in r for r in d.reasons)


def test_empty_legs_fail_closed():
    cand = Order(id="c", account=SCOPE, envelope="SELL", qty=1, remaining=1, legs=())
    d = evaluate([], [], cand, View(), SCOPE)
    assert not d.accept


def test_ratio_multiplies_demand():
    cand = Order(
        id="c",
        account=SCOPE,
        envelope="SELL",
        qty=1,
        remaining=1,
        legs=(_opt_leg("BUY", ratio=2),),
    )
    d = evaluate([Holding(account=SCOPE, sym="AAPL", sec="STK", qty=100)], [], cand, View(), SCOPE)
    assert not d.accept  # 2 contracts vs 100 shares
    assert d.uncovered[_KEY] == 1


def test_view_defaults_and_flags():
    cand = _sell_call_cand()
    good = evaluate([Holding(account=SCOPE, sym="AAPL", sec="STK", qty=100)], [], cand, View(), SCOPE)
    assert good.accept
    assert not evaluate([], [], cand, View(complete=False), SCOPE).accept
    assert not evaluate([], [], cand, View(stale=True), SCOPE).accept


# ---------------------------------------------------------------------------
# orders_store.reserve_attempt — durable legs + account-scoped idempotency
# ---------------------------------------------------------------------------

_SCOPE = dict(broker="IB", account_env="paper", broker_account="DU_TEST")


@pytest.fixture(scope="module")
def _throwaway_pg_db():
    """Point the requesting DB test at a throwaway DB on the test Postgres.

    Lead directive: never touch the remote shared core_test — migrations and
    write tests run only against `xe_a2_mig_<rand>` created on the server
    from DATABASE_URL_TEST, migrated to head, and dropped at teardown.
    Module-scoped and opt-in: pure coverage cases never touch PG and run
    unconditionally. Only the requesting tests skip when provisioning fails.

    Admin connection = the configured DATABASE_URL_TEST role on database
    `postgres`, rebuilt via render_as_string(hide_password=False) so the
    configured password is carried (str() must not mask it). CI's postgres
    service user (POSTGRES_USER xenon_app) is the bootstrap superuser, so
    CREATE DATABASE works there; the remote app role has no CREATEDB on a
    local instance, where this skips with an explicit reason.

    The created name already carries the xdist worker suffix
    (`xe_a2_mig_<rand>_gw0`) when PYTEST_XDIST_WORKER is set — each worker
    provisions its own DB and `_test_db.sync_test_db_url`'s idempotent
    suffix guard then returns the env URL unchanged. Provisioning or
    connection failures are pytest.skip locally but pytest.fail under CI
    (`env CI`), so the eight migration/writer tests are a hard gate there.
    """

    def _skip_or_fail(reason: str) -> None:
        if os.environ.get("CI"):
            pytest.fail(f"throwaway test DB provisioning must succeed under CI: {reason}")
        pytest.skip(reason)

    base_env = os.environ.get("DATABASE_URL_TEST")
    if not base_env:
        _skip_or_fail("DATABASE_URL_TEST not set — no throwaway test Postgres")
    try:
        base = make_url(base_env)
        # Unmasked render so the configured password survives the round-trip.
        admin = make_url(base.render_as_string(hide_password=False)).set(
            drivername="postgresql+psycopg", database="postgres"
        )
        admin_engine = create_engine(
            admin, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3}
        )
    except Exception:
        _skip_or_fail("no usable Postgres admin URL derivable from DATABASE_URL_TEST")
    dbname = f"xe_a2_mig_{uuid.uuid4().hex[:10]}"
    # Mirror _test_db._resolve_worker_id so the env URL's dbname already ends
    # with the suffix sync_test_db_url would append (idempotent-guard hit).
    wid = os.environ.get("PYTEST_XDIST_WORKER")
    if wid and wid != "master":
        dbname = f"{dbname}_{wid}"
    try:
        with admin_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            # Same role creates the throwaway — it then owns it, so
            # schema creation and `alembic upgrade head` run as configured.
            conn.execute(text(f'CREATE DATABASE "{dbname}"'))
        test_async = base.set(database=dbname)
        boot = create_engine(
            test_async.set(drivername="postgresql+psycopg"),
            isolation_level="AUTOCOMMIT",
            connect_args={"connect_timeout": 3},
        )
        with boot.connect() as conn:
            conn.execute(text("CREATE SCHEMA IF NOT EXISTS xenon"))
            conn.execute(text("CREATE SCHEMA IF NOT EXISTS events"))
        boot.dispose()
    except Exception as e:
        admin_engine.dispose()
        _skip_or_fail(f"throwaway DB provisioning failed (CREATE DATABASE or schema init): {e}")

    old_db, old_test = os.environ.get("DATABASE_URL"), os.environ.get("DATABASE_URL_TEST")
    test_dsn = test_async.render_as_string(hide_password=False)
    os.environ["DATABASE_URL"] = test_dsn
    os.environ["DATABASE_URL_TEST"] = test_dsn
    if _test_db._SESSION_ENGINE is not None:
        _test_db._SESSION_ENGINE.dispose()
    _test_db._SESSION_ENGINE = None
    _test_db._PG_REACHABLE = None
    try:
        from alembic import command

        cfg = _alembic_cfg()
        command.upgrade(cfg, "head")
        yield str(test_async)
    finally:
        if _test_db._SESSION_ENGINE is not None:
            _test_db._SESSION_ENGINE.dispose()
            _test_db._SESSION_ENGINE = None
        _test_db._PG_REACHABLE = None
        if old_db is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old_db
        if old_test is None:
            os.environ.pop("DATABASE_URL_TEST", None)
        else:
            os.environ["DATABASE_URL_TEST"] = old_test
        with admin_engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid()"
                ),
                {"d": dbname},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}"'))
        admin_engine.dispose()


def _req(**over) -> RequestRow:
    base = dict(
        ticker="SPY",
        security_type="OPT",
        action="SELL",
        quantity=1,
        expiry="20261218",
        strike=Decimal("200"),
        right="C",
        multiplier=100,
        con_id=111,
        limit_price=Decimal("1.25"),
    )
    base.update(over)
    return RequestRow(**base)


_LEGS = [
    {
        "action": "SELL",
        "sym": "SPY",
        "sec": "OPT",
        "ratio": 1,
        "right": "C",
        "exp": "20261218",
        "mult": 100,
        "tc": "SPY",
        "con_id": 111,
        "guaranteed": False,
    }
]


def _fetch_submission(submission_id):
    from xenon.db.engine import get_sync_engine

    with get_sync_engine().connect() as conn:
        return conn.execute(
            select(order_submissions).where(order_submissions.c.submission_id == submission_id)
        ).first()


def test_reserve_attempt_stores_legs(_throwaway_pg_db):
    out = reserve_attempt("u1", "att-legs-1", _req(), legs=_LEGS, **_SCOPE)
    assert out.status == "winner"
    row = _fetch_submission(out.submission_id)
    assert row.legs == _LEGS


def test_reserve_attempt_without_legs_still_works(_throwaway_pg_db):
    out = reserve_attempt("u1", "att-nolegs-1", _req(), **_SCOPE)
    assert out.status == "winner"
    assert _fetch_submission(out.submission_id).legs is None


def test_reserve_attempt_same_body_is_duplicate_across_identities(_throwaway_pg_db):
    first = reserve_attempt("u1", "att-dup-1", _req(), legs=_LEGS, **_SCOPE)
    # Different caller identity, same attempt id + same body -> duplicate,
    # not a second row (idempotency is account-scoped, not identity-scoped).
    second = reserve_attempt("u2", "att-dup-1", _req(), legs=_LEGS, **_SCOPE)
    assert first.status == "winner"
    assert second.status == "duplicate"
    assert second.submission_id == first.submission_id


def test_reserve_attempt_body_mismatch_same_key(_throwaway_pg_db):
    reserve_attempt("u1", "att-mm-1", _req(), legs=_LEGS, **_SCOPE)
    # Same key, different request body -> distinct outcome for 409 mapping.
    changed = reserve_attempt("u2", "att-mm-1", _req(quantity=2), legs=_LEGS, **_SCOPE)
    assert changed.status == "body_mismatch"
    assert changed.reason_code == ATTEMPT_BODY_MISMATCH
    # A legs-only difference is still a different body.
    legs_changed = [dict(_LEGS[0], guaranteed=True)]
    changed2 = reserve_attempt("u1", "att-mm-1", _req(), legs=legs_changed, **_SCOPE)
    assert changed2.status == "body_mismatch"
    # Same body again -> idempotent duplicate (not mismatch).
    same = reserve_attempt("u1", "att-mm-1", _req(), legs=_LEGS, **_SCOPE)
    assert same.status == "duplicate"


def test_reserve_attempt_scope_isolation(_throwaway_pg_db):
    # Same attempt id under a different account scope is an independent key.
    first = reserve_attempt("u1", "att-scope-1", _req(), **_SCOPE)
    second = reserve_attempt("u1", "att-scope-1", _req(), **{**_SCOPE, "broker_account": "DU_OTHER"})
    assert first.status == "winner"
    assert second.status == "winner"
    assert second.submission_id != first.submission_id


def test_previous_release_writer_sql_still_succeeds(_throwaway_pg_db):
    """The previous release's actual reserve_attempt statement — old insert
    shape (no `legs`) with ON CONFLICT ON CONSTRAINT uq_order_sub_user_attempt
    DO NOTHING — keeps working on the expanded schema (both constraints live).
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from xenon.db.engine import get_sync_engine

    def old_reserve(conn, submission_id, user_id="u1", attempt="att-old-1"):
        stmt = pg_insert(order_submissions).values(
            submission_id=submission_id,
            user_id=user_id,
            client_attempt_id=attempt,
            ticker="SPY",
            security_type="STK",
            action="SELL",
            quantity=100,
            expiry=None,
            strike=None,
            right=None,
            multiplier=100,
            con_id=756733,
            limit_price=Decimal("500.15"),
            state="PENDING",
            submitted_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            broker="IB",
            account_env="paper",
            broker_account="DU_TEST",
        )
        stmt = stmt.on_conflict_do_nothing(constraint="uq_order_sub_user_attempt")
        stmt = stmt.returning(order_submissions.c.submission_id)
        return conn.execute(stmt).first()

    with get_sync_engine().begin() as conn:
        # Fresh attempt inserts (legs stays NULL).
        assert old_reserve(conn, "old-w-1")[0] == "old-w-1"
        # Same (scope, user, attempt): old key's ON CONFLICT swallows it —
        # the existing row wins, no second submission.
        assert old_reserve(conn, "old-w-2") is None
    row = _fetch_submission("old-w-1")
    assert row is not None and row.legs is None
    assert _fetch_submission("old-w-2") is None


def test_previous_release_writer_cross_identity_insert_blocked(_throwaway_pg_db):
    """Expand-state compatibility: the old writer's key includes user_id, so
    a same-(scope, attempt) retry under a DIFFERENT identity misses the old
    constraint and collides with the new scope key instead — an error, not a
    silent duplicate insert (the new key still protects cross-identity
    replay while the old writer is deployed).
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.exc import IntegrityError

    from xenon.db.engine import get_sync_engine

    def old_reserve(conn, submission_id, user_id):
        stmt = pg_insert(order_submissions).values(
            submission_id=submission_id,
            user_id=user_id,
            client_attempt_id="att-xid-1",
            ticker="SPY",
            security_type="STK",
            action="SELL",
            quantity=100,
            state="PENDING",
            submitted_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            broker="IB",
            account_env="paper",
            broker_account="DU_TEST",
        )
        stmt = stmt.on_conflict_do_nothing(constraint="uq_order_sub_user_attempt")
        stmt = stmt.returning(order_submissions.c.submission_id)
        return conn.execute(stmt).first()

    with get_sync_engine().begin() as conn:
        assert old_reserve(conn, "xid-w-1", "u1") is not None
        with pytest.raises(IntegrityError):
            with conn.begin_nested():
                old_reserve(conn, "xid-w-2", "u2")


# ---------------------------------------------------------------------------
# Alembic migration — collision guard + upgrade/downgrade round-trip
# ---------------------------------------------------------------------------

_MIGRATION_REV = "2026_10_16_scope_attempt_legs"
_MIGRATION_PARENT = "2026_06_22_positions_currency"


def _alembic_cfg():
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    cfg = Config()
    cfg.set_main_option("script_location", str(root / "src" / "xenon" / "db" / "migrations"))
    return cfg


def _sub_cols(engine):
    with engine.connect() as c:
        return {
            r[0]
            for r in c.execute(
                text(
                    "select column_name from information_schema.columns "
                    "where table_schema='xenon' and table_name='order_submissions'"
                )
            )
        }


def _sub_unique(engine):
    with engine.connect() as c:
        return {
            r[0]
            for r in c.execute(
                text(
                    "select c.conname from pg_constraint c "
                    "join pg_class t on t.oid=c.conrelid "
                    "join pg_namespace n on n.oid=t.relnamespace "
                    "where n.nspname='xenon' and t.relname='order_submissions' "
                    "and c.contype='u'"
                )
            )
        }


def _insert_collision(engine):
    """Two rows, same (scope, attempt), different user_id — legal under the
    old key, a collision under the new one."""
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        for i, uid in enumerate(("u1", "u2")):
            conn.execute(
                insert(order_submissions).values(
                    submission_id=f"collide-{i}",
                    user_id=uid,
                    client_attempt_id="att-collide",
                    ticker="SPY",
                    security_type="STK",
                    action="SELL",
                    quantity=1,
                    state="PENDING",
                    submitted_at=now,
                    updated_at=now,
                    broker="IB",
                    account_env="paper",
                    broker_account="DU_TEST",
                )
            )


def _delete_collision(engine):
    with engine.begin() as conn:
        conn.execute(
            text(
                "delete from xenon.order_submissions "
                "where submission_id in ('collide-0','collide-1')"
            )
        )


def test_migration_upgrade_downgrade_upgrade(_throwaway_pg_db, pg_test_engine, monkeypatch):
    """Collision check raises loudly, then upgrade/downgrade/upgrade round-trips.

    EXPAND-only migration: upgrade keeps uq_order_sub_user_attempt and adds
    uq_order_sub_scope_attempt + legs alongside; downgrade removes only what
    the upgrade added (the old constraint is untouched throughout). Runs
    against the throwaway local DB (already at head via the fixture); rows
    are inserted/removed explicitly.
    """
    pytest.importorskip("alembic")
    from alembic import command

    from xenon._test_db import async_test_db_url

    monkeypatch.setenv("DATABASE_URL", async_test_db_url())
    cfg = _alembic_cfg()
    engine = pg_test_engine
    try:
        # Fixture upgraded to head: expand state — BOTH unique constraints
        # exist alongside, and legs was added.
        assert "legs" in _sub_cols(engine)
        assert _sub_unique(engine) >= {
            "uq_order_sub_user_attempt",
            "uq_order_sub_scope_attempt",
        }

        command.downgrade(cfg, _MIGRATION_PARENT)
        # Downgrade drops only what the upgrade added; the old key remains.
        assert "legs" not in _sub_cols(engine)
        assert "uq_order_sub_user_attempt" in _sub_unique(engine)
        assert "uq_order_sub_scope_attempt" not in _sub_unique(engine)

        # The old key permits same (scope, attempt) across identities — the
        # collision the new key would violate.
        _insert_collision(engine)
        with pytest.raises(Exception, match="collid|scope-attempt|more than one row"):
            command.upgrade(cfg, _MIGRATION_REV)
        _delete_collision(engine)

        command.upgrade(cfg, _MIGRATION_REV)
        assert "legs" in _sub_cols(engine)
        assert _sub_unique(engine) >= {
            "uq_order_sub_user_attempt",
            "uq_order_sub_scope_attempt",
        }
    finally:
        _delete_collision(engine)
        try:
            command.upgrade(cfg, "head")
        except Exception:
            pass
