"""Sync upsert helper for ``xenon.ib_cash_flow``.

Mirror of ``upsert_nav_sync`` in ``portfolio_loader.py`` for the IB cash-flow
table. Sync-only because the writer is the daily Flex CLI (subprocess) — the
FastAPI read path is in ``performance_ib_flows.py`` and stays async.

Auditability invariant: every row carries the original Flex CashTransaction
fields verbatim in ``raw`` (JSONB). The derived columns (``amount_usd``,
``fx_rate``) record the conversion we applied so a future reconciliation can
re-derive them and confirm parity.

FX policy:

* USD → ``fx_rate=1.0``, ``amount_usd=amount_native``.
* HKD → ``fx_rate=0.128205`` (the HKMA peg, 1 USD ≈ 7.80 HKD). The peg has
  held for ~40 years; for IB deposit/withdrawal flows the rounding error vs
  the daily fix is well under 1%. Documented in the runbook so an operator
  can override the constant if the peg is ever re-floated.
* Any other currency → ``ValueError``. We refuse to silently invent an FX
  rate; the caller should log + skip and surface the row for manual review.

The peg constant is kept here (not in DB config) so the conversion is part
of the deterministic ingest pipeline. Tests pin both the constant and the
unknown-currency rejection path.
"""

from __future__ import annotations

import json as _json
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope

HKD_USD_PEG: Decimal = Decimal("0.128205")
"""HKMA peg ≈ 1 USD / 7.80 HKD. See module docstring."""


def _resolve_fx(currency: str) -> Decimal:
    """Return native→USD rate; raise on unsupported currency."""
    c = (currency or "").strip().upper()
    if c == "USD":
        return Decimal("1.0")
    if c == "HKD":
        return HKD_USD_PEG
    raise ValueError(f"unsupported currency for IB cash flow: {currency!r}")


_UPSERT_SQL = text(
    """
    INSERT INTO xenon.ib_cash_flow
      (broker, account_env, broker_account, transaction_id,
       txn_type, description, amount_native, currency, amount_usd, fx_rate,
       occurred_at, raw)
    VALUES
      (:broker, :account_env, :broker_account, :transaction_id,
       :txn_type, :description, :amount_native, :currency, :amount_usd, :fx_rate,
       :occurred_at, CAST(:raw AS JSONB))
    ON CONFLICT (broker, account_env, broker_account, transaction_id)
    DO UPDATE SET
        txn_type      = EXCLUDED.txn_type,
        description   = EXCLUDED.description,
        amount_native = EXCLUDED.amount_native,
        currency      = EXCLUDED.currency,
        amount_usd    = EXCLUDED.amount_usd,
        fx_rate       = EXCLUDED.fx_rate,
        occurred_at   = EXCLUDED.occurred_at,
        raw           = EXCLUDED.raw
    RETURNING (xmax = 0) AS was_inserted
    """
)


def upsert_ib_cash_flow_sync(
    *,
    scope: AccountScope,
    transaction_id: str,
    txn_type: str,
    description: str | None,
    amount_native: Decimal | float | int | str,
    currency: str,
    occurred_at: datetime,
    raw: Mapping[str, Any],
) -> bool:
    """Upsert one ``xenon.ib_cash_flow`` row. Returns True on INSERT, False on UPDATE.

    Derives ``amount_usd`` and ``fx_rate`` from ``currency``. Raises
    ``ValueError`` for unsupported currencies — caller decides whether to log
    and skip or surface for manual review.

    Primary key is ``(broker, account_env, broker_account, transaction_id)``;
    re-running with the same Flex CashTransactions section is idempotent.
    """
    if scope.broker != "IB":
        raise ValueError(f"ib_cash_flow is IB-only; got broker={scope.broker!r}")

    amount = Decimal(str(amount_native))
    fx_rate = _resolve_fx(currency)
    amount_usd = (amount * fx_rate).quantize(Decimal("0.0001"))

    engine = get_sync_engine()
    with engine.begin() as conn:
        result = conn.execute(
            _UPSERT_SQL,
            {
                "broker": scope.broker,
                "account_env": scope.account_env,
                "broker_account": scope.broker_account,
                "transaction_id": transaction_id,
                "txn_type": txn_type,
                "description": description,
                "amount_native": amount,
                "currency": currency.upper(),
                "amount_usd": amount_usd,
                "fx_rate": fx_rate,
                "occurred_at": occurred_at,
                "raw": _json.dumps(dict(raw)),
            },
        ).first()
    return bool(result.was_inserted) if result else False
