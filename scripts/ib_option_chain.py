#!/usr/bin/env python3
"""Fetch option chain data from IB for a given symbol.

Usage:
    python3 scripts/ib_option_chain.py --symbol AAPL
    python3 scripts/ib_option_chain.py --symbol AAPL --expiry 20260417
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.clients.ib_client import IBClient


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--expiry", default=None, help="If provided, fetch strikes for this expiry")
    parser.add_argument("--port", type=int, default=4001)
    parser.add_argument("--client-id", type=int, default=27)
    args = parser.parse_args()

    client = IBClient()

    try:
        client.connect(port=args.port, client_id=args.client_id)

        # Qualify the underlying to get a valid conId (required by reqSecDefOptParams)
        from ib_insync import Stock
        stk = Stock(args.symbol, "SMART", "USD")
        client._ib.qualifyContracts(stk)
        if not stk.conId:
            print(json.dumps({"error": f"Could not qualify {args.symbol}"}))
            return

        chains = client._ib.reqSecDefOptParams(args.symbol, "", "STK", stk.conId)

        if args.expiry:
            # Find the matching chain
            target_chain = None
            for chain in chains:
                if args.expiry in [e.replace("-", "") for e in chain.expirations]:
                    target_chain = chain
                    break

            if not target_chain:
                print(json.dumps({"error": f"No chain found for expiry {args.expiry}"}))
                return

            # Get strikes for this expiry
            strikes = sorted(target_chain.strikes)

            print(json.dumps({
                "symbol": args.symbol,
                "expiry": args.expiry,
                "exchange": target_chain.exchange,
                "strikes": strikes,
                "multiplier": str(target_chain.multiplier),
            }))
        else:
            # Fetch all expirations
            all_expirations = set()
            exchanges = []
            for chain in chains:
                for exp in chain.expirations:
                    all_expirations.add(exp.replace("-", ""))
                exchanges.append(chain.exchange)

            expirations = sorted(all_expirations)

            print(json.dumps({
                "symbol": args.symbol,
                "expirations": expirations,
                "exchanges": exchanges,
            }))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
