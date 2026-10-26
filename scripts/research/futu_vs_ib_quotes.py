#!/usr/bin/env python3
"""Futu vs IB quote calibration (X7-0, plan v2.1 section D8). Read-only research script.

Samples Futu (real QUOTE + ORDER_BOOK subscriptions on OpenD) and IB (through the
xenon query API, no direct IB connection) side by side for the same instruments,
writes raw samples as JSONL and a markdown summary under docs/research/.

Read-only by construction: no orders, no trade context, no database. It writes
only the JSONL file and the markdown summary. Real Futu subscriptions use the
shared OpenD quota, so run it while `total_used` is low (plan G7).

Examples:
    uv run python scripts/research/futu_vs_ib_quotes.py --dry-run \
        --stocks AAPL,SPY --options AAPL_20261120_200_C,AAPL_20261120_210_C \
        --vertical AAPL_20261120_200_C:AAPL_20261120_210_C
    XENON_QUERY_API_KEY=... uv run python scripts/research/futu_vs_ib_quotes.py \
        --stocks AAPL,SPY,QQQ,NVDA,TSLA --options ... --vertical ... --minutes 10

Definitions used in the summary (all measured, none assumed):
- Price gaps are in cents (dollars x 100). "within 1 cent" and "within 5 cents" are
  policy tolerances chosen for this report, not the instrument's tick.
- Matched time: at each IB sample time t the Futu value is the latest Futu push
  whose receipt time is at or before t. Futu age is t minus that receipt time.
- Greek ratio = Futu value / IB value, where the Futu IV is divided by 100 first.
  A factor that converts Futu units to IB units is the inverse of the median ratio.
- Spread mid = sum of per-leg signed mids (long +, short -). For each vertical three
  spreads are compared with the all-IB spread: all-Futu, Futu long with IB short, and
  IB long with Futu short. Legs are sampled serially, each at its own IB response time.
- IB request duration = response time minus request time of one IB sample. The time at
  which IB acquired the value inside that interval is not recorded.
- `--from-jsonl PATH` rebuilds every metric and the markdown from a stored JSONL, offline.
- The IB endpoints give bid/ask (and greeks for options) but no last price, so a
  Futu last can only be compared with the IB mid; this is labelled as such.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from xenon.utils.symbol_norm import futu_to_ib, ib_to_futu  # noqa: E402  (pure module, no SDK imports)

ET = ZoneInfo("America/New_York")
IB_LINE_LIMIT = 11  # plan D8: the IB line budget is shared with prod
UNSUBSCRIBE_MIN_AGE_S = 60.0  # Futu forbids unsubscribe sooner than 60 s after subscribe
GREEK_FIELDS = ("iv", "delta", "gamma", "vega", "theta")
_OPTION_KEY_RE = re.compile(r"^([A-Z][A-Z0-9]*)_(\d{8})_(\d+(?:\.\d+)?)_([CP])$")
_STOCK_RE = re.compile(r"^[A-Z][A-Z0-9.]*$")


# ---------------------------------------------------------------------------
# Pure parts: instruments, code mapping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OptionKey:
    symbol: str
    expiry: str  # YYYYMMDD
    strike: Decimal
    right: str  # C | P

    @property
    def key(self) -> str:
        return f"{self.symbol}_{self.expiry}_{_strike_text(self.strike)}_{self.right}"


def _strike_text(strike: Decimal) -> str:
    text = format(strike.normalize(), "f")
    return text


def parse_option_key(key: str) -> OptionKey:
    """`SYM_YYYYMMDD_STRIKE_RIGHT` (relay key form) -> OptionKey. Raises ValueError."""
    m = _OPTION_KEY_RE.match(key.strip().upper())
    if not m:
        raise ValueError(f"option key {key!r} is not SYM_YYYYMMDD_STRIKE_RIGHT")
    symbol, expiry, strike_s, right = m.groups()
    try:
        datetime.strptime(expiry, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"option key {key!r} has an invalid expiry") from exc
    try:
        strike = Decimal(strike_s)
    except InvalidOperation as exc:  # pragma: no cover - regex already limits the text
        raise ValueError(f"option key {key!r} has an invalid strike") from exc
    milli = strike * 1000
    if strike <= 0 or milli != milli.to_integral_value():
        raise ValueError(f"option key {key!r}: strike must be positive with at most 3 decimals")
    return OptionKey(symbol, expiry, strike, right)


def futu_stock_code(symbol: str) -> str:
    sym = symbol.strip().upper()
    if not _STOCK_RE.match(sym):
        raise ValueError(f"stock symbol {symbol!r} is not valid")
    return ib_to_futu({"kind": "STK", "symbol": sym, "market": "US"})


def futu_option_code(opt: OptionKey) -> str:
    """Same rule as `xenon.execution.futu_orders.futu_code_for` (OPT branch):
    `US.<UNDERLYING><YYMMDD><C|P><strike x 1000>`, no zero padding. A local copy,
    because importing futu_orders pulls in the orders store. Verified by a
    round trip through `futu_to_ib`; raises ValueError when it does not round-trip."""
    code = f"US.{opt.symbol}{opt.expiry[2:]}{opt.right}{int(opt.strike * 1000)}"
    back = futu_to_ib(code)
    if (
        back.get("kind") != "OPT"
        or back.get("symbol") != opt.symbol
        or back.get("expiry") != opt.expiry
        or back.get("right") != opt.right
        or Decimal(str(back.get("strike"))) != opt.strike
    ):
        raise ValueError(f"option code {code!r} does not round-trip for {opt.key}")
    return code


@dataclass(frozen=True)
class Vertical:
    long_key: str
    short_key: str

    @property
    def name(self) -> str:
        return f"{self.long_key}:{self.short_key}"


def parse_vertical(text: str) -> Vertical:
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError(f"--vertical {text!r} must be LONGKEY:SHORTKEY")
    long_o, short_o = parse_option_key(parts[0]), parse_option_key(parts[1])
    if long_o.key == short_o.key:
        raise ValueError(f"--vertical {text!r} has the same leg twice")
    return Vertical(long_o.key, short_o.key)


@dataclass(frozen=True)
class Instrument:
    key: str  # IB-style display key: AAPL or AAPL_20261120_200_C
    kind: str  # STK | OPT
    futu_code: str
    option: OptionKey | None = None


def build_instruments(stocks: Sequence[str], options: Sequence[str], verticals: Sequence[Vertical]) -> list[Instrument]:
    """Stocks, then options (explicit plus vertical legs), de-duplicated, order kept."""
    out: list[Instrument] = []
    seen: set[str] = set()
    for s in stocks:
        sym = s.strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            out.append(Instrument(sym, "STK", futu_stock_code(sym)))
    opt_keys = [o.strip() for o in options if o.strip()]
    for v in verticals:
        opt_keys += [v.long_key, v.short_key]
    for k in opt_keys:
        opt = parse_option_key(k)
        if opt.key not in seen:
            seen.add(opt.key)
            out.append(Instrument(opt.key, "OPT", futu_option_code(opt), opt))
    return out


# ---------------------------------------------------------------------------
# Pure parts: cents, mids, ratios, stats, matched-time join
# ---------------------------------------------------------------------------


def diff_cents(a: float | None, b: float | None) -> float | None:
    """(a - b) in cents (dollars x 100), None if either side is missing."""
    if a is None or b is None:
        return None
    return round((a - b) * 100.0, 6)


def mid_of(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2.0


def combo_mid(legs: Sequence[tuple[int, float | None]]) -> float | None:
    """Sum of signed per-leg mids (sign +1 long, -1 short). None if any leg mid is missing."""
    total = 0.0
    for sign, leg_mid in legs:
        if leg_mid is None:
            return None
        total += sign * leg_mid
    return round(total, 10)


def to_float(value: Any) -> float | None:
    """Futu cells can be 'N/A', None, NaN or lists: only finite numbers pass."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (list, tuple, dict, str)):
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                return None
        else:
            return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def greek_ratio(field: str, futu_value: float | None, ib_value: float | None) -> float | None:
    """Futu / IB, the Futu IV divided by 100 first. None if a side is missing or IB is 0."""
    if futu_value is None or ib_value is None or ib_value == 0:
        return None
    f = futu_value / 100.0 if field == "iv" else futu_value
    return f / ib_value


