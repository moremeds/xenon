"""Trend scanner CLI and pipeline orchestration."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

import pandas as pd

# Ensure project root is on sys.path when run as subprocess from scripts/.
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from scripts.analysis.ticker_data import fetch_ticker_data
from scripts.scanner_lib.cache import write_json_cache
from scripts.scanner_lib.executor import parallel_fetch
from scripts.ta_lib.apex_sync import sync_if_stale
from scripts.trend_scan_lib.config import TrendScanConfig
from scripts.trend_scan_lib.models import TrendCandidate
from scripts.trend_scan_lib.ranking import apply_min_thresholds, compute_final_score, rank_candidates
from scripts.trend_scan_lib.stages.catalysts import fetch_catalysts
from scripts.trend_scan_lib.stages.flow_confirmation import compute_flow_score
from scripts.trend_scan_lib.stages.options_structure import compute_structure_score
from scripts.trend_scan_lib.stages.ta_prefilter import (
    compute_trend_score,
    passes_bearish_gate,
    passes_bullish_gate,
)
from scripts.trend_scan_lib.stages.volatility import compute_vol_score
from scripts.trend_scan_lib.storage import (
    DEFAULT_DB_PATH,
    get_connection,
    init_schema,
    write_scan_candidates,
    write_scan_run,
)
from scripts.trend_scan_lib.universe import load_universe_from_mirror

logger = logging.getLogger(__name__)


class DataFetcher(Protocol):
    def fetch_ohlcv(self, ticker: str) -> dict: ...
    def fetch_structure(self, ticker: str) -> dict: ...
    def fetch_volatility(self, ticker: str) -> dict: ...
    def fetch_flow(self, ticker: str) -> dict: ...
    def fetch_market_context(self) -> dict: ...


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _generate_scan_id() -> str:
    now = datetime.now(timezone.utc)
    return f"trend_{now.strftime('%Y%m%d_%H%M')}"


def _parse_option_right(payload: dict[str, Any]) -> str:
    option_type = str(payload.get("option_type") or payload.get("optionType") or "").lower()
    if option_type in {"call", "put"}:
        return option_type
    if payload.get("is_call") is True:
        return "call"
    if payload.get("is_put") is True:
        return "put"
    symbol = str(payload.get("option_symbol") or payload.get("symbol") or "")
    if len(symbol) >= 9 and symbol[-9:-8] in {"C", "P"}:
        return "call" if symbol[-9:-8] == "C" else "put"
    return "call"


def _parse_expiry_date(payload: dict[str, Any]) -> Optional[date]:
    raw = payload.get("expiry") or payload.get("expiration_date") or payload.get("expiry_date")
    if isinstance(raw, str):
        token = raw[:10]
        try:
            return date.fromisoformat(token)
        except ValueError:
            pass

    symbol = str(payload.get("option_symbol") or "")
    if len(symbol) >= 10:
        digits = symbol[-15:-9]
        if digits.isdigit():
            try:
                year = 2000 + int(digits[:2])
                return date(year, int(digits[2:4]), int(digits[4:6]))
            except ValueError:
                return None
    return None


def _term_structure_shape(rows: Optional[list[dict[str, Any]]]) -> str:
    if not rows:
        return "flat"
    frame_rows: list[tuple[str, float]] = []
    for row in rows:
        expiry = str(row.get("expiry") or row.get("expiration_date") or row.get("date") or "")
        iv = _safe_float(
            row.get("iv")
            or row.get("atm_iv")
            or row.get("implied_volatility")
            or row.get("volatility")
            or row.get("value"),
            default=float("nan"),
        )
        if expiry and pd.notna(iv):
            frame_rows.append((expiry, iv))
    if len(frame_rows) < 2:
        return "flat"
    frame_rows.sort(key=lambda item: item[0])
    front = frame_rows[0][1]
    back = frame_rows[-1][1]
    if back >= front * 1.05:
        return "normal"
    if back <= front * 0.95:
        return "inverted"
    return "flat"


def _option_oi_change_totals(rows: list[dict[str, Any]]) -> tuple[float, float]:
    call_total = 0.0
    put_total = 0.0
    for row in rows:
        delta = _safe_float(row.get("oi_diff_plain") or row.get("oi_diff"))
        if _parse_option_right(row) == "put":
            put_total += delta
        else:
            call_total += delta
    return call_total, put_total


def _compute_dark_pool_direction(darkpool_rows: list[dict[str, Any]]) -> str:
    buy_volume = 0.0
    sell_volume = 0.0
    for row in darkpool_rows:
        size = _safe_float(row.get("size"), default=0.0)
        price = _safe_float(row.get("price"), default=0.0)
        bid = _safe_float(row.get("nbbo_bid"), default=0.0)
        ask = _safe_float(row.get("nbbo_ask"), default=0.0)
        if size <= 0 or price <= 0 or bid <= 0 or ask <= 0:
            continue
        midpoint = (bid + ask) / 2
        if price >= midpoint:
            buy_volume += size
        else:
            sell_volume += size
    total = buy_volume + sell_volume
    if total <= 0:
        return "neutral"
    ratio = buy_volume / total
    if ratio >= 0.55:
        return "bullish"
    if ratio <= 0.45:
        return "bearish"
    return "neutral"


def _compute_greek_flow_totals(rows: Any) -> tuple[float, float]:
    if isinstance(rows, dict):
        data_rows = rows.get("data") if isinstance(rows.get("data"), list) else []
    elif isinstance(rows, list):
        data_rows = rows
    else:
        data_rows = []

    if not data_rows:
        return 0.0, 0.0

    net_delta = 0.0
    net_vega = 0.0
    for row in data_rows:
        if not isinstance(row, dict):
            continue
        delta = row.get("net_delta") or row.get("delta") or row.get("delta_notional") or 0
        vega = row.get("net_vega") or row.get("vega") or row.get("vega_notional") or 0
        net_delta += _safe_float(delta)
        net_vega += _safe_float(vega)
    return net_delta, net_vega


class LiveTrendDataFetcher:
    """Best-effort live fetcher backed by Unusual Whales with graceful degradation."""

    def __init__(self, *, uw_client: Any, ta_service: Any = None):
        self.uw_client = uw_client
        self._ta_service = ta_service
        self._spy_df: Optional[pd.DataFrame] = None
        self._stock_info_cache: dict[str, dict[str, Any]] = {}
        self._ticker_data_cache: dict[str, Any] = {}
        self._oi_change_cache: dict[str, list[dict[str, Any]]] = {}
        self._greek_flow_cache: dict[str, tuple[float, float]] = {}

    def pre_cache_spy(self) -> None:
        """Cache SPY indicator DataFrame for rs_vs_spy calculations.

        Failure is non-fatal — RS benchmark is a nice-to-have; scan proceeds
        with rs_vs_spy=1.0 fallback if SPY is cold and IB unavailable.
        """
        if self._ta_service is None:
            return
        try:
            self._spy_df = self._ta_service.get_indicators("SPY")
        except Exception as exc:
            logger.warning("pre_cache_spy: SPY unavailable (%s) — falling back to rs_vs_spy=1.0", exc)
            self._spy_df = None

    def _stock_info(self, ticker: str) -> dict[str, Any]:
        upper = ticker.upper()
        if upper not in self._stock_info_cache:
            payload = self.uw_client.get_stock_info(upper)
            raw = payload.get("data", {}) if isinstance(payload, dict) else {}
            if isinstance(raw, list):
                info = raw[0] if raw else {}
            elif isinstance(raw, dict):
                info = raw
            else:
                info = {}
            self._stock_info_cache[upper] = info
        return self._stock_info_cache[upper]

    def _analysis_snapshot(self, ticker: str):
        upper = ticker.upper()
        if upper not in self._ticker_data_cache:
            self._ticker_data_cache[upper] = fetch_ticker_data(upper, self.uw_client, deep=True)
        return self._ticker_data_cache[upper]

    def _oi_changes(self, ticker: str) -> list[dict[str, Any]]:
        upper = ticker.upper()
        if upper not in self._oi_change_cache:
            try:
                payload = self.uw_client.get_stock_oi_change(upper) or {}
                rows = payload.get("data", []) if isinstance(payload, dict) else []
                self._oi_change_cache[upper] = rows if isinstance(rows, list) else []
            except Exception:
                logger.debug("stock_oi_change failed for %s", upper, exc_info=True)
                self._oi_change_cache[upper] = []
        return self._oi_change_cache[upper]

    def _greek_flow(self, ticker: str) -> tuple[float, float]:
        upper = ticker.upper()
        if upper not in self._greek_flow_cache:
            try:
                payload = self.uw_client.get_greek_flow(upper)
            except Exception:
                logger.debug("greek_flow failed for %s", upper, exc_info=True)
                payload = {}
            self._greek_flow_cache[upper] = _compute_greek_flow_totals(payload)
        return self._greek_flow_cache[upper]

    def fetch_ohlcv(self, ticker: str) -> dict:
        if self._ta_service is None:
            raise RuntimeError("TAService not configured — cannot fetch OHLCV")

        snapshot = self._ta_service.get_snapshot(ticker)
        if snapshot is None:
            return None

        # rs_vs_spy: cross-ticker logic using pre-cached SPY DataFrame.
        # New parquet schema uses the `close` column on the OHLCV (historical)
        # parquet, not the indicators parquet; pull closes from get_ohlcv for
        # both SPY and the ticker. spy_df (indicators) is only used to gate
        # the length check.
        rs_vs_spy = 1.0
        if ticker.upper() != "SPY" and self._spy_df is not None:
            try:
                spy_ohlcv = self._ta_service.get_ohlcv("SPY")
                ticker_ohlcv = self._ta_service.get_ohlcv(ticker)
                if (
                    spy_ohlcv is not None
                    and ticker_ohlcv is not None
                    and len(spy_ohlcv) >= 21
                    and len(ticker_ohlcv) >= 21
                ):
                    spy_closes = spy_ohlcv.set_index("timestamp")["close"]
                    ticker_closes = ticker_ohlcv.set_index("timestamp")["close"]
                    aligned = pd.concat([ticker_closes, spy_closes], axis=1, join="inner").tail(40)
                    aligned.columns = ["stock", "spy"]
                    if len(aligned) >= 21:
                        stock_return = aligned["stock"].iloc[-1] / aligned["stock"].iloc[-21]
                        spy_return = aligned["spy"].iloc[-1] / aligned["spy"].iloc[-21]
                        if spy_return > 0:
                            rs_vs_spy = float(stock_return / spy_return)
            except Exception:
                logger.debug("rs_vs_spy calculation failed for %s", ticker, exc_info=True)

        snapshot["rs_vs_spy"] = rs_vs_spy

        info = self._stock_info(ticker)
        snapshot["market_cap"] = _safe_float(info.get("marketcap") or info.get("market_cap") or info.get("marketCap"))

        return snapshot

    def fetch_structure(self, ticker: str) -> dict:
        snapshot = self._analysis_snapshot(ticker)
        call_oi_change, put_oi_change = _option_oi_change_totals(self._oi_changes(ticker))
        strikes = snapshot.gex_by_strike.get("strikes", []) if snapshot.gex_by_strike else []
        nearest_gamma = 0.0
        if strikes and snapshot.price is not None:
            nearest = min(strikes, key=lambda row: abs(_safe_float(row.get("strike")) - snapshot.price))
            nearest_gamma = abs(_safe_float(nearest.get("gamma")))

        return {
            "spot": _safe_float(snapshot.price),
            "gamma_flip": _safe_float((snapshot.gex or {}).get("flip")),
            "call_wall": _safe_float(snapshot.call_wall_strike),
            "put_wall": _safe_float(snapshot.put_wall_strike),
            "max_pain": _safe_float(snapshot.max_pain),
            "net_gex": _safe_float((snapshot.gex or {}).get("net")),
            "net_call_oi_change": call_oi_change,
            "net_put_oi_change": put_oi_change,
            "gex_at_spot": nearest_gamma,
        }

    def fetch_volatility(self, ticker: str) -> dict:
        snapshot = self._analysis_snapshot(ticker)
        earnings_days = None
        if snapshot.earnings_date is not None:
            earnings_days = (snapshot.earnings_date - date.today()).days
        iv = _safe_float(snapshot.iv)
        rv = _safe_float(snapshot.rv)
        iv_rv_ratio = (iv / rv) if iv > 0 and rv > 0 else 1.0
        return {
            "iv_rank": _safe_float(snapshot.iv_rank or snapshot.iv_percentile, default=50.0),
            "term_structure": _term_structure_shape(snapshot.term_structure),
            "iv_rv_ratio": iv_rv_ratio,
            "earnings_days": earnings_days,
        }

    def fetch_flow(self, ticker: str) -> dict:
        snapshot = self._analysis_snapshot(ticker)
        alerts = snapshot.flow_alerts or []
        spot = max(_safe_float(snapshot.price, default=0.0), 1.0)

        relevant = [row for row in alerts if _parse_option_right(row) == "call"] or alerts
        ask_side_count = 0
        expiry_cluster_hits = 0
        strike_distances: list[float] = []
        premium_bias = 0.0
        for row in relevant:
            is_ask_side = row.get("is_ask_side")
            side = str(row.get("side") or row.get("execution_estimate") or "").lower()
            if is_ask_side is True or side in {"ask", "above_ask", "at_ask"}:
                ask_side_count += 1
            expiry = _parse_expiry_date(row)
            if expiry is not None and 0 <= (expiry - date.today()).days <= 28:
                expiry_cluster_hits += 1
            strike = _safe_float(row.get("strike") or row.get("strike_price"), default=0.0)
            if strike > 0:
                strike_distances.append(abs(strike - spot) / spot)
            premium = _safe_float(row.get("premium") or row.get("total_premium"), default=0.0)
            premium_bias += premium if _parse_option_right(row) == "call" else -premium

        ask_dominance = ask_side_count / len(relevant) if relevant else 0.5
        expiry_cluster_ratio = expiry_cluster_hits / len(relevant) if relevant else 0.0
        avg_strike_pct_otm = sum(strike_distances) / len(strike_distances) if strike_distances else 0.10

        net_delta, net_vega = self._greek_flow(ticker)
        if net_delta == 0 and premium_bias != 0:
            net_delta = premium_bias
            net_vega = abs(premium_bias)

        darkpool_rows = snapshot.darkpool.get("data", []) if snapshot.darkpool else []
        return {
            "ask_dominance": ask_dominance,
            "flow_count": len(relevant),
            "expiry_cluster_ratio": expiry_cluster_ratio,
            "avg_strike_pct_otm": avg_strike_pct_otm,
            "net_delta": net_delta,
            "net_vega": net_vega,
            "dp_direction": _compute_dark_pool_direction(darkpool_rows if isinstance(darkpool_rows, list) else []),
        }

    def fetch_market_context(self) -> dict:
        spy_snapshot = self.fetch_ohlcv("SPY")
        market_context = {
            "spy_close": spy_snapshot.get("close", 0.0),
            "vix_close": 0.0,
            "regime": "bullish" if spy_snapshot.get("close", 0.0) >= spy_snapshot.get("ma_20", 0.0) else "bearish",
        }

        cri_path = Path(_project_root) / "data" / "cri.json"
        if cri_path.exists():
            try:
                payload = json.loads(cri_path.read_text())
                market_context["vix_close"] = _safe_float(payload.get("vix"), default=0.0)
                level = str((payload.get("cri") or {}).get("level") or "").lower()
                if level:
                    market_context["regime"] = level
            except Exception:
                logger.debug("Failed to read data/cri.json for market context", exc_info=True)
        return market_context


def _stage_a_data(
    ticker: str,
    universe_row: dict,
    data_fetcher: DataFetcher,
    cfg: TrendScanConfig,
) -> Optional[dict]:
    """Direction-neutral parquet read + liquidity/size/tier floor (joined from universe)."""
    try:
        ohlcv = data_fetcher.fetch_ohlcv(ticker)
    except Exception:
        logger.warning("Stage A fetch failed for %s", ticker, exc_info=True)
        return None
    if ohlcv is None:
        return None
    if ohlcv.get("close", 0) < cfg.min_price:
        return None
    if universe_row.get("dollar_volume", 0) < cfg.min_dollar_volume:
        return None
    if universe_row.get("marketCap", 0) < cfg.min_market_cap:
        return None
    if universe_row.get("turnover_rate", 0) < cfg.min_turnover_rate:
        return None
    if universe_row.get("tier") in cfg.exclude_tiers:
        return None
    return ohlcv


def _stage_a_gate(ohlcv: dict, direction: str, cfg: TrendScanConfig) -> Optional[dict]:
    """Direction-specific gate + trend score. Returns ohlcv with trend_score
    attached if the direction's gate passes, else None."""
    if direction == "bullish":
        if not passes_bullish_gate(
            close=ohlcv["close"],
            ma_20=ohlcv["ma_20"],
            rsi=ohlcv["rsi"],
            dollar_volume=ohlcv["dollar_volume"],
            min_dollar_volume=cfg.min_dollar_volume,
        ):
            return None
    else:
        if not passes_bearish_gate(
            close=ohlcv["close"],
            ma_20=ohlcv["ma_20"],
            rsi=ohlcv["rsi"],
            dollar_volume=ohlcv["dollar_volume"],
            min_dollar_volume=cfg.min_dollar_volume,
        ):
            return None
    result = dict(ohlcv)
    result["trend_score"] = compute_trend_score(ohlcv, direction=direction)
    return result


