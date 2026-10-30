"""Probe: does reqMktData(snapshot=True) deliver modelGreeks for index options?

Answers the open review question for the option-chain snapshotter: the daemon
uses snapshot=True and reads ticker.modelGreeks. Snapshot mode is widely
believed to be one-shot bid/ask/last only (greeks come via streaming generic
tick 106 / model-option-computation). This connects to live IB (read-only) and
compares snapshot vs streaming for a single ATM SPX option.

Usage:
    IB_HOST=100.66.147.98 IB_PORT=4001 uv run python scripts/research/probe_snapshot_greeks.py
"""

from __future__ import annotations

import asyncio
import os

from ib_async import IB, Index, Option

HOST = os.environ.get("IB_HOST", "100.66.147.98")
PORT = int(os.environ.get("IB_PORT", "4001"))
CLIENT_ID = int(os.environ.get("IB_CLIENT_ID", "947"))  # avoid prod clientId clashes


def _greeks_str(mg) -> str:
    if mg is None:
        return "modelGreeks=None"
    return (
        f"modelGreeks(impliedVol={mg.impliedVol}, delta={mg.delta}, "
        f"gamma={mg.gamma}, vega={mg.vega}, theta={mg.theta}, undPrice={mg.undPrice})"
    )


async def _poll(ib: IB, ticker, seconds: float, label: str) -> None:
    deadline = asyncio.get_event_loop().time() + seconds
    got_greeks_at = None
    got_quote_at = None
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.25)
        now = asyncio.get_event_loop().time() - t0
        if got_quote_at is None and any(
            isinstance(v, (int, float)) and v == v and v > 0 for v in (ticker.bid, ticker.ask, ticker.last)
        ):
            got_quote_at = now
        if got_greeks_at is None and ticker.modelGreeks is not None and ticker.modelGreeks.delta is not None:
            got_greeks_at = now
            break
    print(f"  [{label}] quote@={got_quote_at}  greeks@={got_greeks_at}")
    print(f"  [{label}] bid={ticker.bid} ask={ticker.ask} last={ticker.last}")
    print(f"  [{label}] {_greeks_str(ticker.modelGreeks)}")


async def main() -> None:
    ib = IB()
    print(f"connecting {HOST}:{PORT} clientId={CLIENT_ID} (read-only probe) ...")
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, timeout=20, readonly=True)
    print(f"connected (serverVersion={ib.client.serverVersion()})")
    try:
        # Frozen so we get last-known values even though the market is closed.
        ib.reqMarketDataType(2)

        und = Index(symbol="SPX", exchange="CBOE", currency="USD")
        await ib.qualifyContractsAsync(und)
        print(f"SPX underlier conId={und.conId}")

        params = await ib.reqSecDefOptParamsAsync(
            underlyingSymbol="SPX", futFopExchange="", underlyingSecType="IND", underlyingConId=und.conId
        )
        p = (
            next(x for x in params if x.tradingClass == "SPXW")
            if any(x.tradingClass == "SPXW" for x in params)
            else params[0]
        )
        expiries = sorted(e for e in p.expirations if len(e) == 8)
        expiry = expiries[len(expiries) // 4]  # a near-ish expiry

        # Fetch spot so we can pick a real ATM strike.
        und_t = ib.reqMktData(und, "", snapshot=True, regulatorySnapshot=False)
        for _ in range(20):
            await asyncio.sleep(0.25)
            spot = und_t.last if (und_t.last and und_t.last == und_t.last and und_t.last > 0) else und_t.close
            if spot and spot == spot and spot > 0:
                break
        ib.cancelMktData(und)
        print(f"SPX spot≈{spot}")

        # Enumerate REAL contracts exactly like the daemon does: partial Option
        # (no strike/right) via reqContractDetails → real conIds in one call.
        # This also verifies the bulk-enumeration path used for NDX/RUT.
        partial = Option("SPX", expiry, exchange=p.exchange, tradingClass=p.tradingClass, currency="USD")
        details = await ib.reqContractDetailsAsync(partial)
        calls = [cd.contract for cd in details if cd.contract.right == "C"]
        print(f"reqContractDetails returned {len(details)} contracts for {expiry} ({len(calls)} calls)")
        # Pick the call whose strike is closest to spot.
        opt = min(calls, key=lambda c: abs(c.strike - (spot or calls[len(calls) // 2].strike)))
        print(f"probe contract: SPXW {expiry} C {opt.strike} conId={opt.conId} on {opt.exchange}/{opt.tradingClass}")

        print("\n--- A) snapshot=True (what the daemon uses) ---")
        t_snap = ib.reqMktData(opt, "", snapshot=True, regulatorySnapshot=False)
        await _poll(ib, t_snap, 8.0, "snapshot")
        ib.cancelMktData(opt)

        await asyncio.sleep(1.0)

        print("\n--- B) snapshot=False, genericTickList='106' (streaming greeks) ---")
        t_stream = ib.reqMktData(opt, "106", snapshot=False, regulatorySnapshot=False)
        await _poll(ib, t_stream, 10.0, "stream")
        ib.cancelMktData(opt)

        print("\n=== VERDICT ===")
        snap_ok = t_snap.modelGreeks is not None and t_snap.modelGreeks.delta is not None
        stream_ok = t_stream.modelGreeks is not None and t_stream.modelGreeks.delta is not None
        print(f"snapshot delivered greeks: {snap_ok}")
        print(f"streaming delivered greeks: {stream_ok}")
        print("(market may be closed → frozen data; treat a NO on both as inconclusive, not proof)")
    finally:
        ib.disconnect()
        print("disconnected")


if __name__ == "__main__":
    asyncio.run(main())
