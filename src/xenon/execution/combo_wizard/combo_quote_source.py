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

import json
import logging
import math
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

# ib_insync imports. Citations:
#   ib_insync/contract.py:193 — class Option(Contract)
from ib_insync import Option  # type: ignore

from xenon.execution import orders_store

from .combo_quotes import compute_combo_quote
from .models import ComboLegQuote, ComboLegSpec

logger = logging.getLogger(__name__)

DEFAULT_TICK_TTL_S = 30.0


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
    """Load the session's legs from the wizard_sessions payload_json."""
    con = orders_store._connect_utc(orders_store._resolve_path(db_path))
    try:
        row = con.execute(
            "SELECT payload_json FROM wizard_sessions WHERE session_id = ?",
            [session_id],
        ).fetchone()
    finally:
        con.close()
    if row is None or not row[0]:
        return []
    payload = json.loads(row[0])
    return list(payload.get("legs") or [])


def build_default_quote_fn(
    ib_client_factory: Callable[[], Any],
    *,
    db_path: Path | str | None = None,
    ttl_s: float = DEFAULT_TICK_TTL_S,
    now_fn: Callable[[], datetime] | None = None,
) -> Callable[[str], Optional[Decimal]]:
    """Build the ``quote_fn(session_id)`` that the wizard stop monitor calls.

    Returns a signed Decimal combo mid, or ``None`` if any freshness gate
    fails for any leg (fail-closed — a stale tick must NOT fire an alert).
    """
    _now = now_fn or (lambda: datetime.now(timezone.utc))

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

            # IBClient.get_quote wraps ib.reqMktData (NOT reqTickersAsync,
            # which hangs on index options — per feedback memory).
            # Citation: src/xenon/clients/ib_client.py:696 — get_quote.
            try:
                ticker = ib.get_quote(contract, snapshot=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning("get_quote failed for leg %s of %s: %s", con_id, session_id, exc)
                return None

            if not _ticker_is_fresh(ticker, now=now, ttl_s=ttl_s):
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

    return quote_fn