def _stage_bc(ticker: str, ohlcv: dict, direction: str, data_fetcher: DataFetcher) -> Optional[dict]:
    try:
        struct_data = data_fetcher.fetch_structure(ticker)
        structure_score, rejected = compute_structure_score(struct_data, direction=direction)
        if rejected:
            return None

        vol_data = data_fetcher.fetch_volatility(ticker)
        vol_score, vol_flags = compute_vol_score(vol_data)
        flow_data = data_fetcher.fetch_flow(ticker)
        flow_score = compute_flow_score(flow_data, direction=direction)

        return {
            "structure_score": structure_score,
            "vol_score": vol_score,
            "flow_score": flow_score,
            "vol_flags": vol_flags,
            "struct_data": struct_data,
            "vol_data": vol_data,
            "flow_data": flow_data,
        }
    except Exception:
        logger.warning("Stage B/C failed for %s", ticker, exc_info=True)
        return None


def _trend_summary(ohlcv: dict, *, direction: str = "bullish") -> str:
    parts = []
    c = ohlcv.get("close", 0)
    m20 = ohlcv.get("ma_20", 0)
    m50 = ohlcv.get("ma_50", 0)
    m200 = ohlcv.get("ma_200", 0)
    if direction == "bearish":
        if c < m20 < m50 < m200:
            parts.append("Full MA stack (bearish)")
        elif c < m20 < m50:
            parts.append("Below 20/50 DMA")
        elif c < m20:
            parts.append("Below 20DMA")
        if ohlcv.get("low_52w", 0) and (c - ohlcv["low_52w"]) / max(ohlcv["low_52w"], 1) <= 0.03:
            parts.append("breakdown flag")
    else:
        if c > m20 > m50 > m200:
            parts.append("Full MA stack")
        elif c > m20 > m50:
            parts.append("Above 20/50 DMA")
        elif c > m20:
            parts.append("Above 20DMA")
        if ohlcv.get("high_52w", 0) and (ohlcv["high_52w"] - c) / max(ohlcv["high_52w"], 1) <= 0.03:
            parts.append("breakout flag")
    adx = ohlcv.get("adx", 0)
    if adx:
        parts.append(f"ADX {adx:.0f}")
    rs = ohlcv.get("rs_vs_spy", 0)
    if rs and rs != 1.0:
        parts.append(f"RS {rs:.2f} vs SPY")
    return ", ".join(parts) if parts else "N/A"


