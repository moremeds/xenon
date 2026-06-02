from datetime import date
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "research"
    / "validate_futu_official_performance.py"
)
spec = spec_from_file_location("validate_futu_official_performance", SCRIPT)
assert spec and spec.loader
mod = module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def test_official_time_weighted_formula_matches_futu_help_example():
    nav_points = [
        mod.NavPoint(date(2026, 1, 1), Decimal("100")),
        mod.NavPoint(date(2026, 1, 2), Decimal("150")),
        mod.NavPoint(date(2026, 1, 3), Decimal("1050")),
    ]
    cashflows = [
        mod.CashFlow(date(2026, 1, 3), "Others", "<blank>", Decimal("1000")),
    ]

    perf = mod.official_performance(
        nav_points,
        cashflows,
        classification_mode="others-blank",
    )

    assert perf.daily[0].daily_return == Decimal("0.5")
    assert perf.daily[1].daily_return.quantize(Decimal("0.0001")) == Decimal("-0.1538")
    assert perf.time_weighted_return.quantize(Decimal("0.0001")) == Decimal("0.2692")


def test_official_simple_formula_uses_half_period_net_inflow_denominator():
    nav_points = [
        mod.NavPoint(date(2026, 1, 1), Decimal("100")),
        mod.NavPoint(date(2026, 1, 2), Decimal("1050")),
    ]
    cashflows = [
        mod.CashFlow(date(2026, 1, 2), "Others", "<blank>", Decimal("1000")),
    ]

    perf = mod.official_performance(
        nav_points,
        cashflows,
        classification_mode="others-blank",
    )

    assert perf.income == Decimal("-50")
    assert perf.simple_return == Decimal("-50") / Decimal("600")


def test_official_formula_can_use_trade_income_instead_of_nav_diff():
    nav_points = [
        mod.NavPoint(date(2026, 1, 1), Decimal("100")),
        mod.NavPoint(date(2026, 1, 2), Decimal("150")),
        mod.NavPoint(date(2026, 1, 3), Decimal("1050")),
    ]
    cashflows = [
        mod.CashFlow(date(2026, 1, 3), "Others", "<blank>", Decimal("1000")),
    ]
    daily_income = {
        date(2026, 1, 2): Decimal("40"),
        date(2026, 1, 3): Decimal("-10"),
    }

    perf = mod.official_performance_from_daily_income(
        nav_points,
        cashflows,
        daily_income_by_date=daily_income,
        classification_mode="others-blank",
    )

    assert perf.income == Decimal("30")
    assert perf.daily[0].daily_return == Decimal("0.4")
    assert perf.daily[1].daily_return == Decimal("-10") / Decimal("650")
    assert perf.simple_return == Decimal("30") / Decimal("600")


def test_opend_cashflow_rows_map_to_formula_inputs():
    rows = [
        {
            "cashflow_type": "Others",
            "amount": 123.45,
            "occurred_at": date(2026, 6, 2),
            "raw": {"cashflow_remark": ""},
        },
        {
            "cashflow_type": "Cash Dividend",
            "amount": 6.78,
            "occurred_at": date(2026, 6, 2),
            "raw": {"cashflow_remark": "AAPL"},
        },
    ]

    flows = mod.cashflows_from_opend_rows(rows)

    assert flows == [
        mod.CashFlow(date(2026, 6, 2), "Others", "<blank>", Decimal("123.45")),
        mod.CashFlow(date(2026, 6, 2), "Cash Dividend", "AAPL", Decimal("6.78")),
    ]


def test_can_compute_period_requires_two_nav_points():
    assert not mod.can_compute_period([])
    assert not mod.can_compute_period([mod.NavPoint(date(2026, 6, 2), Decimal("100"))])
    assert mod.can_compute_period(
        [
            mod.NavPoint(date(2026, 6, 1), Decimal("100")),
            mod.NavPoint(date(2026, 6, 2), Decimal("101")),
        ]
    )
