"""Health/status payload for position rules. Spec §12.2."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from xenon.execution.account_scope import AccountScope

_ALL_RULE_STATES = ("PENDING_ARM", "ARMED", "TRIGGERED", "FAILED", "CANCELED", "CLOSED", "SUPERSEDED")
_ALL_CLAIM_STATUSES = ("PENDING", "SUBMITTED", "FILLED", "FAILED", "ABANDONED")


def _market_window(now: datetime | None = None) -> tuple[str, datetime]:
    et = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    open_at = et.replace(hour=9, minute=30, second=0, microsecond=0)
    close_at = et.replace(hour=16, minute=0, second=0, microsecond=0)

    if et.weekday() >= 5:
        days_until_monday = 7 - et.weekday()
        return "closed", (open_at + timedelta(days=days_until_monday)).astimezone(timezone.utc)
    if et < open_at:
        return "pre_open", open_at.astimezone(timezone.utc)
    if et >= close_at:
        days_ahead = 1 if et.weekday() < 4 else 7 - et.weekday()
        return "post_close", (open_at + timedelta(days=days_ahead)).astimezone(timezone.utc)
    return "open", close_at.astimezone(timezone.utc)


def _ib_connected() -> bool:
    try:
        from xenon.clients.ib_client import IBClient

        singleton = getattr(IBClient, "singleton", None)
        client = singleton() if callable(singleton) else None
        return bool(getattr(client, "connected", False))
    except Exception:  # noqa: BLE001
        return False


def compute_health(*, engine, scope: AccountScope) -> dict[str, Any]:
    market_window, next_event = _market_window()
    params = {"broker": scope.broker, "account_env": scope.account_env, "broker_account": scope.broker_account}

    with engine.connect() as conn:
        rule_counts = dict(
            conn.execute(
                text(
                    """
                    SELECT state, COUNT(*) FROM xenon.position_protection
                    WHERE broker = :broker
                      AND account_env = :account_env
                      AND broker_account = :broker_account
                    GROUP BY state
                    """
                ),
                params,
            ).all()
        )
        claim_counts = dict(
            conn.execute(
                text(
                    """
                    SELECT status, COUNT(*) FROM xenon.position_close_claims
                    WHERE broker = :broker
                      AND account_env = :account_env
                      AND broker_account = :broker_account
                    GROUP BY status
                    """
                ),
                params,
            ).all()
        )
        stale_quote_skips = conn.execute(
            text(
                """
                SELECT COUNT(*) FROM events.outbox
                WHERE channel = 'position_rule.transition'
                  AND emitted_at >= now() - interval '1 hour'
                  AND payload->>'reason' IN ('stale_quote_skip','silent_market_suspected','ib_connection_stale')
                """
            )
        ).scalar_one()
        unprotected = conn.execute(
            text(
                """
                SELECT COUNT(*) FROM events.outbox
                WHERE channel = 'position_rule.transition'
                  AND emitted_at >= now() - interval '24 hours'
                  AND payload->>'kind' = 'unprotected_position_detected'
                """
            )
        ).scalar_one()
        dlq_count = conn.execute(text("SELECT COUNT(*) FROM events.outbox_dlq")).scalar_one()
        last_tick = conn.execute(
            text(
                """
                SELECT MAX(emitted_at) FROM events.outbox
                WHERE (
                    (channel = 'position_rule.transition' AND source = 'cas_transition')
                    OR (channel = 'position_rule.heartbeat' AND source = 'position_rules_handler')
                  )
                  AND payload->'scope'->>'broker' = :broker
                  AND payload->'scope'->>'account_env' = :account_env
                  AND payload->'scope'->>'broker_account' = :broker_account
                """
            ),
            params,
        ).scalar_one()

    for state in _ALL_RULE_STATES:
        rule_counts.setdefault(state, 0)
    for status in _ALL_CLAIM_STATUSES:
        claim_counts.setdefault(status, 0)

    last_tick_age_s = int((datetime.now(timezone.utc) - last_tick).total_seconds()) if last_tick else None
    daemon_alive = last_tick_age_s is not None and last_tick_age_s < 600 if market_window == "open" else True

    return {
        "schema_version": 1,
        "daemon_alive": daemon_alive,
        "advisory_lock_held": True,
        "last_tick_at": last_tick.isoformat() if last_tick else None,
        "last_tick_age_seconds": last_tick_age_s,
        "market_window": market_window,
        "next_market_event_at": next_event.isoformat(),
        "rule_counts_by_state": rule_counts,
        "claim_counts_by_status": claim_counts,
        "in_flight_claims": claim_counts["PENDING"] + claim_counts["SUBMITTED"],
        "stale_quote_skips_last_hour": stale_quote_skips,
        "unprotected_position_count": unprotected,
        "ib_connected": _ib_connected(),
        "outbox_dlq_count": dlq_count,
        "scope": scope.as_dict(),
    }
