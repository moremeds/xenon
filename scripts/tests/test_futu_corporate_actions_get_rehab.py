"""Tests for FutuQuoteClient.fetch_corporate_actions via get_rehab."""

from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest

from xenon.clients.futu_quote_client import FutuQuoteClient


def test_fetch_splits_parses_ratio():
    quote_ctx = MagicMock()
    quote_ctx.get_rehab.return_value = (
        0,
        pd.DataFrame(
            [
                {
                    "ex_div_date": "2024-06-10",
                    "split_ratio": 10.0,
                    "join_ratio": 1.0,
                    "company_act_flag": 0x04,
                },  # 0x04 = split per Futu docs
            ]
        ),
    )
    c = FutuQuoteClient(quote_ctx=quote_ctx)
    actions = list(c.fetch_corporate_actions("US.NVDA"))
    assert len(actions) == 1
    a = actions[0]
    assert a.ticker == "NVDA"
    assert a.action_type == "split"
    assert a.effective_date == date(2024, 6, 10)
    assert float(a.ratio_numerator) == 10.0
    assert float(a.ratio_denominator) == 1.0


def test_fetch_reverse_split_flag():
    quote_ctx = MagicMock()
    quote_ctx.get_rehab.return_value = (
        0,
        pd.DataFrame(
            [
                {"ex_div_date": "2024-08-01", "split_ratio": 1.0, "join_ratio": 10.0, "company_act_flag": 0x08},
            ]
        ),
    )
    c = FutuQuoteClient(quote_ctx=quote_ctx)
    actions = list(c.fetch_corporate_actions("US.XYZ"))
    assert len(actions) == 1
    assert actions[0].action_type == "reverse_split"


def test_fetch_returns_empty_on_no_rows():
    quote_ctx = MagicMock()
    quote_ctx.get_rehab.return_value = (0, pd.DataFrame())
    c = FutuQuoteClient(quote_ctx=quote_ctx)
    assert list(c.fetch_corporate_actions("US.AAPL")) == []


def test_fetch_raises_on_sdk_error():
    quote_ctx = MagicMock()
    quote_ctx.get_rehab.return_value = (-1, "API error")
    c = FutuQuoteClient(quote_ctx=quote_ctx)
    with pytest.raises(RuntimeError):
        list(c.fetch_corporate_actions("US.AAPL"))
