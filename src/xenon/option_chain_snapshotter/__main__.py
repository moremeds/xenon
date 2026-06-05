"""Option-chain snapshotter entry point.

Fires every 10 min (launchd cadence) and, when inside NYSE RTH, captures
an ATM-call snapshot for each ticker into archive.option_chain +
archive.snapshot_run. Outside RTH (weekends, holidays, off-session)
returns 0 immediately without touching IB or the DB.

This is the promoted form of `scripts/spike/option_chain_minimal.py`
(PR #125). Scope changes from the spike:
  - TICKERS = ("SPX", "VIX")  -- was 4
  - Default host = 127.0.0.1 (host-native Gateway)  -- was macmini IP
  - NYSE RTH gate via hours.is_nyse_rth
  - --force bypasses RTH for ad-hoc testing

Env: OPTION_CHAIN_DATABASE_URL  (required when --force or inside RTH)
Exit codes:
  0 = success OR off-RTH skip
  1 = at least one ticker failed
  2 = config error (env var missing)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone

import psycopg
from ib_async import IB, Index, Option

from xenon.clients.ib_client import CLIENT_IDS
from xenon.option_chain_snapshotter.hours import is_nyse_rth

INDEX_EXCHANGE = {
    "SPX": "CBOE",
    "VIX": "CBOE",
}

TICKERS: tuple[str, ...] = ("SPX", "VIX")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("xenon.option_chain_snapshotter")


def pick_front_expiry(expirations: list[str], min_dte: int = 7) -> str | None:
    today = date.today()
    for exp in sorted(expirations):
        if len(exp) != 8:
            continue
        d = date(int(exp[:4]), int(exp[4:6]), int(exp[6:8]))
        if (d - today).days >= min_dte:
            return exp
    return None


def pick_atm_strike(strikes: list[float], spot: float) -> float:
    return min(strikes, key=lambda k: abs(k - spot))


async def fetch_underlying_spot(ib: IB, contract: Index, timeout: float = 5.0) -> float | None:
    ticker = ib.reqMktData(contract, "", snapshot=True, regulatorySnapshot=False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        for fld in ("last", "close", "markPrice"):
            v = getattr(ticker, fld, None)
            if v is not None and isinstance(v, (int, float)) and v == v and v > 0:
                ib.cancelMktData(contract)
                return float(v)
        try:
            mp = ticker.marketPrice()
            if mp is not None and mp == mp and mp > 0:
                ib.cancelMktData(contract)
                return float(mp)
        except Exception:
            pass
    ib.cancelMktData(contract)
    return None


@contextmanager
def pg_connect(dsn: str):
    with psycopg.connect(dsn, autocommit=True) as c:
        yield c


def insert_snapshot_run(conn, ticker: str, started_at: datetime) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO archive.snapshot_run (ticker, started_at, status) "
            "VALUES (%s, %s, 'running') RETURNING id",
            (ticker, started_at),
        )
        return cur.fetchone()[0]


def finish_snapshot_run(
    conn, run_id: int, status: str, contracts_persisted: int, duration_ms: int, error: str | None = None
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE archive.snapshot_run "
            "SET finished_at = now(), status = %s, "
            "    contracts_persisted = %s, contracts_attempted = 1, duration_ms = %s, error = %s "
            "WHERE id = %s",
            (status, contracts_persisted, duration_ms, error, run_id),
        )


def insert_chain_row(conn, **kwargs) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO archive.option_chain (
                snapshot_ts, con_id, ticker, trading_class, expiry, strike, "right",
                request_ts, quote_ts, greeks_ts,
                bid, ask, bid_size, ask_size, last, last_size, volume,
                iv, delta, gamma, vega, theta, underlying_px, run_id
            ) VALUES (
                %(snapshot_ts)s, %(con_id)s, %(ticker)s, %(trading_class)s,
                %(expiry)s, %(strike)s, %(right)s,
                %(request_ts)s, %(quote_ts)s, %(greeks_ts)s,
                %(bid)s, %(ask)s, %(bid_size)s, %(ask_size)s,
                %(last)s, %(last_size)s, %(volume)s,
                %(iv)s, %(delta)s, %(gamma)s, %(vega)s, %(theta)s,
                %(underlying_px)s, %(run_id)s
            )
            """,
            kwargs,
        )


def _safe(v):
    if v is None or (isinstance(v, float) and v != v):
        return None
    return v


