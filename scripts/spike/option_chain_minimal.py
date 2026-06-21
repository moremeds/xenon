#!/usr/bin/env python3
# SPIKE — delete after option_chain_snapshotter service module merges (PRs 4-9).
# Not used in production; gated on OPTION_CHAIN_DATABASE_URL being set.
"""Minimal end-to-end spike for option_chain_snapshotter.

For each of SPX/NDX/RUT/VIX:
  1. Qualify Index() underlier on its correct exchange (verified live 2026-06-02)
  2. reqSecDefOptParamsAsync to enumerate expirations + strikes
  3. Pick first expiry that is >= 7 days out (avoid 0DTE)
  4. Pick the strike closest to the underlying spot (ATM)
  5. Build + qualify Option contract (C)
  6. INSERT snapshot_run with status='running'
  7. reqMktData(snapshot=True), poll Ticker for bid/ask/modelGreeks up to 12s
  8. cancelMktData
  9. INSERT one ChainRow into archive.option_chain
 10. UPDATE snapshot_run to 'ok' or 'partial'

Production paths (limiter, persister, pool, daily refresh) are NOT exercised.
This is a proof-of-life demonstrating end-to-end IB → PG flow for the
4-ticker universe. Uses clientId 901 (registered as option_chain_snapshotter_a).

Read-only against live IB at 100.66.147.98:4001. No orders, no money risk.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import psycopg
from ib_async import IB, Index, Option

from xenon.clients.ib_client import CLIENT_IDS

INDEX_EXCHANGE = {
    "SPX": "CBOE",
    "NDX": "NASDAQ",  # verified live 2026-06-02: NDX/CBOE returns Error 200
    "RUT": "RUSSELL",  # verified live 2026-06-02: RUT/CBOE returns Error 200
    "VIX": "CBOE",
}

TICKERS = ("SPX", "NDX", "RUT", "VIX")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("spike")


def pick_front_expiry(expirations: list[str], min_dte: int = 7) -> str | None:
    """Pick the first expiry that is at least min_dte days out (avoids 0DTE).
    Returns YYYYMMDD string or None."""
    today = date.today()
    for exp in sorted(expirations):
        if len(exp) != 8:
            continue
        d = date(int(exp[:4]), int(exp[4:6]), int(exp[6:8]))
        if (d - today).days >= min_dte:
            return exp
    return None


async def fetch_underlying_spot(ib: IB, contract: Index, timeout: float = 5.0) -> float | None:
    """Quick reqMktData on the underlier to find ATM. snapshot=True so it
    completes in < 11s. We just need an approximate spot."""
    ticker = ib.reqMktData(contract, "", snapshot=True, regulatorySnapshot=False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        # For an Index, .last or .close is the relevant field. NOT .marketPrice
        # (that's a method, not an attribute — calling it before any tick arrives
        # returns NaN). Order: live `last` (during RTH) → `close` (overnight) →
        # markPrice attribute → call marketPrice() method as fallback.
        for fld in ("last", "close", "markPrice"):
            v = getattr(ticker, fld, None)
            if v is not None and isinstance(v, (int, float)) and v == v and v > 0:
                ib.cancelMktData(contract)
                return float(v)
        # Fallback: marketPrice() method (computes from available fields)
        try:
            mp = ticker.marketPrice()
            if mp is not None and mp == mp and mp > 0:
                ib.cancelMktData(contract)
                return float(mp)
        except Exception:
            pass
    ib.cancelMktData(contract)
    return None


def pick_atm_strike(strikes: list[float], spot: float) -> float:
    return min(strikes, key=lambda k: abs(k - spot))


@contextmanager
def pg_run(dsn: str):
    with psycopg.connect(dsn, autocommit=True) as c:
        yield c


def insert_snapshot_run(conn, ticker: str, started_at: datetime) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO archive.snapshot_run (ticker, started_at, status) VALUES (%s, %s, 'running') RETURNING id",
            (ticker, started_at),
        )
        return cur.fetchone()[0]


def finish_snapshot_run(conn, run_id: int, status: str, contracts_persisted: int, duration_ms: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE archive.snapshot_run "
            "SET finished_at = now(), status = %s, "
            "    contracts_persisted = %s, contracts_attempted = 1, duration_ms = %s "
            "WHERE id = %s",
            (status, contracts_persisted, duration_ms, run_id),
        )


def insert_chain_row(
    conn,
    *,
    run_id: int,
    snapshot_ts,
    request_ts,
    quote_ts,
    greeks_ts,
    con_id,
    ticker,
    trading_class,
    expiry,
    strike,
    right,
    bid,
    ask,
    bid_size,
    ask_size,
    last,
    last_size,
    volume,
    iv,
    delta,
    gamma,
    vega,
    theta,
    underlying_px,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO archive.option_chain (
                snapshot_ts, con_id, ticker, trading_class, expiry, strike, "right",
                request_ts, quote_ts, greeks_ts,
                bid, ask, bid_size, ask_size, last, last_size, volume,
                iv, delta, gamma, vega, theta, underlying_px, run_id
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                snapshot_ts,
                con_id,
                ticker,
                trading_class,
                expiry,
                strike,
                right,
                request_ts,
                quote_ts,
                greeks_ts,
                bid,
                ask,
                bid_size,
                ask_size,
                last,
                last_size,
                volume,
                iv,
                delta,
                gamma,
                vega,
                theta,
                underlying_px,
                run_id,
            ),
        )


async def snapshot_one_ticker(ib: IB, conn, ticker: str) -> dict:
    log.info("[%s] qualifying underlier on %s", ticker, INDEX_EXCHANGE[ticker])
    underlier = Index(symbol=ticker, exchange=INDEX_EXCHANGE[ticker], currency="USD")
    await ib.qualifyContractsAsync(underlier)
    if not underlier.conId:
        return {"ticker": ticker, "status": "underlier_unqualified"}

    log.info("[%s]   underlier conId=%d, fetching spot", ticker, underlier.conId)
    spot = await fetch_underlying_spot(ib, underlier, timeout=8.0)
    if spot is None:
        log.warning("[%s]   spot unavailable; will use mid-strike heuristic", ticker)

    log.info("[%s] reqSecDefOptParams", ticker)
    params = await ib.reqSecDefOptParamsAsync(
        underlyingSymbol=ticker,
        futFopExchange="",
        underlyingSecType="IND",
        underlyingConId=underlier.conId,
    )
    if not params:
        return {"ticker": ticker, "status": "no_secdef_params"}

    # secdef can return MULTIPLE (exchange, tradingClass) combos per underlier
    # (e.g. NDX returns NDXP/IBUSOPT but the option actually qualifies on CBOE).
    # Try each combo plus a CBOE-override fallback until one qualifies.
    candidates = []
    for p_iter in sorted(params, key=lambda x: -len(x.expirations)):
        candidates.append(
            (p_iter.exchange, p_iter.tradingClass, p_iter.multiplier, list(p_iter.expirations), list(p_iter.strikes))
        )
        if p_iter.exchange != "CBOE":
            candidates.append(
                ("CBOE", p_iter.tradingClass, p_iter.multiplier, list(p_iter.expirations), list(p_iter.strikes))
            )

    opt = None
    chosen_exchange = chosen_tc = None
    chosen_mult = None
    expiry = None
    atm = None
    for exchange, trading_class, multiplier, expirations, strikes in candidates:
        cand_expiry = pick_front_expiry(expirations, min_dte=7)
        if not cand_expiry:
            continue
        strikes_sorted = sorted(strikes)
        cand_atm = pick_atm_strike(strikes_sorted, spot if spot else strikes_sorted[len(strikes_sorted) // 2])
        log.info(
            "[%s]   try expiry=%s strike=%.2f tradingClass=%s exchange=%s",
            ticker,
            cand_expiry,
            cand_atm,
            trading_class,
            exchange,
        )
        cand_opt = Option(
            ticker,
            cand_expiry,
            float(cand_atm),
            "C",
            exchange=exchange,
            tradingClass=trading_class,
            multiplier=str(multiplier),
            currency="USD",
        )
        await ib.qualifyContractsAsync(cand_opt)
        if cand_opt.conId:
            log.info(
                "[%s]   QUALIFIED conId=%d on %s/%s",
                ticker,
                cand_opt.conId,
                exchange,
                trading_class,
            )
            opt = cand_opt
            chosen_exchange = exchange
            chosen_tc = trading_class
            chosen_mult = multiplier
            expiry = cand_expiry
            atm = cand_atm
            break

    if opt is None:
        return {"ticker": ticker, "status": "option_unqualified"}

    # Construct a lightweight namespace for downstream code that referenced `p.*`
    class _P:
        pass

    p = _P()
    p.exchange = chosen_exchange
    p.tradingClass = chosen_tc
    p.multiplier = chosen_mult

    snapshot_ts = datetime.now(tz=timezone.utc)
    run_started = snapshot_ts
    run_id = insert_snapshot_run(conn, ticker, run_started)
    request_ts = datetime.now(tz=timezone.utc)
    t_start = time.monotonic()

    ticker_data = ib.reqMktData(opt, "", snapshot=True, regulatorySnapshot=False)

    quote_ts = None
    greeks_ts = None
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        if (
            quote_ts is None
            and ticker_data.bid is not None
            and ticker_data.ask is not None
            and ticker_data.bid > 0
            and ticker_data.ask > 0
        ):
            quote_ts = datetime.now(tz=timezone.utc)
        if greeks_ts is None and ticker_data.modelGreeks is not None and ticker_data.modelGreeks.delta is not None:
            greeks_ts = datetime.now(tz=timezone.utc)
        if quote_ts and greeks_ts:
            break

    ib.cancelMktData(opt)
    duration_ms = int((time.monotonic() - t_start) * 1000)

    def safe(v):
        if v is None or (isinstance(v, float) and v != v):  # NaN check
            return None
        return v

    mg = ticker_data.modelGreeks
    insert_chain_row(
        conn,
        run_id=run_id,
        snapshot_ts=snapshot_ts,
        request_ts=request_ts,
        quote_ts=quote_ts,
        greeks_ts=greeks_ts,
        con_id=opt.conId,
        ticker=ticker,
        trading_class=p.tradingClass,
        expiry=date(int(expiry[:4]), int(expiry[4:6]), int(expiry[6:8])),
        strike=atm,
        right="C",
        bid=safe(ticker_data.bid),
        ask=safe(ticker_data.ask),
        bid_size=safe(ticker_data.bidSize),
        ask_size=safe(ticker_data.askSize),
        last=safe(ticker_data.last),
        last_size=safe(ticker_data.lastSize),
        volume=safe(ticker_data.volume) if ticker_data.volume and ticker_data.volume != -1 else None,
        iv=safe(mg.impliedVol) if mg else None,
        delta=safe(mg.delta) if mg else None,
        gamma=safe(mg.gamma) if mg else None,
        vega=safe(mg.vega) if mg else None,
        theta=safe(mg.theta) if mg else None,
        underlying_px=safe(mg.undPrice) if mg else (spot if spot else None),
    )

    persisted = 1
    status = "ok" if (quote_ts and greeks_ts) else "partial"
    finish_snapshot_run(conn, run_id, status, persisted, duration_ms)

    log.info(
        "[%s] DONE in %dms status=%s bid=%s ask=%s iv=%s delta=%s",
        ticker,
        duration_ms,
        status,
        safe(ticker_data.bid),
        safe(ticker_data.ask),
        safe(mg.impliedVol) if mg else None,
        safe(mg.delta) if mg else None,
    )
    return {
        "ticker": ticker,
        "status": status,
        "run_id": run_id,
        "duration_ms": duration_ms,
        "expiry": expiry,
        "strike": atm,
        "bid": safe(ticker_data.bid),
        "ask": safe(ticker_data.ask),
        "iv": safe(mg.impliedVol) if mg else None,
        "delta": safe(mg.delta) if mg else None,
    }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="100.66.147.98", help="IB Gateway host (default: macmini live)")
    parser.add_argument("--port", type=int, default=4001, help="IB Gateway port (live=4001, paper=4002)")
    parser.add_argument(
        "--client-id",
        type=int,
        default=CLIENT_IDS["option_chain_snapshotter_a"],
        help="IB clientId (default: 901 from registry)",
    )
    args = parser.parse_args()

    dsn = os.environ.get("OPTION_CHAIN_DATABASE_URL")
    if not dsn:
        print("ERROR: OPTION_CHAIN_DATABASE_URL not set", file=sys.stderr)
        return 2

    # psycopg expects postgresql://; strip the +psycopg dialect prefix if present
    dsn_pg = dsn.replace("postgresql+psycopg://", "postgresql://", 1)

    ib = IB()
    log.info("Connecting to IB Gateway %s:%d clientId=%d", args.host, args.port, args.client_id)
    await ib.connectAsync(args.host, args.port, clientId=args.client_id, timeout=15)
    log.info("Connected. serverVersion=%d", ib.client.serverVersion())

    results = []
    try:
        with pg_run(dsn_pg) as conn:
            for ticker in TICKERS:
                try:
                    r = await snapshot_one_ticker(ib, conn, ticker)
                except Exception as e:
                    log.exception("[%s] failed", ticker)
                    r = {"ticker": ticker, "status": f"error: {type(e).__name__}: {e}"}
                results.append(r)
    finally:
        ib.disconnect()
        log.info("Disconnected.")

    print()
    print("=== Summary ===")
    for r in results:
        print(
            f"  {r['ticker']:4s} {r['status']:10s}  "
            f"strike={r.get('strike', '-'):>8} bid={r.get('bid', '-'):>7} ask={r.get('ask', '-'):>7} "
            f"iv={r.get('iv', '-')} delta={r.get('delta', '-')}"
        )
    return 0 if all(r.get("status") in ("ok", "partial") for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
