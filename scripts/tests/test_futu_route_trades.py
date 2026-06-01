"""GET /futu/trades — cursor pagination + sort sanity."""

from datetime import datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from xenon.api.server import app
from xenon.db.queries.futu_history import insert_fills
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")


def _f(deal_id, t):
    return dict(
        futu_deal_id=deal_id,
        futu_order_id="o",
        ticker="AAPL",
        futu_code="US.AAPL",
        market="US",
        action="BUY",
        quantity=1,
        price=Decimal("100"),
        fees=Decimal("0"),
        filled_at=datetime.fromisoformat(t),
        raw={},
    )


def test_pagination_with_cursor(clean_scope, futu_pytest_app_scope):
    insert_fills(
        SCOPE,
        [
            _f("d1", "2024-05-01T14:00:00+00:00"),
            _f("d2", "2024-05-02T14:00:00+00:00"),
            _f("d3", "2024-05-03T14:00:00+00:00"),
        ],
    )
    with TestClient(app) as client:
        r1 = client.get(
            "/futu/trades",
            params={
                "from": "2024-05-01",
                "to": "2024-05-03",
                "limit": 2,
                "sort": "filled_at",
                "direction": "desc",
            },
        )
        body1 = r1.json()
        assert len(body1["rows"]) == 2
        assert body1["next_cursor"] is not None
        r2 = client.get(
            "/futu/trades",
            params={
                "from": "2024-05-01",
                "to": "2024-05-03",
                "limit": 2,
                "sort": "filled_at",
                "direction": "desc",
                "cursor": body1["next_cursor"],
            },
        )
        body2 = r2.json()
        assert len(body2["rows"]) == 1
        assert body2["next_cursor"] is None


def test_cursor_requires_filled_at_sort(clean_scope, futu_pytest_app_scope):
    """C34: cursor + sort=realized_pnl returns 422 because the cursor
    predicate is keyed on filled_at.
    """
    with TestClient(app) as client:
        r = client.get(
            "/futu/trades",
            params={
                "from": "2024-05-01",
                "to": "2024-05-03",
                "sort": "realized_pnl",
                "cursor": "anything",
            },
        )
    assert r.status_code == 422
    assert r.json()["detail"]["reason_code"] == "cursor_requires_filled_at_sort"
