"""Unusual flow lifecycle tracker.

When the diff engine emits an UNUSUAL_CALL_SWEEP / UNUSUAL_PUT_SWEEP change,
this module captures the dominant contract and tracks its OI + mid through
subsequent EOD snapshots, classifying anomalies (premium collapse, OI
evaporation, closing-volume spike) and closing it out when positioning unwinds
or the contract expires.

Storage: data/uw_unusual_flow_log.json (atomic writes via tmpfile + os.replace).

Spec: docs/superpowers/specs/2026-04-08-uw-analyze-overhaul-design.md
      §"Unusual flow lifecycle tracker"
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

logger = logging.getLogger("xenon.uw_analyze_flow_tracker")

# ── Tunables ────────────────────────────────────────────────────────────────
PREMIUM_COLLAPSE_FRAC = 0.60  # 60% drop in mid
PREMIUM_COLLAPSE_UNDERLYING_FRAC = 0.015  # underlying moved < 1.5%
OI_EVAPORATION_FRAC = 0.50  # 50% drop in oi
OI_EVAPORATION_DAYS = 3  # within 3 trading days of detection
CLOSING_VOLUME_OI_FRAC = 0.80  # single-day vol > 80% of OI
ANOMALY_DTE_GUARD = 3  # skip rules within 3 DTE of expiry

Side = Literal["call", "put"]
Status = Literal["open", "closed", "anomaly", "expired"]

_DEFAULT_PATH = _SCRIPTS.parent / "data" / "uw_unusual_flow_log.json"


# ── Data classes ────────────────────────────────────────────────────────────


@dataclass
class FlowInitial:
    premium_usd: float
    oi: int
    volume: int
    mid: float
    underlying_price: float


@dataclass
class FlowDailyRow:
    date: str
    oi: int
    mid: float
    underlying_price: float
    pct_change_premium: float
    volume: int = 0


@dataclass
class FlowEvent:
    id: str
    ticker: str
    side: Side
    strike: float
    expiry: str  # YYYY-MM-DD
    detected_at: str  # ISO; informational, NOT in id
    initial: FlowInitial
    daily_track: list[FlowDailyRow] = field(default_factory=list)
    status: Status = "open"
    anomaly_reason: Optional[str] = None
    closed_at: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> Optional[int]:
    f = _to_float(v)
    return int(f) if f is not None else None


def _today_iso() -> str:
    try:
        from api.services.uw_analyze_daily_job import now_et_date

        return now_et_date().isoformat()
    except Exception:  # noqa: BLE001
        return date.today().isoformat()


def make_event_id(ticker: str, side: str, strike: float, expiry: str, trade_date: str) -> str:
    # Normalize: ticker upper, side lower, strike → fixed 6-decimal float repr
    # so 900 and 900.0 collide, expiry stripped to YYYY-MM-DD.
    norm_strike = f"{float(strike):.6f}"
    norm_exp = expiry[:10] if expiry else ""
    raw = f"{ticker.upper()}|{side.lower()}|{norm_strike}|{norm_exp}|{trade_date}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _pick_dominant_alert(alerts: Iterable[dict], side: Side) -> Optional[dict]:
    """Largest total_premium alert matching `side` from td.flow_alerts."""
    best = None
    best_prem = -1.0
    for a in alerts or []:
        if not isinstance(a, dict):
            continue
        atype = (a.get("option_type") or "").lower()
        if not atype:
            atype = "call" if a.get("is_call") else ("put" if a.get("is_put") else "")
        if atype != side:
            continue
        prem = _to_float(a.get("total_premium") or a.get("premium")) or 0.0
        if prem > best_prem:
            best = a
            best_prem = prem
    return best


def _parse_alert_strike_expiry(alert: dict) -> tuple[Optional[float], Optional[str]]:
    strike = _to_float(alert.get("strike") or alert.get("strike_price"))
    expiry = alert.get("expiry") or alert.get("expiration_date") or alert.get("expiration")
    if isinstance(expiry, str) and expiry:
        return strike, expiry[:10]
    return strike, None


def _dte(expiry: str, today: Optional[date] = None) -> int:
    if today is None:
        try:
            from api.services.uw_analyze_daily_job import now_et_date

            today = now_et_date()
        except Exception:  # noqa: BLE001
            today = date.today()
    try:
        exp = date.fromisoformat(expiry)
    except ValueError:
        return 9999
    return (exp - today).days


# ── Capture from diff changes ──────────────────────────────────────────────


def capture_event_from_alert(
    *,
    ticker: str,
    side: Side,
    alert: dict,
    underlying_price: float,
    trade_date: Optional[str] = None,
) -> Optional[FlowEvent]:
    strike, expiry = _parse_alert_strike_expiry(alert)
    if strike is None or expiry is None:
        return None
    trade_date = trade_date or _today_iso()
    eid = make_event_id(ticker, side, strike, expiry, trade_date)
    return FlowEvent(
        id=eid,
        ticker=ticker.upper(),
        side=side,
        strike=strike,
        expiry=expiry,
        detected_at=datetime.now(timezone.utc).isoformat(),
        initial=FlowInitial(
            premium_usd=_to_float(alert.get("total_premium") or alert.get("premium")) or 0.0,
            oi=_to_int(alert.get("open_interest") or alert.get("oi")) or 0,
            volume=_to_int(alert.get("volume")) or 0,
            mid=_to_float(alert.get("mid") or alert.get("fill_price") or alert.get("price")) or 0.0,
            underlying_price=float(underlying_price),
        ),
    )


def capture_from_changes(
    *,
    ticker: str,
    changes: list,
    flow_alerts: Optional[list[dict]],
    underlying_price: Optional[float],
    trade_date: Optional[str] = None,
) -> list[FlowEvent]:
    """Build new FlowEvents for any UNUSUAL_*_SWEEP changes in `changes`."""
    if not changes or not flow_alerts or underlying_price is None:
        return []
    out: list[FlowEvent] = []
    sweep_codes = {"UNUSUAL_CALL_SWEEP": "call", "UNUSUAL_PUT_SWEEP": "put"}
    for ch in changes:
        code = getattr(ch, "code", None) if not isinstance(ch, dict) else ch.get("code")
        side = sweep_codes.get(code)
        if side is None:
            continue
        alert = _pick_dominant_alert(flow_alerts, side)  # type: ignore[arg-type]
        if not alert:
            continue
        ev = capture_event_from_alert(
            ticker=ticker,
            side=side,  # type: ignore[arg-type]
            alert=alert,
            underlying_price=underlying_price,
            trade_date=trade_date,
        )
        if ev:
            out.append(ev)
    return out


# ── Lifecycle progression ──────────────────────────────────────────────────


def advance_daily_track(
    event: FlowEvent,
    *,
    today: str,
    oi: int,
    mid: float,
    underlying_price: float,
    volume: int = 0,
) -> FlowEvent:
    """Append today's reading to the event's daily_track (idempotent on date)."""
    if any(row.date == today for row in event.daily_track):
        return event
    pct = 0.0
    if event.initial.mid:
        pct = (mid - event.initial.mid) / event.initial.mid
    event.daily_track.append(
        FlowDailyRow(
            date=today,
            oi=oi,
            mid=mid,
            underlying_price=underlying_price,
            pct_change_premium=pct,
            volume=volume,
        )
    )
    return event


def classify_anomaly(event: FlowEvent, *, today: Optional[date] = None) -> Optional[str]:
    """Return the anomaly reason string, or None.

    Skipped within 3 DTE of expiry to avoid late-cycle decay false positives.
    """
    if not event.daily_track:
        return None
    if _dte(event.expiry, today) <= ANOMALY_DTE_GUARD:
        return None

    latest = event.daily_track[-1]

    # Rule 1: premium collapse
    if event.initial.mid:
        mid_drop = (event.initial.mid - latest.mid) / event.initial.mid
        if event.initial.underlying_price:
            u_move = abs(latest.underlying_price - event.initial.underlying_price) / event.initial.underlying_price
        else:
            u_move = 1.0
        if mid_drop >= PREMIUM_COLLAPSE_FRAC and u_move < PREMIUM_COLLAPSE_UNDERLYING_FRAC:
            pct = int(mid_drop * 100)
            return f"premium collapsed -{pct}% (positioning unwound)"

    # Rule 2: OI evaporation within 3 trading days
    try:
        detected = datetime.fromisoformat(event.detected_at).date()
    except ValueError:
        detected = date.today()
    try:
        from api.services.uw_analyze_daily_job import trading_days_between

        days_since = trading_days_between(detected, date.fromisoformat(latest.date))
    except Exception:  # noqa: BLE001
        days_since = (date.fromisoformat(latest.date) - detected).days
    if 0 <= days_since <= OI_EVAPORATION_DAYS and event.initial.oi > 0:
        oi_drop = (event.initial.oi - latest.oi) / event.initial.oi
        if oi_drop >= OI_EVAPORATION_FRAC:
            pct = int(oi_drop * 100)
            return f"OI evaporated -{pct}% within {days_since}d"

    # Rule 3: closing volume spike — single day volume > 80% of current OI.
    # Skipped implicitly by the DTE guard at the top.
    if latest.oi > 0 and latest.volume > 0:
        ratio = latest.volume / latest.oi
        if ratio >= CLOSING_VOLUME_OI_FRAC:
            return f"closing volume spike: {int(ratio * 100)}% of OI traded in one day"

    return None


def maybe_close_or_expire(event: FlowEvent, *, today: Optional[date] = None) -> FlowEvent:
    if today is None:
        try:
            from api.services.uw_analyze_daily_job import now_et_date

            today = now_et_date()
        except Exception:  # noqa: BLE001
            today = date.today()
    # Expired?
    try:
        if date.fromisoformat(event.expiry) < today:
            event.status = "expired"
            event.closed_at = today.isoformat()
            return event
    except ValueError:
        pass
    # OI returned to ≤ initial → closed
    if event.daily_track and event.initial.oi:
        latest = event.daily_track[-1]
        if latest.oi < event.initial.oi:
            event.status = "closed"
            event.closed_at = today.isoformat()
    return event


def progress_event(
    event: FlowEvent,
    *,
    today: str,
    oi: int,
    mid: float,
    underlying_price: float,
    volume: int = 0,
) -> FlowEvent:
    """End-to-end one-step progression for the daily cron."""
    advance_daily_track(event, today=today, oi=oi, mid=mid, underlying_price=underlying_price, volume=volume)
    # Classify anomaly — only set status=anomaly once, preserve reason.
    reason = classify_anomaly(event)
    if reason and event.anomaly_reason is None:
        event.anomaly_reason = reason
        if event.status == "open":
            event.status = "anomaly"
    # Always check close/expire — anomaly is informational, not terminal.
    if event.status in ("open", "anomaly"):
        try:
            today_d = date.fromisoformat(today)
        except ValueError:
            today_d = None
        maybe_close_or_expire(event, today=today_d)
    return event


# ── Persistence ────────────────────────────────────────────────────────────


class FlowLog:
    """Thin file-backed dict-of-events with atomic writes."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else _DEFAULT_PATH
        self._events: dict[str, FlowEvent] = {}
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except Exception as exc:  # noqa: BLE001
            logger.warning("uw_unusual_flow_log corrupt — starting empty: %s", exc)
            return
        events = raw.get("events") if isinstance(raw, dict) else None
        if not isinstance(events, dict):
            return
        for eid, payload in events.items():
            try:
                self._events[eid] = _event_from_dict(payload)
            except Exception as exc:  # noqa: BLE001
                logger.warning("skipping malformed flow event %s: %s", eid, exc)

    def all(self) -> list[FlowEvent]:
        self.load()
        return list(self._events.values())

    def for_ticker(self, ticker: str) -> list[FlowEvent]:
        self.load()
        t = ticker.upper()
        return [e for e in self._events.values() if e.ticker == t]

    def upsert(self, event: FlowEvent) -> bool:
        """Insert or update by id. Returns True if newly added."""
        self.load()
        new = event.id not in self._events
        if new:
            self._events[event.id] = event
        else:
            existing = self._events[event.id]
            # Preserve existing daily_track + status if already past 'open'
            event.daily_track = existing.daily_track
            event.status = existing.status if existing.status != "open" else event.status
            event.anomaly_reason = existing.anomaly_reason
            event.closed_at = existing.closed_at
            self._events[event.id] = event
        return new

    def replace(self, event: FlowEvent) -> None:
        self.load()
        self._events[event.id] = event

    def save(self) -> None:
        self.load()
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "events": {eid: ev.to_dict() for eid, ev in self._events.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(prefix=".uw_unusual_flow_log_", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(tmp_fd, "w") as fh:
                json.dump(payload, fh, indent=2, default=str)
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def _event_from_dict(d: dict) -> FlowEvent:
    init = d.get("initial") or {}
    track_rows = d.get("daily_track") or []
    return FlowEvent(
        id=d["id"],
        ticker=d["ticker"],
        side=d["side"],
        strike=float(d["strike"]),
        expiry=d["expiry"],
        detected_at=d["detected_at"],
        initial=FlowInitial(
            premium_usd=float(init.get("premium_usd", 0)),
            oi=int(init.get("oi", 0)),
            volume=int(init.get("volume", 0)),
            mid=float(init.get("mid", 0)),
            underlying_price=float(init.get("underlying_price", 0)),
        ),
        daily_track=[
            FlowDailyRow(
                date=r["date"],
                oi=int(r["oi"]),
                mid=float(r["mid"]),
                underlying_price=float(r.get("underlying_price", 0)),
                pct_change_premium=float(r.get("pct_change_premium", 0)),
                volume=int(r.get("volume", 0)),
            )
            for r in track_rows
        ],
        status=d.get("status", "open"),
        anomaly_reason=d.get("anomaly_reason"),
        closed_at=d.get("closed_at"),
    )
