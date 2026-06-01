"""Cross-broker equity curve: IB + Futu lines, optional combined sum.

C30: real IB account IDs (DU0000000, U1234567) and Futu account IDs differ.
Resolve scope PER BROKER — `get_account_scope` for IB,
`get_futu_account_scope` for FUTU — instead of filtering both with the same
`broker_account` (which only worked in tests because both seeds used
`pytest`).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import text

from xenon.api.guards import get_futu_account_scope
from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope

router = APIRouter(tags=["equity_curve"])


def _resolve_ib_scope(request: Request) -> AccountScope:
    """Build the IB scope independently of app.state.broker.

    `get_account_scope` reads `app.state.broker`, which can be FUTU on a
    Futu-tab request — the cross-broker route always wants the IB side
    pinned to broker='IB' regardless of the active UI tab.
    """
    trading_mode = getattr(request.app.state, "trading_mode", "paper")
    ib_account = getattr(request.app.state, "ib_account", None) or getattr(
        request.app.state, "account", "legacy_unknown"
    )
    return AccountScope(
        broker="IB",
        account_env=trading_mode,
        broker_account=ib_account,
    )


@router.get("/equity-curve")
def equity_curve_cross_broker(
    request: Request,
    brokers: str = Query("IB,FUTU"),
    combined: bool = False,
) -> dict:
    broker_list = [b.strip().upper() for b in brokers.split(",") if b.strip()]
    allowed = {"IB", "FUTU"}
    bad = set(broker_list) - allowed
    if bad:
        raise HTTPException(
            status_code=422,
            detail={"reason_code": "broker_invalid", "values": list(bad)},
        )

    # C30: resolve scope per broker so the FUTU side reads its own
    # broker_account (which may differ from the IB account in production).
    ib_scope = _resolve_ib_scope(request)
    futu_scope = get_futu_account_scope(request)
    scopes_by_broker = {"IB": ib_scope, "FUTU": futu_scope}

    per_broker: dict[str, list[dict]] = {}
    with get_sync_engine().connect() as conn:
        for broker in broker_list:
            sc = scopes_by_broker[broker]
            rows = conn.execute(
                text(
                    """
                    SELECT date, nav, nav_type FROM xenon.nav_history
                     WHERE broker=:broker
                       AND account_env=:env AND broker_account=:acc
                     ORDER BY date
                    """
                ),
                {
                    "broker": broker,
                    "env": sc.account_env,
                    "acc": sc.broker_account,
                },
            ).fetchall()
            per_broker[broker] = [
                {
                    "date": r.date.isoformat(),
                    "nav": float(r.nav),
                    "source": r.nav_type,
                }
                for r in rows
            ]

    combined_series: list[dict] | None = None
    if combined and len(per_broker) >= 2:
        per_date: dict[str, dict[str, float]] = {}
        for broker, rows in per_broker.items():
            for r in rows:
                per_date.setdefault(r["date"], {})[broker] = r["nav"]
        combined_series = [
            {"date": d, "nav": sum(per_date[d][b] for b in broker_list)}
            for d in sorted(per_date)
            if all(b in per_date[d] for b in broker_list)
        ]

    return {"per_broker": per_broker, "combined": combined_series}
