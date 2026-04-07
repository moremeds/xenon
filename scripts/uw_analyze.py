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
from scripts.analysis.models import AnalysisReport
from scripts.analysis.scoring import score_buckets
from scripts.analysis.ticker_data import fetch_ticker_data
from scripts.analysis.vrp import build_vrp_state, classify_regime
from scripts.clients.uw_client import UWClient


def run_analysis(
    ticker: str,
    *,
    fast: bool = False,
    client: Optional[UWClient] = None,
) -> AnalysisReport:
    """Run the full analysis pipeline for a single ticker."""
    owns_client = client is None
    if owns_client:
        client = UWClient()

    try:
        td = fetch_ticker_data(ticker, client, deep=True)
        vrp = build_vrp_state(td)
        regime = classify_regime(td, vrp)
        scores = score_buckets(td, vrp, regime, mode="fast" if fast else "full")

        ticker_sector: Optional[str] = None
        try:
            info = client.get_stock_info(td.ticker) or {}
            data = info.get("data") or {}
            ticker_sector = data.get("sector") or data.get("gics_sector")
        except Exception:  # noqa: BLE001
            ticker_sector = None
        benchmark = load_benchmark_context(client, ticker_sector=ticker_sector)

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

        return AnalysisReport(
            ticker=td.ticker,
            price=td.price,
            fetched_at=td.fetched_at.isoformat(),
            data_freshness=data_freshness,
            benchmark=benchmark,
            vrp=vrp,
            regime=regime,
            scores=scores,
            notes=notes,
        )
    finally:
        if owns_client:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def _report_to_dict(report: AnalysisReport) -> dict:
    return asdict(report)


def _format_summary(report: AnalysisReport) -> str:
    vrp_z = (
        f"{report.vrp.vrp_zscore:.2f}"
        if report.vrp.vrp_zscore is not None
        else "unavailable"
    )
    lines = [
        "=" * 60,
        f"{report.ticker}  @  ${report.price if report.price is not None else 'N/A'}",
        f"Fetched: {report.fetched_at}",
        "-" * 60,
        f"Bias:        {report.scores.bias}  (composite {report.scores.composite:+.1f})",
        f"Grade:       {report.scores.grade}  ({report.scores.mode} mode)",
        f"Regime:      {report.regime.regime}  — {report.regime.reason}",
        f"VRP z-score: {vrp_z}",
        f"IV pctl:     {report.vrp.iv_percentile}",
        "-" * 60,
        "Buckets:",
        f"  Market Structure: {report.scores.market_structure:+.1f}",
        f"  Volatility:       {report.scores.volatility:+.1f}",
        f"  Flow:             {report.scores.flow:+.1f}",
        f"  Positioning:      {report.scores.positioning:+.1f}",
    ]
    if report.scores.skipped_buckets:
        lines.append(f"  (skipped: {', '.join(report.scores.skipped_buckets)})")
    if report.notes:
        lines.append("-" * 60)
        lines.append("Notes:")
        for n in report.notes:
            lines.append(f"  - {n}")
    lines.append("=" * 60)
    lines.append("NOTE: This is a signal summary, not a trade recommendation.")
    lines.append("=" * 60)
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
