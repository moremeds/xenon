from xenon.reports.performance_explainer_report import (
    build_html,
    chart_family_contract,
    chart_role_color,
    load_chart_system,
)

# Minimal payload matching the shape build_sections() reads. Hand-curated
# because data/performance.json is gitignored runtime state; seeding it
# from a fixture decouples the test from live portfolio data.
_MINIMAL_PAYLOAD = {
    "as_of": "2099-01-01",
    "last_sync": "2099-01-01T00:00:00Z",
    "period_start": "2099-01-01",
    "period_end": "2099-01-01",
    "period_label": "YTD",
    "benchmark": "SPY",
    "benchmark_total_return": 0.0,
    "trades_source": "ib_flex",
    "price_sources": {"stocks": "IB", "options": "IB"},
    "methodology": {
        "curve_type": "equity",
        "return_basis": "time_weighted",
        "risk_free_rate": 0.0,
        "library_strategy": "empyrical",
    },
    "summary": {
        "starting_equity": 100000.0,
        "ending_equity": 100000.0,
        "pnl": 0.0,
        "total_return": 0.0,
        "trading_days": 1,
        "sharpe_ratio": 0.0,
        "annualized_volatility": 0.0,
        "sortino_ratio": 0.0,
        "downside_deviation": 0.0,
        "max_drawdown": 0.0,
        "max_drawdown_duration_days": 0,
        "beta": 0.0,
        "alpha": 0.0,
        "information_ratio": 0.0,
        "tracking_error": 0.0,
        "calmar_ratio": 0.0,
        "current_drawdown": 0.0,
        "var_95": 0.0,
        "cvar_95": 0.0,
        "tail_ratio": 0.0,
        "ulcer_index": 0.0,
        "worst_day": 0.0,
        "hit_rate": 0.0,
        "upside_capture": 0.0,
        "downside_capture": 0.0,
        "correlation": 0.0,
        "skew": 0.0,
        "kurtosis": 0.0,
        "max_drawdown_start": "2099-01-01",
        "max_drawdown_end": "2099-01-01",
        "win_rate": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "profit_factor": 0.0,
        "total_trades": 0,
        "best_day_return": 0.0,
        "worst_day_return": 0.0,
        "best_day_date": "2099-01-01",
        "worst_day_date": "2099-01-01",
        "sessions": 0,
    },
    "contracts_missing_history": [],
    "warnings": [],
    "series": [
        {
            "date": "2099-01-01",
            "equity": 100000.0,
            "daily_return": 0.0,
            "drawdown": 0.0,
            "benchmark_close": 100.0,
            "benchmark_return": 0.0,
        }
    ],
}


def test_chart_family_contract_comes_from_shared_chart_system():
    chart_system = load_chart_system()

    contract = chart_family_contract(chart_system, "analytical-time-series")

    assert contract["id"] == "analytical-time-series"
    assert contract["label"] == "Analytical Time Series"
    assert contract["renderer"] == "svg"
    assert contract["requires_axes"] is True
    assert contract["renderer_description"] == chart_system["sanctionedRenderers"]["svg"]
    assert chart_role_color(chart_system, "comparison") == chart_system["seriesRoles"]["comparison"]["fallback"]


def test_build_html_mentions_shared_chart_contract():
    chart_system = load_chart_system()

    report_html = build_html(_MINIMAL_PAYLOAD, chart_system)

    assert "web/lib/chart-system-spec.json" in report_html
    assert "PRIMARY / COMPARISON" in report_html
    assert "ANALYTICAL TIME SERIES" in report_html
    assert "This panel uses the shared chart-system contract" in report_html
