"""Trend scanner — 3-stage pre-market trend scanner for swing trade identification.

Usage:
    python scripts/trend_scan.py --top 25
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

from scripts.scanner_lib.cache import write_json_cache
from scripts.scanner_lib.scoring import weighted_composite
from scripts.trend_scan_lib.config import TrendScanConfig
from scripts.trend_scan_lib.models import TrendCandidate
from scripts.trend_scan_lib.ranking import apply_min_thresholds, compute_final_score, rank_candidates
from scripts.trend_scan_lib.stages.flow_confirmation import compute_flow_score
from scripts.trend_scan_lib.stages.options_structure import compute_structure_score
from scripts.trend_scan_lib.stages.ta_prefilter import compute_trend_score, passes_bullish_gate
from scripts.trend_scan_lib.stages.volatility import compute_vol_score, suggest_trade_type
from scripts.trend_scan_lib.storage import (
    DEFAULT_DB_PATH,
    get_connection,
    init_schema,
    write_scan_candidates,
    write_scan_run,
)
from scripts.trend_scan_lib.universe import build_universe

logger = logging.getLogger(__name__)


class DataFetcher(Protocol):
    def fetch_ohlcv(self, ticker: str) -> dict: ...
    def fetch_structure(self, ticker: str) -> dict: ...
    def fetch_volatility(self, ticker: str) -> dict: ...
    def fetch_flow(self, ticker: str) -> dict: ...
    def fetch_market_context(self) -> dict: ...


def _generate_scan_id() -> str:
    now = datetime.now(timezone.utc)
    return f"trend_{now.strftime('%Y%m%d_%H%M')}"


def _stage_a(ticker: str, data_fetcher: DataFetcher, cfg: TrendScanConfig) -> Optional[dict]:
    try:
        ohlcv = data_fetcher.fetch_ohlcv(ticker)
    except Exception:
        logger.warning("Failed to fetch OHLCV for %s", ticker, exc_info=True)
        return None
    if not passes_bullish_gate(
        close=ohlcv.get("close", 0),
        ma_20=ohlcv.get("ma_20", 0),
        rsi=ohlcv.get("rsi", 0),
        dollar_volume=ohlcv.get("dollar_volume", 0),
        min_dollar_volume=cfg.min_dollar_volume,
    ):
        return None
    trend_score = compute_trend_score(ohlcv)
    ohlcv["trend_score"] = trend_score
    return ohlcv


def _stage_bc(ticker: str, ohlcv: dict, data_fetcher: DataFetcher) -> Optional[dict]:
    try:
        struct_data = data_fetcher.fetch_structure(ticker)
        structure_score, rejected = compute_structure_score(struct_data)
        if rejected:
            return None
        vol_data = data_fetcher.fetch_volatility(ticker)
        vol_score, vol_flags = compute_vol_score(vol_data)
        flow_data = data_fetcher.fetch_flow(ticker)
        flow_score = compute_flow_score(flow_data)
        capped = struct_data.get("call_wall", 0) > 0 and (
            (struct_data["call_wall"] - struct_data.get("spot", 0)) / max(struct_data.get("spot", 1), 1) < 0.05
        )
        trade_type = suggest_trade_type(
            iv_rank=vol_data.get("iv_rank", 50), term_structure=vol_data.get("term_structure", "flat"), capped=capped
        )
        return {
            "structure_score": structure_score,
            "vol_score": vol_score,
            "flow_score": flow_score,
            "vol_flags": vol_flags,
            "suggested_trade": trade_type,
            "struct_data": struct_data,
            "vol_data": vol_data,
            "flow_data": flow_data,
        }
    except Exception:
        logger.warning("Stage B/C failed for %s", ticker, exc_info=True)
        return None


def _trend_summary(ohlcv: dict) -> str:
    parts = []
    c, m20, m50, m200 = ohlcv.get("close", 0), ohlcv.get("ma_20", 0), ohlcv.get("ma_50", 0), ohlcv.get("ma_200", 0)
    if c > m20 > m50 > m200:
        parts.append("Full MA stack")
    elif c > m20 > m50:
        parts.append("Above 20/50 DMA")
    elif c > m20:
        parts.append("Above 20DMA")
    adx = ohlcv.get("adx", 0)
    if adx:
        parts.append(f"ADX {adx:.0f}")
    rs = ohlcv.get("rs_vs_spy", 0)
    if rs and rs != 1.0:
        parts.append(f"RS {rs:.2f} vs SPY")
    return ", ".join(parts) if parts else "N/A"


def _structure_summary(data: dict) -> str:
    parts = []
    spot = data.get("spot", 0)
    gf = data.get("gamma_flip", 0)
    if spot and gf:
        pct = ((spot - gf) / spot) * 100
        parts.append(f"{'Above' if pct > 0 else 'Below'} gamma flip by {abs(pct):.1f}%")
    cw = data.get("call_wall", 0)
    if spot and cw:
        pct = ((cw - spot) / spot) * 100
        parts.append(f"call wall at +{pct:.0f}%")
    pw = data.get("put_wall", 0)
    if spot and pw:
        pct = ((spot - pw) / spot) * 100
        parts.append(f"put support at -{pct:.0f}%")
    return ", ".join(parts) if parts else "N/A"


def _vol_summary(data: dict) -> str:
    parts = []
    ivr = data.get("iv_rank")
    if ivr is not None:
        parts.append(f"IV rank {ivr:.0f}")
    ts = data.get("term_structure")
    if ts:
        parts.append(f"{ts} term structure")
    ratio = data.get("iv_rv_ratio")
    if ratio:
        parts.append(f"IV/RV {ratio:.2f}")
    return ", ".join(parts) if parts else "N/A"


def _flow_summary(data: dict) -> str:
    parts = []
    cnt = data.get("flow_count", 0)
    if cnt:
        parts.append(f"{cnt} flow prints")
    ask = data.get("ask_dominance", 0)
    if ask:
        parts.append(f"{ask:.0%} ask-side")
    ecr = data.get("expiry_cluster_ratio", 0)
    if ecr >= 0.7:
        parts.append("clustered expiry")
    return ", ".join(parts) if parts else "N/A"


def run_scan_pipeline(
    cfg: TrendScanConfig,
    *,
    data_fetcher: DataFetcher,
    uw_client: Any = None,
    ib_client: Any = None,
    db_path: str = DEFAULT_DB_PATH,
    json_cache_path: Optional[str] = None,
) -> dict:
    start = time.monotonic()
    scan_id = _generate_scan_id()
    now = datetime.now(timezone.utc)

    universe = build_universe(cfg, uw_client=uw_client, ib_client=ib_client)

    stage_a_results: dict[str, dict] = {}
    for ticker in universe:
        result = _stage_a(ticker, data_fetcher, cfg)
        if result is not None:
            stage_a_results[ticker] = result
    stage_a_survivors = len(stage_a_results)

    candidates: list[TrendCandidate] = []
    for ticker, ohlcv in stage_a_results.items():
        bc = _stage_bc(ticker, ohlcv, data_fetcher)
        if bc is None:
            continue
        scores = {
            "trend": ohlcv["trend_score"],
            "structure": bc["structure_score"],
            "volatility": bc["vol_score"],
            "flow": bc["flow_score"],
        }
        final = compute_final_score(scores, cfg.weights)
        flags = list(bc.get("vol_flags", []))
        candidate = TrendCandidate(
            ticker=ticker,
            direction="bullish",
            final_score=final,
            scores=scores,
            spot_price=ohlcv.get("close", 0),
            indicators={
                "ma_20": ohlcv.get("ma_20", 0),
                "ma_50": ohlcv.get("ma_50", 0),
                "ma_200": ohlcv.get("ma_200", 0),
                "rsi": ohlcv.get("rsi", 0),
                "adx": ohlcv.get("adx", 0),
                "macd_histogram": ohlcv.get("macd_histogram", 0),
                "bbw": ohlcv.get("bbw", 0),
                "rs_vs_spy": ohlcv.get("rs_vs_spy", 0),
                "iv_rank": bc["vol_data"].get("iv_rank", 0),
                "gamma_flip": bc["struct_data"].get("gamma_flip", 0),
                "call_wall": bc["struct_data"].get("call_wall", 0),
                "put_wall": bc["struct_data"].get("put_wall", 0),
            },
            summaries={
                "trend": _trend_summary(ohlcv),
                "structure": _structure_summary(bc["struct_data"]),
                "vol": _vol_summary(bc["vol_data"]),
                "flow": _flow_summary(bc["flow_data"]),
            },
            suggested_trade=bc["suggested_trade"],
            invalidation=ohlcv.get("ma_20", 0),
            flags=flags,
        )
        candidates.append(candidate)
    stage_b_survivors = len(candidates)

    candidates = apply_min_thresholds(candidates, cfg.min_thresholds)
    ranked = rank_candidates(candidates, top_n=cfg.top_n)

    try:
        market_ctx = data_fetcher.fetch_market_context()
    except Exception:
        market_ctx = {"spy_close": 0, "vix_close": 0, "regime": "unknown"}

    duration = time.monotonic() - start

    output = {
        "scan_id": scan_id,
        "scan_timestamp": now.isoformat(),
        "market_context": market_ctx,
        "universe_size": len(universe),
        "stage_a_survivors": stage_a_survivors,
        "stage_b_survivors": stage_b_survivors,
        "candidates": [{**c.to_dict(), "snapshot_timestamp": now.isoformat()} for c in ranked],
    }

    try:
        conn = get_connection(db_path)
        init_schema(conn)
        write_scan_run(
            conn,
            {
                "scan_id": scan_id,
                "scan_timestamp": now,
                "universe_size": len(universe),
                "stage_a_pass": stage_a_survivors,
                "stage_b_pass": stage_b_survivors,
                "candidates_out": len(ranked),
                "spy_close": market_ctx.get("spy_close", 0),
                "vix_close": market_ctx.get("vix_close", 0),
                "regime": market_ctx.get("regime", "unknown"),
                "duration_secs": duration,
            },
        )
        write_scan_candidates(
            conn,
            [
                {
                    "scan_id": scan_id,
                    "ticker": c.ticker,
                    "snapshot_timestamp": now,
                    "spot_price": c.spot_price,
                    "direction": c.direction,
                    "final_score": c.final_score,
                    "trend_score": c.scores.get("trend", 0),
                    "structure_score": c.scores.get("structure", 0),
                    "vol_score": c.scores.get("volatility", 0),
                    "flow_score": c.scores.get("flow", 0),
                    **{k: v for k, v in c.indicators.items()},
                    "suggested_trade": c.suggested_trade,
                    "invalidation": c.invalidation,
                    "flags": c.flags,
                    "trend_summary": c.summaries.get("trend", ""),
                    "structure_summary": c.summaries.get("structure", ""),
                    "vol_summary": c.summaries.get("vol", ""),
                    "flow_summary": c.summaries.get("flow", ""),
                }
                for c in ranked
            ],
        )
        conn.close()
    except Exception:
        logger.warning("Failed to write to DuckDB", exc_info=True)

    if json_cache_path:
        try:
            write_json_cache(Path(json_cache_path), output)
        except Exception:
            logger.warning("Failed to write JSON cache", exc_info=True)

    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Trend scanner")
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--json-cache", default="data/trend_scan.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    cfg = TrendScanConfig(top_n=args.top)
    from scripts.clients.uw_client import UWClient

    uw_client = UWClient()
    logger.error("Real DataFetcher not yet wired — use via FastAPI POST /trend-scan")
    sys.exit(1)


if __name__ == "__main__":
    main()
