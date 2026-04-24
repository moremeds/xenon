from decimal import Decimal

import pytest

from xenon.execution.combo_wizard import planner
from xenon.execution.combo_wizard.models import ComboLegQuote, ComboLegSpec


def test_planner_returns_natural_mid_and_ladder_for_supported_vertical():
    plan = planner.build_plan(
        ticker="AAPL",
        legs=[
            ComboLegSpec(
                contract_id="AAPL_20260417_200_C",
                action="BUY",
                right="C",
                strike=Decimal("200"),
                expiry="20260417",
                quantity=1,
            ),
            ComboLegSpec(
                contract_id="AAPL_20260417_210_C",
                action="SELL",
                right="C",
                strike=Decimal("210"),
                expiry="20260417",
                quantity=1,
            ),
        ],
        quotes={
            "AAPL_20260417_200_C": ComboLegQuote(
                bid=Decimal("4.50"),
                ask=Decimal("4.70"),
            ),
            "AAPL_20260417_210_C": ComboLegQuote(
                bid=Decimal("2.00"),
                ask=Decimal("2.20"),
            ),
        },
    )

    assert plan.structure_name == "Bull Call Spread"
    assert plan.mode == "COMBO"
    assert plan.natural_price == Decimal("2.70")
    assert plan.mid_price == Decimal("2.50")
    assert plan.ladder_step == Decimal("0.05")


def test_planner_rejects_ratio_vertical_as_unsupported():
    with pytest.raises(ValueError, match="Unsupported"):
        planner.build_plan(
            ticker="AAPL",
            legs=[
                ComboLegSpec(
                    contract_id="AAPL_20260417_200_C",
                    action="BUY",
                    right="C",
                    strike=Decimal("200"),
                    expiry="20260417",
                    quantity=1,
                ),
                ComboLegSpec(
                    contract_id="AAPL_20260417_210_C",
                    action="SELL",
                    right="C",
                    strike=Decimal("210"),
                    expiry="20260417",
                    quantity=2,
                ),
            ],
            quotes={
                "AAPL_20260417_200_C": ComboLegQuote(
                    bid=Decimal("4.50"),
                    ask=Decimal("4.70"),
                ),
                "AAPL_20260417_210_C": ComboLegQuote(
                    bid=Decimal("2.00"),
                    ask=Decimal("2.20"),
                ),
            },
        )


def test_planner_preserves_signed_prices_for_credit_vertical():
    plan = planner.build_plan(
        ticker="AAPL",
        legs=[
            ComboLegSpec(
                contract_id="AAPL_20260417_200_P",
                action="BUY",
                right="P",
                strike=Decimal("200"),
                expiry="20260417",
                quantity=1,
            ),
            ComboLegSpec(
                contract_id="AAPL_20260417_210_P",
                action="SELL",
                right="P",
                strike=Decimal("210"),
                expiry="20260417",
                quantity=1,
            ),
        ],
        quotes={
            "AAPL_20260417_200_P": ComboLegQuote(
                bid=Decimal("2.00"),
                ask=Decimal("2.20"),
            ),
            "AAPL_20260417_210_P": ComboLegQuote(
                bid=Decimal("4.50"),
                ask=Decimal("4.70"),
            ),
        },
    )

    assert plan.structure_name == "Bull Put Spread"
    assert plan.price_polarity == "CREDIT"
    assert plan.signed_natural_price == Decimal("-2.30")
    assert plan.signed_mid_price == Decimal("-2.50")
