"""PR-08 §2.6: every state writer requires `expected_states`; a transition
from an unexpected state is a logged no-op (returns 0), never a raise."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert, select

from xenon.db.engine import get_sync_engine
from xenon.db.queries import combo_wizard
from xenon.db.schema import order_submissions
from xenon.execution import orders_store
from xenon.api.services import order_entry

SCOPE = {"broker": "IB", "account_env": "paper", "broker_account": "DU_SM"}


def _seed(sid: str, state: str, *, age_s: int = 0) -> None:
    at = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    with get_sync_engine().begin() as conn:
        conn.execute(
            insert(order_submissions).values(
                submission_id=sid,
                user_id="u",
                client_attempt_id=f"att-{sid}",
                ticker="SPY",
                security_type="STK",
                action="BUY",
                quantity=1,
                multiplier=1,
                state=state,
                submitted_at=at,
                updated_at=at,
                **SCOPE,
            )
        )


def _state(sid: str) -> str:
    with get_sync_engine().connect() as conn:
        return conn.execute(
            select(order_submissions.c.state).where(order_submissions.c.submission_id == sid)
        ).scalar_one()


_TERMINAL_KW = dict(reason_code="X", filled_qty=None, avg_fill_price=None)
_SUBMIT_KW = dict(ib_order_id="5", perm_id="55", placing_client_id=0)
_UNKNOWN_KW = dict(ib_order_id=None, perm_id=None, placing_client_id=None, reason_code="X")


@pytest.mark.parametrize(
    "call",
    [
        lambda sid, exp: orders_store.mark_terminal(
            submission_id=sid, state="CANCELLED", **_TERMINAL_KW, expected_states=exp
        ),
        lambda sid, exp: orders_store.mark_submitted(submission_id=sid, **_SUBMIT_KW, expected_states=exp),
        lambda sid, exp: orders_store.mark_place_unknown(submission_id=sid, **_UNKNOWN_KW, expected_states=exp),
        lambda sid, exp: _update_state(sid, exp),
    ],
    ids=["mark_terminal", "mark_submitted", "mark_place_unknown", "update_order_state"],
)
def test_writer_rejects_unexpected_state(call, caplog):
    _seed("sm-filled", "FILLED")
    with caplog.at_level(logging.INFO):
        assert call("sm-filled", ("PENDING", "WORKING")) == 0
    assert _state("sm-filled") == "FILLED"
    assert "skipped" in caplog.text and "sm-filled" in caplog.text

    _seed("sm-pending", "PENDING")
    assert call("sm-pending", ("PENDING",)) == 1
    assert _state("sm-pending") != "PENDING"


def _update_state(sid: str, exp) -> int:
    with get_sync_engine().begin() as conn:
        return combo_wizard.update_order_state(conn, sid, "UNKNOWN", expected_states=exp)


@pytest.mark.parametrize(
    "call",
    [
        lambda: orders_store.mark_terminal(submission_id="s", state="CANCELLED", **_TERMINAL_KW),
        lambda: orders_store.mark_submitted(submission_id="s", **_SUBMIT_KW),
        lambda: orders_store.mark_place_unknown(submission_id="s", **_UNKNOWN_KW),
        lambda: combo_wizard.update_order_state(None, "s", "WORKING"),
        lambda: orders_store.mark_terminal("s", "CANCELLED", "X", None, None, ("WORKING",)),
    ],
    ids=["mark_terminal", "mark_submitted", "mark_place_unknown", "update_order_state", "positional"],
)
def test_expected_states_is_required_keyword(call):
    with pytest.raises(TypeError):
        call()


def test_user_cancel_never_overwrites_filled():
    """`_mark_submission_cancelled` only leaves a non-terminal state: a fill
    the poller recorded first wins over the cancel reply."""

    _seed("sm-cx-filled", "FILLED")
    _seed("sm-cx-working", "WORKING")
    _seed("sm-cx-unknown", "UNKNOWN")
    order_entry._mark_submission_cancelled("sm-cx-filled")
    order_entry._mark_submission_cancelled("sm-cx-working")
    order_entry._mark_submission_cancelled("sm-cx-unknown")
    assert _state("sm-cx-filled") == "FILLED"
    assert _state("sm-cx-working") == "CANCELLED"
    assert _state("sm-cx-unknown") == "CANCELLED"


def test_snapshot_resurrect_is_noop_when_row_moved_on(monkeypatch):
    """register_from_snapshot writes a resurrect only from the state it read
    in the same transaction; a row that changed since then is left alone."""
    _seed("snapshot-4401", "CANCELLED")
    with get_sync_engine().begin() as conn:
        conn.execute(
            order_submissions.update()
            .where(order_submissions.c.submission_id == "snapshot-4401")
            .values(perm_id="4401", ib_order_id="1")
        )
    real_update = orders_store.update

    def racing_update(table):
        # Another writer moves the row to FILLED between the read and the write.
        with get_sync_engine().begin() as conn:
            conn.execute(
                real_update(order_submissions)
                .where(order_submissions.c.submission_id == "snapshot-4401")
                .values(state="FILLED")
            )
        monkeypatch.setattr(orders_store, "update", real_update)
        return real_update(table)

    monkeypatch.setattr(orders_store, "update", racing_update)
    out = orders_store.register_from_snapshot(
        perm_id="4401",
        ib_order_id="1",
        ticker="SPY",
        security_type="STK",
        action="BUY",
        quantity=1,
        limit_price=1.0,
        observed_at=datetime.now(timezone.utc) + timedelta(seconds=5),
        **SCOPE,
    )
    assert out["action"] == "NOOP"
    assert _state("snapshot-4401") == "FILLED"


def test_health_counts_stale_pending_without_changing_it():
    from xenon.api.routes.system import _order_submissions_health
    from xenon.api.services import ib_activity_mirror
    from xenon.execution.account_scope import AccountScope

    _seed("sm-stale", "PENDING", age_s=3600)
    _seed("sm-young", "PENDING")
    _seed("sm-unk", "UNKNOWN", age_s=3600)
    out = _order_submissions_health(SCOPE)
    assert (out["stale_pending_count"], out["unknown_count"]) == (1, 1)

    scope = AccountScope(**SCOPE)
    res = ib_activity_mirror.recover_by_order_ref([], [], scope=scope)
    assert res == {"recovered": 0, "unresolved": 3}
    assert [_state(s) for s in ("sm-stale", "sm-young", "sm-unk")] == ["PENDING", "PENDING", "UNKNOWN"]
