"""Gate-4 PR-03b round 4 — observation watermark (item 1) and generation-safe
MODIFY_PENDING recovery (item 4) at the gate4 / orders_store layer."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone

from scripts.tests.test_gate4_reserve import (
    SCOPE,
    _env_for_subprocess,
    _reserve,
    _seed_snapshot,
    _snapshot_payload,
    _stock,
    _working_with_legs,
)
from scripts.tests.test_gate4_round3 import _legs, _mirror, _set_perm
from xenon.execution import gate4, orders_store


def _now():
    return datetime.now(timezone.utc)


def _deadline(sub_id):
    return datetime.fromisoformat(_legs(sub_id)[1]["pending_amend_deadline"])


# ---------------------------------------------------------------------------
# item 1 — a mirror read that began before an amend can never resolve it
# ---------------------------------------------------------------------------


def test_delayed_mirror_cannot_confirm_newer_amend():
    """Astra P1-1: the mirror reads qty=1, then stalls. Amend A (1->2) is
    confirmed; amend B (2->1) opens but has not reached IB. The stale qty=1
    observation lands: it must not confirm B, and the 200 shares stay fully
    reserved (IB still has qty=2)."""
    _seed_snapshot(_snapshot_payload([_stock("SPY", 200)]))
    sub_id = _working_with_legs("r4-delay-1", qty=1)
    _set_perm(sub_id, "990101")
    read_started = _now()  # mirror starts its IB read here ...

    a = gate4.begin_modify(sub_id, 2, SCOPE)
    assert gate4.settle_modify(sub_id, a.amend_id, "confirmed", SCOPE, new_qty=2)
    b = gate4.begin_modify(sub_id, 1, SCOPE)
    assert b.accept

    out = _mirror("990101", 1, observed_at=read_started)  # ... and writes now

    assert "reconciled" not in out
    qty, legs = _legs(sub_id)
    assert qty == 2
    assert legs["pending_amend_id"] == b.amend_id and legs["pending_amend_qty"] == 1
    assert _reserve("r4-delay-next").status == "rejected"

    # Control: a read that STARTED after B was opened may confirm it.
    assert _mirror("990101", 1)["reconciled"] is True
    assert _legs(sub_id)[0] == 1
    assert _reserve("r4-delay-after").status == "winner"


def test_stale_read_never_lowers_observed_demand():
    """UUID row qty 1; a fresh read saw TWS raise it to 3 (observed_qty=3).
    An older read showing 1 lands later: demand stays 3."""
    _seed_snapshot(_snapshot_payload([_stock("SPY", 300)]))
    sub_id = _working_with_legs("r4-stale-obs", qty=1)
    _set_perm(sub_id, "990102")
    old_read = _now()
    assert _mirror("990102", 3)["observed"] is True
    _mirror("990102", 1, observed_at=old_read)
    assert _legs(sub_id)[1]["observed_qty"] == 3
    assert _reserve("r4-stale-next").status == "rejected"


# ---------------------------------------------------------------------------
# item 4 — MODIFY_PENDING recovery: sender finished + later fresh read
# ---------------------------------------------------------------------------


def test_lost_price_only_reply_recovers_after_sender_deadline():
    """API died after opening a price-only amend (no settle ever runs). A
    read before the sender deadline changes nothing; the first fresh read
    after it releases the token and the next modify is accepted."""
    _seed_snapshot(_snapshot_payload([_stock("SPY", 300)]))
    sub_id = _working_with_legs("r4-price-lost", qty=1)
    _set_perm(sub_id, "990201")
    opened = gate4.begin_modify(sub_id, None, SCOPE)
    deadline = _deadline(sub_id)

    _mirror("990201", 1, observed_at=_now())
    assert _legs(sub_id)[1]["pending_amend_id"] == opened.amend_id
    assert gate4.begin_modify(sub_id, None, SCOPE).reasons == [gate4.MODIFY_PENDING_REASON]

    out = _mirror("990201", 1, observed_at=deadline + timedelta(seconds=1))
    assert out["expired"] is True
    assert _legs(sub_id)[1]["pending_amend_id"] is None
    assert gate4.begin_modify(sub_id, None, SCOPE).accept


def test_unapplied_quantity_amend_keeps_max_until_fresh_read_after_sender_finished():
    """1->2 was sent, the reply was lost (uncertain), IB never applied it.
    Until a fresh read after send_by + grace, demand stays max(1,2)=2; the
    deadline alone (no read) never clears it. Afterwards demand is 1."""
    _seed_snapshot(_snapshot_payload([_stock("SPY", 200)]))
    sub_id = _working_with_legs("r4-qty-lost", qty=1)
    _set_perm(sub_id, "990202")
    a = gate4.begin_modify(sub_id, 2, SCOPE)
    opened_deadline = _deadline(sub_id)
    assert not gate4.settle_modify(sub_id, a.amend_id, "uncertain", SCOPE)
    finished = _deadline(sub_id)
    assert finished == opened_deadline  # round 5: settle never moves the fence-based deadline

    _mirror("990202", 1, observed_at=_now())  # inside the IB grace
    assert _legs(sub_id)[1]["pending_amend_qty"] == 2
    assert _reserve("r4-qty-mid").status == "rejected"

    out = _mirror("990202", 1, observed_at=finished + timedelta(seconds=1))
    assert out["expired"] is True
    qty, legs = _legs(sub_id)
    assert qty == 1 and legs["pending_amend_qty"] is None
    assert _reserve("r4-qty-after").status == "winner"


def test_api_restart_amend_expires_only_after_cli_bound():
    """No settle at all (API process died mid-modify). The deadline is
    send_by + grace (round 5): no sender transmits after send_by (route and
    CLI fence), so after that only IB's processing grace remains."""
    from xenon.api import server
    from xenon.execution import ib_order_manage

    assert server._MODIFY_SUBPROCESS_TIMEOUT_S < ib_order_manage.MODIFY_WALL_CLOCK_S
    _seed_snapshot(_snapshot_payload([_stock("SPY", 300)]))
    sub_id = _working_with_legs("r4-restart", qty=1)
    _set_perm(sub_id, "990203")
    before = _now()
    gate4.begin_modify(sub_id, 2, SCOPE)
    deadline = _deadline(sub_id)
    send_by = datetime.fromisoformat(_legs(sub_id)[1]["pending_amend_send_by"])
    assert send_by >= before + timedelta(seconds=gate4.MODIFY_SEND_WINDOW_S)
    assert deadline == send_by + timedelta(seconds=gate4.AMEND_IB_GRACE_S)

    _mirror("990203", 1, observed_at=deadline - timedelta(seconds=1))
    assert _legs(sub_id)[1]["pending_amend_qty"] == 2
    assert _mirror("990203", 1, observed_at=deadline + timedelta(seconds=1))["expired"] is True