def quantile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolation quantile of unsorted values; None when empty."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def stat_summary(values: Iterable[float | None]) -> dict[str, Any]:
    """n, median, q1, q3 (the IQR) of the non-missing values."""
    vals = [v for v in values if v is not None]
    return {
        "n": len(vals),
        "median": quantile(vals, 0.5),
        "q1": quantile(vals, 0.25),
        "q3": quantile(vals, 0.75),
    }


WITHIN_CENTS = (1, 5)  # policy tolerances of this report, not the instrument's tick


def abs_summary(values: Iterable[float | None], withins: Sequence[int] = WITHIN_CENTS) -> dict[str, Any]:
    """n, median/p90/max of |value| and, per threshold, the share with |value| <= threshold."""
    vals = [abs(v) for v in values if v is not None]
    return {
        "n": len(vals),
        "median": quantile(vals, 0.5),
        "p90": quantile(vals, 0.9),
        "max": max(vals) if vals else None,
        "within": {w: ((sum(1 for v in vals if v <= w + 1e-9) / len(vals)) if vals else None) for w in withins},
    }


class Series:
    """Time-ordered pushes of one stream. `at(t)` is the latest push with ts <= t."""

    def __init__(self) -> None:
        self._ts: list[float] = []
        self._rows: list[dict[str, Any]] = []

    def add(self, ts: float, row: dict[str, Any]) -> None:
        # pushes arrive in order on one SDK thread; keep the invariant if they do not
        i = bisect_right(self._ts, ts)
        self._ts.insert(i, ts)
        self._rows.insert(i, row)

    def at(self, t: float) -> tuple[float, dict[str, Any]] | None:
        i = bisect_right(self._ts, t)
        if i == 0:
            return None
        return self._ts[i - 1], self._rows[i - 1]

    def __len__(self) -> int:
        return len(self._ts)

    @property
    def timestamps(self) -> list[float]:
        return list(self._ts)

    @property
    def rows(self) -> list[dict[str, Any]]:
        return list(self._rows)


def et_wall_to_epoch(data_date: Any, data_time: Any) -> float | None:
    """Futu `data_date` YYYY-MM-DD + `data_time` HH:MM:SS (US wall clock, ET) -> epoch seconds."""
    if not data_date or not data_time:
        return None
    text = f"{str(data_date).strip()} {str(data_time).strip()}"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=ET).timestamp()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Pure parts: comparison and summary
# ---------------------------------------------------------------------------


def compare_sample(
    inst: Instrument,
    sample: Mapping[str, Any],
    quotes: Series | None,
    books: Series | None,
) -> dict[str, Any]:
    """One comparison row for one IB sample (`sample["t"]` is the matched time)."""
    t = float(sample["t"])
    ib_bid, ib_ask = to_float(sample.get("bid")), to_float(sample.get("ask"))
    ib_mid = mid_of(ib_bid, ib_ask)
    t_request = to_float(sample.get("t_request"))

    q = quotes.at(t) if quotes is not None else None
    b = books.at(t) if books is not None else None
    q_row = q[1] if q else {}
    b_row = b[1] if b else {}
    f_bid, f_ask = to_float(b_row.get("bid")), to_float(b_row.get("ask"))
    f_mid = mid_of(f_bid, f_ask)
    f_last = to_float(q_row.get("last_price"))
    q_src = et_wall_to_epoch(q_row.get("data_date"), q_row.get("data_time")) if q else None

    row: dict[str, Any] = {
        "key": inst.key,
        "kind": inst.kind,
        "cycle": sample.get("cycle"),
        "t": t,
        "ib_error": sample.get("error"),
        "ib_bid": ib_bid,
        "ib_ask": ib_ask,
        "ib_mid": ib_mid,
        "f_bid": f_bid,
        "f_ask": f_ask,
        "f_mid": f_mid,
        "f_last": f_last,
        "d_bid": diff_cents(f_bid, ib_bid),
        "d_ask": diff_cents(f_ask, ib_ask),
        "d_mid": diff_cents(f_mid, ib_mid),
        "d_last_vs_ib_mid": diff_cents(f_last, ib_mid),
        "ib_req_s": (t - t_request) if t_request is not None else None,
        "quote_age_s": (t - q[0]) if q else None,
        "quote_src_age_s": (t - q_src) if q_src is not None else None,
        "book_age_s": (t - b[0]) if b else None,
    }
    ib_greeks = sample.get("greeks") or {}
    ib_map = {"iv": "impliedVol", "delta": "delta", "gamma": "gamma", "vega": "vega", "theta": "theta"}
    f_map = {"iv": "implied_volatility", "delta": "delta", "gamma": "gamma", "vega": "vega", "theta": "theta"}
    for field in GREEK_FIELDS:
        row[f"r_{field}"] = (
            greek_ratio(field, to_float(q_row.get(f_map[field])), to_float(ib_greeks.get(ib_map[field])))
            if inst.kind == "OPT"
            else None
        )
    return row


ORIENTATIONS = (
    ("all_futu", "all-Futu"),
    ("fl_is", "Futu long / IB short"),
    ("il_fs", "IB long / Futu short"),
)