def _structure_summary(data: dict, *, direction: str = "bullish") -> str:
    parts = []
    spot = data.get("spot", 0)
    gf = data.get("gamma_flip", 0)
    if spot and gf:
        pct = ((spot - gf) / max(spot, 1)) * 100
        parts.append(f"{'Above' if pct >= 0 else 'Below'} gamma flip by {abs(pct):.1f}%")
    call_wall = data.get("call_wall", 0)
    if spot and call_wall:
        parts.append(f"call wall at +{((call_wall - spot) / max(spot, 1)) * 100:.0f}%")
    put_wall = data.get("put_wall", 0)
    if spot and put_wall:
        parts.append(f"put support at -{((spot - put_wall) / max(spot, 1)) * 100:.0f}%")
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


def _flow_summary(data: dict, *, direction: str = "bullish") -> str:
    parts = []
    if data.get("flow_count"):
        parts.append(f"{data['flow_count']} ask-side prints")
    if data.get("expiry_cluster_ratio", 0) >= 0.7:
        parts.append("clustered 1-4 week expiry")
    dp = data.get("dp_direction", "neutral")
    if direction == "bearish":
        if dp == "bearish":
            parts.append("dark-pool aligned bearish")
    else:
        if dp == "bullish":
            parts.append("dark-pool alignment")
    return ", ".join(parts) if parts else "N/A"


