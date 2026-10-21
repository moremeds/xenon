"""PR-11: naked-short audit enforce mode behind XENON_AUDIT_CANCEL (spec §2.4 Phase B).

Two layers, fakes only (no IB, no Postgres):

- `audit_enforce.enforce` — the loop: gates, candidate choice (owner
  clientId, permId, account), newest first, one cancel then re-sync, bound.
- server glue — `_sync_via_subprocess` reads the audit from the ib_sync
  ScriptResult `data` and cancels through the same core as POST
  /orders/cancel (`_cancel_order` over the order owner), marking the row
  terminal with reason_code NAKED_SHORT_AUDIT only on a confirmed cancel.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from scripts.tests.order_owner_fakes import install_cores, install_owner, orders_id
from xenon.api import server as server_mod
from xenon.api.services import audit_enforce
from xenon.api.subprocess import ScriptResult
from xenon.execution import orders_store
from xenon.api.services import order_entry

ACCT = "DU0000000"


def _v(order_id, perm_id, *, client_id=None, account=ACCT, symbol="AAPL"):
    return {
        "client_id": orders_id() if client_id is None else client_id,
        "order_id": order_id,
        "perm_id": perm_id,
        "account": account,
        "symbol": symbol,
        "action": "SELL",
        "status": "Submitted",
        "uncovered_calls": {f"{symbol}|20261120|500.0": 1},
        "short_stock": {},
    }


def _audit(*violations, status=None, account=ACCT, reason=None):
    return {
        "mode": "shadow",
        "status": status or ("violations" if violations else "ok"),
        "reason": reason,
        "account": account,
        "violations": list(violations),
        "skipped": [],
    }


class Harness:
    """Records the order of cancel / resync calls. Every cancel needs a
    confirming resync first (Fable F1), so scripted `resyncs` alternate
    confirm pass / post-cancel pass."""

    def __init__(self, resyncs=(), kinds=None, blocked=None):
        self.calls: list[tuple] = []
        self._resyncs = list(resyncs)
        self._kinds = list(kinds or [])
        self.blocked_reason = blocked

    def blocked(self):
        return self.blocked_reason

    async def cancel(self, v):
        self.calls.append(("cancel", v["order_id"], v["perm_id"]))
        kind = self._kinds.pop(0) if self._kinds else "ok"
        if isinstance(kind, BaseException):
            raise kind
        return kind

    async def resync(self):
        self.calls.append(("resync",))
        nxt = self._resyncs.pop(0) if self._resyncs else _audit()
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    def run(self, audit, **kw):
        return asyncio.run(
            audit_enforce.enforce(
                audit,
                scope_account=kw.pop("scope_account", ACCT),
                owner_client_id=orders_id(),
                blocked=self.blocked,
                cancel=self.cancel,
                resync=self.resync,
                **kw,
            )
        )


R, C11, C10 = ("resync",), ("cancel", 11, 1002), ("cancel", 10, 1001)


# -- gates: nothing is attempted -------------------------------------------


@pytest.mark.parametrize(
    "audit",
    [
        None,
        {},
        _audit(status="skipped", reason="observation_stale"),
        _audit(status="skipped", reason="observation_incomplete"),
        _audit(status="error", reason="RuntimeError: boom"),
        {**_audit(_v(10, 1001)), "status": "weird"},
    ],
)
def test_audit_not_ok_never_cancels(audit):
    h = Harness()
    out = h.run(audit)
    assert h.calls == []
    assert out.cancelled == [] and out.stopped is not None


@pytest.mark.parametrize("reason", ["read_only", "owner_halted", "owner_unavailable", "mode_unverified"])
def test_blocked_gate_never_cancels(reason):
    h = Harness(blocked=reason)
    out = h.run(_audit(_v(10, 1001)))
    assert h.calls == []
    assert out.stopped == reason


def test_ok_audit_without_violations_does_nothing():
    h = Harness()
    out = h.run(_audit())
    assert h.calls == [] and out.state == "ok" and out.stopped is None


@pytest.mark.parametrize("scope", ["", "legacy_unknown", "unknown"])
def test_placeholder_scope_never_cancels(scope):
    h = Harness()
    out = h.run(_audit(_v(10, 1001), account=scope), scope_account=scope)
    assert h.calls == [] and out.stopped == "scope_account_invalid"


def test_audit_of_another_account_never_cancels():
    h = Harness()
    out = h.run(_audit(_v(10, 1001, account="U9999999"), account="U9999999"))
    assert h.calls == [] and out.stopped == "audit_account_mismatch"


# -- confirmation pass (Fable F1) ---------------------------------------------


def test_first_cancel_needs_a_confirming_pass():
    """One pass can show a just-covered call as naked (positions are read
    seconds before open orders): a target the fresh pass does not confirm
    is dropped, never cancelled."""
    h = Harness(resyncs=[_audit()])
    out = h.run(_audit(_v(11, 1002)))
    assert h.calls == [R]
    assert out.cancelled == [] and out.state == "ok"
    assert out.skipped[-1]["reason"] == "not_confirmed"


def test_unconfirmed_target_dropped_then_next_chosen_from_confirming_audit():
    first = _audit(_v(10, 1001), _v(11, 1002))
    only_10 = _audit(_v(10, 1001))
    h = Harness(resyncs=[only_10, only_10, _audit()])
    out = h.run(first)
    assert h.calls == [R, R, C10, R]
    assert [c["perm_id"] for c in out.cancelled] == [1001]
    assert {s["perm_id"]: s["reason"] for s in out.skipped} == {1002: "not_confirmed"}


@pytest.mark.parametrize(
    "fresh",
    [
        _audit(status="skipped", reason="observation_incomplete"),
        _audit(status="error", reason="boom"),
        _audit(_v(11, 1002), account="U9999999"),
    ],
)
def test_confirming_audit_not_usable_never_cancels(fresh):
    h = Harness(resyncs=[fresh])
    out = h.run(_audit(_v(11, 1002)))
    assert h.calls == [R] and out.cancelled == []


def test_confirm_target_must_still_be_owned_with_same_identity():
    """Same orderId but a different permId in the fresh pass is not the
    same order: dropped, not cancelled."""
    h = Harness(resyncs=[_audit(_v(11, 9999)), _audit(_v(11, 9999)), _audit()])
    out = h.run(_audit(_v(11, 1002)))
    assert h.calls[:2] == [R, R]
    assert ("cancel", 11, 1002) not in h.calls
    assert out.skipped[0]["reason"] == "not_confirmed"


def test_unconfirmed_drops_are_bounded():
    """Each confirming pass shows a different order: the loop cannot spin."""
    flapping = [_audit(_v(100 + i, 5000 + i)) for i in range(20)]
    h = Harness(resyncs=flapping)
    out = h.run(_audit(_v(99, 4999)))
    assert all(c == R for c in h.calls)
    assert len(h.calls) == audit_enforce.MAX_UNCONFIRMED_PER_TRIGGER
    assert out.stopped == "confirm_bound" and out.cancelled == []


def test_confirm_resync_failure_stops_with_error():
    h = Harness(resyncs=[None])
    out = h.run(_audit(_v(11, 1002)))
    assert h.calls == [R] and out.cancelled == []
    assert out.state == "error" and out.stopped == "resync_failed"


# -- the loop ----------------------------------------------------------------


def test_two_violations_cancel_newest_then_resync_then_next():
    first = _audit(_v(10, 1001), _v(11, 1002))
    after_one = _audit(_v(10, 1001))
    h = Harness(resyncs=[first, after_one, after_one, _audit()])
    out = h.run(first)
    assert h.calls == [R, C11, R, R, C10, R]
    assert [c["perm_id"] for c in out.cancelled] == [1002, 1001]
    assert out.state == "ok" and out.stopped is None


def test_one_cancel_that_clears_the_audit_stops_after_resync():
    """Violations list every contributor; cancelling one may suffice."""
    first = _audit(_v(10, 1001), _v(11, 1002))
    h = Harness(resyncs=[first, _audit()])
    out = h.run(first)
    assert h.calls == [R, C11, R]
    assert len(out.cancelled) == 1 and out.state == "ok"


def test_order_of_another_client_is_skipped_not_attempted():
    first = _audit(_v(50, 5001, client_id=0), _v(10, 1001))
    h = Harness(resyncs=[first, _audit(_v(50, 5001, client_id=0))])
    out = h.run(first)
    assert h.calls == [R, C10, R]
    assert {"perm_id": 5001, "reason": "not_owned"}.items() <= out.skipped[0].items()
    assert out.state == "violations"  # the foreign order is still live


def test_only_foreign_orders_attempt_nothing():
    h = Harness()
    out = h.run(_audit(_v(50, 5001, client_id=0), _v(51, 5002, client_id=26)))
    assert h.calls == []
    assert [s["reason"] for s in out.skipped] == ["not_owned", "not_owned"]
    assert out.stopped == "no_eligible_order"


@pytest.mark.parametrize("perm_id", [0, None, "", "abc", -3])
def test_missing_perm_id_is_skipped_no_order_id_fallback(perm_id):
    h = Harness()
    out = h.run(_audit(_v(10, perm_id)))
    assert h.calls == []
    assert out.skipped[0]["reason"] == "perm_id_missing"


def test_violation_of_another_account_is_skipped():
    h = Harness()
    out = h.run(_audit(_v(10, 1001, account="U9999999")))
    assert h.calls == [] and out.skipped[0]["reason"] == "account_mismatch"


def test_perm_id_not_found_at_cancel_time_skips_and_stops():
    """The core never falls back to orderId; a vanished permId is logged +
    skipped and the trigger ends (state changed; the next sync recomputes)."""
    first = _audit(_v(10, 1001), _v(11, 1002))
    h = Harness(resyncs=[first], kinds=["not_found"])
    out = h.run(first)
    assert h.calls == [R, C11]
    assert out.cancelled == [] and out.skipped[-1]["reason"] == "perm_id_not_found"
    assert out.state != "error"


def test_order_no_longer_working_is_skipped_not_error():
    """Fable F2: the owner core says 'already Filled/Cancelled'."""
    first = _audit(_v(11, 1002))
    h = Harness(resyncs=[first], kinds=["gone"])
    out = h.run(first)
    assert h.calls == [R, C11]
    assert out.cancelled == [] and out.skipped[-1]["reason"] == "order_not_working"
    assert out.state != "error" and out.stopped == "order_not_working"


@pytest.mark.parametrize("kind", ["uncertain", "failed"])
def test_unconfirmed_or_failed_cancel_stops_loop_with_error(kind):
    first = _audit(_v(10, 1001), _v(11, 1002))
    h = Harness(resyncs=[first], kinds=[kind])
    out = h.run(first)
    assert h.calls == [R, C11]
    assert out.cancelled == [] and out.state == "error" and out.stopped == f"cancel_{kind}"


def test_cancel_that_raises_is_an_error_outcome_keeping_evidence():
    """Astra 2: a raising callback ends the loop inside `enforce` with an
    error Outcome; earlier cancels and skips are kept, nothing further runs."""
    first = _audit(_v(50, 5001, client_id=0), _v(10, 1001), _v(11, 1002))
    after_one = _audit(_v(50, 5001, client_id=0), _v(10, 1001))
    h = Harness(resyncs=[first, after_one, after_one], kinds=["ok", RuntimeError("db down")])
    out = h.run(first)
    assert h.calls == [R, C11, R, R, C10]
    assert [c["perm_id"] for c in out.cancelled] == [1002]
    assert out.skipped[0]["reason"] == "not_owned"
    assert out.state == "error" and out.stopped == "exception:RuntimeError"


def test_resync_that_raises_is_an_error_outcome():
    h = Harness(resyncs=[RuntimeError("spawn")])
    out = h.run(_audit(_v(11, 1002)))
    assert h.calls == [R] and out.state == "error" and out.stopped == "exception:RuntimeError"


def test_gate_rechecked_right_before_each_cancel():
    """Blocked after the confirming pass: nothing is sent."""
    first = _audit(_v(11, 1002))
    h = Harness(resyncs=[first])
    seq = iter([None, "owner_halted"])
    h.blocked = lambda: next(seq)
    out = h.run(first)
    assert h.calls == [R]
    assert out.stopped == "owner_halted" and out.cancelled == []


def test_gate_rechecked_before_the_next_target():
    first = _audit(_v(10, 1001), _v(11, 1002))
    h = Harness(resyncs=[first, _audit(_v(10, 1001))])
    seq = iter([None, None, "owner_halted"])
    h.blocked = lambda: next(seq)
    out = h.run(first)
    assert h.calls == [R, C11, R]
    assert out.stopped == "owner_halted" and len(out.cancelled) == 1


def test_resync_failure_after_cancel_stops_with_error():
    first = _audit(_v(10, 1001), _v(11, 1002))
    h = Harness(resyncs=[first, None])
    out = h.run(first)
    assert h.calls == [R, C11, R]
    assert out.state == "error" and out.stopped == "resync_failed"


class _LiveBook(Harness):
    """Every pass shows all not-yet-cancelled orders as violations."""

    def __init__(self, n):
        super().__init__()
        self.live = {1000 + i: _v(i, 1000 + i) for i in range(1, n + 1)}

    async def cancel(self, v):
        self.calls.append(("cancel", v["order_id"], v["perm_id"]))
        self.live.pop(v["perm_id"])
        return "ok"

    async def resync(self):
        self.calls.append(R)
        return _audit(*self.live.values())


def test_loop_bound_respected(caplog):
    h = _LiveBook(10)
    with caplog.at_level("WARNING", logger="xenon.audit_enforce"):
        out = h.run(_audit(*h.live.values()), max_cancels=3)
    assert [c[0] for c in h.calls].count("cancel") == 3
    assert [c[2] for c in h.calls if c[0] == "cancel"] == [1010, 1009, 1008]  # newest first
    assert out.stopped == "max_cancels" and out.state == "violations"
    assert "bound" in caplog.text


def test_default_bounds():
    assert audit_enforce.MAX_CANCELS_PER_TRIGGER == 5
    assert audit_enforce.MAX_UNCONFIRMED_PER_TRIGGER == 3


def test_health_detail_reports_enforce_mode_and_counts():
    first = _audit(_v(50, 5001, client_id=0), _v(10, 1001))
    h = Harness(resyncs=[first, _audit()])
    out = h.run(first)
    detail = json.loads(out.health_detail())
    assert detail["mode"] == "enforce"
    assert detail["cancelled"] == 1 and detail["enforce_skipped"] == 1
    assert detail["orders_cancelled"][0]["perm_id"] == 1001
    assert detail["enforce_skipped_orders"][0]["reason"] == "not_owned"


def test_health_keeps_the_audits_coverage_evidence_over_the_shadow_row():
    """Astra 1: the enforce row replaces the shadow row; it must keep the
    latest audit's own skips and held exposure, apart from enforce skips."""
    from xenon.execution import naked_short_audit as nsa

    shadow = nsa.AuditResult(
        "ok",
        skipped=[{"symbol": "XYZ", "reason": "holding_unknown_metadata:multiplier"}],
        held_uncovered={"AAPL|20261120|500.0|C": 1},
        held_short_stock={"TSLA": 50},
    )
    audit = {**shadow.summary(), "account": ACCT}
    out = Harness().run(audit)
    detail = json.loads(out.health_detail())
    shadow_detail = json.loads(nsa._health_detail(shadow))
    assert out.state == "ok"
    for key in ("skipped", "held_uncovered", "held_short_stock"):
        assert detail[key] == shadow_detail[key], key
    assert detail["audit_skipped"] == shadow.skipped
    assert detail["enforce_skipped"] == 0


