"""uw-scan: tiered opportunity scanner CLI."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Optional

from scripts.analysis.gates import earnings_gate, liquidity_gate, regime_gate
from scripts.analysis.ticker_data import fetch_ticker_data
from scripts.analysis.vrp import build_vrp_state, classify_regime
from scripts.clients.uw_client import UWClient
from scripts.uw_scan_lib.context.pcr_sentiment import flag as pcr_flag
from scripts.uw_scan_lib.models import ContextFlag, ScanCandidate, SignalHit
from scripts.uw_scan_lib.ranking import build_candidate, rank_candidates
from scripts.uw_scan_lib.signals.dark_pool_accumulation import detect as dp_detect
from scripts.uw_scan_lib.signals.deep_conviction_flow import detect as dcf_detect
from scripts.uw_scan_lib.signals.earnings_iv_crush import detect as eic_detect
from scripts.uw_scan_lib.signals.gex_pinning import detect as gp_detect
from scripts.uw_scan_lib.universe import load_universe

logger = logging.getLogger(__name__)

Mode = Literal["watchlist", "targeted"]


@dataclass
class ScanConfig:
    mode: Mode
    tickers: list[str] = field(default_factory=list)
    full: bool = False
    min_confluence: int = 0
    analyze_top: int = 0
    max_workers: int = 10


def _run_signals(ticker: str, td, *, full: bool, today: date) -> list[SignalHit]:
    hits: list[SignalHit] = []

    for detector in (dcf_detect, eic_detect):
        hit = detector(ticker, td)
        if hit is not None:
            hits.append(hit)

    gp_hit = gp_detect(ticker, td, today=today)
    if gp_hit is not None:
        hits.append(gp_hit)

    if full:
        dp_hit = dp_detect(ticker, td)
        if dp_hit is not None:
            hits.append(dp_hit)

    return hits


def _run_context(ticker: str, td, *, full: bool) -> list[ContextFlag]:
    if not full:
        return []
    flags: list[ContextFlag] = []
    pcr = pcr_flag(ticker, td)
    if pcr is not None:
        flags.append(pcr)
    return flags


def scan_universe(cfg: ScanConfig, *, client) -> dict:
    if cfg.mode == "targeted":
        universe = load_universe(mode="targeted", tickers=cfg.tickers)
    elif cfg.mode == "watchlist":
        universe = load_universe(mode="watchlist")
    else:
        raise ValueError(f"unsupported mode: {cfg.mode}")

    today_date = date.today()
    candidates: list[ScanCandidate] = []

    def _process(ticker: str) -> Optional[ScanCandidate]:
        try:
            td = fetch_ticker_data(ticker, client)
        except Exception as exc:  # noqa: BLE001
            logger.warning("fetch failed for %s: %s", ticker, exc)
            return None

        ticker_vrp = build_vrp_state(td)
        ticker_regime = classify_regime(td, ticker_vrp)

        option_volume = None
        if td.flow_alerts:
            try:
                option_volume = sum(int(a.get("volume") or 0) for a in td.flow_alerts)
            except (TypeError, ValueError):
                option_volume = None

        gates_result = {
            "earnings": "pass" if earnings_gate(
                earnings_within_14d=td.earnings_within_14d
            ) else "block",
            "liquidity": "pass" if liquidity_gate(
                option_volume=option_volume
            ) else "block",
            "regime": "pass" if regime_gate(regime=ticker_regime.regime) else "block",
        }

        if gates_result["regime"] == "block":
            return None

        hits = _run_signals(ticker, td, full=cfg.full, today=today_date)
        flags = _run_context(ticker, td, full=cfg.full)
        if not hits:
            return None
        candidate = build_candidate(ticker, hits, flags)
        if candidate is None:
            return None
        candidate.gates = gates_result
        return candidate

    with ThreadPoolExecutor(max_workers=cfg.max_workers) as pool:
        futures = {pool.submit(_process, t): t for t in universe}
        for future in as_completed(futures):
            candidate = future.result()
            if candidate is not None:
                candidates.append(candidate)

    if cfg.min_confluence >= 2:
        candidates = [c for c in candidates if c.is_type_f]

    ranked = rank_candidates(candidates)

    try:
        spy_td = fetch_ticker_data("SPY", client)
        spy_vrp = build_vrp_state(spy_td)
        spy_regime = classify_regime(spy_td, spy_vrp)
        regime_dict = {"regime": spy_regime.regime, "reason": spy_regime.reason}
    except Exception as exc:  # noqa: BLE001
        logger.debug("regime fetch failed: %s", exc)
        regime_dict = {"regime": "R1", "reason": "regime probe failed"}

    result = {
        "scan_time": datetime.now().isoformat(),
        "mode": cfg.mode,
        "universe_size": len(universe),
        "candidates_analyzed": len(universe),
        "candidates_with_hits": len(candidates),
        "full": cfg.full,
        "regime": regime_dict,
        "candidates": [_candidate_to_dict(c) for c in ranked],
    }

    if cfg.analyze_top > 0 and ranked:
        from scripts.uw_analyze import run_analysis
        analyses = []
        for c in ranked[: cfg.analyze_top]:
            try:
                report = run_analysis(c.ticker, fast=False, client=client)
                analyses.append(asdict(report))
            except Exception as exc:  # noqa: BLE001
                logger.warning("analyze failed for %s: %s", c.ticker, exc)
        result["analyses"] = analyses

    return result


def _candidate_to_dict(c: ScanCandidate) -> dict:
    return {
        "ticker": c.ticker,
        "is_type_f": c.is_type_f,
        "final_score": c.final_score,
        "raw_score": c.raw_score,
        "confluence_score": c.confluence_score,
        "hits": [
            {
                "signal": h.signal_type,
                "tier": h.tier,
                "score": h.score,
                "evidence": h.evidence,
                "freshness": h.freshness,
            }
            for h in c.hits
        ],
        "context_flags": [
            {"layer": f.layer, "label": f.label, "value": f.value}
            for f in c.context_flags
        ],
        "gates": c.gates,
    }


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="uw-scan: tiered UW opportunity scanner")
    p.add_argument("tickers", nargs="*", help="Explicit ticker list (targeted mode)")
    p.add_argument("--watchlist", action="store_true", help="Scan data/watchlist.json")
    p.add_argument("--full", action="store_true", help="Full mode (tier 1 + dark pool + PCR)")
    p.add_argument("--min-confluence", type=int, default=0,
                   help="Require N independent signals (2 = Type F only)")
    p.add_argument("--analyze-top", type=int, default=0,
                   help="Chain to uw_analyze for top N candidates")
    p.add_argument("--json", action="store_true", help="Print JSON to stdout")
    args = p.parse_args(argv)

    if args.watchlist:
        mode = "watchlist"
    elif args.tickers:
        mode = "targeted"
    else:
        print("ERROR: specify tickers or --watchlist. Market-wide mode is "
              "deferred to a follow-up spec — use `discover` for flow-led "
              "market-wide scanning.", file=sys.stderr)
        return 2

    cfg = ScanConfig(
        mode=mode,
        tickers=[t.upper() for t in args.tickers],
        full=args.full,
        min_confluence=args.min_confluence,
        analyze_top=args.analyze_top,
    )

    out_dir = Path("data/uw_scan")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")

    with UWClient() as client:
        result = scan_universe(cfg, client=client)

    out_file = out_dir / f"{stamp}.json"
    out_file.write_text(json.dumps(result, default=str, indent=2))

    if args.json:
        print(json.dumps(result, default=str, indent=2))
    else:
        print(f"Scan complete: {result['candidates_with_hits']} candidates "
              f"(of {result['universe_size']}) — regime {result['regime']['regime']}")
        for c in result["candidates"][:10]:
            type_f = "*" if c["is_type_f"] else " "
            signals = ",".join(h["signal"] for h in c["hits"])
            print(f"  {type_f} {c['ticker']:<6} score={c['final_score']:.1f}  [{signals}]")
        print(f"\nOutput: {out_file}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
