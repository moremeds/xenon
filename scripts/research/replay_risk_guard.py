"""Replay the tail-risk guard market tiers over historical SPY bars (spec 2026-11-08 §8 P1).

Data: Unusual Whales ``GET /api/stock/SPY/ohlc/{1m|1d}`` (UWClient). SPY stands in
for SPX (same move in %, the guard reads both). Bars are cached under
``data/research/risk_guard_bars/`` (gitignored); the report goes to
``docs/research/``.

Modes:
- intraday: regular-session 1 m closes fed to the guard's own ``PriceWindow`` at
  each bar end; ``market_tier`` with the default ``Thresholds`` on 15/30/60 m and
  day returns (no account inputs, no key levels). Per day: first time each tier
  is reached, the price there, and the further fall after it.
- daily (no 1 m history for 2020): day tier from the daily low and close vs the
  prior close only.

"False fire" (a stated definition, not a ground truth): after the first crossing
of a tier, SPY did not fall at least another 0.5 % that day.

Usage::

    uv run python scripts/research/replay_risk_guard.py \
        --out docs/research/2026-11-08-risk-guard-replay.md
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from xenon.api.services.risk_guard.incidents import TIER_NAMES, MarketInputs, Thresholds, market_tier
from xenon.api.services.risk_guard.windows import PriceWindow, day_return
from xenon.utils import market_calendar

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "research" / "risk_guard_bars"
FURTHER_FALL = 0.005
EPISODES = {
    "2026-11-05 fall": [date(2026, 11, 5)],
    "2025-04 tariff crash": [date(2025, 4, d) for d in range(1, 31)],
}
DAILY_EPISODE = ("2020-02-24..03-23 COVID crash", date(2020, 2, 24), date(2020, 3, 23))


def _client():
    from dotenv import load_dotenv

    load_dotenv(ROOT / "web" / ".env")
    from xenon.clients.uw_client import UWClient

    return UWClient()


def _cached(name: str, fetch) -> list[dict]:
    path = CACHE / name
    if path.exists():
        return json.loads(path.read_text())
    rows = fetch()
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows))
    time.sleep(0.2)  # ponytail: fixed pacing; UWClient retries on 429
    return rows


def minute_closes(client, d: date) -> list[tuple[float, float]]:
    """Regular-session (epoch end time, close), oldest first."""
    rows = _cached(
        f"SPY_1m_{d}.json", lambda: client.get_stock_ohlc("SPY", "1m", date=str(d), limit=2500).get("data", [])
    )
    out = [
        (datetime.fromisoformat(r["end_time"].replace("Z", "+00:00")).timestamp(), float(r["close"]))
        for r in rows
        if r.get("market_time") == "r"
    ]
    return sorted(out)


def daily_bars(client, end: date, timeframe: str) -> dict[date, dict]:
    rows = _cached(
        f"SPY_1d_{timeframe}_{end}.json",
        lambda: client.get_stock_ohlc("SPY", "1d", timeframe=timeframe, end_date=str(end)).get("data", []),
    )
    return {date.fromisoformat(r["date"]): r for r in rows if r.get("market_time") == "r"}


@dataclass
class Crossing:
    tier: int
    at: str  # HH:MM ET
    price: float
    reasons: list[str]
    further_fall: float  # min price after the crossing vs the crossing price (≤ 0)

    @property
    def false_fire(self) -> bool:
        return self.further_fall > -FURTHER_FALL


def replay_day(closes: list[tuple[float, float]], prior_close: float | None, th: Thresholds) -> list[Crossing]:
    w = PriceWindow(reconnect_gap_s=61)  # one sample per minute: a 60 s gap is not a reconnect
    first: dict[int, tuple[int, list[str]]] = {}
    for i, (t, px) in enumerate(closes):
        w.add(t, px, "rth")
        m = MarketInputs(
            ret_15m=w.ret(t, 900),
            ret_30m=w.ret(t, 1800),
            ret_60m=w.ret(t, 3600),
            day=day_return(px, prior_close),
        )
        tier, reasons = market_tier(m, th)
        for level in range(1, tier + 1):
            first.setdefault(level, (i, reasons))
    out = []
    for level, (i, reasons) in sorted(first.items()):
        t, px = closes[i]
        low_after = min(p for _, p in closes[i:])
        et = datetime.fromtimestamp(t, market_calendar.ET)
        out.append(Crossing(level, et.strftime("%H:%M"), px, reasons, low_after / px - 1))
    return out


def trading_days(start: date, end: date) -> list[date]:
    d, out = start, []
    while d <= end:
        if market_calendar.is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def run_intraday(client, days: list[date], th: Thresholds) -> dict[date, tuple[float | None, list[Crossing]]]:
    out: dict[date, tuple[float | None, list[Crossing]]] = {}
    prev_close: float | None = None
    prev_day = days[0] - timedelta(days=1)
    while not market_calendar.is_trading_day(prev_day):
        prev_day -= timedelta(days=1)
    seed = minute_closes(client, prev_day)
    prev_close = seed[-1][1] if seed else None
    for d in days:
        closes = minute_closes(client, d)
        if not closes:
            print(f"no 1m bars for {d}", file=sys.stderr)
            prev_close = None
            continue
        out[d] = (closes[-1][1] / prev_close - 1 if prev_close else None, replay_day(closes, prev_close, th))
        prev_close = closes[-1][1]
    return out


def daily_tiers(bars: dict[date, dict], start: date, end: date, th: Thresholds) -> list[tuple[date, float, float, str]]:
    days = sorted(bars)
    rows = []
    for prev, d in zip(days, days[1:], strict=False):
        if not start <= d <= end:
            continue
        pc = float(bars[prev]["close"])
        low, close = float(bars[d]["low"]) / pc - 1, float(bars[d]["close"]) / pc - 1
        tier, _ = market_tier(MarketInputs(day=low), th)
        rows.append((d, low, close, TIER_NAMES[tier]))
    return rows


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:+.2f}%"


def render(episodes, year, daily, th: Thresholds, year_range: tuple[date, date]) -> str:
    lines = [
        "# Tail-risk guard replay (P1 market tiers)",
        "",
        f"Generated {datetime.now(market_calendar.ET):%Y-%m-%d %H:%M} ET by `scripts/research/replay_risk_guard.py`.",
        "Data: Unusual Whales SPY OHLC (1 m regular session; 1 d for 2020). SPY stands in for SPX.",
        "Thresholds: code defaults (`Thresholds()`), "
        f"WATCH 15m {_pct(th.watch_15m)} / day {_pct(th.watch_day)}, "
        f"FAST 30m {_pct(th.fast_30m)} / day {_pct(th.fast_day)}, "
        f"CRASH 60m {_pct(th.crash_60m)} / day {_pct(th.crash_day)}.",
        "Not replayed: account inputs (cushion, strike distance, long ITM), key levels, BLIND causes.",
        f"False fire = after the first crossing SPY did not fall another {FURTHER_FALL * 100:.1f}% that day (a chosen definition).",
        "",
    ]
    for name, res in episodes.items():
        lines += [
            f"## {name}",
            "",
            "| Day | Close vs prior | Tier | First at (ET) | SPY | Further fall | Why |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for d, (ret, xs) in sorted(res.items()):
            if not xs:
                continue
            for x in xs:
                lines.append(
                    f"| {d} | {_pct(ret)} | {TIER_NAMES[x.tier]} | {x.at} | {x.price:.2f} | {_pct(x.further_fall)} | {'; '.join(x.reasons)} |"
                )
        quiet = sum(1 for _, xs in res.values() if not xs)
        lines += ["", f"Days without any tier: {quiet} of {len(res)}.", ""]
    lines += [f"## 12 months intraday ({year_range[0]} to {year_range[1]})", ""]
    lines += ["| Tier | Days fired | False fires | Share false |", "| --- | --- | --- | --- |"]
    for level in (1, 2, 3):
        fired = [x for _, xs in year.values() for x in xs if x.tier == level]
        false = [x for x in fired if x.false_fire]
        share = f"{len(false) / len(fired) * 100:.0f}%" if fired else "n/a"
        lines.append(f"| {TIER_NAMES[level]} | {len(fired)} | {len(false)} | {share} |")
    lines += ["", f"Trading days replayed: {len(year)}.", "", "Days with FAST or CRASH:", ""]
    for d, (ret, xs) in sorted(year.items()):
        top = max((x.tier for x in xs), default=0)
        if top >= 2:
            x = next(x for x in xs if x.tier == top)
            lines.append(
                f"- {d}: {TIER_NAMES[top]} at {x.at} ET, close {_pct(ret)}, further fall {_pct(x.further_fall)}"
            )
    name, start, end = DAILY_EPISODE
    lines += [
        "",
        f"## {name} (daily bars: day tier from the low only)",
        "",
        "| Day | Low vs prior close | Close vs prior close | Day tier |",
        "| --- | --- | --- | --- |",
    ]
    for d, low, close, tier in daily:
        lines.append(f"| {d} | {_pct(low)} | {_pct(close)} | {tier} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--year-end", type=date.fromisoformat, default=date(2026, 11, 6))
    args = ap.parse_args(argv)
    th, client = Thresholds(), _client()
    episodes = {
        name: run_intraday(client, [d for d in days if market_calendar.is_trading_day(d)], th)
        for name, days in EPISODES.items()
    }
    year_range = (args.year_end - timedelta(days=365), args.year_end)
    year = run_intraday(client, trading_days(*year_range), th)
    name, start, end = DAILY_EPISODE
    daily = daily_tiers(daily_bars(client, date(2020, 3, 31), "3M"), start, end, th)
    args.out.write_text(render(episodes, year, daily, th, year_range))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