def test_flag_parsing(monkeypatch):
    monkeypatch.delenv("XENON_AUDIT_CANCEL", raising=False)
    assert audit_enforce.enabled() is False
    for val in ("0", "true", "yes", ""):
        monkeypatch.setenv("XENON_AUDIT_CANCEL", val)
        assert audit_enforce.enabled() is False
    monkeypatch.setenv("XENON_AUDIT_CANCEL", "1")
    assert audit_enforce.enabled() is True


# -- server glue -------------------------------------------------------------


@pytest.fixture
def glue(monkeypatch):
    """Server with a fake owner + cancel core, a scripted ib_sync runner and
    recorded store / health writes. Flag on, writable, mode verified."""
    state = {"runs": [], "results": [], "cancels": [], "replies": [], "marks": [], "health": []}

    async def _runner(entry, args, timeout=30):
        state["runs"].append(entry)
        return state["results"].pop(0) if state["results"] else ScriptResult(ok=True, data={})

    def _core(client, order_id, perm_id, **kw):
        state["cancels"].append((order_id, perm_id, kw.get("client_id")))
        return state["replies"].pop(0) if state["replies"] else {"status": "ok", "message": "Order cancelled"}

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", _runner)
    monkeypatch.setattr(server_mod, "_sync_task", None, raising=False)
    monkeypatch.setattr(server_mod, "_is_test_mode", lambda: False)
    monkeypatch.setattr(order_entry, "_record_manage_event", lambda *a, **k: None)
    install_cores(monkeypatch, cancel=_core)
    owner, _pool = install_owner(monkeypatch)
    state["owner"] = owner
    monkeypatch.setattr(
        orders_store,
        "load_submission_for_modify",
        lambda **kw: {"submission_id": f"sub-{kw['perm_id']}", "placing_client_id": orders_id()},
    )
    monkeypatch.setattr(orders_store, "mark_terminal", lambda **kw: state["marks"].append(kw) or 1)
    monkeypatch.setattr(audit_enforce, "record_service_health", lambda *a, **kw: state["health"].append((a, kw)))
    for attr, val in (("trading_mode", "paper"), ("account", ACCT), ("broker", "IB"), ("mode_verified", True)):
        monkeypatch.setattr(server_mod.app.state, attr, val, raising=False)
    monkeypatch.setenv("XENON_AUDIT_CANCEL", "1")
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)
    return state


