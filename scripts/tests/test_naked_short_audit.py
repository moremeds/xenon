"""Naked-short audit — shadow mode (spec §2.4 Phase A, PR-06).

The audit is an adapter over `xenon.execution.coverage`: IB open orders
(the `open_orders_from_trades` shape) -> working-order legs -> coverage
over the in-process holdings of the sync pass. It only logs violations and
records `service_health("naked_short_audit")`; no cancel path exists.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

import xenon.execution as execution_pkg
from xenon.execution import naked_short_audit as nsa

ACCT = "U1"
FOREIGN = "U2"
EXP = "2026-12-18"


# ---------------------------------------------------------------------------
# Builders — payload legs as ib_sync stamps them, orders as ib_orders shapes them
# ---------------------------------------------------------------------------


def _stock_pos(sym="AAPL", shares=100, account=ACCT):
    return {
        "ticker": sym,
        "expiry": "N/A",
        "legs": [
            {
                "type": "Stock",
                "direction": "LONG" if shares >= 0 else "SHORT",
                "contracts": abs(shares),
                "account": account,
            }
        ],
    }


def _call_pos(sym="AAPL", contracts=1, direction="LONG", account=ACCT, tc=None, mult=100, und_sec="STK"):
    return {
        "ticker": sym,
        "expiry": EXP,
        "legs": [
            {
                "type": "Call",
                "direction": direction,
                "contracts": contracts,
                "account": account,
                "tc": tc or sym,
                "mult": mult,
                "und_sec": und_sec,
            }
        ],
    }


def _obs(*, account=ACCT, complete=True, age_s=5.0):
    as_of = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    return {"as_of": as_of.isoformat(), "complete": complete, "account": account, "reads": {}}


def _payload(*positions, observation=None):
    return {"positions": list(positions), "observation": _obs() if observation is None else observation}


def _opt_contract(sym="AAPL", right="C", strike=200.0, con_id=111, tc=None, mult=100, und_sec="STK", meta=True):
    c = {"conId": con_id, "symbol": sym, "secType": "OPT", "strike": strike, "right": right, "expiry": EXP}
    if meta:
        c.update({"tradingClass": tc or sym, "multiplier": mult, "underSecType": und_sec})
    return c


def _order(
    contract,
    *,
    action="SELL",
    qty=1,
    order_id=1,
    perm_id=1001,
    client_id=0,
    account=ACCT,
    status="Submitted",
    filled=0,
    non_guaranteed=None,
    order_ref=None,
):
    return {
        "orderId": order_id,
        "permId": perm_id,
        "clientId": client_id,
        "symbol": contract.get("symbol"),
        "contract": contract,
        "action": action,
        "orderType": "LMT",
        "totalQuantity": float(qty),
        "status": status,
        "account": account,
        "nonGuaranteed": non_guaranteed,
        "filled": float(filled),
        "remaining": float(qty - filled),
        "orderRef": order_ref,
    }


def _stk_contract(sym="AAPL"):
    return {"conId": 265598, "symbol": sym, "secType": "STK", "strike": 0.0, "right": None, "expiry": None}


def _bag(legs, sym="AAPL"):
    return {
        "conId": 28812380,
        "symbol": sym,
        "secType": "BAG",
        "strike": 0.0,
        "right": None,
        "expiry": None,
        "comboLegs": legs,
    }


def _bag_leg(action, right="C", strike=200.0, con_id=111, sym="AAPL", ratio=1, meta=True):
    leg = {
        "conId": con_id,
        "ratio": ratio,
        "action": action,
        "symbol": sym,
        "strike": strike,
        "right": right,
        "expiry": EXP,
        "secType": "OPT",
    }
    if meta:
        leg.update({"tradingClass": sym, "multiplier": 100, "underSecType": "STK"})
    return leg


def _find(payload, orders, *, scope=ACCT):
    return nsa.find_violations(payload, orders, scope, observation=payload.get("observation"))


def _run(payload, orders, *, scope=ACCT):
    return nsa.run_shadow_audit(payload, orders, scope, observation=payload.get("observation"))


def _perm_ids(result):
    return sorted(v["perm_id"] for v in result.violations)


# ---------------------------------------------------------------------------
# Policy over coverage
# ---------------------------------------------------------------------------


def test_naked_short_call_is_a_violation():
    r = _find(_payload(), [_order(_opt_contract())])
    assert r.status == "violations"
    assert _perm_ids(r) == [1001]
    v = r.violations[0]
    assert (v["client_id"], v["order_id"], v["perm_id"], v["account"]) == (0, 1, 1001, ACCT)
    assert v["symbol"] == "AAPL"
    assert v["uncovered_calls"] == {"AAPL|AAPL|20261218|100": 1}


def test_short_call_covered_by_shares_is_clean():
    r = _find(_payload(_stock_pos(shares=100)), [_order(_opt_contract())])
    assert r.status == "ok" and r.violations == []


def test_short_calls_beyond_share_cover_flag_every_contributor():
    orders = [
        _order(_opt_contract(), perm_id=1001),
        _order(_opt_contract(strike=210.0, con_id=112), perm_id=1002, order_id=2),
    ]
    r = _find(_payload(_stock_pos(shares=100)), orders)
    assert _perm_ids(r) == [1001, 1002]


def test_short_call_covered_by_long_call_same_key_is_clean():
    r = _find(_payload(_call_pos(contracts=1)), [_order(_opt_contract(strike=210.0))])
    assert r.violations == []


def test_cash_secured_put_is_clean():
    r = _find(_payload(), [_order(_opt_contract(right="P"))])
    assert r.violations == []


def test_buy_orders_never_violate_and_never_supply_cover():
    orders = [
        _order(_stk_contract(), action="BUY", qty=100, perm_id=2001, order_id=2),
        _order(_opt_contract(), perm_id=1001),
    ]
    r = _find(_payload(), orders)
    # The resting BUY of shares is not cover (coverage: holdings only).
    assert _perm_ids(r) == [1001]


def test_sell_stock_beyond_holdings_is_short_stock():
    r = _find(_payload(_stock_pos(shares=50)), [_order(_stk_contract(), qty=100)])
    assert _perm_ids(r) == [1001]
    assert r.violations[0]["short_stock"] == {"AAPL": 50}
    assert _find(_payload(_stock_pos(shares=100)), [_order(_stk_contract(), qty=100)]).violations == []


def test_bag_short_risk_reversal_is_a_violation():
    bag = _bag([_bag_leg("SELL", "C", 210.0, 112), _bag_leg("BUY", "P", 190.0, 113)])
    r = _find(_payload(), [_order(bag, action="BUY")])
    assert _perm_ids(r) == [1001]


def test_bag_sell_envelope_flips_legs():
    # SELL envelope of (BUY call) = selling a call -> naked without cover.
    bag = _bag([_bag_leg("BUY", "C", 200.0, 111), _bag_leg("BUY", "P", 190.0, 113)])
    assert _perm_ids(_find(_payload(), [_order(bag, action="SELL")])) == [1001]
    assert _find(_payload(), [_order(bag, action="BUY")]).violations == []


def test_terminal_orders_are_not_live():
    for status in ("Cancelled", "ApiCancelled", "Filled", "Inactive"):
        assert _find(_payload(), [_order(_opt_contract(), status=status)]).violations == []


def test_partially_filled_order_counts_only_remaining():
    # 2 of 3 filled (now in holdings as -2 calls covered by 200 shares).
    payload = _payload(_stock_pos(shares=300), _call_pos(contracts=2, direction="SHORT"))
    r = _find(payload, [_order(_opt_contract(), qty=3, filled=2)])
    assert r.violations == []


def test_wizard_tagged_orders_are_evaluated_like_others():
    r = _find(_payload(), [_order(_opt_contract(), order_ref="leg_wizard:abc")])
    assert _perm_ids(r) == [1001]


# ---------------------------------------------------------------------------
# Scope (Fable N1) and skip-and-log cases
# ---------------------------------------------------------------------------


def test_foreign_account_holdings_never_cover():
    r = _find(_payload(_stock_pos(shares=100, account=FOREIGN)), [_order(_opt_contract())])
    assert _perm_ids(r) == [1001]


def test_foreign_account_orders_are_not_audited():
    r = _find(_payload(), [_order(_opt_contract(), account=FOREIGN)])
    assert r.status == "ok" and r.violations == []


def test_order_without_account_skips_its_symbol():
    orders = [
        _order(_opt_contract(), account=None, perm_id=1001),
        _order(_opt_contract(con_id=112), perm_id=1002, order_id=2),
        _order(_opt_contract(sym="MSFT", con_id=300), perm_id=3001, order_id=3),
    ]
    r = _find(_payload(), orders)
    assert _perm_ids(r) == [3001]
    assert {"symbol": "AAPL", "reason": "order_missing_account"}.items() <= r.skipped[0].items()


def test_unresolved_bag_skips_its_symbol():
    bag = _bag([])  # comboLegs never resolved
    orders = [_order(bag, action="BUY", perm_id=1001), _order(_opt_contract(), perm_id=1002, order_id=2)]
    r = _find(_payload(), orders)
    assert r.violations == []
    assert r.skipped and r.skipped[0]["reason"].startswith("unresolved")


def test_unknown_option_metadata_skips_its_symbol():
    r = _find(_payload(), [_order(_opt_contract(meta=False))])
    assert r.violations == []
    assert r.skipped[0]["symbol"] == "AAPL"


def test_unsupported_instrument_skips_its_symbol():
    fut = {"conId": 5, "symbol": "ES", "secType": "FUT", "strike": 0.0, "right": None, "expiry": "2026-12-18"}
    r = _find(_payload(), [_order(fut)])
    assert r.violations == [] and r.skipped[0]["symbol"] == "ES"


def test_holding_with_unknown_metadata_skips_its_symbol():
    # A long call we cannot classify must not turn into a false violation.
    payload = _payload(_call_pos(mult=None))
    r = _find(payload, [_order(_opt_contract(strike=210.0))])
    assert r.violations == []
    assert r.skipped[0]["symbol"] == "AAPL"


def test_fractional_quantity_skips_its_symbol():
    o = _order(_stk_contract(), qty=1)
    o["totalQuantity"] = 0.5
    r = _find(_payload(), [o])
    assert r.violations == [] and r.skipped[0]["reason"] == "invalid_quantity"


# Astra P2-1: the snapshot itself must be complete, of this scope, and fresh.
BAD_OBSERVATIONS = {
    "observation_missing": {},
    "observation_incomplete": {"complete": False},
    "observation_account_mismatch": {"account": FOREIGN},
    "observation_stale": {"age_s": nsa.AUDIT_MAX_AGE_S + 60},
    "observation_as_of_invalid": {"as_of": "not-a-time"},
}


def _bad_obs(reason):
    if reason == "observation_missing":
        return None
    kw = dict(BAD_OBSERVATIONS[reason])
    as_of = kw.pop("as_of", None)
    obs = _obs(**kw)
    if as_of is not None:
        obs["as_of"] = as_of
    return obs


@pytest.mark.parametrize("reason", sorted(BAD_OBSERVATIONS))
def test_bad_snapshot_observation_skips_whole_audit(reason, monkeypatch):
    payload = {"positions": [], "observation": _bad_obs(reason)}
    r = _find(payload, [_order(_opt_contract())])
    assert (r.status, r.reason, r.violations) == ("skipped", reason, [])

    calls = []
    monkeypatch.setattr(nsa, "record_service_health", lambda *a, **k: calls.append((a, k)))
    r = _run(payload, [_order(_opt_contract())])
    assert (r.status, r.violations) == ("skipped", [])
    assert calls[0][0] == ("naked_short_audit", "skipped")
    assert json.loads(calls[0][1]["detail"])["reason"] == reason


def test_observation_complete_must_be_true_not_truthy():
    obs = _obs()
    obs["complete"] = "yes"
    r = _find({"positions": [], "observation": obs}, [_order(_opt_contract())])
    assert r.reason == "observation_incomplete"


@pytest.mark.parametrize("reason", sorted(BAD_OBSERVATIONS))
def test_cli_skips_bad_snapshot_observation(reason, tmp_path, capsys):
    pf = tmp_path / "portfolio.json"
    of = tmp_path / "orders.json"
    pf.write_text(json.dumps({"positions": [], "observation": _bad_obs(reason)}))
    of.write_text(json.dumps({"open_orders": [_order(_opt_contract())]}))
    summary = nsa.main(["--portfolio", str(pf), "--orders", str(of), "--account", ACCT])
    assert (summary["status"], summary["reason"], summary["violations"]) == ("skipped", reason, [])


@pytest.mark.parametrize("scope", ["", "  ", "legacy_unknown", "unknown"])
def test_placeholder_scope_account_skips_whole_audit(scope, monkeypatch):
    """Astra P2-3: a placeholder scope is never an account identity — the audit
    skips (and records it), it never reports violations under it."""
    stk = _stock_pos(shares=100, account=scope)
    orders = [_order(_opt_contract(), account=scope), _order(_opt_contract(con_id=112), account=scope, perm_id=1002)]
    r = _find(_payload(stk), orders, scope=scope)
    assert r.status == "skipped" and r.reason == "scope_account_invalid" and r.violations == []

    calls = []
    monkeypatch.setattr(nsa, "record_service_health", lambda *a, **k: calls.append((a, k)))
    r = _run(_payload(stk), orders, scope=scope)
    assert r.status == "skipped" and r.violations == []
    assert calls[0][0] == ("naked_short_audit", "skipped")
    assert json.loads(calls[0][1]["detail"])["reason"] == "scope_account_invalid"


# ---------------------------------------------------------------------------
# Shadow only: logging + service_health, no cancel path
# ---------------------------------------------------------------------------


def test_module_has_no_cancel_path():
    src = inspect.getsource(nsa)
    for needle in ("cancel_violations", "cancel_order", "cancelOrder", "IBClient", "acquire_owner"):
        assert needle not in src, needle


def test_run_shadow_audit_logs_and_records_health(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(nsa, "record_service_health", lambda *a, **k: calls.append((a, k)))
    with caplog.at_level("WARNING", logger="xenon.naked_short_audit"):
        r = _run(_payload(), [_order(_opt_contract())])
    assert r.status == "violations"
    assert "NAKED SHORT AUDIT (shadow)" in caplog.text
    ((args, kwargs),) = calls
    assert args == ("naked_short_audit", "violations")
    assert kwargs["broker_account"] == ACCT
    detail = json.loads(kwargs["detail"])
    assert detail["mode"] == "shadow" and detail["violations"] == 1


def test_run_shadow_audit_never_raises(monkeypatch):
    calls = []
    monkeypatch.setattr(nsa, "record_service_health", lambda *a, **k: calls.append((a, k)))

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(nsa, "find_violations", _boom)
    r = _run(_payload(), [])
    assert r.status == "error"
    assert calls[0][0] == ("naked_short_audit", "error")


def test_cli_is_offline_and_shadow_only(tmp_path, capsys):
    pf = tmp_path / "portfolio.json"
    of = tmp_path / "orders.json"
    pf.write_text(json.dumps(_payload()))
    of.write_text(json.dumps({"open_orders": [_order(_opt_contract())]}))
    summary = nsa.main(["--portfolio", str(pf), "--orders", str(of), "--account", ACCT])
    assert summary["status"] == "violations"
    assert json.loads(capsys.readouterr().out)["violations"][0]["perm_id"] == 1001


# ---------------------------------------------------------------------------
# ib_sync imports the audit as a package module (no ImportError skip)
# ---------------------------------------------------------------------------


def test_ib_sync_import_fails_loudly_when_audit_is_broken(monkeypatch):
    monkeypatch.setitem(sys.modules, "xenon.execution.naked_short_audit", None)
    monkeypatch.delattr(execution_pkg, "naked_short_audit", raising=False)
    spec = importlib.util.find_spec("xenon.execution.ib_sync")
    fresh = importlib.util.module_from_spec(spec)
    with pytest.raises(ImportError):
        spec.loader.exec_module(fresh)
