"""
Synthetic BSM backtest — SPX short put / put credit spread
===========================================================
Strategy: sell SPX put at N DTE with target delta, exit at 50% profit OR 21 DTE time stop.
Tests: naked put vs credit spread, various deltas, VIX filters.

Data:
  - SPX daily close: yfinance (^GSPC)
  - VIX daily close: CBOE public CSV (used as ATM IV proxy)

Limitations:
  - No vol skew: real OTM puts trade at VIX + 2-5 pts; this is slightly conservative
  - Fills at BSM theoretical mid: no bid/ask friction (~0.5-1% CAGR optimistic)
  - Daily granularity: exit triggers checked at close, not intraday
  - Position sizing: 2.5% of portfolio per trade (Xenon Gate 3 cap)

Usage:
  uv run python scripts/research/spx_short_put_backtest.py

Results (2007-2024):
  SPX buy-and-hold:              CAGR 8.25%  Sharpe 0.50  MaxDD 56.8%
  A Naked 0.16Δ, 50%/21DTE:     CAGR 4.30%  Sharpe 0.45  MaxDD 32.6%
  B Spread 0.16/0.05Δ, 50%/21:  CAGR 4.89%  Sharpe 1.10  MaxDD  9.8%  ← Gates-compliant
  C Naked, hold to expiry:       CAGR -0.09% Sharpe 0.11  MaxDD 61.6%  ← never do this
  D Naked 0.30Δ, 50%/21DTE:     CAGR 7.57%  Sharpe 0.77  MaxDD 24.4%
  E Naked 0.16Δ + VIX>16:       CAGR 3.56%  Sharpe 0.34  MaxDD 51.6%  ← filter hurts naked
  F Spread 0.16/0.05Δ + VIX>16: CAGR 4.35%  Sharpe 1.00  MaxDD  9.8%
  G Spread 0.30/0.10Δ, 50%/21:  CAGR 7.24%  Sharpe 1.30  MaxDD 13.3%  ← best risk-adjusted
"""

import csv
import io
import math
import urllib.request
from datetime import datetime, timedelta

# ── BSM helpers ───────────────────────────────────────────────────────────────


def norm_cdf(x):
    t = 1 / (1 + 0.2316419 * abs(x))
    d = 0.3989422820 * math.exp(-0.5 * x * x)
    p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.7814779 + t * (-1.8212560 + t * 1.3302744))))
    return 1 - p if x > 0 else p


def bsm_put(S, K, t, r, sigma):
    if t <= 0 or sigma <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    return K * math.exp(-r * t) * norm_cdf(-d2) - S * norm_cdf(-d1)


def bsm_put_delta(S, K, t, r, sigma):
    if t <= 0 or sigma <= 0:
        return -1.0 if K > S else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * t) / (sigma * math.sqrt(t))
    return norm_cdf(d1) - 1


def strike_for_delta(S, t, r, sigma, target_delta):
    """Binary search: find K s.t. |put_delta(K)| == target_delta."""
    lo, hi = S * 0.3, S * 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if bsm_put_delta(S, mid, t, r, sigma) < -target_delta:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


# ── Data ──────────────────────────────────────────────────────────────────────


def load_vix():
    url = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req).read().decode()
    out = {}
    for row in csv.DictReader(io.StringIO(raw)):
        try:
            dt = datetime.strptime(row["DATE"].strip(), "%m/%d/%Y").date()
            out[dt] = float(row["CLOSE"]) / 100
        except Exception:
            pass
    return out


def load_spx(start="2005-01-01"):
    import yfinance as yf

    df = yf.download("^GSPC", start=start, progress=False, auto_adjust=True)
    close = df["Close"]
    if close.ndim > 1:
        close = close.iloc[:, 0]
    return {dt.date(): float(val) for dt, val in close.items()}


# ── Backtest engine ───────────────────────────────────────────────────────────