def _sync_result(audit):
    return ScriptResult(ok=True, data={"naked_short_audit": audit})


def _sync(state, *results):
    state["results"].extend(results)
    return asyncio.run(server_mod._sync_via_subprocess())


def test_glue_flag_off_never_cancels(glue, monkeypatch):
    monkeypatch.delenv("XENON_AUDIT_CANCEL")
    result = _sync(glue, _sync_result(_audit(_v(10, 1001))))
    assert result.ok
    assert glue["runs"] == ["xenon-ib-sync"]
    assert glue["cancels"] == [] and glue["marks"] == [] and glue["health"] == []


def test_glue_read_only_never_cancels(glue, monkeypatch):
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    _sync(glue, _sync_result(_audit(_v(10, 1001))))
    assert glue["runs"] == ["xenon-ib-sync"]
    assert glue["cancels"] == [] and glue["marks"] == []
    assert glue["health"] == []  # read-only + flag on writes no enforce row


def test_glue_owner_halted_never_cancels(glue):
    glue["owner"]._latch_halt("test")
    _sync(glue, _sync_result(_audit(_v(10, 1001))))
    assert glue["cancels"] == [] and glue["marks"] == []
    ((_a, kw),) = glue["health"]
    assert json.loads(kw["detail"])["stopped"] == "owner_halted"


