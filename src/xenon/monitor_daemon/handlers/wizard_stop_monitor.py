#!/usr/bin/env python3
"""Wizard Stop Monitor Handler.

Polls wizard sessions in state PROTECTED and checks each session's Risk Alert
threshold against the current combo net-mid. When a threshold is crossed, the
handler emits a session event and sends an operator notification.

Spec §9.2 is strict: this is a Risk Alert → Assisted Exit, NOT a stop-loss.
The handler NEVER auto-places a close order — the operator confirms.

Runtime fits the ``BaseHandler`` template under ``monitor_daemon/handlers/``.
"""

from __future__ import annotations

import logging
import os
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

from xenon.execution import orders_store  # noqa: F401 — kept for external import paths
from xenon.execution.combo_wizard.protect import risk_alert_popup_copy

from .base import BaseHandler

logger = logging.getLogger(__name__)


def _default_notify(payload: dict) -> None:
    """Best-effort macOS notification; silent on non-Darwin systems."""
    title = payload.get("title", "Xenon Risk Alert")
    body = payload.get("body", "")
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{body}" with title "{title}"',
            ],
            check=False,
            timeout=3,
        )
    except Exception:  # noqa: BLE001
        pass


class WizardStopMonitorHandler(BaseHandler):
    """Poll PROTECTED wizard sessions and surface Risk Alert crossings."""

    name = "wizard_stop_monitor"
    interval_seconds = 30
    requires_market_hours = True

    def __init__(
        self,
        *,
        quote_fn: Optional[Callable[[str], Decimal | None]] = None,
        notify_fn: Optional[Callable[[dict], None]] = None,
        db_path: Path | str | None = None,
        ib_client_factory: Optional[Callable[[], object]] = None,
        ttl_s: float | None = None,
    ):
        super().__init__()
        # If no quote_fn is injected but we have an ib_client_factory, wire the
        # real combo-quote source (spec §10 freshness gates). Tests inject a
        # fake quote_fn directly.
        #
        # ttl_s=None → combo_quote_source falls back to DEFAULT_TICK_TTL_S
        # (read at import time from XENON_WIZARD_QUOTE_TTL_S env var).
        self._ib_client_factory = ib_client_factory
        if quote_fn is None and ib_client_factory is not None:
            from xenon.execution.combo_wizard.combo_quote_source import (
                build_default_quote_fn,
            )

            quote_fn = build_default_quote_fn(ib_client_factory, db_path=db_path, ttl_s=ttl_s)
        self._quote_fn = quote_fn
        self._notify_fn = notify_fn or _default_notify
        self._db_path = db_path
        # In-memory set of session_ids that have already crossed this lifecycle;
        # prevents duplicate notifications while the condition persists.
        self._already_fired: set[str] = set()

    # -- helpers -----------------------------------------------------------

    def _list_protected(self) -> list[dict[str, Any]]:
        """Return PROTECTED sessions with active alert from Postgres.

        Filters by the daemon's broker account scope (env-resolved). Empty
        env vars resolve to None → no filter, preserving legacy behavior.
        """
        from xenon.db.engine import get_sync_engine
        from xenon.db.queries import combo_wizard

        broker = os.environ.get("XENON_BROKER") or None
        account_env = os.environ.get("XENON_TRADING_MODE") or None
        broker_account = os.environ.get("XENON_BROKER_ACCOUNT") or None

        engine = get_sync_engine()
        with engine.connect() as conn:
            rows = combo_wizard.list_protected_sessions(
                conn,
                broker=broker,
                account_env=account_env,
                broker_account=broker_account,
            )

        out: list[dict[str, Any]] = []
        for r in rows:
            payload = r.get("payload") or {}
            config = r.get("config") or {}
            # Postgres stores alert fields inside config JSONB
            alert_enabled = config.get("alert_enabled", False)
            if not alert_enabled:
                continue
            threshold_raw = config.get("alert_net_mid_threshold")
            out.append(
                {
                    "session_id": r["session_id"],
                    "ticker": r["ticker"],
                    "payload": payload,
                    "threshold": Decimal(str(threshold_raw)) if threshold_raw is not None else None,
                }
            )
        return out

    def _record_event(self, session_id: str, kind: str, detail: dict) -> None:
        """Persist a wizard session event to Postgres."""
        from xenon.db.engine import get_sync_engine
        from xenon.db.queries import combo_wizard

        engine = get_sync_engine()
        with engine.begin() as conn:
            combo_wizard.record_event(conn, session_id=session_id, kind=kind, detail=detail)

    @staticmethod
    def _crossed(quote: Decimal, threshold: Decimal) -> bool:
        """Risk Alert crosses when quote drops to/below threshold for DEBIT
        (and rises to/above for CREDIT where threshold is negative). Because
        signed combo pricing is preserved end-to-end, a single signed
        comparison works: cross when quote - threshold <= 0 for long (DEBIT)
        or quote - threshold >= 0 for short (CREDIT). The threshold is stored
        signed to match the polarity, so the uniform rule is: quote reaches
        the threshold in the unfavorable direction.

        For V1 we use the magnitude-based rule: the long-spread holder worries
        about the mid shrinking; the short-spread holder worries about the
        absolute credit widening. We treat the threshold as the "alert line"
        in the signed space and compare quote <= threshold in absolute terms
        — consistent with how `signed_mid_price` flows into the planner.
        """
        return Decimal(str(quote)) <= Decimal(str(threshold))

    # -- main --------------------------------------------------------------

    def execute(self) -> dict[str, Any]:
        sessions = self._list_protected()
        checked = 0
        crossed = 0

        for s in sessions:
            checked += 1
            sid = s["session_id"]
            threshold = s["threshold"]
            if threshold is None:
                continue
            quote: Decimal | None = None
            if self._quote_fn is not None:
                try:
                    quote = self._quote_fn(sid)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("quote_fn failed for %s: %s", sid, exc)
                    continue
            if quote is None:
                continue

            if not self._crossed(quote, threshold):
                # Reset fired flag if quote recovered, so a re-cross can fire.
                self._already_fired.discard(sid)
                continue

            crossed += 1
            if sid in self._already_fired:
                continue
            self._already_fired.add(sid)

            payload = {
                "session_id": sid,
                "ticker": s["ticker"],
                "title": "Xenon Risk Alert",
                "body": risk_alert_popup_copy() + f"  [{s['ticker']} @ {quote}, threshold {threshold}]",
                "quote": str(quote),
                "threshold": str(threshold),
            }
            try:
                self._notify_fn(payload)
            except Exception as exc:  # noqa: BLE001
                logger.warning("notify_fn failed for %s: %s", sid, exc)

            self._record_event(
                sid,
                "RISK_ALERT_CROSSED",
                {
                    "quote": str(quote),
                    "threshold": str(threshold),
                    "note": "Assisted Exit — operator confirmation required",
                },
            )

        return {
            "checked": checked,
            "crossed": crossed,
            "orders_placed": 0,  # explicit: Risk Alert never auto-places
        }