def _filter_universe_to_covered(
    mirror_dir: Path,
    universe_rows: list[dict],
    timeframes: tuple[str, ...] = ("1d",),
) -> tuple[list[dict], list[str]]:
    """A19: Split universe into (has_parquet, missing_symbols). Scanner warns
    about missing and proceeds with covered rows.
    """
    covered: list[dict] = []
    missing: list[str] = []
    for row in universe_rows:
        sym = row.get("symbol")
        if not sym:
            continue
        if all((mirror_dir / "parquet" / "historical" / tf / f"{sym}.parquet").exists() for tf in timeframes):
            covered.append(row)
        else:
            missing.append(sym)
    return covered, missing


def _infer_structure_hint(direction: str, bc: dict, ohlcv: dict) -> str:
    """Return a defined-risk long-side structure hint.

    Never emits short premium — that would fail Gate 4 (naked short cover)
    if taken literally at order-entry time. Hint is informational only;
    actual structure selection happens at order-build time under Four Gates."""
    iv_rank = bc.get("vol_data", {}).get("iv_rank", 0.5)
    high_iv = iv_rank >= 0.6
    if direction == "bullish":
        return "long_call_vertical" if high_iv else "long_call"
    if direction == "bearish":
        return "long_put_vertical" if high_iv else "long_put"
    return ""