def test_expired_amend_needs_a_read_after_the_last_write():
    """Past the sender deadline is not enough: the read must also have begun
    after the row's last local write. Here the row was written again after
    the deadline; a read that began between the two cannot expire it."""
    from sqlalchemy import text

    from xenon.db.engine import get_sync_engine

    _seed_snapshot(_snapshot_payload([_stock("SPY", 300)]))
    sub_id = _working_with_legs("r4-old-read", qty=1)
    _set_perm(sub_id, "990204")
    a = gate4.begin_modify(sub_id, 2, SCOPE)
    gate4.settle_modify(sub_id, a.amend_id, "uncertain", SCOPE)
    deadline = _deadline(sub_id)
    with get_sync_engine().begin() as conn:  # a later local write
        conn.execute(
            text("UPDATE xenon.order_submissions SET updated_at=:t WHERE submission_id=:s"),
            {"t": deadline + timedelta(seconds=5), "s": sub_id},
        )

    out = _mirror("990204", 1, observed_at=deadline + timedelta(seconds=1))

    assert "expired" not in out
    assert _legs(sub_id)[1]["pending_amend_qty"] == 2
    assert _mirror("990204", 1, observed_at=deadline + timedelta(seconds=6))["expired"] is True


def test_modify_cli_watchdog_ends_the_sender():
    code = (
        "import time\nfrom xenon.execution import ib_order_manage as m\nm._arm_sender_watchdog(0.2)\ntime.sleep(10)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], env=_env_for_subprocess(), timeout=8)
    assert proc.returncode == 3


def test_reconcile_requires_fresh_read():
    payload = {"pending_amend_id": "x", "pending_amend_qty": 2, "pending_amend_deadline": _now().isoformat()}
    assert orders_store._reconcile_pending(payload, 2, observed_at=_now(), fresh=False) == (payload, None)
    assert orders_store._reconcile_pending(payload, 2, observed_at=_now(), fresh=True)[1] == "confirmed"
