"""A backfilled older BUY after later SELLs computed realized_pnl triggers
rematch on next sync — no operator action.
"""

from datetime import date, datetime
from decimal import Decimal

from xenon.api.services.futu_history import detect_tickers_needing_rematch
from xenon.db.queries.futu_history import insert_fills, update_realized_pnl
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")


def _row(deal_id, action, qty, price, t):
    return dict(
        futu_deal_id=deal_id,
        futu_order_id="o",
        ticker="AAPL",
        futu_code="US.AAPL",
        market="US",
        action=action,
        quantity=qty,
        price=Decimal(str(price)),
        fees=Decimal("0"),
        filled_at=datetime.fromisoformat(t),
        raw={},
    )


def test_late_buy_triggers_rematch_signal(clean_scope):
    # Forward-FIFO ran: BUY then SELL on 5/02, realized_pnl recorded.
    insert_fills(
        SCOPE,
        [
            _row("d1", "BUY", 100, 150, "2024-05-02T14:00:00+00:00"),
            _row("d2", "SELL", 100, 160, "2024-05-02T15:00:00+00:00"),
        ],
    )
    update_realized_pnl(SCOPE, "d2", Decimal("1000"))

    # Late-arriving BUY backfilled on 5/01.
    insert_fills(
        SCOPE,
        [
            _row("d0", "BUY", 50, 100, "2024-05-01T13:00:00+00:00"),
        ],
    )

    tickers = detect_tickers_needing_rematch(SCOPE)
    assert "AAPL" in tickers


def test_fully_matched_ticker_excluded(clean_scope):
    """A ticker whose newest fill has a realized_pnl row is not in the set."""
    insert_fills(
        SCOPE,
        [
            _row("d1", "BUY", 100, 150, "2024-05-02T14:00:00+00:00"),
            _row("d2", "SELL", 100, 160, "2024-05-02T15:00:00+00:00"),
        ],
    )
    update_realized_pnl(SCOPE, "d2", Decimal("1000"))
    tickers = detect_tickers_needing_rematch(SCOPE)
    assert "AAPL" not in tickers
