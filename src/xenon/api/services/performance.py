"""Inline FastAPI service computing /performance from xenon.nav_history.

Spec: docs/superpowers/specs/2026-05-31-performance-rebuild-design.md

Corrections applied (see plan's PRE-EXECUTION CORRECTIONS):
  - #5: returns[0] zeroed (no prior NAV)
  - #6: _build_series uses the already-computed returns array
  - #7: every async test must have pytestmark (callers responsibility)
  - #14: benchmark-relative metrics computed via date-join, not tail alignment
  - #15: all PerformanceSummary nullable fields are populated when not masked
  - Phase 0 env gate (XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS) selects whether
    IB risk metrics ship (default true = mask, safe-by-default)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Optional

import numpy as np
import pandas as pd
from sqlalchemy.ext.asyncio import AsyncEngine

from xenon.db.queries.futu_history import list_cashflows
from xenon.db.queries.nav_history import load_benchmark_cached, load_nav_curve
from xenon.execution.account_scope import AccountScope
from xenon.reports import performance_metrics as M
from xenon.utils.market_calendar import current_session_date_et

logger = logging.getLogger(__name__)

PERIODS_PER_YEAR = 252

ANNUALIZED_RISK_FIELDS = (
    "sharpe_ratio", "sortino_ratio", "calmar_ratio",
    "annualized_return", "annualized_volatility", "downside_deviation",
    "var_95", "cvar_95", "tail_ratio", "ulcer_index",
)
BENCH_RELATIVE_FIELDS = (
    "beta", "alpha", "correlation", "r_squared",
    "tracking_error", "information_ratio", "treynor_ratio",
    "upside_capture", "downside_capture",
)
DISTRIBUTION_FIELDS = (
    "hit_rate", "positive_days", "negative_days", "flat_days",
    "best_day", "worst_day", "average_up_day", "average_down_day",
    "win_loss_ratio", "skew", "kurtosis",
)


@dataclass(frozen=True)
class FutuOfficialPerformance:
    returns: np.ndarray
    net_inflows: np.ndarray
    income_by_day: np.ndarray
    net_inflow: float
    income: float
    simple_return: float
    time_weighted_return: float


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key, "").strip().lower()
    if v in ("true", "1", "yes"):
        return True
    if v in ("false", "0", "no"):
        return False
    return default


def _ib_should_mask_metrics() -> bool:
    """Phase 0 gate (spec §8). Default True = safe-but-pessimistic.

    When True: IB risk metrics inherit the FUTU masking treatment (rendered
    as null with a warning). Set to False ONLY after empirical verification
    confirms dailyPnL excludes cash flows.
    """
    return _env_bool("XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS", True)


def _insufficient(*, reason: str, days_collected: int,
                  hero_net_liq: float | None, inception: str | None) -> dict[str, Any]:
    return {
        "status": "insufficient_history",
        "reason": reason,
        "days_collected": days_collected,
        "days_required_for_curve": _env_int("XENON_PERF_MIN_DAYS_CURVE", 5),
        "days_required_for_metrics": _env_int("XENON_PERF_MIN_DAYS_METRICS", 30),
        "inception_date": inception,
        "hero_net_liq": hero_net_liq,
        "currency": "USD",
    }


def _period_start(as_of: Optional[date]) -> date:
    today = as_of or current_session_date_et()
    return date(today.year, 1, 1)


def _period_label(inception: date) -> str:
    return (
        "YTD NAV Change"
        if inception <= date(inception.year, 1, 2)
        else "INCEPTION-TO-DATE NAV CHANGE"
    )


def _base_summary(nav: np.ndarray, n: int) -> dict[str, Any]:
    start, end = float(nav[0]), float(nav[-1])
    depth, dur, _ = M.max_drawdown(nav)
    peak = float(np.maximum.accumulate(nav)[-1])
    curr_dd = (end - peak) / peak if peak else 0.0
    out: dict[str, Any] = {
        "starting_equity": start,
        "ending_equity": end,
        "pnl": end - start,
        "total_return": (end - start) / start if start else 0.0,
        "trading_days": n,
        "max_drawdown": depth,
        "max_drawdown_duration_days": dur,
        "current_drawdown": curr_dd,
        "low_confidence": False,
        "sharpe_se": None,
        "sortino_se": None,
    }
    # initialize every nullable field
    for k in ANNUALIZED_RISK_FIELDS + BENCH_RELATIVE_FIELDS + DISTRIBUTION_FIELDS:
        out[k] = None
    return out


def _fill_annualized(summary: dict, returns: np.ndarray) -> None:
    """Populate all ANNUALIZED_RISK_FIELDS. None for fields requiring benchmark."""
    summary["sharpe_ratio"] = M.sharpe(returns)
    summary["sortino_ratio"] = M.sortino(returns)
    ann_ret = float(np.mean(returns) * PERIODS_PER_YEAR)
    ann_vol = float(np.std(returns, ddof=1) * np.sqrt(PERIODS_PER_YEAR)) if len(returns) > 1 else 0.0
    summary["annualized_return"] = ann_ret
    summary["annualized_volatility"] = ann_vol
    downside = returns[returns < 0]
    summary["downside_deviation"] = (
        float(np.std(downside, ddof=1) * np.sqrt(PERIODS_PER_YEAR))
        if len(downside) > 1 else 0.0
    )
    summary["var_95"], summary["cvar_95"] = M.var_cvar(returns, 0.05)
    summary["tail_ratio"] = M.tail_ratio(returns)
    # Need equity for ulcer / calmar.
    # calmar = annualized return / |max_drawdown| (re-use the summary value)
    md = summary.get("max_drawdown") or 0.0
    summary["calmar_ratio"] = (ann_ret / abs(md)) if md else 0.0


def _fill_bench_relative(summary: dict, joined: pd.DataFrame) -> None:
    """Populate BENCH_RELATIVE_FIELDS from a date-joined frame [nav_ret, bench_ret].

    Correction #14: pre-joined by date, no tail alignment.
    """
    if joined.empty or len(joined) < 2:
        return
    r = joined["nav_ret"].to_numpy()
    b = joined["bench_ret"].to_numpy()
    beta, alpha = M.beta_alpha(r, b)
    ir, te = M.information_ratio(r, b)
    up, down = M.upside_downside_capture(r, b)
    # correlation + r_squared
    if len(r) >= 2 and float(np.std(r, ddof=1)) > 0 and float(np.std(b, ddof=1)) > 0:
        corr = float(np.corrcoef(r, b)[0, 1])
    else:
        corr = 0.0
    summary["beta"] = beta
    summary["alpha"] = alpha
    summary["correlation"] = corr
    summary["r_squared"] = corr * corr
    summary["tracking_error"] = te
    summary["information_ratio"] = ir
    summary["upside_capture"] = up
    summary["downside_capture"] = down
    # treynor = ann_return / beta (re-use what's already in summary)
    ann_ret = summary.get("annualized_return")
    summary["treynor_ratio"] = (ann_ret / beta) if (beta and ann_ret is not None) else 0.0


def _fill_distribution(summary: dict, returns: np.ndarray) -> None:
    pos = int((returns > 0).sum())
    neg = int((returns < 0).sum())
    flat = int((returns == 0).sum())
    summary["positive_days"] = pos
    summary["negative_days"] = neg
    summary["flat_days"] = flat
    summary["hit_rate"] = pos / (pos + neg) if (pos + neg) else 0.0
    summary["best_day"] = float(returns.max()) if len(returns) else 0.0
    summary["worst_day"] = float(returns.min()) if len(returns) else 0.0
    pos_rets = returns[returns > 0]
    neg_rets = returns[returns < 0]
    summary["average_up_day"] = float(np.mean(pos_rets)) if len(pos_rets) else 0.0
    summary["average_down_day"] = float(np.mean(neg_rets)) if len(neg_rets) else 0.0
    summary["win_loss_ratio"] = (
        (summary["average_up_day"] / abs(summary["average_down_day"]))
        if summary["average_down_day"] else 0.0
    )
    summary["skew"] = M.skew(returns)
    summary["kurtosis"] = M.kurtosis(returns)


def _ib_returns(curve: pd.DataFrame) -> np.ndarray:
    """daily_pnl / prev_nav. returns[0] = 0 (correction #5)."""
    nav = curve["nav"].astype(float).to_numpy()
    dp = curve["daily_pnl"].astype(float).fillna(0.0).to_numpy()
    prev = np.concatenate(([nav[0]], nav[:-1]))
    returns = np.where(prev > 0, dp / prev, 0.0)
    if len(returns) > 0:
        returns[0] = 0.0  # correction #5
    return returns


def _futu_returns(curve: pd.DataFrame) -> np.ndarray:
    """(nav_t - nav_{t-1}) / nav_{t-1}. returns[0] = 0."""
    nav = curve["nav"].astype(float).to_numpy()
    if len(nav) < 1:
        return np.array([])
    prev = np.concatenate(([nav[0]], nav[:-1]))
    returns = np.where(prev > 0, (nav - prev) / prev, 0.0)
    returns[0] = 0.0
    return returns


def _is_futu_net_inflow(row: dict) -> bool:
    """Return True for Futu cashflows that change invested capital.

    Dividends, interest, taxes, and fees are investment income/costs; treating
    them as net inflow would subtract real performance from the numerator.
    The current OpenD backfill marks user cash movement as `Others` with an
    empty remark, which is the same conservative classification used by the
    Futu NAV backfill.
    """
    if str(row.get("cashflow_type") or "") != "Others":
        return False
    raw = row.get("raw") or {}
    remark = str(raw.get("cashflow_remark") or "").strip()
    return remark == ""


def _cashflow_date(row: dict) -> date:
    value = row.get("occurred_at")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date()
    if isinstance(value, date):
        return value
    return pd.to_datetime(value).date()


def _futu_official_performance(curve: pd.DataFrame, cashflows: list[dict]) -> FutuOfficialPerformance:
    """Futu official simple/time-weighted return formulas.

    Daily income = NAV[t] - NAV[t-1] - daily net inflow.
    Daily return = income / (NAV[t-1] + 0.5 * daily net inflow).
    TWR = product(1 + daily return) - 1.
    Simple return = period income / (start NAV + 0.5 * period net inflow).
    """
    nav = curve["nav"].astype(float).to_numpy()
    if len(nav) == 0:
        empty = np.array([])
        return FutuOfficialPerformance(empty, empty, empty, 0.0, 0.0, 0.0, 0.0)

    flows_by_date: dict[date, float] = {}
    for row in cashflows:
        if not _is_futu_net_inflow(row):
            continue
        d = _cashflow_date(row)
        flows_by_date[d] = flows_by_date.get(d, 0.0) + float(row.get("amount") or 0.0)

    returns = np.zeros(len(nav), dtype=float)
    net_inflows = np.zeros(len(nav), dtype=float)
    income_by_day = np.zeros(len(nav), dtype=float)
    twr = 1.0

    dates = list(curve["date"])
    for i in range(1, len(nav)):
        d = dates[i]
        flow = flows_by_date.get(d, 0.0)
        income = nav[i] - nav[i - 1] - flow
        denominator = nav[i - 1] + 0.5 * flow
        daily_return = income / denominator if denominator else 0.0
        net_inflows[i] = flow
        income_by_day[i] = income
        returns[i] = daily_return
        twr *= 1.0 + daily_return

    period_net_inflow = float(net_inflows.sum())
    period_income = float(income_by_day.sum())
    simple_denominator = nav[0] + 0.5 * period_net_inflow
    simple_return = period_income / simple_denominator if simple_denominator else 0.0

    return FutuOfficialPerformance(
        returns=returns,
        net_inflows=net_inflows,
        income_by_day=income_by_day,
        net_inflow=period_net_inflow,
        income=period_income,
        simple_return=simple_return,
        time_weighted_return=twr - 1.0,
    )


def _build_series(curve: pd.DataFrame, bench_df: pd.DataFrame | None,
                  returns: np.ndarray) -> list[dict[str, Any]]:
    """Wire-shape series. daily_return = returns[i] (correction #6)."""
    nav = curve["nav"].astype(float).to_numpy()
    peak = np.maximum.accumulate(nav)
    drawdown = (nav - peak) / peak

    bench_map: dict = {}
    if bench_df is not None and not bench_df.empty:
        bench_map = dict(zip(bench_df["date"], bench_df["close"].astype(float)))

    out: list[dict[str, Any]] = []
    prev_close: float | None = None
    for i, (_, row) in enumerate(curve.iterrows()):
        d = row["date"]
        close = bench_map.get(d)
        bret = None
        if close is not None and prev_close is not None and prev_close != 0:
            bret = (close - prev_close) / prev_close
        out.append({
            "date": str(d),
            "equity": float(nav[i]),
            "daily_return": float(returns[i]) if i < len(returns) else None,
            "drawdown": float(drawdown[i]),
            "benchmark_close": close,
            "benchmark_return": bret,
        })
        if close is not None:
            prev_close = close
    return out


def _bench_total_return(bench_df: pd.DataFrame | None) -> float | None:
    if bench_df is None or len(bench_df) < 2:
        return None
    first = float(bench_df["close"].iloc[0])
    last = float(bench_df["close"].iloc[-1])
    return (last - first) / first if first else None


async def compute(
    engine: AsyncEngine, scope: AccountScope, *, ib_pool=None, as_of: date | None = None,
) -> dict[str, Any]:
    """Build the PerformanceData dict for one (broker, account_env, broker_account)."""
    period_start = _period_start(as_of)
    curve = await load_nav_curve(engine, scope, period_start)
    days_collected = len(curve)
    min_curve = _env_int("XENON_PERF_MIN_DAYS_CURVE", 5)
    min_metrics = _env_int("XENON_PERF_MIN_DAYS_METRICS", 30)
    low_conf_threshold = _env_int("XENON_PERF_LOW_CONFIDENCE_DAYS", 126)

    if days_collected < min_curve:
        return _insufficient(
            reason="collecting",
            days_collected=days_collected,
            hero_net_liq=float(curve["nav"].iloc[-1]) if days_collected else None,
            inception=str(curve["date"].iloc[0]) if days_collected else None,
        )

    bench_df, bench_err = await load_benchmark_cached(engine, ib_pool, "SPY", period_start)

    futu_official: FutuOfficialPerformance | None = None
    if scope.broker == "IB":
        returns = _ib_returns(curve)
    else:
        cashflows = await list_cashflows(
            engine,
            scope,
            since=datetime.combine(period_start, time.min, tzinfo=timezone.utc),
        )
        futu_official = _futu_official_performance(curve, cashflows)
        returns = futu_official.returns

    nav = curve["nav"].astype(float).to_numpy()
    summary = _base_summary(nav, days_collected)
    if futu_official is not None:
        summary["pnl"] = futu_official.income
        summary["total_return"] = futu_official.time_weighted_return
        summary["simple_return"] = futu_official.simple_return
        summary["time_weighted_return"] = futu_official.time_weighted_return
        summary["net_inflow"] = futu_official.net_inflow

    warnings: list[str] = []
    metrics_unlocked = days_collected >= min_metrics
    ib_mask = scope.broker == "IB" and _ib_should_mask_metrics()
    if ib_mask:
        warnings.append(
            "IB TWR requires cash-flow tracking — follow-up. "
            "See docs/superpowers/reports/2026-06-01-ib-dailypnl-verification.md."
        )

    risk_masked = ib_mask or not metrics_unlocked
    if not risk_masked:
        # First-pass returns excluding the synthetic returns[0]
        returns_for_metrics = returns[1:] if len(returns) > 1 else returns
        _fill_annualized(summary, returns_for_metrics)
        _fill_distribution(summary, returns_for_metrics)

        if bench_df is not None and not bench_df.empty:
            # Build date-joined frame (correction #14)
            curve_ret_df = pd.DataFrame({"date": curve["date"], "nav_ret": returns})
            bench_ret_df = bench_df.copy()
            bench_ret_df["bench_ret"] = bench_ret_df["close"].astype(float).pct_change()
            joined = curve_ret_df.merge(bench_ret_df[["date", "bench_ret"]], on="date", how="inner")
            joined = joined.dropna(subset=["bench_ret"])
            if not joined.empty:
                _fill_bench_relative(summary, joined)
        if bench_err:
            warnings.append(f"benchmark_unavailable: {bench_err}")

    # Low-confidence indicator (spec §4)
    summary["low_confidence"] = (
        not risk_masked and metrics_unlocked and days_collected < low_conf_threshold
    )
    if summary["low_confidence"]:
        summary["sharpe_se"] = M.sharpe_se(days_collected, PERIODS_PER_YEAR)
        summary["sortino_se"] = M.sharpe_se(days_collected, PERIODS_PER_YEAR)

    series = _build_series(curve, bench_df, returns)

    return {
        "status": "ok",
        "as_of": str(current_session_date_et()),
        "last_sync": str(curve["date"].iloc[-1]),
        "period_start": str(period_start),
        "period_end": str(curve["date"].iloc[-1]),
        "period_label": "YTD RETURN" if scope.broker == "FUTU" else _period_label(curve["date"].iloc[0]),
        "scope": {
            "broker": scope.broker,
            "account_env": scope.account_env,
            "broker_account": scope.broker_account,
        },
        "currency": "USD",
        "benchmark": "SPY" if bench_df is not None and not bench_df.empty else None,
        "benchmark_total_return": _bench_total_return(bench_df),
        "trades_source": "nav_history",
        "methodology": {
            "basis": "Futu official TWR" if scope.broker == "FUTU" else "NAV change",
            "annualization_periods": PERIODS_PER_YEAR,
        },
        "price_sources": {"primary": "nav_history", "benchmark": "ib_historical_daily"},
        "summary": summary,
        "series": series,
        "warnings": warnings,
        "contracts_missing_history": [],
    }