def test_glue_mode_unverified_never_cancels(glue, monkeypatch):
    monkeypatch.setattr(server_mod.app.state, "mode_verified", False)
    _sync(glue, _sync_result(_audit(_v(10, 1001))))
    assert glue["cancels"] == [] and glue["marks"] == []


def test_glue_failed_sync_never_cancels(glue):
    _sync(glue, ScriptResult(ok=False, error="gateway down"))
    assert glue["cancels"] == [] and glue["health"] == []


@pytest.mark.parametrize("audit", [_audit(status="skipped", reason="observation_stale"), None])
def test_glue_audit_not_ok_never_cancels(glue, audit):
    _sync(glue, ScriptResult(ok=True, data={} if audit is None else {"naked_short_audit": audit}))
    assert glue["cancels"] == [] and glue["marks"] == []
    assert glue["runs"] == ["xenon-ib-sync"]


def test_glue_confirmed_cancel_marks_terminal_and_resyncs(glue):
    first = _sync_result(_audit(_v(10, 1001), _v(11, 1002)))
    result = _sync(glue, first, first, _sync_result(_audit()))
    assert result.ok
    assert glue["runs"] == ["xenon-ib-sync"] * 3  # pass, confirming pass, post-cancel pass
    assert glue["cancels"] == [(11, 1002, orders_id())]
    (mark,) = glue["marks"]
    assert mark["submission_id"] == "sub-1002"
    assert mark["state"] == "CANCELLED"
    assert mark["reason_code"] == "NAKED_SHORT_AUDIT"
    assert mark["expected_states"] == orders_store.NON_TERMINAL_STATES
    assert mark["filled_qty"] is None
    ((args, kw),) = glue["health"]
    assert args[0] == "naked_short_audit" and args[1] == "ok"
    assert kw["broker_account"] == ACCT
    detail = json.loads(kw["detail"])
    assert detail["mode"] == "enforce" and detail["cancelled"] == 1