def combo_gaps(verticals: Sequence[Vertical], rows: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Per vertical and per IB cycle: the all-IB spread, the three Futu-based spreads and each gap
    to the all-IB spread in cents. A cycle is valid only when all four leg mids exist; otherwise
    every gap is None. `sep_s` is the time between the two IB leg responses (legs are sampled serially)."""
    by_key_cycle: dict[tuple[str, Any], Mapping[str, Any]] = {(r["key"], r["cycle"]): r for r in rows}
    cycles = sorted({r["cycle"] for r in rows if r.get("cycle") is not None})
    out: dict[str, list[dict[str, Any]]] = {}
    for v in verticals:
        series: list[dict[str, Any]] = []
        for c in cycles:
            long_r, short_r = by_key_cycle.get((v.long_key, c)), by_key_cycle.get((v.short_key, c))
            if long_r is None or short_r is None:
                continue
            il, is_, fl, fs = long_r["ib_mid"], short_r["ib_mid"], long_r["f_mid"], short_r["f_mid"]
            complete = None not in (il, is_, fl, fs)
            spreads = {
                "ib_spread": combo_mid([(+1, il), (-1, is_)]),
                "all_futu": combo_mid([(+1, fl), (-1, fs)]),
                "fl_is": combo_mid([(+1, fl), (-1, is_)]),
                "il_fs": combo_mid([(+1, il), (-1, fs)]),
            }
            entry: dict[str, Any] = {"cycle": c, **spreads, "sep_s": abs(float(long_r["t"]) - float(short_r["t"]))}
            for o, _label in ORIENTATIONS:
                entry[f"gap_{o}_c"] = diff_cents(spreads[o], spreads["ib_spread"]) if complete else None
            series.append(entry)
        out[v.name] = series
    return out


def combo_summary_of(combos: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, series in combos.items():
        valid = [s for s in series if s["gap_all_futu_c"] is not None]
        seps = [s["sep_s"] for s in valid]
        out[name] = {
            "cycles": len(series),
            "pairs": len(valid),
            "orient": {
                o: {
                    "gap": stat_summary(s[f"gap_{o}_c"] for s in series),
                    "gap_abs": abs_summary(s[f"gap_{o}_c"] for s in series),
                }
                for o, _label in ORIENTATIONS
            },
            "sep": {
                "n": len(seps),
                "min": min(seps) if seps else None,
                "median": quantile(seps, 0.5),
                "max": max(seps) if seps else None,
            },
        }
    return out


def book_cadence(book_ts: Sequence[float], window_start: float, window_end: float) -> dict[str, Any]:
    """Push count and pushes per minute over the window, and the age at window end of the latest
    push received at or before window end (a push after the window never counts)."""
    inside = [t for t in book_ts if window_start <= t <= window_end]
    minutes = max((window_end - window_start) / 60.0, 1e-9)
    upto = [t for t in book_ts if t <= window_end]
    last = max(upto) if upto else None
    return {
        "pushes": len(inside),
        "per_min": len(inside) / minutes,
        "last_age_s": (window_end - last) if last is not None else None,
    }


def analyze(
    instruments: Sequence[Instrument],
    verticals: Sequence[Vertical],
    quotes: Mapping[str, Series],
    books: Mapping[str, Series],
    ib_samples: Sequence[Mapping[str, Any]],
    window: tuple[float, float],
) -> dict[str, Any]:
    by_key = {i.key: i for i in instruments}
    rows = [
        compare_sample(
            by_key[s["key"]], s, quotes.get(by_key[s["key"]].futu_code), books.get(by_key[s["key"]].futu_code)
        )
        for s in ib_samples
        if s["key"] in by_key
    ]
    per: dict[str, dict[str, Any]] = {}
    for inst in instruments:
        mine = [r for r in rows if r["key"] == inst.key]
        q = quotes.get(inst.futu_code)
        b = books.get(inst.futu_code)
        entry: dict[str, Any] = {
            "kind": inst.kind,
            "futu_code": inst.futu_code,
            "ib_samples": len(mine),
            "ib_errors": sum(1 for r in mine if r["ib_error"]),
            "pairs": sum(1 for r in mine if r["d_mid"] is not None),
            "ib_req": {
                "n": sum(1 for r in mine if r["ib_req_s"] is not None),
                "median": quantile([r["ib_req_s"] for r in mine if r["ib_req_s"] is not None], 0.5),
                "p90": quantile([r["ib_req_s"] for r in mine if r["ib_req_s"] is not None], 0.9),
            },
            "bid": stat_summary(r["d_bid"] for r in mine),
            "ask": stat_summary(r["d_ask"] for r in mine),
            "mid": stat_summary(r["d_mid"] for r in mine),
            "mid_abs": abs_summary(r["d_mid"] for r in mine),
            "last_vs_ib_mid": stat_summary(r["d_last_vs_ib_mid"] for r in mine),
            "quote_age": stat_summary(r["quote_age_s"] for r in mine),
            "quote_age_p90": quantile([r["quote_age_s"] for r in mine if r["quote_age_s"] is not None], 0.9),
            "quote_src_age": stat_summary(r["quote_src_age_s"] for r in mine),
            "book_age": stat_summary(r["book_age_s"] for r in mine),
            "book_age_p90": quantile([r["book_age_s"] for r in mine if r["book_age_s"] is not None], 0.9),
            "quote_pushes": len(q) if q else 0,
            "book_pushes": len(b) if b else 0,
            "book_cadence": book_cadence(b.timestamps if b else [], *window),
            "quote_cadence": book_cadence(q.timestamps if q else [], *window),
        }
        if inst.kind == "OPT":
            entry["greeks"] = {f: stat_summary(r[f"r_{f}"] for r in mine) for f in GREEK_FIELDS}
        if q:
            ov = [r.get("overnight_price") for r in q.rows]
            entry["overnight"] = {
                "pushes_with_overnight_price": sum(1 for v in ov if to_float(v) is not None),
                "last_overnight_price": next((to_float(v) for v in reversed(ov) if to_float(v) is not None), None),
                "last_overnight_volume": next(
                    (
                        to_float(r.get("overnight_volume"))
                        for r in reversed(q.rows)
                        if to_float(r.get("overnight_volume")) is not None
                    ),
                    None,
                ),
            }
        per[inst.key] = entry
    agg_greeks = {f: stat_summary(r[f"r_{f}"] for r in rows if r["kind"] == "OPT") for f in GREEK_FIELDS}
    combos = combo_gaps(verticals, rows)
    return {
        "rows": rows,
        "per": per,
        "greeks": agg_greeks,
        "combos": combos,
        "combo_summary": combo_summary_of(combos),
    }


def quota_units(snapshots: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Units per subscription step from consecutive `query_subscription` snapshots.
    Each snapshot: label, n_ok (instruments subscribed OK in the step before it), own_used, total_used."""
    out: list[dict[str, Any]] = []
    for prev, cur in zip(snapshots, snapshots[1:], strict=False):
        n_ok = cur.get("n_ok") or 0
        d_own = _delta(cur.get("own_used"), prev.get("own_used"))
        d_tot = _delta(cur.get("total_used"), prev.get("total_used"))
        out.append(
            {
                "step": cur.get("label"),
                "n_ok": n_ok,
                "d_own": d_own,
                "d_total": d_tot,
                "own_per_instrument": (d_own / n_ok) if (d_own is not None and n_ok) else None,
                "total_per_instrument": (d_tot / n_ok) if (d_tot is not None and n_ok) else None,
            }
        )
    return out


def _delta(a: Any, b: Any) -> float | None:
    fa, fb = to_float(a), to_float(b)
    return None if fa is None or fb is None else fa - fb


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _n(value: Any, digits: int = 3) -> str:
    f = to_float(value)
    return "n/a" if f is None else f"{f:.{digits}f}"


def _iqr(s: Mapping[str, Any], digits: int = 3) -> str:
    if not s or not s.get("n"):
        return "n/a"
    return f"{_n(s['median'], digits)} [{_n(s['q1'], digits)}, {_n(s['q3'], digits)}] (n={s['n']})"


def _pct(value: Any) -> str:
    f = to_float(value)
    return "n/a" if f is None else f"{f * 100:.0f}%"


def _et(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ET).strftime("%Y-%m-%d %H:%M:%S %Z")


def render_markdown(meta: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    """Summary doc. `meta` carries the run facts (see `run`); `result` is `analyze()` output.
    Every number is printed from the run data; the Decisions section is a stub for the lead."""
    L: list[str] = []
    session = meta["session"]
    L.append(f"# Futu vs IB quotes calibration ({meta['date_et']}, {session})")
    L.append("")
    L.append(
        "Source: `scripts/research/futu_vs_ib_quotes.py` (X7-0, plan v2.1 section D8). "
        "Every number below was observed in this run; nothing is estimated or carried over from another run. "
        "n/a means no matched data."
    )
    L.append("")
    L.append("## Run")
    L.append("")
    L.append(f"- Window (ET): {_et(meta['window_start'])} to {_et(meta['window_end'])}")
    L.append(f"- Window bounds: {meta.get('window_note') or 'recorded by the run'}")
    L.append(
        f"- Session mode: `{session}`; requested minutes: {meta['minutes']}; IB poll interval: {meta['ib_interval']} s; IB cycles: {meta['ib_cycles']}"
    )
    L.append(f"- OpenD: `{meta['opend']}`; xenon API: `{meta['api_url']}`")
    counts = meta.get("raw_counts")
    L.append(
        f"- Raw samples: `{meta['jsonl']}`"
        + (
            f" (gitignored; {counts['book']} book pushes, {counts['quote']} quote pushes, {counts['ib']} IB samples)"
            if counts
            else ""
        )
    )
    L.append(
        '- Units: price gaps are in cents (dollars x 100). "within 1¢" and "within 5¢" are policy tolerance, not the instrument\'s tick; the instrument and combo price increments were not verified.'
    )
    L.append(
        '- Matched time: Futu value = latest Futu push with receipt time at or before the IB sample time. IB `last` is not exposed by the IB endpoints, so "Futu last - IB mid" is only a coarse indicator.'
    )
    L.append("")
    L.append("## Instruments")
    L.append("")
    L.append("| Key | Kind | Futu code |")
    L.append("| --- | --- | --- |")
    for key, p in result["per"].items():
        L.append(f"| {key} | {p['kind']} | `{p['futu_code']}` |")
    for v in meta.get("verticals", []):
        L.append("")
        L.append(f"- Vertical (long:short): `{v}`")
    L.append("")
    L.append("## Quota units measured")
    L.append("")
    L.append(
        "Differences of `query_subscription(is_all_conn=True)` between steps. `own` is this connection, `total` is all connections (argon shares the account, so `total` can move for other reasons)."
    )
    L.append("")
    L.append("| Step | OK subscribes | d own_used | d total_used | own per instrument | total per instrument |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    for q in meta["quota"]:
        L.append(
            f"| {q['step']} | {q['n_ok']} | {_n(q['d_own'], 0)} | {_n(q['d_total'], 0)} | {_n(q['own_per_instrument'], 2)} | {_n(q['total_per_instrument'], 2)} |"
        )
    snaps = meta["snapshots"]
    if snaps:
        L.append("")
        L.append(
            "Raw snapshots: "
            + "; ".join(
                f"{s['label']}: own_used={s.get('own_used')}, total_used={s.get('total_used')}, remain={s.get('remain')}"
                for s in snaps
            )
        )
    L.append("")
    L.append("## Subscribe results and refusals")
    L.append("")
    refusals = [r for r in meta["subscribe_results"] if r["error"]]
    L.append(f"- Subscribe attempts: {len(meta['subscribe_results'])}; refused: {len(refusals)}")
    if refusals:
        L.append("")
        L.append("| Code | Subtype | Refusal message (verbatim) |")
        L.append("| --- | --- | --- |")
        for r in refusals:
            L.append(f"| `{r['code']}` | {r['subtype']} | {str(r['error']).replace('|', '/')} |")
    unsub_err = [r for r in meta.get("unsubscribe_results", []) if r["error"]]
    L.append(f"- Unsubscribe attempts: {len(meta.get('unsubscribe_results', []))}; failed: {len(unsub_err)}")
    for r in unsub_err:
        L.append(f"  - `{r['code']}` {r['subtype']}: {str(r['error'])}")
    L.append("")
    L.append("## Agreement at matched times (cents, Futu minus IB)")
    L.append("")
    L.append(
        "Signed columns show median [Q1, Q3] (n). `|mid|` columns are absolute. 1¢ and 5¢ are policy tolerance, not the instrument's tick."
    )
    L.append("")
    L.append(
        "| Key | IB samples | IB errors | pairs | bid | ask | mid | median abs mid | p90 abs mid | max abs mid | within 1¢ | within 5¢ | Futu last - IB mid |"
    )
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for key, p in result["per"].items():
        a = p["mid_abs"]
        L.append(
            f"| {key} | {p['ib_samples']} | {p['ib_errors']} | {p['pairs']} | {_iqr(p['bid'], 2)} | {_iqr(p['ask'], 2)} | {_iqr(p['mid'], 2)} | "
            f"{_n(a['median'], 2)} | {_n(a['p90'], 2)} | {_n(a['max'], 2)} | {_pct(a['within'][1])} | {_pct(a['within'][5])} | {_iqr(p['last_vs_ib_mid'], 2)} |"
        )
    L.append("")
    L.append("## IB request duration")
    L.append("")
    L.append(
        "Per IB sample: response time minus request time, in seconds. The time at which IB acquired the quoted value inside that interval is not recorded, so this table does not say which source is fresher."
    )
    L.append("")
    L.append("| Key | IB samples with request time | median (s) | p90 (s) |")
    L.append("| --- | --- | --- | --- |")
    for key, p in result["per"].items():
        r = p["ib_req"]
        L.append(f"| {key} | {r['n']} | {_n(r['median'], 3)} | {_n(r['p90'], 3)} |")
    L.append("")
    L.append("## Futu freshness and push cadence")
    L.append("")
    L.append(
        "Ages in seconds at each IB sample time. BOOK last age at end uses the latest push received at or before the window end. Quote source age is the QUOTE `data_date`/`data_time` (ET wall clock) against the sample time; ORDER_BOOK has no source time, so its age is receipt time."
    )
    L.append("")
    L.append(
        "| Key | QUOTE pushes | QUOTE pushes/min | QUOTE receipt age median / p90 | QUOTE source age median | BOOK pushes | BOOK pushes/min | BOOK last age at end (s) | BOOK age median / p90 |"
    )
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for key, p in result["per"].items():
        L.append(
            f"| {key} | {p['quote_pushes']} | {_n(p['quote_cadence']['per_min'], 2)} | {_n(p['quote_age']['median'], 2)} / {_n(p['quote_age_p90'], 2)} | "
            f"{_n(p['quote_src_age']['median'], 2)} | {p['book_pushes']} | {_n(p['book_cadence']['per_min'], 2)} | "
            f"{_n(p['book_cadence']['last_age_s'], 1)} | {_n(p['book_age']['median'], 2)} / {_n(p['book_age_p90'], 2)} |"
        )
    opts = [k for k, p in result["per"].items() if p["kind"] == "OPT"]
    if opts:
        L.append("")
        L.append("## Greek ratios (Futu / IB; Futu IV divided by 100 first)")
        L.append("")
        L.append(
            "Median [Q1, Q3] (n). A conversion factor from Futu units to IB units is the inverse of the median ratio."
        )
        L.append("")
        L.append("| Key | iv | delta | gamma | vega | theta |")
        L.append("| --- | --- | --- | --- | --- | --- |")
        for key in opts:
            g = result["per"][key]["greeks"]
            L.append(f"| {key} | " + " | ".join(_iqr(g[f], 4) for f in GREEK_FIELDS) + " |")
        g = result["greeks"]
        L.append("| **all options** | " + " | ".join(_iqr(g[f], 4) for f in GREEK_FIELDS) + " |")
    if result["combo_summary"]:
        L.append("")
        L.append("## Spread gap vs the all-IB spread (cents)")
        L.append("")
        L.append(
            "Spread = long leg mid minus short leg mid. Each Futu-based spread is compared with the all-IB spread of the same cycle: "
            "all-Futu, Futu long with IB short, and IB long with Futu short. Gap = Futu-based spread minus all-IB spread. "
            "A Futu leg value is the latest Futu push at or before that leg's own IB response time. "
            "A cycle counts as a pair only when all four leg mids exist. 1¢ and 5¢ are policy tolerance, not the instrument's tick."
        )
        L.append("")
        L.append(
            "| Vertical (long:short) | Spread source | cycles | pairs | gap median [Q1, Q3] | median abs | p90 abs | max abs | within 1¢ | within 5¢ |"
        )
        L.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for name, c in result["combo_summary"].items():
            for o, label in ORIENTATIONS:
                g = c["orient"][o]
                a = g["gap_abs"]
                L.append(
                    f"| `{name}` | {label} | {c['cycles']} | {c['pairs']} | {_iqr(g['gap'], 2)} | {_n(a['median'], 2)} | {_n(a['p90'], 2)} | {_n(a['max'], 2)} | {_pct(a['within'][1])} | {_pct(a['within'][5])} |"
                )
        L.append("")
        L.append("### IB leg-response time separation")
        L.append("")
        L.append(
            "Time between the two IB leg responses of one pair. The legs are sampled serially, so these spreads are not a simultaneous combo quote."
        )
        L.append("")
        L.append("| Vertical (long:short) | pairs | min (s) | median (s) | max (s) |")
        L.append("| --- | --- | --- | --- | --- |")
        for name, c in result["combo_summary"].items():
            sp = c["sep"]
            L.append(f"| `{name}` | {sp['n']} | {_n(sp['min'], 2)} | {_n(sp['median'], 2)} | {_n(sp['max'], 2)} |")
    if session == "overnight":
        L.append("")
        L.append("## Overnight observations (stocks)")
        L.append("")
        L.append(
            "| Key | BOOK pushes | BOOK pushes/min | BOOK last receipt age at end (s) | QUOTE pushes with overnight_price | last overnight_price | last overnight_volume |"
        )
        L.append("| --- | --- | --- | --- | --- | --- | --- |")
        for key, p in result["per"].items():
            ov = p.get("overnight", {})
            L.append(
                f"| {key} | {p['book_pushes']} | {_n(p['book_cadence']['per_min'], 2)} | {_n(p['book_cadence']['last_age_s'], 1)} | "
                f"{ov.get('pushes_with_overnight_price', 0)} | {_n(ov.get('last_overnight_price'), 4)} | {_n(ov.get('last_overnight_volume'), 0)} |"
            )
    if meta.get("push_errors"):
        L.append("")
        L.append(
            f"## Handler errors\n\n{meta['push_errors']} push(es) could not be parsed; see the raw JSONL (`kind=push_error`)."
        )
    L.append("")
    L.append("## Decisions (lead fills)")
    L.append("")
    L.append("- Greek unit factors (iv, delta, gamma, vega, theta): ")
    L.append("- Quota units per subtype (QUOTE, ORDER_BOOK; stock vs option): ")
    L.append("- ORDER_BOOK available for options under the current OpenD level: ")
    L.append("- Overnight ORDER_BOOK cadence and stale threshold (D11): ")
    L.append("- Combo mid gap verdict (D12, one source per underlying?): ")
    L.append("- Stocks flip (`XENON_QUOTES_DEFAULT=futu-first`) pass/fail: ")
    L.append("- Options flip (`XENON_FUTU_OPTIONS=1`) pass/fail: ")
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# IB side: xenon query API (read-only GET/POST of query-key-allowed paths)
# ---------------------------------------------------------------------------


class ApiClient:
    """Minimal urllib client for the xenon query API. The key is only ever sent as a header."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 45.0) -> None:
        self.base = base_url.rstrip("/")
        self._key = api_key
        self.timeout = timeout

    def call(self, method: str, path: str, params: Mapping[str, Any] | None = None, body: Any = None) -> Any:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-API-Key", self._key)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - operator-supplied URL
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise RuntimeError(f"HTTP {exc.code} {path}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"{path}: {exc}") from None


def qualify_stock(api: ApiClient, symbol: str) -> int:
    out = api.call("POST", "/contract/qualify", body={"contracts": [{"sec_type": "STK", "symbol": symbol}]})
    return int(out["contracts"][0]["conId"])


def ib_sample_stock(api: ApiClient, symbol: str, con_id: int) -> dict[str, Any]:
    out = api.call("GET", "/orders/quote", params={"ticker": symbol, "con_id": con_id})
    return {
        "bid": to_float(out.get("bid")),
        "ask": to_float(out.get("ask")),
        "bid_size": out.get("bid_size"),
        "ask_size": out.get("ask_size"),
    }  # the signed quote token is deliberately not kept


def ib_sample_option(api: ApiClient, opt: OptionKey) -> dict[str, Any]:
    out = api.call(
        "GET",
        "/options/greeks",
        params={"symbol": opt.symbol, "expiry": opt.expiry, "strike": float(opt.strike), "right": opt.right},
    )
    return {
        "bid": to_float(out.get("bid")),
        "ask": to_float(out.get("ask")),
        "greeks": out.get("greeks"),
        "con_id": out.get("conId"),
        "note": out.get("note"),
    }


# ---------------------------------------------------------------------------
# Futu side (SDK imported lazily so tests and --dry-run never need it)
# ---------------------------------------------------------------------------


class Recorder:
    """Thread-safe JSONL writer plus the in-memory series the analysis reads."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = path.open("w", encoding="utf-8")
        self._lock = threading.Lock()
        self.quotes: dict[str, Series] = {}
        self.books: dict[str, Series] = {}
        self.push_errors = 0

    def write(self, kind: str, ts: float, **fields: Any) -> None:
        rec = {
            "kind": kind,
            "ts": round(ts, 6),
            "ts_utc": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            **fields,
        }
        with self._lock:
            self._fh.write(json.dumps(rec, default=str) + "\n")
            self._fh.flush()

    def quote_push(self, ts: float, code: str, row: dict[str, Any]) -> None:
        with self._lock:
            self.quotes.setdefault(code, Series()).add(ts, row)
        self.write("futu_quote", ts, code=code, **row)

    def book_push(self, ts: float, code: str, row: dict[str, Any]) -> None:
        with self._lock:
            self.books.setdefault(code, Series()).add(ts, row)
        self.write("futu_book", ts, code=code, **row)

    def error(self, ts: float, where: str, err: str) -> None:
        with self._lock:
            self.push_errors += 1
        self.write("push_error", ts, where=where, error=err)

    def close(self) -> None:
        with self._lock:
            self._fh.close()


QUOTE_FIELDS = (
    "last_price", "data_date", "data_time", "volume",
    "implied_volatility", "delta", "gamma", "vega", "theta",
    "overnight_price", "overnight_volume",
)  # fmt: skip


def quote_row_fields(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Raw Futu QUOTE record -> JSON-safe subset. 'N/A', NaN and lists become None for numbers."""
    out: dict[str, Any] = {}
    for f in QUOTE_FIELDS:
        v = raw.get(f)
        if f in ("data_date", "data_time"):
            out[f] = None if v is None or str(v) in ("N/A", "") else str(v)
        else:
            out[f] = to_float(v)
    return out


def book_row_fields(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Futu order book push dict -> top bid/ask price and volume."""

    def top(side: Any) -> tuple[float | None, float | None]:
        if not side:
            return None, None
        first = side[0]
        return to_float(first[0]), to_float(first[1])

    bid, bid_vol = top(raw.get("Bid"))
    ask, ask_vol = top(raw.get("Ask"))
    return {"bid": bid, "bid_volume": bid_vol, "ask": ask, "ask_volume": ask_vol}


def _futu_subtype(futu: Any, name: str) -> Any:
    return getattr(futu.SubType, name)


def subscribe_all(
    ctx: Any,
    futu: Any,
    rec: Recorder,
    codes: Sequence[str],
    subtype: str,
) -> tuple[int, list[dict[str, Any]]]:
    """Per-code subscribe; a refusal is recorded as a finding, never raised. Returns (n_ok, results)."""
    results: list[dict[str, Any]] = []
    n_ok = 0
    for code in codes:
        t = time.time()
        try:
            ret, msg = ctx.subscribe([code], [_futu_subtype(futu, subtype)], is_first_push=True)
            err = None if ret == futu.RET_OK else str(msg)
        except Exception as exc:  # noqa: BLE001 - a refusal or transport error is data
            err = f"{type(exc).__name__}: {exc}"
        if err is None:
            n_ok += 1
        res = {"code": code, "subtype": subtype, "ok": err is None, "error": err, "t": t}
        results.append(res)
        rec.write("subscribe", t, code=code, subtype=subtype, ok=err is None, error=err)
    return n_ok, results


def snapshot_subscription(ctx: Any, futu: Any, rec: Recorder, label: str, n_ok: int) -> dict[str, Any]:
    t = time.time()
    try:
        ret, data = ctx.query_subscription(is_all_conn=True)
        if ret != futu.RET_OK:
            snap: dict[str, Any] = {"label": label, "n_ok": n_ok, "error": str(data)}
        else:
            snap = {
                "label": label,
                "n_ok": n_ok,
                "own_used": data.get("own_used"),
                "total_used": data.get("total_used"),
                "remain": data.get("remain"),
                "sub_list": data.get("sub_list"),
            }
    except Exception as exc:  # noqa: BLE001
        snap = {"label": label, "n_ok": n_ok, "error": f"{type(exc).__name__}: {exc}"}
    rec.write("query_subscription", t, **snap)
    return snap


def run_futu_session(
    args: argparse.Namespace, instruments: Sequence[Instrument], rec: Recorder
) -> tuple[Any, Any, list[dict[str, Any]], list[dict[str, Any]]]:
    """Connect, install handlers, subscribe QUOTE then ORDER_BOOK with quota snapshots in between.
    Returns (ctx, futu module, subscribe results, snapshots)."""
    import futu  # lazy: tests and --dry-run never import the SDK

    class QuoteHandler(futu.StockQuoteHandlerBase):  # type: ignore[misc]
        def on_recv_rsp(self, rsp_pb: Any) -> tuple:
            ret, content = super().on_recv_rsp(rsp_pb)
            ts = time.time()
            if ret == futu.RET_OK:
                try:
                    for raw in content.to_dict(orient="records"):
                        rec.quote_push(ts, str(raw.get("code")), quote_row_fields(raw))
                except Exception as exc:  # noqa: BLE001 - never kill the SDK thread
                    rec.error(ts, "quote", f"{type(exc).__name__}: {exc}")
            else:
                rec.error(ts, "quote", str(content))
            return ret, content

    class BookHandler(futu.OrderBookHandlerBase):  # type: ignore[misc]
        def on_recv_rsp(self, rsp_pb: Any) -> tuple:
            ret, content = super().on_recv_rsp(rsp_pb)
            ts = time.time()
            if ret == futu.RET_OK:
                try:
                    row = book_row_fields(content)
                    row["svr_recv_time_bid"] = content.get("svr_recv_time_bid")
                    row["svr_recv_time_ask"] = content.get("svr_recv_time_ask")
                    rec.book_push(ts, str(content.get("code")), row)
                except Exception as exc:  # noqa: BLE001
                    rec.error(ts, "book", f"{type(exc).__name__}: {exc}")
            else:
                rec.error(ts, "book", str(content))
            return ret, content

    ctx = futu.OpenQuoteContext(host=args.opend_host, port=args.opend_port)
    ctx.set_handler(QuoteHandler())
    ctx.set_handler(BookHandler())

    stock_codes = [i.futu_code for i in instruments if i.kind == "STK"]
    option_codes = [i.futu_code for i in instruments if i.kind == "OPT"]
    snaps = [snapshot_subscription(ctx, futu, rec, "before", 0)]
    results: list[dict[str, Any]] = []
    for subtype in ("QUOTE", "ORDER_BOOK"):
        for kind, codes in (("stock", stock_codes), ("option", option_codes)):
            if not codes:
                continue
            n_ok, res = subscribe_all(ctx, futu, rec, codes, subtype)
            results += res
            snaps.append(snapshot_subscription(ctx, futu, rec, f"{subtype} {kind}s", n_ok))
    return ctx, futu, results, snaps


def unsubscribe_all(ctx: Any, futu: Any, rec: Recorder, results: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Wait until every subscription is at least 60 s old, then unsubscribe what subscribed OK."""
    ok = [r for r in results if r["ok"]]
    if not ok:
        return []
    wait = UNSUBSCRIBE_MIN_AGE_S - (time.time() - max(r["t"] for r in ok)) + 1.0
    if wait > 0:
        print(
            f"waiting {wait:.0f}s before unsubscribe (Futu minimum age {UNSUBSCRIBE_MIN_AGE_S:.0f}s)", file=sys.stderr
        )
        time.sleep(wait)
    out: list[dict[str, Any]] = []
    for r in ok:
        t = time.time()
        try:
            ret, msg = ctx.unsubscribe([r["code"]], [_futu_subtype(futu, r["subtype"])])
            err = None if ret == futu.RET_OK else str(msg)
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
        out.append({"code": r["code"], "subtype": r["subtype"], "error": err})
        rec.write("unsubscribe", t, code=r["code"], subtype=r["subtype"], error=err)
    return out


# ---------------------------------------------------------------------------
# CLI and orchestration
# ---------------------------------------------------------------------------


def _split_csv(text: str) -> list[str]:
    return [p.strip() for p in text.split(",") if p.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Futu vs IB quote calibration (read-only research script).")
    p.add_argument("--stocks", default="", help="comma-separated stock symbols, e.g. AAPL,SPY")
    p.add_argument("--options", default="", help="comma-separated relay keys SYM_YYYYMMDD_STRIKE_RIGHT")
    p.add_argument(
        "--vertical",
        action="append",
        default=[],
        metavar="LONGKEY:SHORTKEY",
        help="repeatable; legs are added to --options",
    )
    p.add_argument("--minutes", type=float, default=10.0)
    p.add_argument("--ib-interval", type=float, default=30.0, help="seconds between IB sample cycles")
    p.add_argument("--api-url", default="http://127.0.0.1:8321")
    p.add_argument("--opend-host", default="127.0.0.1")
    p.add_argument("--opend-port", type=int, default=11111)
    p.add_argument("--session", choices=("rth", "overnight"), default="rth")
    p.add_argument("--out-dir", default="output/research", help="raw JSONL directory")
    p.add_argument("--doc-dir", default=str(REPO_ROOT / "docs" / "research"), help="markdown summary directory")
    p.add_argument("--dry-run", action="store_true", help="validate args and code mapping, connect to nothing")
    p.add_argument(
        "--from-jsonl",
        metavar="PATH",
        help="rebuild every metric and the markdown from a stored raw JSONL; offline, no OpenD, no API",
    )
    return p


def resolve_plan(args: argparse.Namespace) -> tuple[list[Instrument], list[Vertical]]:
    """Validate the arguments and return instruments and verticals. Raises ValueError."""
    stocks = _split_csv(args.stocks)
    options = _split_csv(args.options)
    verticals = [parse_vertical(v) for v in args.vertical]
    if args.session == "overnight" and (options or verticals):
        raise ValueError("--session overnight is stocks only: drop --options/--vertical")
    instruments = build_instruments(stocks, options, verticals)
    if not instruments:
        raise ValueError("no instruments: give --stocks and/or --options")
    if len(instruments) > IB_LINE_LIMIT:
        raise ValueError(
            f"{len(instruments)} instruments exceed the IB line budget of {IB_LINE_LIMIT} (shared with prod)"
        )
    if args.minutes <= 0 or args.ib_interval <= 0:
        raise ValueError("--minutes and --ib-interval must be positive")
    return instruments, verticals


def ib_cycle(
    api: ApiClient, instruments: Sequence[Instrument], con_ids: Mapping[str, int], cycle: int, rec: Recorder
) -> list[dict[str, Any]]:
    """One IB pass, serial (the IB data role is serialized and the line budget is shared)."""
    samples: list[dict[str, Any]] = []
    for inst in instruments:
        sample: dict[str, Any] = {"key": inst.key, "cycle": cycle}
        t0 = time.time()
        try:
            if inst.kind == "STK":
                if inst.key not in con_ids:
                    raise RuntimeError("no conId (qualify failed)")
                sample.update(ib_sample_stock(api, inst.key, con_ids[inst.key]))
            else:
                assert inst.option is not None
                sample.update(ib_sample_option(api, inst.option))
        except Exception as exc:  # noqa: BLE001 - an IB failure is data, not a crash
            sample["error"] = str(exc)
        sample["t"] = time.time()  # IB value is matched at response time
        sample["t_request"] = t0
        samples.append(sample)
        rec.write("ib_sample", sample["t"], **{k: v for k, v in sample.items() if k != "t"})
    return samples


def build_meta(
    *,
    args_view: Mapping[str, Any],
    date_et: str,
    ib_cycles: int,
    window: tuple[float, float],
    window_note: str,
    jsonl: Path | str,
    verticals: Sequence[Vertical],
    snaps: Sequence[Mapping[str, Any]],
    sub_results: Sequence[Mapping[str, Any]],
    unsub_results: Sequence[Mapping[str, Any]],
    push_errors: int,
    raw_counts: Mapping[str, int],
) -> dict[str, Any]:
    """The run facts `render_markdown` prints. Shared by a live run and `--from-jsonl`."""
    return {
        "date_et": date_et,
        "session": args_view["session"],
        "minutes": args_view["minutes"],
        "ib_interval": args_view["ib_interval"],
        "ib_cycles": ib_cycles,
        "window_start": window[0],
        "window_end": window[1],
        "window_note": window_note,
        "opend": f"{args_view['opend_host']}:{args_view['opend_port']}",
        "api_url": args_view["api_url"],
        "jsonl": str(jsonl),
        "verticals": [v.name for v in verticals],
        "quota": quota_units(snaps),
        "snapshots": [
            {k: s.get(k) for k in ("label", "own_used", "total_used", "remain")} for s in snaps if "error" not in s
        ],
        "subscribe_results": list(sub_results),
        "unsubscribe_results": list(unsub_results),
        "push_errors": push_errors,
        "raw_counts": dict(raw_counts),
    }


@dataclass
class StoredRun:
    instruments: list[Instrument]
    verticals: list[Vertical]
    quotes: dict[str, Series]
    books: dict[str, Series]
    ib_samples: list[dict[str, Any]]
    window: tuple[float, float]
    window_source: str  # "recorded" | "derived"
    meta: dict[str, Any]


_PUSH_ENVELOPE = ("kind", "ts", "ts_utc", "code")
_DERIVED_WINDOW_NOTE = (
    "derived from the raw samples (the run did not record them): start = earliest IB request time, "
    "end = time of the first `unsubscribe` record"
)


def load_jsonl(path: Path | str) -> StoredRun:
    """Rebuild everything the analysis needs from a stored raw JSONL. Offline: no OpenD, no API."""
    path = Path(path)
    meta_rec: dict[str, Any] | None = None
    window_rec: dict[str, Any] | None = None
    quotes: dict[str, Series] = {}
    books: dict[str, Series] = {}
    ib_samples: list[dict[str, Any]] = []
    snaps: list[dict[str, Any]] = []
    subs: list[dict[str, Any]] = []
    unsubs: list[dict[str, Any]] = []
    unsub_ts: list[float] = []
    push_errors = 0
    n_book = n_quote = 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            rec = json.loads(line)
            kind, ts = rec.get("kind"), float(rec["ts"])
            if kind == "meta":
                meta_rec = rec
            elif kind == "meta_window":
                window_rec = rec
            elif kind in ("futu_quote", "futu_book"):
                target = quotes if kind == "futu_quote" else books
                target.setdefault(str(rec["code"]), Series()).add(
                    ts, {k: v for k, v in rec.items() if k not in _PUSH_ENVELOPE}
                )
                if kind == "futu_quote":
                    n_quote += 1
                else:
                    n_book += 1
            elif kind == "ib_sample":
                sample = {k: v for k, v in rec.items() if k not in ("kind", "ts", "ts_utc")}
                sample["t"] = ts
                ib_samples.append(sample)
            elif kind == "query_subscription":
                snaps.append({k: v for k, v in rec.items() if k not in ("kind", "ts", "ts_utc")})
            elif kind == "subscribe":
                subs.append(
                    {"code": rec["code"], "subtype": rec["subtype"], "ok": rec["ok"], "error": rec["error"], "t": ts}
                )
            elif kind == "unsubscribe":
                unsubs.append({"code": rec["code"], "subtype": rec["subtype"], "error": rec["error"]})
                unsub_ts.append(ts)
            elif kind == "push_error":
                push_errors += 1
    if meta_rec is None:
        raise ValueError(f"{path}: no meta record; not a calibration JSONL")
    args_view = dict(meta_rec["args"])
    args_view.setdefault("session", meta_rec.get("session", "rth"))
    instruments, verticals = resolve_plan(argparse.Namespace(**args_view))
    if window_rec is not None:
        window = (float(window_rec["window_start"]), float(window_rec["window_end"]))
        source, note = "recorded", "recorded by the run"
    else:
        reqs = [float(x["t_request"]) for x in ib_samples if x.get("t_request") is not None]
        start = min(reqs) if reqs else float(meta_rec["ts"])
        end = min(unsub_ts) if unsub_ts else max([float(x["t"]) for x in ib_samples] + [start])
        window, source, note = (start, end), "derived", _DERIVED_WINDOW_NOTE
    codes = {i.futu_code for i in instruments}
    date_et = datetime.fromtimestamp(float(meta_rec["ts"]), tz=timezone.utc).astimezone(ET).strftime("%Y-%m-%d")
    meta = build_meta(
        args_view=args_view,
        date_et=date_et,
        ib_cycles=len({x["cycle"] for x in ib_samples}),
        window=window,
        window_note=note,
        jsonl=path,
        verticals=verticals,
        snaps=snaps,
        sub_results=subs,
        unsub_results=unsubs,
        push_errors=push_errors,
        raw_counts={"book": n_book, "quote": n_quote, "ib": len(ib_samples)},
    )
    return StoredRun(
        instruments,
        verticals,
        {c: v for c, v in quotes.items() if c in codes},
        {c: v for c, v in books.items() if c in codes},
        ib_samples,
        window,
        source,
        meta,
    )


def render_from_jsonl(path: Path | str, doc_dir: Path | str) -> Path:
    """`--from-jsonl`: re-render the markdown summary from a stored raw JSONL. No network."""
    stored = load_jsonl(path)
    result = analyze(
        stored.instruments, stored.verticals, stored.quotes, stored.books, stored.ib_samples, stored.window
    )
    doc_dir = Path(doc_dir)
    doc_dir.mkdir(parents=True, exist_ok=True)
    doc = doc_dir / f"futu-vs-ib-quotes-{stored.meta['date_et']}-{stored.meta['session']}.md"
    doc.write_text(render_markdown(stored.meta, result), encoding="utf-8")
    return doc


def run(
    args: argparse.Namespace, instruments: Sequence[Instrument], verticals: Sequence[Vertical]
) -> tuple[Path, Path]:
    api_key = os.environ.get("XENON_QUERY_API_KEY", "")
    if not api_key:
        raise SystemExit("XENON_QUERY_API_KEY is not set (needed for the IB side)")
    api = ApiClient(args.api_url, api_key)

    start = time.time()
    date_et = datetime.fromtimestamp(start, tz=timezone.utc).astimezone(ET).strftime("%Y-%m-%d")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(start, tz=timezone.utc).astimezone(ET).strftime("%H%M%S")
    jsonl = out_dir / f"futu-vs-ib-quotes-{date_et}-{args.session}-{stamp}.jsonl"
    rec = Recorder(jsonl)
    rec.write(
        "meta",
        start,
        session=args.session,
        instruments=[i.key for i in instruments],
        args={k: v for k, v in vars(args).items()},
    )

    ctx = futu = None
    sub_results: list[dict[str, Any]] = []
    snaps: list[dict[str, Any]] = []
    unsub_results: list[dict[str, Any]] = []
    ib_samples: list[dict[str, Any]] = []
    window_start = window_end = start
    cycles = 0
    try:
        con_ids: dict[str, int] = {}
        for inst in instruments:
            if inst.kind == "STK":
                try:
                    con_ids[inst.key] = qualify_stock(api, inst.key)
                except Exception as exc:  # noqa: BLE001
                    rec.write("qualify_error", time.time(), key=inst.key, error=str(exc))
        ctx, futu, sub_results, snaps = run_futu_session(args, instruments, rec)
        window_start = time.time()
        deadline = window_start + args.minutes * 60.0
        next_at = window_start
        while True:
            while time.time() < next_at:
                time.sleep(min(next_at - time.time(), 1.0))
            ib_samples += ib_cycle(api, instruments, con_ids, cycles, rec)
            cycles += 1
            next_at += args.ib_interval
            if next_at > deadline:
                break
        while time.time() < deadline:  # keep collecting pushes to the end of the requested window
            time.sleep(min(deadline - time.time(), 1.0))
        window_end = time.time()
        rec.write("meta_window", window_end, window_start=window_start, window_end=window_end)
    finally:
        try:
            if ctx is not None:
                unsub_results = unsubscribe_all(ctx, futu, rec, sub_results)
        finally:
            if ctx is not None:
                ctx.close()
            rec.close()

    with_ok = {inst.futu_code for inst in instruments}
    quotes = {c: s for c, s in rec.quotes.items() if c in with_ok}
    books = {c: s for c, s in rec.books.items() if c in with_ok}
    result = analyze(instruments, verticals, quotes, books, ib_samples, (window_start, window_end))
    meta = build_meta(
        args_view=vars(args),
        date_et=date_et,
        ib_cycles=cycles,
        window=(window_start, window_end),
        window_note="recorded by the run (`meta_window` record in the raw JSONL)",
        jsonl=jsonl,
        verticals=verticals,
        snaps=snaps,
        sub_results=sub_results,
        unsub_results=unsub_results,
        push_errors=rec.push_errors,
        raw_counts={
            "book": sum(len(x) for x in rec.books.values()),
            "quote": sum(len(x) for x in rec.quotes.values()),
            "ib": len(ib_samples),
        },
    )
    doc_dir = Path(args.doc_dir)
    doc_dir.mkdir(parents=True, exist_ok=True)
    doc = doc_dir / f"futu-vs-ib-quotes-{date_et}-{args.session}.md"
    doc.write_text(render_markdown(meta, result), encoding="utf-8")
    return jsonl, doc


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.from_jsonl:
        try:
            doc = render_from_jsonl(args.from_jsonl, args.doc_dir)
        except (OSError, ValueError, KeyError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"summary: {doc}")
        return 0
    try:
        instruments, verticals = resolve_plan(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        print(
            f"dry-run ok: {len(instruments)} instrument(s), session={args.session}, minutes={args.minutes}, ib_interval={args.ib_interval}s"
        )
        for i in instruments:
            print(f"  {i.kind} {i.key} -> {i.futu_code}")
        for v in verticals:
            print(f"  vertical long={v.long_key} short={v.short_key}")
        return 0
    jsonl, doc = run(args, instruments, verticals)
    print(f"raw samples: {jsonl}\nsummary: {doc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