async def snapshot_one_ticker(ib: IB, conn, ticker: str) -> dict:
    log.info("[%s] qualifying underlier on %s", ticker, INDEX_EXCHANGE[ticker])
    underlier = Index(symbol=ticker, exchange=INDEX_EXCHANGE[ticker], currency="USD")
    await ib.qualifyContractsAsync(underlier)
    if not underlier.conId:
        return {"ticker": ticker, "status": "underlier_unqualified"}

    spot = await fetch_underlying_spot(ib, underlier, timeout=8.0)
    if spot is None:
        log.warning("[%s] spot unavailable; using mid-strike heuristic", ticker)

    params = await ib.reqSecDefOptParamsAsync(
        underlyingSymbol=ticker,
        futFopExchange="",
        underlyingSecType="IND",
        underlyingConId=underlier.conId,
    )
    if not params:
        return {"ticker": ticker, "status": "no_secdef_params"}

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
    chosen_exchange = chosen_tc = chosen_mult = None
    expiry = atm = None
    for exchange, trading_class, multiplier, expirations, strikes in candidates:
        cand_expiry = pick_front_expiry(expirations, min_dte=7)
        if not cand_expiry:
            continue
        strikes_sorted = sorted(strikes)
        cand_atm = pick_atm_strike(strikes_sorted, spot if spot else strikes_sorted[len(strikes_sorted) // 2])
        cand_opt = Option(
            ticker, cand_expiry, float(cand_atm), "C",
            exchange=exchange, tradingClass=trading_class,
            multiplier=str(multiplier), currency="USD",
        )
        await ib.qualifyContractsAsync(cand_opt)
        if cand_opt.conId:
            opt = cand_opt
            chosen_exchange, chosen_tc, chosen_mult = exchange, trading_class, multiplier
            expiry, atm = cand_expiry, cand_atm
            log.info("[%s] qualified conId=%d on %s/%s", ticker, opt.conId, chosen_exchange, chosen_tc)
            break

    if opt is None:
        return {"ticker": ticker, "status": "option_unqualified"}

    snapshot_ts = datetime.now(tz=timezone.utc)
    run_id = insert_snapshot_run(conn, ticker, snapshot_ts)
    request_ts = datetime.now(tz=timezone.utc)
    t_start = time.monotonic()

    ticker_data = ib.reqMktData(opt, "", snapshot=True, regulatorySnapshot=False)
    quote_ts = greeks_ts = None
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        if (
            quote_ts is None
            and ticker_data.bid is not None and ticker_data.ask is not None
            and ticker_data.bid > 0 and ticker_data.ask > 0
        ):
            quote_ts = datetime.now(tz=timezone.utc)
        mg = ticker_data.modelGreeks
        if greeks_ts is None and mg is not None and mg.delta is not None:
            greeks_ts = datetime.now(tz=timezone.utc)
        if quote_ts and greeks_ts:
            break

    ib.cancelMktData(opt)
    duration_ms = int((time.monotonic() - t_start) * 1000)
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
        trading_class=chosen_tc,
        expiry=date(int(expiry[:4]), int(expiry[4:6]), int(expiry[6:8])),
        strike=atm,
        right="C",
        bid=_safe(ticker_data.bid),
        ask=_safe(ticker_data.ask),
        bid_size=_safe(ticker_data.bidSize),
        ask_size=_safe(ticker_data.askSize),
        last=_safe(ticker_data.last),
        last_size=_safe(ticker_data.lastSize),
        volume=_safe(ticker_data.volume) if ticker_data.volume and ticker_data.volume != -1 else None,
        iv=_safe(mg.impliedVol) if mg else None,
        delta=_safe(mg.delta) if mg else None,
        gamma=_safe(mg.gamma) if mg else None,
        vega=_safe(mg.vega) if mg else None,
        theta=_safe(mg.theta) if mg else None,
        underlying_px=_safe(mg.undPrice) if mg else (spot if spot else None),
    )

    status = "ok" if (quote_ts and greeks_ts) else "partial"
    finish_snapshot_run(conn, run_id, status, 1, duration_ms)
    log.info(
        "[%s] DONE in %dms status=%s bid=%s ask=%s iv=%s delta=%s",
        ticker, duration_ms, status,
        _safe(ticker_data.bid), _safe(ticker_data.ask),
        _safe(mg.impliedVol) if mg else None,
        _safe(mg.delta) if mg else None,
    )
    return {"ticker": ticker, "status": status, "run_id": run_id, "duration_ms": duration_ms}


async def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4001)
    parser.add_argument(
        "--client-id", type=int,
        default=CLIENT_IDS["option_chain_snapshotter_a"],
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Bypass NYSE RTH gate (for ad-hoc testing or backfill).",
    )
    args = parser.parse_args()

    if not args.force and not is_nyse_rth(datetime.now(tz=timezone.utc)):
        log.info("Outside NYSE RTH; skipping (use --force to override).")
        return 0

    dsn = os.environ.get("OPTION_CHAIN_DATABASE_URL")
    if not dsn:
        log.error("OPTION_CHAIN_DATABASE_URL not set")
        return 2
    dsn_pg = dsn.replace("postgresql+psycopg://", "postgresql://", 1)

    ib = IB()
    log.info("Connecting to IB Gateway %s:%d clientId=%d", args.host, args.port, args.client_id)
    try:
        await ib.connectAsync(args.host, args.port, clientId=args.client_id, timeout=15)
    except Exception as exc:
        log.error("IB connect failed: %s", exc)
        return 1
    log.info("Connected. serverVersion=%d", ib.client.serverVersion())

    results = []
    try:
        with pg_connect(dsn_pg) as conn:
            for ticker in TICKERS:
                try:
                    r = await snapshot_one_ticker(ib, conn, ticker)
                except Exception as exc:
                    log.exception("[%s] failed", ticker)
                    r = {"ticker": ticker, "status": f"error: {type(exc).__name__}: {exc}"}
                results.append(r)
    finally:
        ib.disconnect()

    for r in results:
        log.info("Result: %s", r)
    return 0 if all(r.get("status") in ("ok", "partial") for r in results) else 1


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
