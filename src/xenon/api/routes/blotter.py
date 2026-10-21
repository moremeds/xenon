"""Blotter routes: POST /blotter (Flex sync, IB) and GET /blotter (PG read; Futu via futu_trades)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from xenon.api.guards import get_broker_scope
from xenon.api.subprocess import run_module
from xenon.db.engine import get_sync_engine
from xenon.db.queries.blotter import blotter_has_trades, fetch_blotter_pg, fetch_futu_blotter, merge_pg_and_flex

router = APIRouter()


@router.post("/blotter")
async def blotter_sync(scope=Depends(get_broker_scope)):
    """Return historical trades from Postgres first, then optional IB Flex.

    When IB_FLEX_TOKEN / IB_FLEX_QUERY_ID are unset, return a structured
    empty payload with configured=False rather than a 502, so the UI can
    show a friendly empty state. The Flex CLI emits exit code 2 + a
    JSON marker `{"error":"FLEX_NOT_CONFIGURED",...}` for that case
    (see src/xenon/trade_blotter/flex_query.py). Plan: docs/plans/
    2026-04-28-postgres-migration-completion-IMPL.md § W2.1.
    """
    from xenon.api import server

    if scope.broker == "FUTU":
        # FUTU: best-effort rebuild closed trades from fills, then read them back
        # (no IB Flex). Bounded connect so a down OpenD can't wedge the loop.
        if not server._is_test_mode():
            await server._futu_refresh_db_first(scope)
        engine = get_sync_engine()
        with engine.connect() as conn:
            return fetch_futu_blotter(conn, scope=scope, days=30)

    engine = get_sync_engine()
    with engine.connect() as conn:
        pg_payload = fetch_blotter_pg(conn, scope=scope, days=30)

    pg_has = blotter_has_trades(pg_payload)
    result = await run_module("xenon.trade_blotter.flex_query", ["--json"], timeout=120)

    if not result.ok:
        is_unconfigured = result.exit_code == 2 or (result.error and "FLEX_NOT_CONFIGURED" in result.error)
        if is_unconfigured:
            if pg_has:
                return pg_payload
            return {
                "configured": False,
                "as_of": None,
                "summary": {
                    "closed_trades": 0,
                    "open_trades": 0,
                    "total_commissions": 0,
                    "realized_pnl": 0,
                },
                "closed_trades": [],
                "open_trades": [],
                "source": "none",
                "message": (
                    "IB Flex Query not configured. Set IB_FLEX_TOKEN and "
                    "IB_FLEX_QUERY_ID in .env, then click Refresh. Run "
                    "`uv run python -m xenon.trade_blotter.flex_query --setup` "
                    "for the configuration guide."
                ),
            }
        if pg_has:
            pg_payload["configured"] = True
            pg_payload["flex_error"] = result.error
            return pg_payload
        return {
            "configured": True,
            "flex_error": result.error,
            "as_of": None,
            "summary": {
                "closed_trades": 0,
                "open_trades": 0,
                "total_commissions": 0,
                "realized_pnl": 0,
            },
            "closed_trades": [],
            "open_trades": [],
            "source": "none",
            "message": (
                "IB Flex Query is configured but the fetch failed. "
                "If the error mentions code 1001, set the saved Flex query's "
                "format to XML in the IB portal (the legacy servlet rejects CSV)."
            ),
        }

    flex_payload = {**result.data, "configured": True}
    merged = merge_pg_and_flex(pg_payload, flex_payload)
    merged["configured"] = True
    return merged


@router.get("/blotter")
async def blotter_get(scope=Depends(get_broker_scope)):
    engine = get_sync_engine()
    with engine.connect() as conn:
        if scope.broker == "FUTU":
            return fetch_futu_blotter(conn, scope=scope, days=30)
        return fetch_blotter_pg(conn, scope=scope, days=30)
