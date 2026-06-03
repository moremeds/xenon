"""Unit tests for xenon.utils.ib_cash_flow_loader.

Covers FX policy (USD passthrough, HKD peg, unsupported reject), upsert
idempotency, and the auditability invariant — the raw Flex row round-trips
through the ``raw`` JSONB column unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope
from xenon.utils.ib_cash_flow_loader import (
    HKD_USD_PEG,
    _resolve_fx,
    upsert_ib_cash_flow_sync,
)

SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DUQ999999")


def _read(transaction_id: str) -> dict | None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT txn_type, description, amount_native, currency, "
                "amount_usd, fx_rate, occurred_at, raw "
                "FROM xenon.ib_cash_flow "
                "WHERE broker='IB' AND account_env='paper' "
                "AND broker_account='DUQ999999' AND transaction_id=:t"
            ),
            {"t": transaction_id},
        ).first()
    return None if row is None else dict(row._mapping)


def test_resolve_fx_usd_passthrough():
    assert _resolve_fx("USD") == Decimal("1.0")
    assert _resolve_fx("usd") == Decimal("1.0")


def test_resolve_fx_hkd_peg():
    assert _resolve_fx("HKD") == HKD_USD_PEG
    # Sanity: peg is roughly 1/7.80 ≈ 0.1282
    assert Decimal("0.127") < HKD_USD_PEG < Decimal("0.130")


def test_resolve_fx_unsupported_raises():
    with pytest.raises(ValueError, match="unsupported currency"):
        _resolve_fx("EUR")
    with pytest.raises(ValueError, match="unsupported currency"):
        _resolve_fx("")


def test_upsert_usd_deposit(pg_test_engine):
    raw_row = {
        "ClientAccountID": "DUQ999999",
        "Date/Time": "20260107",
        "Type": "Deposits/Withdrawals",
        "Description": "CASH RECEIPTS / ELECTRONIC FUND TRANSFERS",
        "Amount": "35000",
        "CurrencyPrimary": "USD",
        "TransactionID": "4326167414",
    }
    inserted = upsert_ib_cash_flow_sync(
        scope=SCOPE,
        transaction_id="4326167414",
        txn_type="Deposits/Withdrawals",
        description="CASH RECEIPTS / ELECTRONIC FUND TRANSFERS",
        amount_native="35000",
        currency="USD",
        occurred_at=datetime(2026, 1, 7, 0, 0, 0, tzinfo=timezone.utc),
        raw=raw_row,
    )
    assert inserted is True
    row = _read("4326167414")
    assert row is not None
    assert row["amount_native"] == Decimal("35000.0000")
    assert row["amount_usd"] == Decimal("35000.0000")
    assert row["fx_rate"] == Decimal("1.000000")
    assert row["currency"] == "USD"
    assert row["raw"]["TransactionID"] == "4326167414"


def test_upsert_hkd_deposit_converts(pg_test_engine):
    raw_row = {
        "ClientAccountID": "DUQ999999",
        "Date/Time": "20251026;225011",
        "Type": "Deposits/Withdrawals",
        "Description": "CASH RECEIPTS / ELECTRONIC FUND TRANSFERS",
        "Amount": "10000",
        "CurrencyPrimary": "HKD",
        "TransactionID": "4179119714",
    }
    upsert_ib_cash_flow_sync(
        scope=SCOPE,
        transaction_id="4179119714",
        txn_type="Deposits/Withdrawals",
        description="CASH RECEIPTS / ELECTRONIC FUND TRANSFERS",
        amount_native=Decimal("10000"),
        currency="HKD",
        occurred_at=datetime(2025, 10, 26, 22, 50, 11, tzinfo=timezone.utc),
        raw=raw_row,
    )
    row = _read("4179119714")
    assert row is not None
    assert row["currency"] == "HKD"
    assert row["amount_native"] == Decimal("10000.0000")
    # 10000 HKD * 0.128205 = 1282.05
    assert row["amount_usd"] == Decimal("1282.0500")
    assert row["fx_rate"] == Decimal("0.128205")


def test_upsert_negative_withdrawal(pg_test_engine):
    upsert_ib_cash_flow_sync(
        scope=SCOPE,
        transaction_id="9000000001",
        txn_type="Deposits/Withdrawals",
        description="WIRE OUT",
        amount_native=Decimal("-5000"),
        currency="USD",
        occurred_at=datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc),
        raw={"x": 1},
    )
    row = _read("9000000001")
    assert row is not None
    assert row["amount_native"] == Decimal("-5000.0000")
    assert row["amount_usd"] == Decimal("-5000.0000")


def test_upsert_idempotent_returns_false_on_replay(pg_test_engine):
    """Same TransactionID twice — first INSERT, second UPDATE."""
    kwargs = dict(
        scope=SCOPE,
        transaction_id="REPLAY_ID",
        txn_type="Deposits/Withdrawals",
        description="x",
        amount_native=Decimal("100"),
        currency="USD",
        occurred_at=datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc),
        raw={"first": True},
    )
    assert upsert_ib_cash_flow_sync(**kwargs) is True
    # Replay with different description — should UPDATE the existing row
    kwargs["description"] = "y"
    kwargs["raw"] = {"second": True}
    assert upsert_ib_cash_flow_sync(**kwargs) is False
    row = _read("REPLAY_ID")
    assert row["description"] == "y"
    assert row["raw"] == {"second": True}


def test_upsert_unsupported_currency_raises(pg_test_engine):
    with pytest.raises(ValueError, match="unsupported currency"):
        upsert_ib_cash_flow_sync(
            scope=SCOPE,
            transaction_id="EUR_ID",
            txn_type="Deposits/Withdrawals",
            description="x",
            amount_native=Decimal("100"),
            currency="EUR",
            occurred_at=datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc),
            raw={},
        )
    # No row should have been written
    assert _read("EUR_ID") is None


def test_upsert_malformed_amount_raises_invalid_operation(pg_test_engine):
    """A non-numeric Amount string raises decimal.InvalidOperation, not ValueError.

    Caller (``_persist_ib_cash_transactions``) needs to catch this — otherwise a
    single bad row in a Flex response halts the entire NAV ingest. Pinning the
    exception type so the caller's `except (ValueError, InvalidOperation)`
    clause stays correctly scoped.
    """
    import decimal as _decimal

    with pytest.raises(_decimal.InvalidOperation):
        upsert_ib_cash_flow_sync(
            scope=SCOPE,
            transaction_id="BAD_AMT",
            txn_type="Deposits/Withdrawals",
            description="x",
            amount_native="not-a-number",
            currency="USD",
            occurred_at=datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc),
            raw={},
        )


def test_upsert_rejects_non_ib_broker(pg_test_engine):
    futu_scope = AccountScope(broker="FUTU", account_env="live", broker_account="123")
    with pytest.raises(ValueError, match="IB-only"):
        upsert_ib_cash_flow_sync(
            scope=futu_scope,
            transaction_id="X",
            txn_type="Deposits/Withdrawals",
            description="x",
            amount_native=Decimal("100"),
            currency="USD",
            occurred_at=datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc),
            raw={},
        )
