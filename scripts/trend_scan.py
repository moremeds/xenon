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


def _build_price_frame(bars: list[dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for bar in bars:
        dt = str(bar.get("date") or bar.get("datetime") or "")[:10]
        close = _safe_float(bar.get("close"), default=float("nan"))
        high = _safe_float(bar.get("high"), default=close)
        low = _safe_float(bar.get("low"), default=close)
        open_ = _safe_float(bar.get("open"), default=close)
        volume = _safe_float(bar.get("volume") or bar.get("vol"), default=0.0)
        if not dt or not pd.notna(close):
            continue
        rows.append(
            {
                "date": dt,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )
    if not rows:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    frame = pd.DataFrame(rows).sort_values("date").drop_duplicates(subset=["date"], keep="last")
    frame.reset_index(drop=True, inplace=True)
    return frame


def _series_value(series: pd.Series, default: float = 0.0) -> float:
    if series.empty:
        return default
    value = series.iloc[-1]
    return default if pd.isna(value) else float(value)


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

    def __init__(self, *, uw_client: Any):
        self.uw_client = uw_client
        self._bars_cache: dict[str, pd.DataFrame] = {}
        self._stock_info_cache: dict[str, dict[str, Any]] = {}
        self._ticker_data_cache: dict[str, Any] = {}
        self._oi_change_cache: dict[str, list[dict[str, Any]]] = {}
        self._greek_flow_cache: dict[str, tuple[float, float]] = {}

    def _bars_frame(self, ticker: str) -> pd.DataFrame:
        upper = ticker.upper()
        if upper not in self._bars_cache:
            payload = self.uw_client.get_stock_ohlc(upper, candle_size="1d")
            bars = payload.get("data", []) if isinstance(payload, dict) else []
            self._bars_cache[upper] = _build_price_frame(bars if isinstance(bars, list) else [])
        return self._bars_cache[upper]

    def _stock_info(self, ticker: str) -> dict[str, Any]:
        upper = ticker.upper()
        if upper not in self._stock_info_cache:
            payload = self.uw_client.get_stock_info(upper)
            info = payload.get("data", {}) if isinstance(payload, dict) else {}
            self._stock_info_cache[upper] = info if isinstance(info, dict) else {}
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
        frame = self._bars_frame(ticker)
        if frame.empty or len(frame) < 30:
            raise RuntimeError(f"insufficient OHLCV history for {ticker}")

        closes = frame["close"]
        highs = frame["high"]
        lows = frame["low"]
        volumes = frame["volume"].fillna(0.0)

        ma_20 = closes.rolling(20, min_periods=20).mean()
        ma_50 = closes.rolling(50, min_periods=50).mean()
        ma_200 = closes.rolling(200, min_periods=200).mean()

        delta = closes.diff()
        gains = delta.clip(lower=0)
        losses = (-delta).clip(lower=0)
        avg_gain = gains.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        avg_loss = losses.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        rs = avg_gain / avg_loss.where(avg_loss != 0, pd.NA)
        rsi = 100 - (100 / (1 + rs))
        rsi = rsi.fillna(50.0)
        rsi = rsi.where(avg_loss != 0, 100.0)
        rsi = rsi.where(avg_gain != 0, 0.0)
        rsi = rsi.where((avg_gain != 0) | (avg_loss != 0), 50.0)

        ema_12 = closes.ewm(span=12, adjust=False).mean()
        ema_26 = closes.ewm(span=26, adjust=False).mean()
        macd = ema_12 - ema_26
        macd_signal = macd.ewm(span=9, adjust=False).mean()
        macd_histogram = macd - macd_signal

        prev_close = closes.shift(1)
        true_range = pd.concat(
            [
                highs - lows,
                (highs - prev_close).abs(),
                (lows - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr = true_range.rolling(14, min_periods=14).mean()
        atr_pct = (_series_value(atr) / max(_series_value(closes, default=1.0), 1.0)) if not atr.empty else 0.0

        plus_dm = highs.diff()
        minus_dm = -lows.diff()
        plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
        minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
        tr14 = true_range.rolling(14, min_periods=14).sum()
        plus_di = 100 * plus_dm.rolling(14, min_periods=14).sum() / tr14.replace(0, pd.NA)
        minus_di = 100 * minus_dm.rolling(14, min_periods=14).sum() / tr14.replace(0, pd.NA)
        dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, pd.NA)) * 100
        adx = dx.rolling(14, min_periods=14).mean().fillna(0.0)

        bb_mean = closes.rolling(20, min_periods=20).mean()
        bb_std = closes.rolling(20, min_periods=20).std(ddof=0)
        upper_band = bb_mean + 2 * bb_std
        lower_band = bb_mean - 2 * bb_std
        bbw = ((upper_band - lower_band) / bb_mean.replace(0, pd.NA)).fillna(0.0)

        benchmark_frame = self._bars_frame("SPY") if ticker.upper() != "SPY" else None
        rs_vs_spy = 1.0
        if benchmark_frame is not None and len(benchmark_frame) >= 21 and len(frame) >= 21:
            benchmark = benchmark_frame.set_index("date")["close"]
            aligned = pd.concat([frame.set_index("date")["close"], benchmark], axis=1, join="inner").tail(40)
            aligned.columns = ["stock", "spy"]
            if len(aligned) >= 21:
                stock_return = aligned["stock"].iloc[-1] / aligned["stock"].iloc[-21]
                spy_return = aligned["spy"].iloc[-1] / aligned["spy"].iloc[-21]
                if spy_return > 0:
                    rs_vs_spy = float(stock_return / spy_return)

        ma_20_series = ma_20.dropna().tail(5).tolist()
        recent_up_ratio = float((delta.tail(10) > 0).mean()) if len(delta.tail(10)) else 0.5
        recent_avg_volume = float(volumes.tail(5).mean()) if len(volumes.tail(5)) else 0.0
        avg_20d_volume = float(volumes.tail(20).mean()) if len(volumes.tail(20)) else recent_avg_volume
        range_20d_pct = (
            (float(highs.tail(20).max()) - float(lows.tail(20).min())) / max(_series_value(closes, default=1.0), 1.0)
        )

        info = self._stock_info(ticker)
        market_cap = _safe_float(info.get("market_cap") or info.get("marketCap"))

        return {
            "ticker": ticker.upper(),
            "close": _series_value(closes),
            "ma_20": _series_value(ma_20),
            "ma_50": _series_value(ma_50),
            "ma_200": _series_value(ma_200),
            "rsi": _series_value(rsi, default=50.0),
            "adx": _series_value(adx),
            "macd": _series_value(macd),
            "macd_signal": _series_value(macd_signal),
            "macd_histogram": _series_value(macd_histogram),
            "rs_vs_spy": rs_vs_spy,
            "ma_20_series": [float(v) for v in ma_20_series],
            "recent_avg_volume": recent_avg_volume,
            "avg_20d_volume": avg_20d_volume,
            "recent_up_ratio": recent_up_ratio,
            "bbw": _series_value(bbw),
            "high_52w": float(highs.tail(252).max()) if not highs.empty else _series_value(closes),
            "range_20d_pct": range_20d_pct,
            "atr_pct": atr_pct,
            "dollar_volume": _series_value(closes) * avg_20d_volume,
            "market_cap": market_cap,
            "price": _series_value(closes),
        }

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


def _stage_a(ticker: str, data_fetcher: DataFetcher, cfg: TrendScanConfig) -> Optional[dict]:
    try:
        ohlcv = data_fetcher.fetch_ohlcv(ticker)
    except Exception:
        logger.warning("Failed to fetch OHLCV for %s", ticker, exc_info=True)
        return None
    if ohlcv.get("close", 0) < cfg.min_price or ohlcv.get("market_cap", 0) < cfg.min_market_cap:
        return None
    if not passes_bullish_gate(
        close=ohlcv.get("close", 0),
        ma_20=ohlcv.get("ma_20", 0),
        rsi=ohlcv.get("rsi", 0),
        dollar_volume=ohlcv.get("dollar_volume", 0),
        min_dollar_volume=cfg.min_dollar_volume,
    ):
        return None
    ohlcv["trend_score"] = compute_trend_score(ohlcv)
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

        spot = max(_safe_float(struct_data.get("spot"), default=0.0), 1.0)
        call_wall = _safe_float(struct_data.get("call_wall"))
        capped = call_wall > 0 and ((call_wall - spot) / spot) < 0.05
        trade_type = suggest_trade_type(
            iv_rank=vol_data.get("iv_rank", 50),
            term_structure=vol_data.get("term_structure", "flat"),
            capped=capped,
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
    c = ohlcv.get("close", 0)
    m20 = ohlcv.get("ma_20", 0)
    m50 = ohlcv.get("ma_50", 0)
    m200 = ohlcv.get("ma_200", 0)
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
    if ohlcv.get("high_52w", 0) and (ohlcv["high_52w"] - c) / max(ohlcv["high_52w"], 1) <= 0.03:
        parts.append("breakout flag")
    return ", ".join(parts) if parts else "N/A"


def _structure_summary(data: dict) -> str:
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


def _flow_summary(data: dict) -> str:
    parts = []
    if data.get("flow_count"):
        parts.append(f"{data['flow_count']} ask-side prints")
    if data.get("expiry_cluster_ratio", 0) >= 0.7:
        parts.append("clustered 1-4 week expiry")
    if data.get("dp_direction") == "bullish":
        parts.append("dark-pool alignment")
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

    stage_a_pairs = parallel_fetch(
        items=universe,
        fn=lambda ticker: (ticker, _stage_a(ticker, data_fetcher, cfg)),
        max_workers=cfg.max_workers,
    )
    stage_a_results = {ticker: result for ticker, result in stage_a_pairs if result is not None}
    stage_a_survivors = len(stage_a_results)

    stage_bc_pairs = parallel_fetch(
        items=list(stage_a_results.items()),
        fn=lambda item: (item[0], _stage_bc(item[0], item[1], data_fetcher)),
        max_workers=cfg.max_workers,
    )

    candidates: list[TrendCandidate] = []
    for ticker, bc in stage_bc_pairs:
        if bc is None:
            continue
        ohlcv = stage_a_results[ticker]
        scores = {
            "trend": ohlcv["trend_score"],
            "structure": bc["structure_score"],
            "volatility": bc["vol_score"],
            "flow": bc["flow_score"],
        }
        candidate = TrendCandidate(
            ticker=ticker,
            direction="bullish",
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
                "trend": _trend_summary(ohlcv),
                "structure": _structure_summary(bc["struct_data"]),
                "vol": _vol_summary(bc["vol_data"]),
                "flow": _flow_summary(bc["flow_data"]),
            },
            suggested_trade=bc["suggested_trade"],
            invalidation=ohlcv.get("ma_20", 0),
            flags=list(bc.get("vol_flags", [])),
        )
        candidates.append(candidate)
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
        "universe_size": len(universe),
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
                    "suggested_trade": candidate.suggested_trade,
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
    from scripts.clients.uw_client import UWClient
    from dotenv import load_dotenv

    project_root = Path(_project_root)
    load_dotenv(project_root / ".env")
    load_dotenv(project_root / "web" / ".env")
    uw_client = UWClient()
    return LiveTrendDataFetcher(uw_client=uw_client), uw_client, None


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
        data_fetcher, uw_client, ib_client = build_runtime()
        result = run_scan_pipeline(
            TrendScanConfig(top_n=args.top),
            data_fetcher=data_fetcher,
            uw_client=uw_client,
            ib_client=ib_client,
            db_path=args.db_path,
            json_cache_path=args.json_cache,
        )
        print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        logger.error("Trend scan failed: %s", exc)
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
