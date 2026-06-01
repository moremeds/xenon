"""C27: fx_daily must be populated for the window BEFORE cash flow is pulled.

A non-USD cash flow row on a date with no fx_daily row would have to write an
anomaly. The sync orchestrator must hit FRED before iterating cash flow so the
common case (HKD/CNY rates available on FRED) never falls through to anomaly.
"""

from datetime import date
from unittest.mock import MagicMock

import pandas as pd

from xenon.api.services.futu_history import futu_history_sync
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_fx_backfill_called_before_cash_flow(clean_scope, monkeypatch):
    """Verify call order: FX backfill must precede get_acc_cash_flow."""
    call_order: list[str] = []

    def fake_backfill(currency, start, end):
        call_order.append(f"fx:{currency}")
        return 0

    trade_ctx = MagicMock()
    trade_ctx.history_deal_list_query.return_value = (0, pd.DataFrame())

    def fake_get_acc_cash_flow(**kw):
        call_order.append("cash_flow")
        return (0, pd.DataFrame())

    trade_ctx.get_acc_cash_flow.side_effect = fake_get_acc_cash_flow
    trade_ctx.accinfo_query.return_value = (
        0,
        pd.DataFrame([{"total_assets": 0, "cash": 0, "unrealized_pl": 0}]),
    )
    trade_ctx.position_list_query.return_value = (0, pd.DataFrame())
    trade_ctx.close.return_value = None

    quote_ctx = MagicMock()
    quote_ctx.close.return_value = None

    monkeypatch.setattr(
        "xenon.api.services.futu_history._build_trade_ctx",
        lambda: trade_ctx,
    )
    monkeypatch.setattr(
        "xenon.api.services.futu_history._build_quote_ctx",
        lambda: quote_ctx,
    )
    monkeypatch.setattr(
        "xenon.api.services.futu_history.backfill_fx_series",
        fake_backfill,
    )

    futu_history_sync(
        scope=SCOPE,
        since=date(2024, 5, 1),
        until_date=date(2024, 5, 1),
    )

    # Both HKD and CNY FX must be backfilled before the first cash-flow call.
    fx_calls = [c for c in call_order if c.startswith("fx:")]
    cf_calls_idx = [i for i, c in enumerate(call_order) if c == "cash_flow"]
    last_fx_idx = max(i for i, c in enumerate(call_order) if c.startswith("fx:"))
    assert {"fx:HKD", "fx:CNY"} <= set(fx_calls)
    assert cf_calls_idx, "get_acc_cash_flow was never called"
    assert last_fx_idx < cf_calls_idx[0], f"FX backfill must precede cash flow; got call_order={call_order}"
