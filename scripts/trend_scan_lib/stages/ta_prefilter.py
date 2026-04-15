"""Stage A: Technical Analysis trend prefilter and scoring."""

from __future__ import annotations

from scripts.scanner_lib.scoring import normalize_score


def score_ma_alignment(*, close: float, ma_20: float, ma_50: float, ma_200: float) -> float:
    """Score moving average stack alignment. Full stack (close > 20 > 50 > 200) = 1.0."""
    if close > ma_20 > ma_50 > ma_200:
        return 1.0
    if close > ma_20 > ma_50 and ma_50 > ma_200:
        return 0.7
    if close > ma_20:
        return 0.5
    if close < ma_20 < ma_50 < ma_200:
        return 0.0
    return 0.2


def score_rsi(rsi: float) -> float:
    """Score RSI. Peak at 58-65 (constructive trend), tapers outside."""
    if 58 <= rsi <= 65:
        return 1.0
    if 50 <= rsi < 58:
        return 0.7 + (rsi - 50) * 0.0375
    if 65 < rsi <= 70:
        return 1.0 - (rsi - 65) * 0.06
    if 45 <= rsi < 50:
        return 0.5
    if 70 < rsi <= 80:
        return 0.4
    if 40 <= rsi < 45:
        return 0.3
    return 0.1


def score_adx(adx: float) -> float:
    """Score ADX trend strength. >25 = strong, >40 = very strong."""
    if adx >= 40:
        return 1.0
    if adx >= 25:
        return 0.6 + (adx - 25) * 0.0267
    if adx >= 20:
        return 0.4 + (adx - 20) * 0.04
    return normalize_score(adx / 20 * 0.4)


def score_macd(*, macd: float, signal: float, histogram: float) -> float:
    """Score MACD. Above signal + positive histogram = 1.0."""
    if macd > signal and histogram > 0:
        return 1.0
    if macd > signal:
        return 0.7
    if histogram > 0:
        return 0.5
    return 0.0


def score_relative_strength(rs_ratio: float) -> float:
    """Score relative strength vs SPY. RS > 1.0 = outperforming."""
    if rs_ratio >= 1.2:
        return 1.0
    if rs_ratio >= 1.0:
        return 0.5 + (rs_ratio - 1.0) * 2.5
    if rs_ratio >= 0.9:
        return 0.3
    return 0.1


def score_slope(ma_series: list[float]) -> float:
    """Score 20DMA slope over recent days. Positive slope = good."""
    if len(ma_series) < 2:
        return 0.5
    first, last = ma_series[0], ma_series[-1]
    if first == 0:
        return 0.5
    pct_change = (last - first) / first
    if pct_change > 0.02:
        return 1.0
    if pct_change > 0.005:
        return 0.7
    if pct_change > -0.005:
        return 0.5
    if pct_change > -0.02:
        return 0.3
    return 0.1


def score_volume_profile(
    *,
    recent_avg_volume: float,
    avg_20d_volume: float,
    recent_up_ratio: float,
    up_day_volume_ratio: float = 1.0,
) -> float:
    """Score volume profile. Three signals, last one weighted 2x:
    - Volume pickup (recent vs 20d) — trend attention.
    - Up-day frequency (recent_up_ratio) — directional bias.
    - Up-day vs down-day volume (up_day_volume_ratio) — accumulation vs distribution.
    """
    if avg_20d_volume == 0:
        return 0.5
    vol_ratio = recent_avg_volume / avg_20d_volume
    vol_score = normalize_score(vol_ratio - 0.5)
    up_score = normalize_score(recent_up_ratio * 1.5 - 0.25)
    # up_day_volume_ratio typically 0.3–2.5; 1.0 = neutral, 1.5+ = accumulation, 0.7- = distribution.
    accumulation_score = normalize_score((up_day_volume_ratio - 0.7) / 1.0)
    return (vol_score + up_score + 2 * accumulation_score) / 4


def score_bbw(bbw: float) -> float:
    """Score Bollinger Band Width. Narrow = squeeze = pending breakout."""
    if bbw <= 0.03:
        return 1.0
    if bbw <= 0.06:
        return 0.8
    if bbw <= 0.10:
        return 0.5
    if bbw <= 0.15:
        return 0.3
    return 0.1


def detect_breakout(
    *,
    close: float,
    high_52w: float,
    high_20d: float,
    range_20d_pct: float,
    atr_pct: float,
) -> bool:
    """Detect breakout.

    Two qualifying paths:
      1. Within 3% of 52w high — price is punching through long-term resistance.
      2. Close is above 20d high AND the 20d range was tight — coiled spring release.

    Previous version accepted path 2 on consolidation narrowness alone,
    which flagged stocks sitting mid-range in a tight band as 'breakouts'."""
    near_52w = high_52w > 0 and (high_52w - close) / high_52w <= 0.03
    tight_range = atr_pct > 0 and range_20d_pct < atr_pct * 3
    above_20d_high = high_20d > 0 and close >= high_20d
    consolidation_break = tight_range and above_20d_high
    return near_52w or consolidation_break


def passes_bullish_gate(
    *,
    close: float,
    ma_20: float,
    rsi: float,
    dollar_volume: float,
    min_dollar_volume: float,
) -> bool:
    """Hard gate: close > 20DMA, RSI > 40, dollar volume above floor."""
    return close > ma_20 and rsi > 40 and dollar_volume >= min_dollar_volume


INDICATOR_WEIGHTS = {
    "ma_alignment": 0.20,
    "slope": 0.10,
    "rsi": 0.15,
    "adx": 0.15,
    "macd": 0.10,
    "relative_strength": 0.10,
    "volume_profile": 0.10,
    "bbw": 0.10,
}
BREAKOUT_BONUS = 0.1


def compute_trend_score(indicators: dict) -> float:
    """Compute composite trend score from raw indicators."""
    scores = {
        "ma_alignment": score_ma_alignment(
            close=indicators["close"],
            ma_20=indicators["ma_20"],
            ma_50=indicators["ma_50"],
            ma_200=indicators["ma_200"],
        ),
        "slope": score_slope(indicators.get("ma_20_series", [])),
        "rsi": score_rsi(indicators["rsi"]),
        "adx": score_adx(indicators["adx"]),
        "macd": score_macd(
            macd=indicators["macd"],
            signal=indicators["macd_signal"],
            histogram=indicators["macd_histogram"],
        ),
        "relative_strength": score_relative_strength(indicators.get("rs_vs_spy", 1.0)),
        "volume_profile": score_volume_profile(
            recent_avg_volume=indicators.get("recent_avg_volume", 0),
            avg_20d_volume=indicators.get("avg_20d_volume", 1),
            recent_up_ratio=indicators.get("recent_up_ratio", 0.5),
            up_day_volume_ratio=indicators.get("up_day_volume_ratio", 1.0),
        ),
        "bbw": score_bbw(indicators.get("bbw", 0.10)),
    }

    composite = sum(scores[k] * w for k, w in INDICATOR_WEIGHTS.items())

    if detect_breakout(
        close=indicators["close"],
        high_52w=indicators.get("high_52w", 0),
        high_20d=indicators.get("high_20d", 0),
        range_20d_pct=indicators.get("range_20d_pct", 1.0),
        atr_pct=indicators.get("atr_pct", 0),
    ):
        composite += BREAKOUT_BONUS

    return normalize_score(composite)
