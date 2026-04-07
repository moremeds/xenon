"""uw-analyze: per-ticker signal analysis CLI + run_analysis() library function.

Primary entry is `run_analysis()`, which is also consumed in-process by
`uw-scan --analyze-top N`. The CLI is a thin wrapper for debug/research.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from scripts.analysis.benchmark import load_benchmark_context
from scripts.analysis.models import (
    AnalysisReport,
    BucketScores,
    RegimeState,
    TickerData,
    VRPState,
)
from scripts.analysis.scoring import score_buckets
from scripts.analysis.ticker_data import fetch_ticker_data
from scripts.analysis.vrp import build_vrp_state, classify_regime
from scripts.clients.uw_client import UWClient


def _build_setup_thesis(
    td: TickerData, vrp: VRPState, regime: RegimeState, scores: BucketScores
) -> dict:
    """Deterministic structure-family decision tree (NOT trade emission).

    Returns {bias, regime, structure_family, rationale} — no strikes,
    credits, or stops. Trade construction requires a chain fetch and
    routes through the Four Gates in a separate spec.
    """
    composite = scores.composite
    iv_signal = td.iv_rank if td.iv_rank is not None else vrp.iv_percentile

    if regime.regime == "R2":
        family = "no_trade_R2"
        rationale = (
            f"Regime is R2 ({regime.reason}); risk-off — no new structures."
        )
    elif (
        abs(composite) >= 40
        and vrp.ts_inverted is True
        and iv_signal is not None
        and iv_signal > 70
    ):
        family = "iron_condor"
        rationale = (
            f"Composite {composite:+.0f} with inverted term structure and "
            f"rich IV ({iv_signal:.0f}); fade vol with a defined-risk premium-collect."
        )
    elif abs(composite) >= 40 and iv_signal is not None and iv_signal < 40:
        family = "debit_vertical"
        rationale = (
            f"Directional composite {composite:+.0f} with cheap IV ({iv_signal:.0f}); "
            f"buy directional convexity with a debit spread."
        )
    elif (
        regime.gex_sign == "positive"
        and td.call_wall_strike is not None
        and td.put_wall_strike is not None
        and -40 < composite < 40
    ):
        family = "iron_condor"
        rationale = (
            f"Positive GEX with visible walls (put {td.put_wall_strike:.0f} / "
            f"call {td.call_wall_strike:.0f}) and a mixed composite {composite:+.0f}; "
            f"range-bound — sell premium between the walls."
        )
    else:
        family = "neutral"
        rationale = (
            f"Mixed composite {composite:+.0f} without a clear vol or "
            f"structural edge; stand aside."
        )

    return {
        "bias": scores.bias,
        "regime": regime.regime,
        "structure_family": family,
        "rationale": rationale,
    }


def run_analysis(
    ticker: str,
    *,
    fast: bool = False,
    client: Optional[UWClient] = None,
) -> AnalysisReport:
    """Run the full analysis pipeline for a single ticker.

    Thin wrapper over `run_analysis_with_data` that drops the TickerData
    so existing CLI / in-process callers keep their old return shape.
    """
    report, _td = run_analysis_with_data(ticker, fast=fast, client=client)
    return report


def run_analysis_with_data(
    ticker: str,
    *,
    fast: bool = False,
    client: Optional[UWClient] = None,
) -> tuple[AnalysisReport, TickerData]:
    """Run the analysis pipeline and also return the underlying TickerData.

    The route layer needs raw TickerData attributes (walls, gamma, IV rank,
    net premium, gex_by_strike) that are not part of AnalysisReport.
    """
    owns_client = client is None
    if owns_client:
        client = UWClient()

    try:
        td: TickerData = fetch_ticker_data(ticker, client, deep=True)
        vrp = build_vrp_state(td)
        regime = classify_regime(td, vrp)
        scores = score_buckets(td, vrp, regime, mode="fast" if fast else "full")

        # Sector is already populated by fetch_ticker_data(deep=True) via
        # _deep_enrichment step 1 — no need to re-fetch get_stock_info.
        benchmark = load_benchmark_context(client, ticker_sector=td.sector)

        data_freshness = {
            "gex": "live" if td.gex is not None else "unavailable",
            "volatility": vrp.data_freshness,
            "earnings": "stale" if td.earnings_date is None else "live",
            "benchmark_spy": benchmark.spy.freshness,
        }

        notes: list[str] = []
        if vrp.vrp_zscore is None:
            notes.append("VRP z-score unavailable — regime defaulted to cautious.")
        if scores.reweighted:
            notes.append(
                f"Buckets reweighted due to missing data: {scores.skipped_buckets}"
            )

        setup_thesis = _build_setup_thesis(td, vrp, regime, scores)

        report = AnalysisReport(
            ticker=td.ticker,
            price=td.price,
            fetched_at=td.fetched_at.isoformat(),
            data_freshness=data_freshness,
            benchmark=benchmark,
            vrp=vrp,
            regime=regime,
            scores=scores,
            notes=notes,
            setup_thesis=setup_thesis,
        )
        # Stash td on the report instance for the rich formatter (not part
        # of the dataclass schema; not serialized in --json).
        object.__setattr__(report, "_ticker_data", td)
        return report, td
    finally:
        if owns_client:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def _report_to_dict(report: AnalysisReport) -> dict:
    return asdict(report)


def _fmt(v, spec: str = ".2f", dash: str = "—") -> str:
    if v is None:
        return dash
    try:
        return format(v, spec)
    except (TypeError, ValueError):
        return str(v)


def _format_summary(report: AnalysisReport) -> str:
    td: Optional[TickerData] = getattr(report, "_ticker_data", None)
    s = report.scores
    r = report.regime
    v = report.vrp
    bench = report.benchmark
    th = report.setup_thesis or {}

    width = 72
    lines: list[str] = []
    bar = "=" * width
    sub = "-" * width

    # ── Header ────────────────────────────────────────────────────────
    price_str = f"${_fmt(report.price, '.2f')}"
    sector = (td.sector if td else None) or "—"
    sector_etf = bench.sector_etf.ticker if bench.sector_etf else None
    sector_ctx = ""
    if sector_etf and bench.sector_etf:
        etf = bench.sector_etf
        flip = etf.gex_flip
        price = etf.price
        if flip is not None and price is not None:
            rel = "above flip" if price > flip else "below flip"
            sector_ctx = f" · {sector_etf} {rel}"
        else:
            sector_ctx = f" · {sector_etf}"

    lines.append(bar)
    lines.append(
        f"{report.ticker}  |  {s.bias}  ({s.composite:+.0f})  |  Grade {s.grade}"
    )
    lines.append(f"{price_str}  ·  Sector: {sector}{sector_ctx}")
    lines.append(f"Regime: {r.regime}  —  {r.reason}")
    lines.append(sub)

    # ── Market Structure ──────────────────────────────────────────────
    lines.append(f"Market Structure  ({s.market_structure:+.1f}/28)")
    flip_val = (report.regime.gex_flip_relative or "—").replace("_", " ")
    flip_dist = (
        f"{abs(r.flip_distance_pct):.1f}%" if r.flip_distance_pct is not None else "—"
    )
    gex_sign = r.gex_sign or "—"
    gpp = td.gamma_per_1pct if td else None
    lines.append(
        f"  GEX flip: {flip_val} ({flip_dist})  ·  Net γ sign: {gex_sign}"
        f"  ·  γ per 1%: {_fmt(gpp, ',.0f')}"
    )
    cw_strike = td.call_wall_strike if td else None
    pw_strike = td.put_wall_strike if td else None
    lines.append(
        f"  Call wall: {_fmt(cw_strike, '.2f')}  ·  Put wall: {_fmt(pw_strike, '.2f')}"
    )
    # Deterministic structure assessment
    ms_assess: list[str] = []
    if r.gex_sign == "positive":
        ms_assess.append("dealers long gamma → mean-reverting tape")
    elif r.gex_sign == "negative":
        ms_assess.append("dealers short gamma → trend-amplifying")
    if r.gex_flip_relative == "below_price":
        ms_assess.append("flip below price (supportive)")
    elif r.gex_flip_relative == "above_price":
        ms_assess.append("flip above price (overhead)")
    if ms_assess:
        lines.append(f"  Assessment: {'; '.join(ms_assess)}.")
    lines.append("")

    # ── Volatility ────────────────────────────────────────────────────
    lines.append(f"Volatility  ({s.volatility:+.1f}/28)")
    iv_rank_disp = td.iv_rank if (td and td.iv_rank is not None) else v.iv_percentile
    iv_lo = td.iv_52w_low if td else None
    iv_hi = td.iv_52w_high if td else None
    lines.append(
        f"  IV rank: {_fmt(iv_rank_disp, '.0f')}  ·  IV: {_fmt(td.iv if td else None, '.1f')}"
        f"  ·  RV: {_fmt(td.rv if td else None, '.1f')}  ·  VRP z: {_fmt(v.vrp_zscore)}"
    )
    lines.append(
        f"  52w IV range: {_fmt(iv_lo, '.1f')} – {_fmt(iv_hi, '.1f')}"
        f"  ·  Term structure: {'inverted' if v.ts_inverted else 'normal'}"
    )
    vol_assess: list[str] = []
    if iv_rank_disp is not None:
        if iv_rank_disp > 70:
            vol_assess.append("rich IV (sell premium)")
        elif iv_rank_disp < 30:
            vol_assess.append("cheap IV (buy premium)")
        else:
            vol_assess.append("middling IV")
    if v.vrp_zscore is not None and v.vrp_zscore > 1.0:
        vol_assess.append("VRP elevated (vol overpriced)")
    elif v.vrp_zscore is not None and v.vrp_zscore < 0:
        vol_assess.append("VRP negative (vol underpriced)")
    if v.ts_inverted:
        vol_assess.append("term structure inverted (event/stress)")
    if vol_assess:
        lines.append(f"  Assessment: {'; '.join(vol_assess)}.")
    lines.append("")

    # ── Flow ──────────────────────────────────────────────────────────
    lines.append(f"Flow  ({s.flow:+.1f}/24)  &  Positioning  ({s.positioning:+.1f}/20)")
    ncp = td.net_call_premium if td else None
    npp = td.net_put_premium if td else None
    nt = (ncp or 0) + (npp or 0) if (ncp is not None and npp is not None) else None
    lines.append(
        f"  Net premium  call: {_fmt(ncp, ',.0f')}  ·  put: {_fmt(npp, ',.0f')}"
        f"  ·  net: {_fmt(nt, ',.0f')}"
    )
    sv_ratio = td.short_volume_ratio if td else None
    sv_trend = td.short_volume_trend if td else None
    sv_trend_str = (
        " → ".join(f"{t:.2f}" for t in sv_trend) if sv_trend else "—"
    )
    lines.append(
        f"  Short vol ratio: {_fmt(sv_ratio, '.2f')}  ·  3-day trend: {sv_trend_str}"
    )
    flow_assess: list[str] = []
    if ncp is not None and npp is not None:
        # UW net_*_premium = ask-side minus bid-side. Positive call_prem =
        # call buyers (bullish); negative call_prem = call sellers (bearish).
        # Positive put_prem = put buyers (bearish/hedging); negative put_prem
        # = put sellers (bullish — closing hedges or selling premium).
        # Convert to a directional "bullish_score" so sign drives the label.
        bullish = (ncp if ncp > 0 else 0) + (-npp if npp < 0 else 0)
        bearish = (-ncp if ncp < 0 else 0) + (npp if npp > 0 else 0)
        if bullish > bearish * 1.5:
            flow_assess.append("call buying / put selling (bullish lean)")
        elif bearish > bullish * 1.5:
            flow_assess.append("call selling / put buying (bearish lean)")
        else:
            flow_assess.append("mixed premium")
    if sv_trend and len(sv_trend) >= 2:
        delta = sv_trend[0] - sv_trend[-1]
        if delta > 0.02:
            flow_assess.append("short volume rising")
        elif delta < -0.02:
            flow_assess.append("short volume easing")
    if flow_assess:
        lines.append(f"  Assessment: {'; '.join(flow_assess)}.")
    lines.append("")

    # ── Setup Thesis ──────────────────────────────────────────────────
    lines.append("Setup Thesis")
    lines.append(f"  Bias: {th.get('bias', s.bias)}  ·  Regime: {th.get('regime', r.regime)}")
    lines.append(f"  Structure family: {th.get('structure_family', '—')}")
    lines.append(f"  Rationale: {th.get('rationale', '—')}")
    lines.append(
        "  NOTE: No strikes/credits/stops — trade construction requires a chain fetch."
    )

    if report.scores.skipped_buckets:
        lines.append(sub)
        lines.append(f"Skipped buckets: {', '.join(report.scores.skipped_buckets)}")
    if report.notes:
        lines.append(sub)
        lines.append("Notes:")
        for n in report.notes:
            lines.append(f"  - {n}")

    lines.append(bar)
    lines.append("Signal summary, not a trade recommendation.")
    lines.append(bar)
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="uw-analyze: per-ticker UW signal analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("tickers", nargs="+", help="Ticker(s) to analyze")
    p.add_argument("--fast", action="store_true", help="Skip Flow + Positioning buckets")
    p.add_argument("--json", action="store_true", help="Print JSON instead of summary")
    args = p.parse_args(argv)

    out_dir = Path("data/analysis")
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")

    try:
        client_ctx = UWClient()
    except Exception as exc:  # noqa: BLE001
        print(f"UWClient init failed: {exc}", file=sys.stderr)
        return 2

    had_ticker_error = False
    with client_ctx as client:
        for raw_ticker in args.tickers:
            ticker = raw_ticker.upper()
            try:
                report = run_analysis(ticker, fast=args.fast, client=client)
            except Exception as exc:  # noqa: BLE001
                print(f"[{ticker}] ERROR: {exc}", file=sys.stderr)
                had_ticker_error = True
                continue

            out_file = out_dir / f"{ticker}-{today}.json"
            out_file.write_text(
                json.dumps(_report_to_dict(report), default=str, indent=2)
            )

            if args.json:
                print(json.dumps(_report_to_dict(report), default=str, indent=2))
            else:
                print(_format_summary(report))

    return 0 if not had_ticker_error or len(args.tickers) > 1 else 1


if __name__ == "__main__":
    sys.exit(main())
