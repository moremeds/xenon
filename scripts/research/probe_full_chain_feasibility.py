"""Measure FULL-chain size + snapshot timing to judge 10-min cadence feasibility.

For each ticker: enumerate EVERY listed expiration x EVERY strike x both rights
(no strike filter, no expiration cap) and count contracts. Then, for SPX, time
a few representative batches of 50 snapshots to estimate a full-chain cycle.

    IB_HOST=100.66.147.98 IB_PORT=4001 uv run python scripts/research/probe_full_chain_feasibility.py
"""

from __future__ import annotations

import asyncio
import os
import time

from ib_async import IB, Index, Option

HOST = os.environ.get("IB_HOST", "100.66.147.98")
PORT = int(os.environ.get("IB_PORT", "4001"))
CLIENT_ID = int(os.environ.get("IB_CLIENT_ID", "952"))
INDEX_EXCHANGE = {"SPX": "CBOE", "NDX": "NASDAQ", "RUT": "RUSSELL", "VIX": "CBOE"}
BATCH = 50


async def enumerate_full(ib: IB, sym: str) -> tuple[int, int, list[Option]]:
    und = Index(symbol=sym, exchange=INDEX_EXCHANGE[sym], currency="USD")
    await ib.qualifyContractsAsync(und)
    if not und.conId:
        print(f"{sym}: underlier qualify FAILED")
        return 0, 0, []
    params = await ib.reqSecDefOptParamsAsync(
        underlyingSymbol=sym, futFopExchange="", underlyingSecType="IND", underlyingConId=und.conId
    )
    # Use the (exchange, tradingClass) that lists the most expirations.
    p = max(params, key=lambda x: len(x.expirations))
    expiries = sorted(e for e in p.expirations if len(e) == 8)
    all_contracts: list[Option] = []
    for exp in expiries:
        partial = Option(sym, exp, exchange=p.exchange, tradingClass=p.tradingClass, currency="USD")
        try:
            details = await ib.reqContractDetailsAsync(partial)
        except Exception as exc:
            print(f"  {sym} {exp}: reqContractDetails error {exc}")
            continue
        all_contracts.extend(cd.contract for cd in details if cd.contract.conId)
        await asyncio.sleep(0.15)  # gentle pacing
    return len(expiries), len(all_contracts), all_contracts


async def time_batch(ib: IB, batch: list[Option], timeout: float = 8.0) -> tuple[float, int]:
    t0 = time.monotonic()
    tks = [ib.reqMktData(c, "", snapshot=True, regulatorySnapshot=False) for c in batch]
    deadline = t0 + timeout
    got = 0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.2)
        got = sum(
            1
            for t in tks
            if any(isinstance(v, (int, float)) and v == v and v > 0 for v in (t.bid, t.ask, t.last))
            or (t.modelGreeks is not None and t.modelGreeks.delta is not None)
        )
        if got == len(tks):
            break
    for c in batch:
        try:
            ib.cancelMktData(c)
        except Exception:
            pass
    return time.monotonic() - t0, got


async def main() -> None:
    ib = IB()
    print(f"connecting {HOST}:{PORT} clientId={CLIENT_ID} (read-only) ...")
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, timeout=20, readonly=True)
    print(f"connected (serverVersion={ib.client.serverVersion()})\n")
    try:
        ib.reqMarketDataType(2)  # frozen (market closed) — worst case for timing
        counts = {}
        spx_contracts: list[Option] = []
        for sym in ("SPX", "NDX", "RUT", "VIX"):
            n_exp, n_con, contracts = await enumerate_full(ib, sym)
            counts[sym] = (n_exp, n_con)
            if sym == "SPX":
                spx_contracts = contracts
            print(f"{sym:4s}: {n_exp:3d} expirations  ->  {n_con:6d} contracts (full chain)")

        total = sum(c for _, c in counts.values())
        print(f"\nTOTAL full chain across 4 tickers: {total} contracts")
        print(f"At BATCH={BATCH} concurrent lines that is {(total + BATCH - 1) // BATCH} sequential batches.\n")

        # Representative batch timing on SPX (frozen worst case): ATM, mid, deep.
        if spx_contracts:
            spx_sorted = sorted(spx_contracts, key=lambda c: (c.lastTradeDateOrContractMonth, c.strike))
            n = len(spx_sorted)
            samples = {
                "front/ATM-ish": spx_sorted[:BATCH],
                "middle": spx_sorted[n // 2 : n // 2 + BATCH],
                "back/far": spx_sorted[-BATCH:],
            }
            durs = []
            for label, b in samples.items():
                if not b:
                    continue
                d, got = await time_batch(ib, b)
                durs.append(d)
                print(f"  SPX batch [{label:13s}] {len(b)} lines -> {d:4.1f}s, {got}/{len(b)} had data")
            if durs:
                avg = sum(durs) / len(durs)
                spx_batches = (counts["SPX"][1] + BATCH - 1) // BATCH
                all_batches = (total + BATCH - 1) // BATCH
                print(f"\n  avg batch ~{avg:.1f}s (frozen worst case)")
                print(
                    f"  => SPX full chain alone ~ {spx_batches} batches x {avg:.1f}s = {spx_batches * avg / 60:.1f} min"
                )
                print(
                    f"  => ALL 4 tickers full chain ~ {all_batches} batches x {avg:.1f}s = {all_batches * avg / 60:.1f} min"
                )
                print("  (live RTH is faster for liquid strikes via early-exit; deep OTM still waits full timeout)")
    finally:
        ib.disconnect()
        print("\ndisconnected")


if __name__ == "__main__":
    asyncio.run(main())