def test_glue_unconfirmed_cancel_stops_marks_nothing_records_error(glue):
    glue["replies"].append(
        {
            "status": "error",
            "message": "Cancel not confirmed",
            "classification": "unconfirmed",
            "amend_outcome": "unconfirmed",
        }
    )
    first = _sync_result(_audit(_v(10, 1001), _v(11, 1002)))
    _sync(glue, first, first)
    assert glue["cancels"] == [(11, 1002, orders_id())]
    assert glue["marks"] == []
    assert glue["runs"] == ["xenon-ib-sync"] * 2  # confirming pass only; no re-sync after it
    ((args, kw),) = glue["health"]
    assert args[1] == "error" and kw["error"]


def test_glue_perm_id_not_found_skips_no_fallback(glue):
    glue["replies"].append(
        {
            "status": "error",
            "message": "Trade not found (orderId=11, permId=1002)",
            "classification": "ib_reject",
            "upstream": {"code": 10147, "message": "Order not found (local lookup)"},
        }
    )
    first = _sync_result(_audit(_v(11, 1002)))
    _sync(glue, first, first)
    assert glue["cancels"] == [(11, 1002, orders_id())]  # one call, permId pinned
    assert glue["marks"] == []
    ((_args, kw),) = glue["health"]
    assert json.loads(kw["detail"])["enforce_skipped_orders"][0]["reason"] == "perm_id_not_found"


