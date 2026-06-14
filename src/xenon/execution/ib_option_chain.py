#!/usr/bin/env python3
"""Fetch option chain data from IB for a given symbol.

Usage:
    xenon-ib-option-chain --symbol AAPL
    xenon-ib-option-chain --symbol AAPL --expiry 20260417
"""

import argparse
import json
import sys
from pathlib import Path

from ib_async import Index, Stock

from xenon.clients.ib_client import IBClient

# CBOE is the home exchange for SPX/RUT index options; NDX lives on NASDAQ.
# Mirrors _preferred_index_exchange in src/xenon/api/server.py.
_PREFERRED_INDEX_EXCHANGE = {"NDX": "NASDAQ"}


def underlying_contract(symbol: str):
    """Return (contract, underlyingSecType) for chain qualification.

    Indices in the V1 universe (universe.is_index raises KeyError for
    unknown tickers, hence the is_known gate) qualify as Index on their
    home exchange; everything else stays Stock/SMART.
    """
    from xenon.execution.universe import is_index, is_known

    upper = symbol.upper()
    if is_known(upper) and is_index(upper):
        exchange = _PREFERRED_INDEX_EXCHANGE.get(upper, "CBOE")
        return Index(upper, exchange), "IND"
    return Stock(symbol, "SMART", "USD"), "STK"


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
        contract, sec_type = underlying_contract(args.symbol)
        client._ib.qualifyContracts(contract)
        if not contract.conId:
            print(json.dumps({"error": f"Could not qualify {args.symbol}"}))
            return

        chains = client._ib.reqSecDefOptParams(contract.symbol, "", sec_type, contract.conId)

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

            print(
                json.dumps(
                    {
                        "symbol": args.symbol,
                        "expiry": args.expiry,
                        "exchange": target_chain.exchange,
                        "strikes": strikes,
                        "multiplier": str(target_chain.multiplier),
                    }
                )
            )
        else:
            # Fetch all expirations
            all_expirations = set()
            exchanges = []
            for chain in chains:
                for exp in chain.expirations:
                    all_expirations.add(exp.replace("-", ""))
                exchanges.append(chain.exchange)

            expirations = sorted(all_expirations)

            print(
                json.dumps(
                    {
                        "symbol": args.symbol,
                        "expirations": expirations,
                        "exchanges": exchanges,
                    }
                )
            )
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
