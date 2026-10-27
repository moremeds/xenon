"""Wizard Risk Alert — FastAPI lifespan task (spec §2.9).

Replaces the monitor daemon's ``wizard_stop_monitor`` handler. Every
``DEFAULT_INTERVAL_S`` the task lists PROTECTED wizard sessions with an
enabled Risk Alert, fetches the signed combo net-mid, and compares it with the
stored threshold using the daemon's rule (``quote <= threshold`` crosses).

Spec §9.2: this is a Risk Alert → Assisted Exit, NOT a stop-loss. The task
NEVER places, cancels or modifies an order; the operator confirms the exit.

States per session:

- ``CROSSED``  — fresh quote at/below the threshold. On the first crossing of a
  lifecycle the task records a ``RISK_ALERT_CROSSED`` row in
  ``xenon.wizard_events`` (the daemon's table and kind). No write under
  ``XENON_READ_ONLY=1``.
- ``NO_QUOTE`` — no quote, a quote error, or a quote older than
  ``NO_QUOTE_AFTER_S``. Never a trigger.
- ``OK``       — fresh quote above the threshold.

Quote source: the ib_pool ``data`` role, the in-process equivalent of the
daemon's ``IBClient.get_quote`` path. Each leg is a ``snapshot=True``
``reqMktData`` (no streaming line is held), run through ``pool.run_sync`` on
the role's pinned thread (never ``asyncio.to_thread`` around ib_async), then
``combo_quote_source``'s freshness gates and ``compute_combo_quote``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable

from xenon.api.guards import is_read_only

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 30.0  # daemon handler interval_seconds
NO_QUOTE_AFTER_S = 120.0  # spec §2.9: a quote older than 2 min is "no quote"
CLOCK_SKEW_S = 5.0  # a tick this much newer than our clock still counts as fresh
QUOTE_TIMEOUT_S = 30.0  # data-role deadline for one combo snapshot (legs x snapshot waits)
EVENT_KIND = "RISK_ALERT_CROSSED"


@dataclass(frozen=True)
class ComboQuote:
    """Signed combo net-mid and the time of its oldest leg tick."""

    mid: Decimal
    as_of: datetime | None


QuoteFn = Callable[[dict[str, Any]], Awaitable[ComboQuote | None]]


def crossed(quote: Decimal, threshold: Decimal) -> bool:
    """Daemon rule (signed space): the alert crosses at or below the line."""
    return Decimal(str(quote)) <= Decimal(str(threshold))


def list_alert_sessions(scope: dict[str, Any]) -> list[dict[str, Any]]:
    """PROTECTED sessions in ``scope`` whose protection has the alert enabled."""
    from xenon.db.engine import get_sync_engine
    from xenon.db.queries import combo_wizard

    with get_sync_engine().connect() as conn:
        rows = combo_wizard.list_protected_sessions(conn, **scope)
    out: list[dict[str, Any]] = []
    for r in rows:
        config = r.get("config") or {}
        if not config.get("alert_enabled", False):
            continue
        raw = config.get("alert_net_mid_threshold")
        out.append(
            {
                "session_id": r["session_id"],
                "ticker": r["ticker"],
                "payload": r.get("payload") or {},
                "threshold": Decimal(str(raw)) if raw is not None else None,
            }
        )
    return out


def record_alert_event(session_id: str, kind: str, detail: dict[str, Any]) -> None:
    from xenon.db.engine import get_sync_engine
    from xenon.db.queries import combo_wizard

    with get_sync_engine().begin() as conn:
        combo_wizard.record_event(conn, session_id=session_id, kind=kind, detail=detail)


class RiskAlertMonitor:
    """Evaluate Risk Alerts; hold the latest per-session state in memory."""

    def __init__(
        self,
        *,
        list_sessions_fn: Callable[[], list[dict[str, Any]]],
        quote_fn: QuoteFn,
        record_event_fn: Callable[[str, str, dict[str, Any]], None] = record_alert_event,
        read_only_fn: Callable[[], bool] = is_read_only,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._list_sessions = list_sessions_fn
        self._quote = quote_fn
        self._record = record_event_fn
        self._read_only = read_only_fn
        self._now = now_fn
        # Sessions already alerted this lifecycle (daemon `_already_fired`).
        self._fired: set[str] = set()
        self._states: dict[str, dict[str, Any]] = {}
        self.last_run_at: str | None = None

    def states(self) -> list[dict[str, Any]]:
        return list(self._states.values())

    def alerts(self) -> list[dict[str, Any]]:
        return [s for s in self._states.values() if s["state"] != "OK"]

    async def _fetch(self, session: dict[str, Any]) -> ComboQuote | None:
        try:
            return await self._quote(session)
        except Exception as exc:  # noqa: BLE001 — a quote failure is NO_QUOTE
            logger.warning("risk alert: quote failed for %s: %s", session["session_id"], exc)
            return None

    async def evaluate_once(self) -> dict[str, Any]:
        sessions = self._list_sessions()
        now = self._now()
        states: dict[str, dict[str, Any]] = {}
        crossed_count = 0
        for s in sessions:
            sid = s["session_id"]
            threshold = s["threshold"]
            if threshold is None:
                continue
            quote = await self._fetch(s)
            # Age is measured AFTER the quote arrives: acquisition takes ~2 s
            # per leg, so a clock read before it would make a fresh tick
            # future-dated.
            now = self._now()
            fresh = (
                quote is not None
                and quote.as_of is not None
                and -CLOCK_SKEW_S <= (now - quote.as_of).total_seconds() <= NO_QUOTE_AFTER_S
            )
            state = {
                "session_id": sid,
                "ticker": s["ticker"],
                "threshold": str(threshold),
                "quote": str(quote.mid) if quote is not None else None,
                "quote_as_of": quote.as_of.isoformat() if quote is not None and quote.as_of else None,
                "evaluated_at": now.isoformat(),
            }
            if not fresh:
                # Never a trigger; keep the fired flag so a stale gap does not re-fire.
                states[sid] = {**state, "state": "NO_QUOTE"}
                continue
            assert quote is not None
            if not crossed(quote.mid, threshold):
                self._fired.discard(sid)  # recovered → a re-cross fires again
                states[sid] = {**state, "state": "OK"}
                continue
            crossed_count += 1
            states[sid] = {**state, "state": "CROSSED"}
            if sid in self._fired:
                continue
            self._fired.add(sid)
            if self._read_only():
                logger.warning("risk alert crossed for %s (read-only: not recorded)", sid)
                continue
            try:
                self._record(
                    sid,
                    EVENT_KIND,
                    {
                        "quote": str(quote.mid),
                        "threshold": str(threshold),
                        "note": "Assisted Exit — operator confirmation required",
                    },
                )
            except Exception:  # noqa: BLE001 — retry on the next crossing tick
                self._fired.discard(sid)
                logger.exception("risk alert: failed to record event for %s", sid)
        self._fired &= set(states)
        self._states = states
        self.last_run_at = now.isoformat()
        return {"checked": len(states), "crossed": crossed_count, "orders_placed": 0}


async def risk_alert_loop(monitor: RiskAlertMonitor, *, interval_s: float = DEFAULT_INTERVAL_S) -> None:
    """Forever loop; one failed tick never stops the task."""
    while True:
        try:
            await monitor.evaluate_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("wizard risk alert tick failed")
        await asyncio.sleep(interval_s)


class _SnapshotClient:
    """Adapter: turn ``combo_quote_source``'s streaming ``get_quote`` into a
    one-shot snapshot (no market-data line is held between ticks) and record
    each leg tick time so the combo quote carries its oldest timestamp."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.tick_times: list[datetime] = []

    def get_quote(self, contract: Any, snapshot: bool = False) -> Any:
        ticker = self._client.get_quote(contract, snapshot=True)
        t = getattr(ticker, "time", None)
        if t is not None:
            self.tick_times.append(t)
        return ticker


def _combo_quote_with_client(client: Any, session_id: str) -> ComboQuote | None:
    """Runs on the ``data`` role thread (see ``IBPool.run_sync``)."""
    from xenon.execution.combo_wizard.combo_quote_source import build_default_quote_fn

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    snap = _SnapshotClient(client)
    mid = build_default_quote_fn(lambda: snap, ttl_s=NO_QUOTE_AFTER_S)(session_id)
    if mid is None:
        return None
    return ComboQuote(mid=Decimal(str(mid)), as_of=min(snap.tick_times) if snap.tick_times else None)


def build_pool_quote_fn(pool: Any) -> QuoteFn:
    async def quote_fn(session: dict[str, Any]) -> ComboQuote | None:
        if not pool.is_connected("data"):
            return None
        from xenon.api.ib_pool import call_data_bounded

        # Bounded like the request path (deadline, worker bound, fencing,
        # upstream fast-fail), with a budget for the multi-leg snapshot waits.
        return await call_data_bounded(pool, _combo_quote_with_client, session["session_id"], timeout=QUOTE_TIMEOUT_S)

    return quote_fn