def test_glue_foreign_client_never_reaches_core(glue):
    _sync(glue, _sync_result(_audit(_v(10, 1001, client_id=0))))
    assert glue["cancels"] == [] and glue["marks"] == []


def test_glue_enforcement_runs_inside_the_single_flight_task(glue):
    """A background trigger during enforcement joins the running task."""
    first = _sync_result(_audit(_v(11, 1002)))
    glue["results"].extend([first, first, _sync_result(_audit())])

    async def _go():
        assert await server_mod.portfolio_background_sync() == {"status": "accepted"}
        task = server_mod._sync_task
        await asyncio.sleep(0)
        assert await server_mod.portfolio_background_sync() == {"status": "in_progress"}
        await task

    asyncio.run(_go())
    assert glue["runs"] == ["xenon-ib-sync"] * 3
    assert len(glue["cancels"]) == 1


def test_glue_raising_submission_lookup_records_enforce_error(glue, monkeypatch):
    """Astra 2: the lookup inside `_cancel_order` raises → enforce error
    heartbeat, no send, nothing marked, no further cancels."""

    def _boom(**_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(orders_store, "load_submission_for_modify", _boom)
    first = _sync_result(_audit(_v(10, 1001), _v(11, 1002)))
    result = _sync(glue, first, first)
    assert result.ok
    assert glue["cancels"] == [] and glue["marks"] == []
    ((args, kw),) = glue["health"]
    assert args[1] == "error" and kw["error"]
    detail = json.loads(kw["detail"])
    assert detail["mode"] == "enforce" and detail["stopped"] == "exception:RuntimeError"


def test_glue_order_already_filled_is_skipped_not_error(glue):
    """Fable F2: the core finds the order no longer working."""
    glue["replies"].append(
        {"status": "error", "message": "Order already Filled — cannot cancel", "classification": "ib_reject"}
    )
    first = _sync_result(_audit(_v(11, 1002)))
    _sync(glue, first, first)
    assert glue["cancels"] == [(11, 1002, orders_id())]
    assert glue["marks"] == []
    ((args, kw),) = glue["health"]
    assert args[1] != "error" and kw["error"] is None
    assert json.loads(kw["detail"])["enforce_skipped_orders"][0]["reason"] == "order_not_working"


def test_glue_no_submission_row_cancels_on_owner_and_marks_nothing(glue, monkeypatch):
    """Fable F8(i): no row → owner path without a recorded clientId (the
    core's own ownership check applies); no row to mark."""
    monkeypatch.setattr(orders_store, "load_submission_for_modify", lambda **_kw: None)
    first = _sync_result(_audit(_v(11, 1002)))
    _sync(glue, first, first, _sync_result(_audit()))
    assert glue["cancels"] == [(11, 1002, None)]
    assert glue["marks"] == []


def test_glue_row_with_foreign_placing_client_is_refused_without_send(glue, monkeypatch):
    """Fable F8(ii): a row recorded with another placing clientId → 409
    ORDER_NOT_OWNED, nothing sent, nothing marked, enforce error."""
    monkeypatch.setattr(
        orders_store,
        "load_submission_for_modify",
        lambda **kw: {"submission_id": "sub-x", "placing_client_id": 26},
    )
    first = _sync_result(_audit(_v(11, 1002)))
    _sync(glue, first, first)
    assert glue["cancels"] == [] and glue["marks"] == []
    assert glue["owner"]._pool.client.sent == []
    ((args, kw),) = glue["health"]
    assert args[1] == "error" and json.loads(kw["detail"])["stopped"] == "cancel_failed"


def test_glue_health_keeps_audit_coverage_evidence(glue):
    """Astra 1 at the glue: coverage skips + held exposure survive the
    shadow→enforce overwrite."""
    audit = {
        **_audit(),
        "skipped": [{"symbol": "XYZ", "reason": "holding_unknown_metadata:multiplier"}],
        "held_uncovered": {"AAPL|20261120|500.0|C": 1},
        "held_short_stock": {},
    }
    _sync(glue, _sync_result(audit))
    ((args, kw),) = glue["health"]
    detail = json.loads(kw["detail"])
    assert args[1] == "ok"
    assert detail["skipped"] == 1 and detail["audit_skipped"][0]["symbol"] == "XYZ"
    assert detail["held_uncovered"] == {"AAPL|20261120|500.0|C": 1}
