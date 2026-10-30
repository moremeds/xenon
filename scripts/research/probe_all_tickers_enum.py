"""Probe: verify the daemon's enumeration + snapshot path for ALL four tickers.

For SPX/NDX/RUT/VIX: qualify underlier, reqSecDefOptParams, reqContractDetails
on the front valid expiry (the exact bulk-enumeration the daemon uses), then a
snapshot on the ATM call to confirm quotes + greeks arrive.

    IB_HOST=100.66.147.98 IB_PORT=4001 uv run python scripts/research/probe_all_tickers_enum.py
"""

from __future__ import annotations

import asyncio
import os

from ib_async import IB, Index, Option

HOST = os.environ.get("IB_HOST", "100.66.147.98")
PORT = int(os.environ.get("IB_PORT", "4001"))
CLIENT_ID = int(os.environ.get("IB_CLIENT_ID", "949"))

INDEX_EXCHANGE = {"SPX": "CBOE", "NDX": "NASDAQ", "RUT": "RUSSELL", "VIX": "CBOE"}


async def _spot(ib: IB, und: Index) -> float | None:
    t = ib.reqMktData(und, "", snapshot=True, regulatorySnapshot=False)
    for _ in range(16):
        await asyncio.sleep(0.25)
        for v in (t.last, t.close, t.marketPrice()):
            if isinstance(v, (int, float)) and v == v and v > 0:
                ib.cancelMktData(und)
                return float(v)
    ib.cancelMktData(und)
    return None


async def probe(ib: IB, sym: str) -> None:
    exch = INDEX_EXCHANGE[sym]
    print(f"\n===== {sym} (underlier exchange {exch}) =====")
    und = Index(symbol=sym, exchange=exch, currency="USD")
    await ib.qualifyContractsAsync(und)
    if not und.conId:
        print(f"  {sym}: underlier qualification FAILED")
        return
    spot = await _spot(ib, und)
    print(f"  spot≈{spot} conId={und.conId}")

    params = await ib.reqSecDefOptParamsAsync(
        underlyingSymbol=sym, futFopExchange="", underlyingSecType="IND", underlyingConId=und.conId
    )
    if not params:
        print(f"  {sym}: reqSecDefOptParams returned NOTHING")
        return
    # Prefer a tradingClass that matches the symbol's weeklies if present.
    p = params[0]
    expiries = sorted(e for e in p.expirations if len(e) == 8)
    if not expiries:
        print(f"  {sym}: no 8-digit expiries")
        return
    expiry = expiries[min(2, len(expiries) - 1)]
    print(f"  tradingClass={p.tradingClass} exch={p.exchange} expiries={len(expiries)} probe_expiry={expiry}")

    partial = Option(sym, expiry, exchange=p.exchange, tradingClass=p.tradingClass, currency="USD")
    details = await ib.reqContractDetailsAsync(partial)
    calls = [cd.contract for cd in details if cd.contract.right == "C"]
    print(f"  reqContractDetails → {len(details)} contracts ({len(calls)} calls) with real conIds")
    if not calls:
        print(f"  {sym}: NO contracts enumerated")
        return

    ref = spot or calls[len(calls) // 2].strike
    opt = min(calls, key=lambda c: abs(c.strike - ref))
    t = ib.reqMktData(opt, "", snapshot=True, regulatorySnapshot=False)
    got_q = got_g = None
    for _ in range(32):
        await asyncio.sleep(0.25)
        if got_q is None and any(isinstance(v, (int, float)) and v == v and v > 0 for v in (t.bid, t.ask, t.last)):
            got_q = True
        if got_g is None and t.modelGreeks is not None and t.modelGreeks.delta is not None:
            got_g = True
        if got_q and got_g:
            break
    ib.cancelMktData(opt)
    mg = t.modelGreeks
    print(f"  ATM C {opt.strike} conId={opt.conId}: quote={bool(got_q)} (bid={t.bid} ask={t.ask}) greeks={bool(got_g)}")
    if mg is not None:
        print(f"    delta={mg.delta} iv={mg.impliedVol} theta={mg.theta}")


async def main() -> None:
    ib = IB()
    print(f"connecting {HOST}:{PORT} clientId={CLIENT_ID} (read-only) ...")
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, timeout=20, readonly=True)
    print(f"connected (serverVersion={ib.client.serverVersion()})")
    try:
        ib.reqMarketDataType(2)  # frozen
        for sym in ("SPX", "NDX", "RUT", "VIX"):
            try:
                await probe(ib, sym)
            except Exception as exc:
                print(f"  {sym}: EXCEPTION {type(exc).__name__}: {exc}")
    finally:
        ib.disconnect()
        print("\ndisconnected")


if __name__ == "__main__":
    asyncio.run(main())