def run_backtest(
    start="2007-01-01",
    end="2024-12-31",
    entry_dte=45,
    target_delta=0.16,
    exit_profit_pct=0.50,
    exit_dte=21,
    spread_wing_delta=None,  # None = naked put; 0.05 = credit spread wing
    min_vix=None,  # absolute VIX floor for entry (e.g. 0.16 = VIX > 16%)
    risk_frac=0.025,  # max capital at risk per trade (Xenon Gate 3)
    r=0.0,
):
    vix = load_vix()
    spx = load_spx()

    start_dt = datetime.strptime(start, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end, "%Y-%m-%d").date()
    days = sorted(d for d in spx if start_dt <= d <= end_dt)

    portfolio = 100_000.0
    equity = {days[0]: portfolio}
    trades = []
    pos = None

    for dt in days:
        S = spx.get(dt)
        iv = vix.get(dt)
        if S is None or iv is None:
            continue

        # mark-to-market
        if pos is not None:
            t_rem = (pos["expiry"] - dt).days / 365
            dte_rem = (pos["expiry"] - dt).days
            cur_short = bsm_put(S, pos["short_K"], t_rem, r, iv)
            cur_long = bsm_put(S, pos["long_K"], t_rem, r, iv) if pos["long_K"] else 0.0
            cur_net = cur_short - cur_long
            pnl_pct = (pos["entry_credit"] - cur_net) / pos["entry_credit"]

            reason = None
            if pnl_pct >= exit_profit_pct:
                reason = "profit_target"
            elif dte_rem <= exit_dte:
                reason = "time_stop"
            elif dte_rem <= 0:
                reason = "expiry"

            if reason:
                realized = (pos["entry_credit"] - cur_net) * pos["qty"] * 100
                portfolio += realized
                trades.append(
                    {
                        "entry": pos["entry_date"],
                        "exit": dt,
                        "hold_days": (dt - pos["entry_date"]).days,
                        "exit_reason": reason,
                        "entry_credit": pos["entry_credit"],
                        "exit_debit": cur_net,
                        "pnl": realized,
                        "win": realized > 0,
                        "pnl_pct": pnl_pct,
                    }
                )
                pos = None

        # open new position
        if pos is None:
            if min_vix and iv < min_vix:
                equity[dt] = portfolio
                continue

            t = entry_dte / 365
            short_K = strike_for_delta(S, t, r, iv, target_delta)
            entry_short = bsm_put(S, short_K, t, r, iv)

            if spread_wing_delta:
                long_K = strike_for_delta(S, t, r, iv, spread_wing_delta)
                entry_long = bsm_put(S, long_K, t, r, iv)
            else:
                long_K = None
                entry_long = 0.0

            credit = entry_short - entry_long
            if credit <= 0.10:
                equity[dt] = portfolio
                continue

            max_loss = (short_K - (long_K or 0.0)) * 100 if long_K else short_K * 100
            qty = max(1, int((portfolio * risk_frac) / max_loss))

            pos = {
                "entry_date": dt,
                "expiry": dt + timedelta(days=entry_dte),
                "short_K": short_K,
                "long_K": long_K,
                "entry_credit": credit,
                "qty": qty,
            }

        equity[dt] = portfolio

    # statistics
    dates = sorted(equity)
    vals = [equity[d] for d in dates]
    years = (dates[-1] - dates[0]).days / 365

    cagr = (vals[-1] / vals[0]) ** (1 / years) - 1

    dr = [(vals[i] - vals[i - 1]) / vals[i - 1] for i in range(1, len(vals))]
    mu = sum(dr) / len(dr)
    sigma = (sum((x - mu) ** 2 for x in dr) / len(dr)) ** 0.5
    sharpe = (mu / sigma) * (252**0.5) if sigma else 0.0

    peak = vals[0]
    max_dd = 0.0
    for v in vals:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak)

    wins = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]
    er = {r: sum(1 for t in trades if t["exit_reason"] == r) for r in ["profit_target", "time_stop", "expiry"]}

    return {
        "n": len(trades),
        "per_year": len(trades) / years,
        "avg_hold": sum(t["hold_days"] for t in trades) / len(trades) if trades else 0,
        "win_rate": len(wins) / len(trades) if trades else 0,
        "avg_win_pct": sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0,
        "avg_loss_pct": sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0,
        "cagr": cagr,
        "sharpe": sharpe,
        "max_dd": max_dd,
        "calmar": cagr / max_dd if max_dd else 0,
        "exit_reasons": er,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────


def _fmt(label, res):
    er = res["exit_reasons"]
    total = sum(er.values()) or 1
    print(f"\n{'─' * 62}")
    print(f"  {label}")
    print(f"{'─' * 62}")
    print(f"  Trades {res['n']} ({res['per_year']:.1f}/yr)  Avg hold {res['avg_hold']:.0f}d")
    print(
        f"  Win {res['win_rate'] * 100:.1f}%  AvgWin +{res['avg_win_pct'] * 100:.0f}%cr  AvgLoss {res['avg_loss_pct'] * 100:.0f}%cr"
    )
    print(
        f"  CAGR {res['cagr'] * 100:.2f}%  Sharpe {res['sharpe']:.2f}  MaxDD {res['max_dd'] * 100:.1f}%  Calmar {res['calmar']:.2f}"
    )
    print(
        f"  Exits: profit {er['profit_target'] / total * 100:.0f}%  time {er['time_stop'] / total * 100:.0f}%  expiry {er['expiry'] / total * 100:.0f}%"
    )


def main():
    import yfinance as yf

    print("=" * 62)
    print("  SPX SHORT PUT BACKTEST  2007-2024  (synthetic BSM)")
    print("  VIX as ATM IV proxy · no skew · fills at mid · 2.5% risk/trade")
    print("=" * 62)

    # Buy-and-hold benchmark
    df = yf.download("^GSPC", start="2007-01-01", end="2024-12-31", progress=False, auto_adjust=True)
    bh = df["Close"] if df["Close"].ndim == 1 else df["Close"].iloc[:, 0]
    bh_vals = [float(v) for v in bh]
    bh_years = (bh.index[-1] - bh.index[0]).days / 365
    bh_cagr = (bh_vals[-1] / bh_vals[0]) ** (1 / bh_years) - 1
    bh_dr = [(bh_vals[i] - bh_vals[i - 1]) / bh_vals[i - 1] for i in range(1, len(bh_vals))]
    bh_mu = sum(bh_dr) / len(bh_dr)
    bh_sig = (sum((x - bh_mu) ** 2 for x in bh_dr) / len(bh_dr)) ** 0.5
    bh_sharpe = bh_mu / bh_sig * (252**0.5)
    peak = bh_vals[0]
    bh_dd = 0.0
    for v in bh_vals:
        peak = max(peak, v)
        bh_dd = max(bh_dd, (peak - v) / peak)
    print(f"\n{'─' * 62}")
    print(f"  BENCHMARK: SPX buy-and-hold 2007-2024")
    print(f"{'─' * 62}")
    print(
        f"  CAGR {bh_cagr * 100:.2f}%  Sharpe {bh_sharpe:.2f}  MaxDD {bh_dd * 100:.1f}%  Calmar {bh_cagr / bh_dd:.2f}"
    )

    scenarios = [
        ("A  Naked 0.16Δ · 50%/21DTE", dict()),
        ("B  Spread 0.16/0.05Δ · 50%/21DTE  [Gates-compliant]", dict(spread_wing_delta=0.05)),
        ("C  Naked 0.16Δ · hold to expiry  [baseline: never do this]", dict(exit_profit_pct=1.0, exit_dte=0)),
        ("D  Naked 0.30Δ · 50%/21DTE", dict(target_delta=0.30)),
        ("E  Naked 0.16Δ · VIX>16 filter", dict(min_vix=0.16)),
        ("F  Spread 0.16/0.05Δ · VIX>16 filter", dict(spread_wing_delta=0.05, min_vix=0.16)),
        ("G  Spread 0.30/0.10Δ · 50%/21DTE  [best risk-adjusted]", dict(spread_wing_delta=0.10, target_delta=0.30)),
    ]

    for label, kwargs in scenarios:
        print(f"\n  loading data for {label[:30]}...", end="", flush=True)
        res = run_backtest(**kwargs)
        _fmt(label, res)

    print()


if __name__ == "__main__":
    main()
