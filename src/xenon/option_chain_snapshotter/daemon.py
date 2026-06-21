"""Orchestration loop for the option chain snapshotter.

Designed to run as a supervised asyncio task inside the FastAPI lifespan.
Shutdown is handled via asyncio task cancellation — CancelledError propagates
from the asyncio.sleep() calls and the finally block disconnects cleanly.

Wiring (server.py):
    task = asyncio.create_task(_run_snapshotter_supervised())
    app.state.snapshotter_task = task

    # on shutdown:
    task.cancel(); await task
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import (
    DEFAULT_CADENCE_S,
    DEFAULT_IB_CLIENT_ID_A,
    MAX_RECONNECT_ATTEMPTS,
    RECONNECT_DELAY_S,
    TICKERS,
)
from .fetcher import IBFetcher
from .hours import is_session_open, next_session_open
from .persister import PGPersister

log = logging.getLogger(__name__)


@dataclass
class CycleResult:
    ticker: str
    run_id: int | None
    status: str
    contracts_attempted: int
    contracts_persisted: int
    duration_ms: int
    error: str | None = None


async def run_ticker_cycle(
    ticker: str,
    fetcher: IBFetcher,
    persister: PGPersister,
) -> CycleResult:
    """Snapshot one ticker's option chain and write to archive."""
    run_id = None
    t0 = time.monotonic()
    try:
        run_id, snapshot_ts = persister.insert_run(ticker)
        log.info("[%s] run_id=%d started", ticker, run_id)

        request_ts = datetime.now(tz=timezone.utc)
        contracts, spot = await fetcher.enumerate_contracts(ticker)
        attempted = len(contracts)

        if not contracts:
            persister.finish_run(
                run_id,
                status="error",
                contracts_attempted=0,
                contracts_persisted=0,
                duration_ms=int((time.monotonic() - t0) * 1000),
                error="enumerate_contracts returned empty",
            )
            return CycleResult(
                ticker=ticker,
                run_id=run_id,
                status="error",
                contracts_attempted=0,
                contracts_persisted=0,
                duration_ms=int((time.monotonic() - t0) * 1000),
                error="enumerate_contracts returned empty",
            )

        rows = await fetcher.fetch_snapshots(
            ticker=ticker,
            contracts=contracts,
            spot=spot,
            request_ts=request_ts,
        )

        persisted = persister.persist_rows(rows, run_id=run_id, snapshot_ts=snapshot_ts)
        duration_ms = int((time.monotonic() - t0) * 1000)

        status = "ok" if persisted == attempted else "partial"
        if persisted == 0:
            status = "error"

        persister.finish_run(
            run_id,
            status=status,
            contracts_attempted=attempted,
            contracts_persisted=persisted,
            duration_ms=duration_ms,
            ib_lines_peak=min(attempted, 50),
        )
        log.info(
            "[%s] run_id=%d %s — %d/%d contracts in %dms",
            ticker,
            run_id,
            status,
            persisted,
            attempted,
            duration_ms,
        )
        return CycleResult(
            ticker=ticker,
            run_id=run_id,
            status=status,
            contracts_attempted=attempted,
            contracts_persisted=persisted,
            duration_ms=duration_ms,
        )

    except asyncio.CancelledError:
        # Propagate — the task is being shut down.
        if run_id is not None:
            try:
                persister.finish_run(
                    run_id,
                    status="error",
                    contracts_attempted=0,
                    contracts_persisted=0,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    error="cancelled",
                )
            except Exception:
                pass
        raise

    except Exception as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        err_msg = f"{type(exc).__name__}: {exc}"
        log.exception("[%s] cycle failed: %s", ticker, err_msg)
        if run_id is not None:
            try:
                persister.finish_run(
                    run_id,
                    status="error",
                    contracts_attempted=0,
                    contracts_persisted=0,
                    duration_ms=duration_ms,
                    error=err_msg[:500],
                )
            except Exception:
                pass
        return CycleResult(
            ticker=ticker,
            run_id=run_id,
            status="error",
            contracts_attempted=0,
            contracts_persisted=0,
            duration_ms=duration_ms,
            error=err_msg,
        )


async def run_forever(
    *,
    ib_host: str,
    ib_port: int,
    ib_client_id: int = DEFAULT_IB_CLIENT_ID_A,
    database_url: str | None = None,
) -> None:
    """Main daemon loop.  Runs until asyncio.CancelledError is raised.

    Args:
        ib_host: IB Gateway hostname/IP.
        ib_port: IB Gateway port (4001 live, 4002 paper).
        ib_client_id: IB client ID (default 901).
        database_url: Postgres DSN.  Falls back to DATABASE_URL env var.
    """
    dsn = database_url or os.environ.get("DATABASE_URL", "")
    if not dsn:
        raise RuntimeError("DATABASE_URL is required for the option chain snapshotter")

    persister = PGPersister(dsn)
    persister.connect()

    fetcher = IBFetcher(ib_host, ib_port, ib_client_id)
    consecutive_failures = 0

    def _on_error(req_id: int, error_code: int, error_string: str, contract: object) -> None:
        if error_code == 10187:
            log.error(
                "IB error 10187: market data line limit exceeded (reqId=%d) — reduce BATCH_SIZE in config.py",
                req_id,
            )
        elif error_code not in (2104, 2106, 2158, 2119):
            log.debug("IB error reqId=%d code=%d: %s", req_id, error_code, error_string)

    fetcher.ib.errorEvent += _on_error

    try:
        while True:
            # ── Market-hours gate ──────────────────────────────────────── #
            if not is_session_open():
                next_open = next_session_open()
                wait_s = max(0.0, (next_open - datetime.now(tz=timezone.utc)).total_seconds())
                log.info("Outside session — sleeping %.0f s until next open", wait_s)
                await asyncio.sleep(wait_s)  # CancelledError propagates here
                continue

            # ── IB connection ──────────────────────────────────────────── #
            if not fetcher.is_connected:
                try:
                    await fetcher.connect()
                    consecutive_failures = 0
                except Exception as exc:
                    consecutive_failures += 1
                    log.error(
                        "IB connect failed (%d/%d): %s",
                        consecutive_failures,
                        MAX_RECONNECT_ATTEMPTS,
                        exc,
                    )
                    if consecutive_failures >= MAX_RECONNECT_ATTEMPTS:
                        log.critical("Reconnect budget exhausted — snapshotter stopping")
                        return
                    await asyncio.sleep(RECONNECT_DELAY_S)
                    continue

            # ── Snapshot cycle ─────────────────────────────────────────── #
            cycle_start = time.monotonic()
            cadence = persister.load_cadence(TICKERS[0], DEFAULT_CADENCE_S)

            for ticker in TICKERS:
                if not fetcher.is_connected:
                    log.warning("IB disconnected mid-cycle — will reconnect next iteration")
                    break
                await run_ticker_cycle(ticker, fetcher, persister)

            # ── Sleep for remaining cadence ────────────────────────────── #
            elapsed = time.monotonic() - cycle_start
            sleep_s = max(0.0, cadence - elapsed)
            if sleep_s > 0:
                log.info("Cycle done in %.0fs — sleeping %.0fs", elapsed, sleep_s)
                await asyncio.sleep(sleep_s)
            else:
                log.info("Cycle took %.0fs (over cadence %.0fs) — next cycle immediately", elapsed, cadence)

    except asyncio.CancelledError:
        log.info("option_chain_snapshotter: shutdown requested")
        raise
    finally:
        fetcher.disconnect()
        persister.close()
        log.info("option_chain_snapshotter: shut down cleanly")
