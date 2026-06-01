"""A cash flow with no fx_daily row for that date is stored with amount=NULL
+ an anomaly row to the reconciliation log.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine, text

from xenon.api.services.futu_history import convert_cash_flow_amount
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")


def _sync_url() -> str:
    import os

    url = os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    )
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg://")


def test_missing_fx_writes_null_amount_and_anomaly(clean_scope):
    raw_row = {
        "futu_flow_id": "f1",
        "futu_type_raw": "DIVIDEND",
        "amount_original": Decimal("780"),
        "original_currency": "HKD",
        "flow_at": datetime(2024, 5, 1, 14, tzinfo=timezone.utc),
        "ticker": "00700",
        "description": "div",
        "raw": {},
    }
    # No fx_daily row for HKD on 2024-05-01 → returns (None, None) +
    # writes anomaly.
    amount, fx_rate = convert_cash_flow_amount(SCOPE, raw_row)
    assert amount is None
    assert fx_rate is None

    # The anomaly row is recorded in the reconciliation log.
    eng = create_engine(_sync_url())
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT notes FROM xenon.futu_reconciliation_log "
                "WHERE broker=:b AND account_env=:e AND broker_account=:a "
                "  AND kind='anomaly'"
            ),
            {"b": SCOPE.broker, "e": SCOPE.account_env, "a": SCOPE.broker_account},
        ).fetchall()
    eng.dispose()
    assert any(r.notes.get("anomaly_type") == "missing_fx" for r in rows)