def _compute_invalidation(direction: str, ohlcv: dict) -> float:
    """Price level at which the signal is invalidated. 20DMA for both
    directions (bullish: close below = trend broken; bearish: close above
    = thesis broken)."""
    return float(ohlcv.get("ma_20", 0.0))


def run_scan_pipeline(
    cfg: TrendScanConfig,
    *,
    data_fetcher: DataFetcher,
    uw_client: Any = None,
    ib_client: Any = None,
    db_path: str = DEFAULT_DB_PATH,
    json_cache_path: Optional[str] = None,
    ta_service: Any = None,
) -> dict:
    start = time.monotonic()
    scan_id = _generate_scan_id()
    now = datetime.now(timezone.utc)

    # Sync R2 mirror (apex_sync handles R2 outage fallback per A15).
    mirror_dir = Path(_project_root) / "data" / "apex_mirror"
    sync_result = sync_if_stale(mirror_dir=mirror_dir)
    if sync_result.errors:
        logger.warning("Apex sync errors: %s", sync_result.errors)

    universe_rows = load_universe_from_mirror(mirror_dir)
    covered_rows, missing_symbols = _filter_universe_to_covered(mirror_dir, universe_rows, timeframes=("1d",))
    if missing_symbols:
        logger.warning(
            "A19: %d universe tickers missing from mirror (e.g. %s) — skipping",
            len(missing_symbols),
            ", ".join(missing_symbols[:5]),
        )

    universe_by_symbol: dict[str, dict] = {r["symbol"]: r for r in covered_rows}
    universe_symbols = list(universe_by_symbol.keys())

    # Pre-cache SPY DataFrame so worker threads don't each query DuckDB for it
    if hasattr(data_fetcher, "pre_cache_spy"):
        data_fetcher.pre_cache_spy()

    # Stage A data fetch — direction-neutral, runs once per ticker.
    stage_a_data_pairs = parallel_fetch(
        items=universe_symbols,
        fn=lambda ticker: (
            ticker,
            _stage_a_data(ticker, universe_by_symbol[ticker], data_fetcher, cfg),
        ),
        max_workers=cfg.max_workers,
    )
    stage_a_base = {ticker: result for ticker, result in stage_a_data_pairs if result is not None}

    candidates: list[TrendCandidate] = []
    stage_a_survivors_set: set[str] = set()
    for direction in ("bullish", "bearish"):
        gated_map: dict[str, dict] = {}
        for ticker, ohlcv in stage_a_base.items():
            gated = _stage_a_gate(ohlcv, direction, cfg)
            if gated is not None:
                gated_map[ticker] = gated
                stage_a_survivors_set.add(ticker)

        bc_pairs = parallel_fetch(
            items=list(gated_map.keys()),
            fn=lambda ticker: (ticker, _stage_bc(ticker, gated_map[ticker], direction, data_fetcher)),
            max_workers=cfg.max_workers,
        )

        for ticker, bc in bc_pairs:
            if bc is None:
                continue
            ohlcv = gated_map[ticker]
            catalysts, catalyst_score = fetch_catalysts(
                ticker=ticker,
                direction=direction,
                uw_client=uw_client,
                earnings_days=bc["vol_data"].get("earnings_days", 30),
            )
            scores = {
                "trend": ohlcv["trend_score"],
                "structure": bc["structure_score"],
                "volatility": bc["vol_score"],
                "flow": bc["flow_score"],
                "catalyst": catalyst_score,
            }
            candidate = TrendCandidate(
                ticker=ticker,
                direction=direction,
                final_score=compute_final_score(scores, cfg.weights),
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
                    "trend": _trend_summary(ohlcv, direction=direction),
                    "structure": _structure_summary(bc["struct_data"], direction=direction),
                    "vol": _vol_summary(bc["vol_data"]),
                    "flow": _flow_summary(bc["flow_data"], direction=direction),
                },
                structure_hint=_infer_structure_hint(direction, bc, ohlcv),
                invalidation=_compute_invalidation(direction, ohlcv),
                flags=list(bc.get("vol_flags", [])),
                catalysts=catalysts,
            )
            candidates.append(candidate)
    stage_a_survivors = len(stage_a_survivors_set)
    stage_b_survivors = len(candidates)

    ranked = rank_candidates(apply_min_thresholds(candidates, cfg.min_thresholds), top_n=cfg.top_n)

    try:
        market_ctx = data_fetcher.fetch_market_context()
    except Exception:
        logger.warning("Failed to fetch market context", exc_info=True)
        market_ctx = {"spy_close": 0, "vix_close": 0, "regime": "unknown"}

    duration = time.monotonic() - start
    output = {
        "scan_id": scan_id,
        "scan_timestamp": now.isoformat(),
        "market_context": market_ctx,
        "universe_size": len(universe_symbols),
        "stage_a_survivors": stage_a_survivors,
        "stage_b_survivors": stage_b_survivors,
        "candidates": [{**candidate.to_dict(), "snapshot_timestamp": now.isoformat()} for candidate in ranked],
    }

    try:
        conn = get_connection(db_path)
        init_schema(conn)
        write_scan_run(
            conn,
            {
                "scan_id": scan_id,
                "scan_timestamp": now,
                "universe_size": len(universe_symbols),
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
                    "ticker": candidate.ticker,
                    "snapshot_timestamp": now,
                    "spot_price": candidate.spot_price,
                    "direction": candidate.direction,
                    "final_score": candidate.final_score,
                    "trend_score": candidate.scores.get("trend", 0),
                    "structure_score": candidate.scores.get("structure", 0),
                    "vol_score": candidate.scores.get("volatility", 0),
                    "flow_score": candidate.scores.get("flow", 0),
                    **candidate.indicators,
                    "structure_hint": candidate.structure_hint,
                    "catalysts": candidate.catalysts,
                    "invalidation": candidate.invalidation,
                    "flags": candidate.flags,
                    "trend_summary": candidate.summaries.get("trend", ""),
                    "structure_summary": candidate.summaries.get("structure", ""),
                    "vol_summary": candidate.summaries.get("vol", ""),
                    "flow_summary": candidate.summaries.get("flow", ""),
                }
                for candidate in ranked
            ],
        )
        conn.close()
    except Exception:
        logger.warning("Failed to write trend scan storage", exc_info=True)

    if json_cache_path:
        try:
            write_json_cache(Path(json_cache_path), output)
        except Exception:
            logger.warning("Failed to write JSON cache", exc_info=True)

    return output


