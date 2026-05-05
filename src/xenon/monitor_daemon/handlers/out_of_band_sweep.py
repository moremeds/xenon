"""Daily out-of-band reconciliation for position rules."""
from __future__ import annotations

import logging

from sqlalchemy import text

from xenon.db.events import emit_outbox_in_txn
from xenon.execution.account_scope import AccountScope
from xenon.monitor_daemon.handlers.base import BaseHandler

logger = logging.getLogger(__name__)


def _candidate_position_key(position: dict) -> str | None:
    symbol = position.get("symbol")
    if not symbol:
        return None
    sec_type = str(position.get("sec_type") or position.get("secType") or "STK").upper()
    if sec_type == "STK":
        return f"STK::{symbol}"
    if sec_type == "OPT":
        expiry = position.get("expiry")
        strike = position.get("strike")
        right = position.get("right")
        if expiry is None or strike is None or right is None:
            return None
        strike_value = float(strike)
        strike_text = str(int(strike_value)) if strike_value == int(strike_value) else str(strike).rstrip("0").rstrip(".")
        return f"OPT::{symbol}::{expiry}::{strike_text}::{str(right).upper()}"
    return None


class OutOfBandSweepHandler(BaseHandler):
    name = "position_rules_oob_sweep"
    interval_seconds = 24 * 60 * 60
    requires_market_hours = False

    def __init__(self, *, engine, ib_client, scope: AccountScope):
        super().__init__()
        self._engine = engine
        self._ib = ib_client
        self._scope = scope
        self._last_known_position_count = self._read_last_known_count()

    def _read_last_known_count(self) -> int:
        try:
            with self._engine.connect() as conn:
                row = conn.execute(
                    text(
                        """
                        SELECT (payload->>'count')::int AS count
                        FROM events.outbox
                        WHERE channel = 'position_rule.transition'
                          AND source = 'oob_sweep'
                          AND payload->>'kind' = 'oob_sweep_position_count'
                          AND payload->>'broker_account' = :broker_account
                        ORDER BY id DESC
                        LIMIT 1
                        """
                    ),
                    {"broker_account": self._scope.broker_account},
                ).first()
        except Exception:  # noqa: BLE001
            return 0
        return int(row.count) if row and row.count is not None else 0

    def execute(self) -> dict:
        if not getattr(self._ib, "connected", True):
            return {"status": "skipped_disconnected"}

        positions = list(self._ib.positions() if hasattr(self._ib, "positions") else [])
        observed = len(positions)
        if self._last_known_position_count > 0:
            floor = max(1, int(self._last_known_position_count * 0.7))
            if observed < floor:
                logger.warning("OOB sweep aborted: observed=%d floor=%d", observed, floor)
                self._emit(
                    {
                        "payload_version": 1,
                        "kind": "oob_sweep_aborted",
                        "reason": "positions_response_suspiciously_small",
                        "observed": observed,
                        "floor": floor,
                        "scope": self._scope.as_dict(),
                    }
                )
                return {"status": "aborted_short_response", "observed": observed, "floor": floor}

        unprotected = 0
        with self._engine.connect() as conn:
            for position in positions:
                position_key = _candidate_position_key(position)
                if position_key is None:
                    continue
                existing = conn.execute(
                    text(
                        """
                        SELECT 1 FROM xenon.position_protection
                        WHERE broker = :broker
                          AND account_env = :account_env
                          AND broker_account = :broker_account
                          AND position_key = :position_key
                          AND state IN ('PENDING_ARM','ARMED','TRIGGERED')
                        LIMIT 1
                        """
                    ),
                    {
                        "broker": self._scope.broker,
                        "account_env": self._scope.account_env,
                        "broker_account": self._scope.broker_account,
                        "position_key": position_key,
                    },
                ).first()
                if existing is not None:
                    continue
                unprotected += 1
                self._emit(
                    {
                        "payload_version": 1,
                        "kind": "unprotected_position_detected",
                        "symbol": position.get("symbol"),
                        "qty": position.get("qty"),
                        "scope": self._scope.as_dict(),
                    }
                )

        self._emit(
            {
                "payload_version": 1,
                "kind": "oob_sweep_position_count",
                "broker_account": self._scope.broker_account,
                "count": observed,
            }
        )
        self._last_known_position_count = observed
        return {"status": "ok", "observed": observed, "unprotected_count": unprotected}

    def _emit(self, payload: dict) -> None:
        with self._engine.begin() as conn:
            emit_outbox_in_txn(
                conn,
                channel="position_rule.transition",
                source="oob_sweep",
                payload=payload,
            )
