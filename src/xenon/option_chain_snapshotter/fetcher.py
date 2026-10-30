"""IB data fetcher for the option chain snapshotter.

Universe enumeration strategy (one call per expiry/tradingClass):
  1. reqSecDefOptParams → list of (exchange, tradingClass, expirations, strikes)
  2. For each (tradingClass, expiry): reqContractDetailsAsync with a partial
     Option (no strike/right) → IB returns all matching contracts with real
     conIds in one round-trip.
  3. Strike filter: keep only those within ±STRIKE_PCT_RANGE of spot.

Snapshot strategy:
  Batch reqMktData(snapshot=True) BATCH_SIZE at a time.  Poll for
  bid/ask/greeks up to BATCH_TIMEOUT seconds, then cancel remaining lines.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from ib_async import IB, Index, Option

from .config import (
    BATCH_SIZE,
    BATCH_TIMEOUT,
    DEFAULT_IB_CLIENT_ID_A,
    EXCLUDE_VENUES,
    IB_NO_VOLUME,
    INDEX_EXCHANGE,
    MAX_EXPIRATIONS,
    MIN_DTE,
    STRIKE_PCT_RANGE,
)

log = logging.getLogger(__name__)

# Expiry/DTE math must be anchored to the US market date, not the host's local
# date. On a non-US host (e.g. Asia/Hong_Kong) date.today() rolls over while US
# RTH is still in session, so a next-US-day expiry would be misread as 0DTE and
# dropped by MIN_DTE. Use the NYSE calendar's timezone.
_MARKET_TZ = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------- #
# Connection helpers
# --------------------------------------------------------------------------- #


class IBFetcher:
    """Manages a single IB connection and exposes snapshot-fetching methods."""

    def __init__(
        self,
        host: str,
        port: int,
        client_id: int = DEFAULT_IB_CLIENT_ID_A,
    ) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self.ib = IB()

    async def connect(self, timeout: float = 20.0) -> None:
        log.info("Connecting to IB Gateway %s:%d clientId=%d", self._host, self._port, self._client_id)
        await self.ib.connectAsync(self._host, self._port, clientId=self._client_id, timeout=timeout)
        log.info("IB connected (serverVersion=%d)", self.ib.client.serverVersion())

    def disconnect(self) -> None:
        try:
            self.ib.disconnect()
        except Exception:
            pass
        log.info("IB disconnected")

    @property
    def is_connected(self) -> bool:
        return self.ib.isConnected()

    # ---------------------------------------------------------------------- #
    # Universe enumeration
    # ---------------------------------------------------------------------- #

    async def _fetch_spot(self, underlier: Index, timeout: float = 8.0) -> float | None:
        """Fetch spot price for an Index underlier.  Returns None on timeout."""
        ticker = self.ib.reqMktData(underlier, "", snapshot=True, regulatorySnapshot=False)
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                await asyncio.sleep(0.1)
                for field in ("last", "close", "markPrice"):
                    v = getattr(ticker, field, None)
                    if isinstance(v, (int, float)) and v == v and v > 0:
                        return float(v)
                try:
                    mp = ticker.marketPrice()
                    if mp is not None and mp == mp and mp > 0:
                        return float(mp)
                except Exception:
                    pass
            return None
        finally:
            # Always release the line — even if cancelled by the cycle-timeout
            # guard or shutdown mid-wait.
            try:
                self.ib.cancelMktData(underlier)
            except Exception:
                pass

    async def enumerate_contracts(
        self,
        ticker: str,
        max_expirations: int = MAX_EXPIRATIONS,
        strike_pct_range: float = STRIKE_PCT_RANGE,
        min_dte: int = MIN_DTE,
    ) -> tuple[list[Option], float | None]:
        """Enumerate option contracts for *ticker* with full conIds.

        Returns (contracts, spot) where spot may be None if IB didn't provide
        price data.  Contracts are filtered to front-N expirations and strikes
        within ±strike_pct_range of spot (or unfiltered if spot is unavailable).
        """
        exchange = INDEX_EXCHANGE[ticker]
        log.info("[%s] qualifying underlier on %s", ticker, exchange)
        underlier = Index(symbol=ticker, exchange=exchange, currency="USD")
        await self.ib.qualifyContractsAsync(underlier)
        if not underlier.conId:
            log.error("[%s] underlier qualification failed", ticker)
            return [], None

        spot = await self._fetch_spot(underlier, timeout=8.0)
        if spot is None:
            log.warning("[%s] spot unavailable; strike filter disabled", ticker)

        log.info("[%s] reqSecDefOptParams (conId=%d)", ticker, underlier.conId)
        params_list = await self.ib.reqSecDefOptParamsAsync(
            underlyingSymbol=ticker,
            futFopExchange="",
            underlyingSecType="IND",
            underlyingConId=underlier.conId,
        )
        if not params_list:
            log.error("[%s] reqSecDefOptParams returned nothing", ticker)
            return [], spot

        today = datetime.now(tz=_MARKET_TZ).date()
        contracts: list[Option] = []

        for params in params_list:
            # Skip routing aliases — SMART/IBUSOPT return the same conIds as the
            # listed exchange but inflate the contract count ~3x and produce no
            # additional market data in snapshot mode.
            if params.exchange in EXCLUDE_VENUES:
                log.debug(
                    "[%s] skipping tradingClass=%s exchange=%s (in EXCLUDE_VENUES)",
                    ticker,
                    params.tradingClass,
                    params.exchange,
                )
                continue

            # Pick the front N expirations that meet the min DTE requirement.
            expirations = sorted(
                exp
                for exp in params.expirations
                if len(exp) == 8 and (date(int(exp[:4]), int(exp[4:6]), int(exp[6:8])) - today).days >= min_dte
            )[:max_expirations]

            if not expirations:
                continue

            log.info(
                "[%s] tradingClass=%s exchange=%s — %d expirations selected",
                ticker,
                params.tradingClass,
                params.exchange,
                len(expirations),
            )

            for expiry in expirations:
                expiry_contracts = await self._contracts_for_expiry(
                    ticker=ticker,
                    expiry=expiry,
                    exchange=params.exchange,
                    trading_class=params.tradingClass,
                    spot=spot,
                    strike_pct_range=strike_pct_range,
                )
                contracts.extend(expiry_contracts)

        # Sort NTM-first so the first batch contains the most liquid (ATM) contracts.
        # This ensures the dark-feed check (batch_idx==0 early-abort) sees real data
        # rather than deep-OTM contracts that may have no market quote.
        if spot is not None:
            contracts.sort(key=lambda c: abs(c.strike - spot))

        log.info("[%s] total contracts enumerated: %d", ticker, len(contracts))
        return contracts, spot

    async def _contracts_for_expiry(
        self,
        ticker: str,
        expiry: str,
        exchange: str,
        trading_class: str,
        spot: float | None,
        strike_pct_range: float,
    ) -> list[Option]:
        """Return all qualified Option contracts for one (expiry, tradingClass).

        Uses reqContractDetailsAsync with a partial contract (no strike/right)
        so IB returns every matching contract with its real conId in one call.
        """
        partial = Option(
            symbol=ticker,
            lastTradeDateOrContractMonth=expiry,
            exchange=exchange,
            tradingClass=trading_class,
            currency="USD",
        )

        try:
            details = await self.ib.reqContractDetailsAsync(partial)
        except Exception as exc:
            log.warning("[%s] reqContractDetails failed for %s/%s %s: %s", ticker, exchange, trading_class, expiry, exc)
            return []

        if not details:
            log.debug("[%s] no contracts returned for %s %s %s", ticker, trading_class, expiry, exchange)
            return []

        result: list[Option] = []
        for cd in details:
            c = cd.contract
            # reqContractDetailsAsync always returns generic Contract objects,
            # never typed Option subclasses — isinstance(c, Option) is always False.
            if not c.conId or not getattr(c, "strike", None):
                continue
            # Strike filter (skip if spot is unknown)
            if spot is not None:
                lo = spot * (1 - strike_pct_range)
                hi = spot * (1 + strike_pct_range)
                if not (lo <= c.strike <= hi):
                    continue
            result.append(c)

        return result

    # ---------------------------------------------------------------------- #
    # Snapshot fetching
    # ---------------------------------------------------------------------- #

    async def iter_snapshot_batches(
        self,
        ticker: str,
        contracts: list[Option],
        spot: float | None,
        request_ts: datetime,
        batch_size: int = BATCH_SIZE,
        timeout: float = BATCH_TIMEOUT,
    ):
        """Async generator: yield one batch of row dicts per IB snapshot round-trip.

        Callers persist each batch immediately so partial results survive daemon
        restarts or cycle timeouts. Rows are only produced when at least one of
        bid, ask, or last is present — contracts with no data within *timeout*
        are skipped and counted as attempted-but-not-persisted.

        Yields:
            list[dict] — one per completed batch (may be empty for dark tickers).
        """
        total = len(contracts)

        for batch_idx, batch_start in enumerate(range(0, total, batch_size)):
            batch = contracts[batch_start : batch_start + batch_size]
            batch_rows = await self._fetch_batch(
                ticker=ticker,
                batch=batch,
                spot=spot,
                request_ts=request_ts,
                timeout=timeout,
            )

            # Dark-feed fast-path: if the very first full batch produced zero
            # rows, this ticker has no live market data this cycle (entitlement
            # gap / feed down). Abort instead of burning the full per-batch
            # timeout on every remaining contract.
            if batch_idx == 0 and not batch_rows and total > len(batch):
                log.warning(
                    "[%s] first batch of %d returned no data — treating ticker as dark this cycle, "
                    "skipping remaining %d contracts",
                    ticker,
                    len(batch),
                    total - len(batch),
                )
                return

            yield batch_rows

    async def _fetch_batch(
        self,
        ticker: str,
        batch: list[Option],
        spot: float | None,
        request_ts: datetime,
        timeout: float,
    ) -> list[dict]:
        # Request incrementally and track exactly which lines were opened, so a
        # mid-batch reqMktData failure (bad contract, disconnect, line limit)
        # still lets the finally cancel precisely the lines we created — and a
        # single bad contract is skipped rather than aborting the whole batch.
        requested: list[tuple[Option, object]] = []
        quote_ts_map: dict[int, datetime] = {}
        greeks_ts_map: dict[int, datetime] = {}

        try:
            for c in batch:
                try:
                    td = self.ib.reqMktData(c, "", snapshot=True, regulatorySnapshot=False)
                except Exception as exc:
                    log.warning("[%s] reqMktData failed for conId=%s: %s", ticker, getattr(c, "conId", "?"), exc)
                    continue
                requested.append((c, td))

            tickers = [td for _c, td in requested]
            deadline = time.monotonic() + timeout
            while tickers and time.monotonic() < deadline:
                await asyncio.sleep(0.2)
                now_utc = datetime.now(tz=timezone.utc)
                for i, t in enumerate(tickers):
                    if i not in quote_ts_map and _has_quote(t):
                        quote_ts_map[i] = now_utc
                    if i not in greeks_ts_map and t.modelGreeks is not None and t.modelGreeks.delta is not None:
                        greeks_ts_map[i] = now_utc
                # Exit early once all have at least a quote
                if len(quote_ts_map) == len(tickers):
                    break
        finally:
            # Cancel exactly the lines we opened — including when this batch is
            # cancelled by the cycle-timeout guard or shutdown.
            for c, _td in requested:
                try:
                    self.ib.cancelMktData(c)
                except Exception:
                    pass

        rows = []
        for i, (contract, ticker_data) in enumerate(requested):
            # Skip contracts with no useful data
            if not _has_quote(ticker_data) and ticker_data.modelGreeks is None:
                continue

            vol = ticker_data.volume
            if vol == IB_NO_VOLUME or vol == -1:
                vol = None

            expiry_str = contract.lastTradeDateOrContractMonth
            try:
                expiry_date = date(int(expiry_str[:4]), int(expiry_str[4:6]), int(expiry_str[6:8]))
            except (ValueError, IndexError, TypeError):
                # archive.option_chain.expiry is DATE NOT NULL — a single
                # un-parseable expiry in the batch would make the whole
                # executemany fail and lose every row for this ticker. Skip
                # just this contract instead.
                log.warning("[%s] skipping conId=%s — unparseable expiry %r", ticker, contract.conId, expiry_str)
                continue

            rows.append(
                {
                    "con_id": contract.conId,
                    "ticker": ticker,
                    "trading_class": contract.tradingClass,
                    "expiry": expiry_date,
                    "strike": contract.strike,
                    "right": contract.right,
                    "request_ts": request_ts,
                    "quote_ts": quote_ts_map.get(i),
                    "greeks_ts": greeks_ts_map.get(i),
                    "bid": _safe(ticker_data.bid),
                    "ask": _safe(ticker_data.ask),
                    "bid_size": _safe(ticker_data.bidSize),
                    "ask_size": _safe(ticker_data.askSize),
                    "last": _safe(ticker_data.last),
                    "last_size": _safe(ticker_data.lastSize),
                    "volume": _safe(vol),
                    "open_interest": _safe(ticker_data.openInterest) if hasattr(ticker_data, "openInterest") else None,
                    "model_greeks": ticker_data.modelGreeks,
                    "underlying_px": spot,
                }
            )

        return rows


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _has_quote(ticker_data: object) -> bool:
    """Return True if the Ticker has a valid bid or ask or last."""
    for field in ("bid", "ask", "last"):
        v = getattr(ticker_data, field, None)
        if isinstance(v, (int, float)) and v == v and v > 0:
            return True
    return False


def _safe(v: object) -> object:
    """Convert NaN/None/subnormal to None for DB insertion.

    Postgres FLOAT8 rejects subnormal floats (|v| < ~2.2e-308) with
    NumericValueOutOfRange. Deep-OTM Greeks (gamma, vega) can underflow to
    these values, so clamp them to NULL rather than letting a single bad row
    abort the entire executemany batch.
    """
    if v is None:
        return None
    if isinstance(v, float):
        if v != v:  # NaN
            return None
        if v != 0 and abs(v) < 2.3e-308:  # subnormal / near-underflow
            return None
    return v