def build_runtime():
    from dotenv import load_dotenv

    from scripts.clients.ib_client import IBClient
    from scripts.clients.uw_client import UWClient
    from scripts.ta_lib import TAService

    project_root = Path(_project_root)
    load_dotenv(project_root / ".env")
    load_dotenv(project_root / "web" / ".env")
    uw_client = UWClient()

    ib_client = IBClient()
    try:
        ib_client.connect(client_id="auto")
    except Exception:
        logger.warning("IB Gateway not available — scanner continues without IB")
        ib_client = None

    ta_service = TAService(mirror_dir=project_root / "data" / "apex_mirror")
    data_fetcher = LiveTrendDataFetcher(
        uw_client=uw_client,
        ta_service=ta_service,
    )
    return data_fetcher, uw_client, ib_client, ta_service


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Trend scanner")
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--json-cache", default="data/trend_scan.json")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    uw_client = None
    ib_client = None
    try:
        data_fetcher, uw_client, ib_client, ta_service = build_runtime()
        result = run_scan_pipeline(
            TrendScanConfig(top_n=args.top),
            data_fetcher=data_fetcher,
            uw_client=uw_client,
            ib_client=ib_client,
            db_path=args.db_path,
            json_cache_path=args.json_cache,
            ta_service=ta_service,
        )
        print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        logger.error("Trend scan failed: %s", exc, exc_info=True)
        return 1
    finally:
        if uw_client is not None:
            close_fn = getattr(uw_client, "close", None)
            if callable(close_fn):
                close_fn()
        if ib_client is not None:
            close_fn = getattr(ib_client, "disconnect", None)
            if callable(close_fn):
                close_fn()


if __name__ == "__main__":
    sys.exit(main())
