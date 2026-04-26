"""Real combo quote source for the wizard stop monitor.

Wires `combo_quotes.compute_combo_quote` to fresh IB leg ticks. Applies spec
§10 freshness gates:

- non-crossed quotes (bid <= ask)
- non-zero bid/ask sizes
- no NaN values
- fresh quote timestamps (within a TTL; default 30s)

We deliberately do NOT use `reqTickersAsync` here — per
`feedback_ib_insync_in_fastapi`, it hangs on index options. We use the
``get_quote`` / streaming ``reqMktData`` path wrapped by ``IBClient``, which
keeps the stream alive so subsequent ticks refresh in place.

Returns ``None`` if any freshness gate fails — the stop monitor treats
``None`` as "skip this tick, try again next interval".
"""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

# ib_insync imports. Citations:
#   ib_insync/contract.py:193 — class Option(Contract)
from ib_insync import Option  # type: ignore

from xenon.execution import orders_store  # noqa: F401 — kept for external import paths

from .combo_quotes import compute_combo_quote
from .models import ComboLegQuote, ComboLegSpec

logger = logging.getLogger(__name__)

# Read-once at import time. Frozen / delayed market-data modes need a longer
# TTL than the 30s default (otherwise every tick fails the freshness gate
# forever). Override via env before importing this module:
#   XENON_WIZARD_QUOTE_TTL_S=120  # 2 minutes
DEFAULT_TICK_TTL_S = float(os.environ.get("XENON_WIZARD_QUOTE_TTL_S", "30.0"))


def _isnanlike(x: Any) -> bool:
    try:
        return math.isnan(float(x))
    except (TypeError, ValueError):
        return True


def _ticker_is_fresh(
    ticker: Any,
    *,
    now: datetime,
    ttl_s: float,
) -> bool:
    """Spec §10: non-crossed, non-zero sizes, no NaN, fresh timestamps.

    Citation: ib_insync/ticker.py:19-80 — class Ticker(bid, ask, bidSize,
    askSize, time).
    """
    if ticker is None:
        return False
    bid = getattr(ticker, "bid", float("nan"))
    ask = getattr(ticker, "ask", float("nan"))
    bid_size = getattr(ticker, "bidSize", 0) or 0
    ask_size = getattr(ticker, "askSize", 0) or 0
    tick_time = getattr(ticker, "time", None)

    if _isnanlike(bid) or _isnanlike(ask):
        return False
    if float(bid_size) <= 0 or float(ask_size) <= 0:
        return False
    if float(bid) > float(ask):
        # crossed / inverted
        return False
    if tick_time is None:
        return False
    try:
        age = (now - tick_time).total_seconds()
    except TypeError:
        # Mixed tz-aware/naive; treat as stale.
        return False
    if age < 0 or age > ttl_s:
        return False
    return True


def _session_legs(session_id: str, db_path: Path | str | None) -> list[dict]:
    """Load the session's legs from the wizard_sessions payload (JSONB).

    ``db_path`` is ignored (kept for signature compat) -- reads from Postgres.
    """
    from xenon.db.engine import get_sync_engine
    from xenon.db.queries import combo_wizard

    engine = get_sync_engine()
    with engine.connect() as conn:
        session = combo_wizard.get_session(conn, session_id)
    if session is None:
        return []
    payload = session.get("payload") or {}
    return list(payload.get("legs") or [])


class _TickerCache:
    """Session-scoped cache of streaming ib_insync Tickers keyed by conId.

    First access for a conId calls ``IBClient.get_quote(contract)`` which
    wraps ``ib_insync.IB.reqMktData(contract, ..., snapshot=False)``; the
    returned ``Ticker`` is retained. Subsequent accesses re-read the same
    ``Ticker`` instance — ib_insync live-updates ``bid/ask/bidSize/askSize/time``
    on the object as new ticks arrive (see
    ``.venv/lib/python3.13/site-packages/ib_insync/ib.py:1181`` —
    ``reqMktData`` returns a ticker that is updated in-place).

    Prevents the subscription leak where a long-running monitor otherwise
    appends to ``IBClient._subscriptions`` on every tick.

    Not thread-safe. The monitor daemon runs handlers sequentially.
    """

    def __init__(self) -> None:
        self._tickers: dict[int, tuple[Any, Any]] = {}  # conId -> (contract, ticker)

    def get(self, ib: Any, contract: Any) -> Any:
        con_id = int(getattr(contract, "conId", 0) or 0)
        if con_id and con_id in self._tickers:
            return self._tickers[con_id][1]
        ticker = ib.get_quote(contract, snapshot=False)
        if con_id:
            self._tickers[con_id] = (contract, ticker)
        return ticker

    def cleanup(self, ib: Any) -> None:
        """Cancel every retained subscription.

        Delegates to ``ib_insync.IB.cancelMktData(contract)``
        (``.venv/lib/python3.13/site-packages/ib_insync/ib.py:1241`` —
        ``def cancelMktData(self, contract: Contract)``; unsubscribes
        realtime streaming tick data for the exact contract that was used
        to subscribe). We access it as ``ib.ib.cancelMktData`` because
        ``ib`` here is the Xenon ``IBClient`` wrapper; ``.ib`` is the
        underlying ``ib_insync.IB`` instance.
        """
        inner = getattr(ib, "ib", None)
        for _con_id, (contract, _ticker) in list(self._tickers.items()):
            try:
                if inner is not None and hasattr(inner, "cancelMktData"):
                    inner.cancelMktData(contract)
            except Exception as exc:  # noqa: BLE001 — best-effort cleanup
                logger.warning("cancelMktData failed: %s", exc)
        self._tickers.clear()


