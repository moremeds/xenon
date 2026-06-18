#!/usr/bin/env python3
"""Fetch IB broker-computed option greeks (modelGreeks) for a single contract.

Usage:
    xenon-ib-option-greeks --symbol QQQ --expiry 20260717 --strike 600 --right C

Mirrors ib_market_depth.py: a SYNCHRONOUS subprocess that connects with its own
clientId, subscribes to option market data, polls modelGreeks until they settle,
prints JSON, exits. Run as a subprocess (never inside FastAPI's event loop) so
the sync ib_async API is safe — see memory `ib_async_in_fastapi`.

`reqMktData(snapshot=True)` returns a Ticker immediately; IB fills `modelGreeks`
(impliedVol/delta/gamma/vega/theta/undPrice) via tickOptionComputation events
that `ib.sleep` drives. We poll until delta populates, then read them off.

Greeks are option-only: there is no underlying/stock fallback. A missing leg of
the (expiry, strike, right) triplet is a hard reject (exit 2), and IB delivering
no greeks (illiquid contract, market closed) is a graceful exit 0 with a note.
"""

import argparse
import json
import math
import sys
from datetime import datetime, timezone

from ib_async import Option

from xenon.clients.ib_client import IBClient

GREEKS_SETTLE_MAX_SECS = 12.0
GREEKS_POLL_SECS = 0.1


def _safe(v):
    """None for missing/NaN; passthrough otherwise (mirrors the snapshotter spike)."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return v


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--expiry", default=None, help="YYYYMMDD")
    parser.add_argument("--strike", type=float, default=None)
    parser.add_argument("--right", default=None, help="C or P")
    parser.add_argument("--port", type=int, default=4001)
    parser.add_argument("--client-id", default="auto")
    args = parser.parse_args()

    # Greeks are option-only: the full triplet is mandatory. Unlike market-depth
    # there is no underlying fallback — a partial tuple is always a reject.
    if args.expiry is None or args.strike is None or args.right is None:
        print(json.dumps({"error": "option greeks require --expiry, --strike, and --right"}))
        sys.exit(2)

    client_id = args.client_id if args.client_id == "auto" else int(args.client_id)

    client = IBClient()
    try:
        client.connect(port=args.port, client_id=client_id)

        contract = Option(
            symbol=args.symbol.upper(),
            lastTradeDateOrContractMonth=args.expiry,
            strike=float(args.strike),
            right=args.right.upper(),
            exchange="SMART",
            currency="USD",
        )
        client._ib.qualifyContracts(contract)
        if not contract.conId:
            print(
                json.dumps(
                    {
                        "error": f"could not qualify {args.symbol.upper()} {args.expiry} {args.strike}{args.right.upper()}"
                    }
                )
            )
            sys.exit(1)

        ticker = client._ib.reqMktData(contract, "", True, False)

        # Bounded poll: break as soon as IB has computed greeks (delta present).
        waited = 0.0
        while waited < GREEKS_SETTLE_MAX_SECS:
            client._ib.sleep(GREEKS_POLL_SECS)
            waited += GREEKS_POLL_SECS
            mg = ticker.modelGreeks
            if mg is not None and mg.delta is not None:
                break

        client._ib.cancelMktData(contract)

        mg = ticker.modelGreeks
        greeks = None
        if mg is not None and mg.delta is not None:
            greeks = {
                "impliedVol": _safe(mg.impliedVol),
                "delta": _safe(mg.delta),
                "gamma": _safe(mg.gamma),
                "vega": _safe(mg.vega),
                "theta": _safe(mg.theta),
                "undPrice": _safe(mg.undPrice),
            }

        out = {
            "symbol": args.symbol.upper(),
            "conId": contract.conId,
            "secType": "OPT",
            "expiry": args.expiry,
            "strike": float(args.strike),
            "right": args.right.upper(),
            "asOf": datetime.now(timezone.utc).isoformat(),
            "bid": _safe(ticker.bid),
            "ask": _safe(ticker.ask),
            "greeks": greeks,
        }
        if greeks is None:
            out["note"] = "no greeks returned"
        print(json.dumps(out))
    except SystemExit:
        raise
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