def build_default_quote_fn(
    ib_client_factory: Callable[[], Any],
    *,
    db_path: Path | str | None = None,
    ttl_s: float | None = None,
    now_fn: Callable[[], datetime] | None = None,
    ticker_cache: _TickerCache | None = None,
) -> Callable[[str], Optional[Decimal]]:
    """Build the ``quote_fn(session_id)`` that the wizard stop monitor calls.

    Returns a signed Decimal combo mid, or ``None`` if any freshness gate
    fails for any leg (fail-closed — a stale tick must NOT fire an alert).

    ``ttl_s`` defaults to ``DEFAULT_TICK_TTL_S`` (read at import time from
    ``XENON_WIZARD_QUOTE_TTL_S``). Pass explicitly to override per-caller.

    ``ticker_cache`` — optional. When provided, ticker subscriptions are
    reused across ticks (see ``_TickerCache``). The default builds a fresh
    cache per call so legacy callers continue to work; production callers
    should pass a long-lived cache and call ``cache.cleanup(ib)`` on
    session terminal transitions.
    """
    _now = now_fn or (lambda: datetime.now(timezone.utc))
    effective_ttl = DEFAULT_TICK_TTL_S if ttl_s is None else float(ttl_s)
    cache = ticker_cache if ticker_cache is not None else _TickerCache()

    def quote_fn(session_id: str) -> Optional[Decimal]:
        legs = _session_legs(session_id, db_path)
        if not legs:
            return None

        try:
            ib = ib_client_factory()
        except Exception as exc:  # noqa: BLE001
            logger.warning("quote_fn ib_client_factory failed for %s: %s", session_id, exc)
            return None

        leg_specs: list[ComboLegSpec] = []
        leg_quotes: dict[str, ComboLegQuote] = {}
        now = _now()

        for leg in legs:
            con_id = leg.get("conId") or leg.get("con_id")
            if con_id is None:
                return None

            # Citation: ib_insync/contract.py:193 — class Option(Contract)
            contract = Option(
                symbol=str(leg.get("symbol") or ""),
                lastTradeDateOrContractMonth=str(leg.get("expiry") or ""),
                strike=float(leg.get("strike") or 0.0),
                right=str(leg.get("right") or ""),
                exchange="SMART",
                currency="USD",
            )
            # Stamp the conId so the ticker cache keys correctly.
            try:
                contract.conId = int(con_id)
            except (TypeError, ValueError):
                pass

            # IBClient.get_quote wraps ib.reqMktData (NOT reqTickersAsync,
            # which hangs on index options — per feedback memory).
            # Citation: src/xenon/clients/ib_client.py:696 — get_quote.
            # The _TickerCache only calls get_quote once per conId; subsequent
            # ticks re-read the retained Ticker whose bid/ask/time fields are
            # live-updated in place by ib_insync (ib.py:1181 reqMktData).
            try:
                ticker = cache.get(ib, contract)
            except Exception as exc:  # noqa: BLE001
                logger.warning("get_quote failed for leg %s of %s: %s", con_id, session_id, exc)
                return None

            if not _ticker_is_fresh(ticker, now=now, ttl_s=effective_ttl):
                logger.debug(
                    "quote_fn: freshness gate failed for session=%s leg=%s",
                    session_id,
                    con_id,
                )
                return None

            leg_specs.append(
                ComboLegSpec(
                    contract_id=str(con_id),
                    action=str(leg.get("action", "BUY")).upper(),
                    right=str(leg.get("right", "C")).upper(),
                    strike=Decimal(str(leg.get("strike") or 0)),
                    expiry=str(leg.get("expiry") or ""),
                    quantity=int(leg.get("ratio") or 1),
                )
            )
            leg_quotes[str(con_id)] = ComboLegQuote(
                bid=Decimal(str(ticker.bid)),
                ask=Decimal(str(ticker.ask)),
            )

        try:
            combo_quote = compute_combo_quote(leg_specs, leg_quotes)
        except Exception as exc:  # noqa: BLE001
            logger.warning("compute_combo_quote failed for %s: %s", session_id, exc)
            return None

        # Return SIGNED mid — callers (wizard_stop_monitor._crossed) operate
        # in signed space per combo_quotes.signed_mid. No abs().
        return combo_quote.signed_mid

    # Attach the cache so callers can invoke cleanup on terminal session
    # transitions (ABORTED / FILLED_AND_CLOSED / PROTECTION_REFUSED).
    # e.g. `getattr(quote_fn, "ticker_cache").cleanup(ib)`.
    setattr(quote_fn, "ticker_cache", cache)
    return quote_fn
