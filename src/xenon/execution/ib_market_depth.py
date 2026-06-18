#!/usr/bin/env python3
"""Fetch a point-in-time L2 market-depth snapshot from IB for a contract.

Usage:
    xenon-ib-market-depth --symbol AAPL
    xenon-ib-market-depth --symbol QQQ --expiry 20260618 --strike 200 --right C

Mirrors ib_option_chain.py: a SYNCHRONOUS subprocess that connects with its own
clientId, subscribes to market depth, lets the book settle, prints JSON, exits.
Run as a subprocess (never inside FastAPI's event loop) so the sync ib_async API
is safe — see memory `ib_async_in_fastapi`.

`reqMktDepth` returns a Ticker immediately and fills `domBids`/`domAsks` via
events that `ib.sleep` drives; we poll briefly then read the book.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone

from ib_async import Option

from xenon.clients.ib_client import IBClient

# Single source for the index-exchange map; do not duplicate it here.
from xenon.execution.ib_option_chain import underlying_contract

DEPTH_SETTLE_MAX_SECS = 2.0
DEPTH_POLL_SECS = 0.1
DEFAULT_NUM_ROWS = 10
MAX_NUM_ROWS = 20

# Mirror the relay's isDepthPermissionError (ib_connection_status.js): ONLY
# 10089 (no L2 entitlement) / 10092 (deep depth unsupported for this
# secType/exchange, e.g. index options on CBOE) or the equivalent text are
# genuine permission failures. 2152/309/316/317 are operational chatter on a
# working book — mis-treating them as permission errors froze the relay ladder.
_DEPTH_PERMISSION_PATTERN = re.compile(
    r"depth.*not (allowed|eligible)|not supported for this combination", re.IGNORECASE
)


def _is_depth_permission_error(code, message) -> bool:
    try:
        numeric = int(code)
    except (TypeError, ValueError):
        numeric = None
    if numeric in (10089, 10092):
        return True
    return bool(_DEPTH_PERMISSION_PATTERN.search(str(message or "")))


def _level(level) -> dict:
    return {"price": level.price, "size": level.size, "marketMaker": level.marketMaker}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--expiry", default=None, help="YYYYMMDD; with --strike + --right -> option depth")
    parser.add_argument("--strike", type=float, default=None)
    parser.add_argument("--right", default=None, help="C or P")
    parser.add_argument("--num-rows", type=int, default=DEFAULT_NUM_ROWS)
    parser.add_argument("--port", type=int, default=4001)
    parser.add_argument("--client-id", default="auto")
    args = parser.parse_args()

    # All-or-none option tuple — never silently degrade an option request to
    # stock depth on a partial tuple.
    opt_parts = [args.expiry, args.strike, args.right]
    present = sum(1 for p in opt_parts if p is not None)
    if present not in (0, 3):
        print(json.dumps({"error": "provide all of --expiry/--strike/--right, or none"}))
        sys.exit(2)

    num_rows = max(1, min(args.num_rows, MAX_NUM_ROWS))
    client_id = args.client_id if args.client_id == "auto" else int(args.client_id)

    permission = {"hit": False}
    last_code = {"code": None}

    def _on_error(reqId, errorCode, errorString, *rest):  # ib_async errorEvent signature
        last_code["code"] = errorCode
        if _is_depth_permission_error(errorCode, errorString):
            permission["hit"] = True

    client = IBClient()
    try:
        client.connect(port=args.port, client_id=client_id)
        client._ib.errorEvent += _on_error

        if present == 3:
            contract = Option(
                symbol=args.symbol.upper(),
                lastTradeDateOrContractMonth=args.expiry,
                strike=float(args.strike),
                right=args.right.upper(),
                exchange="SMART",
                currency="USD",
            )
            sec_type = "OPT"
        else:
            contract, sec_type = underlying_contract(args.symbol)

        client._ib.qualifyContracts(contract)
        if not contract.conId:
            print(json.dumps({"error": f"could not qualify {args.symbol}"}))
            sys.exit(1)

        is_smart = True
        ticker = client._ib.reqMktDepth(contract, num_rows, is_smart)

        # Bounded poll: return as soon as the book populates or a permission
        # error fires; cap at DEPTH_SETTLE_MAX_SECS.
        waited = 0.0
        while waited < DEPTH_SETTLE_MAX_SECS:
            client._ib.sleep(DEPTH_POLL_SECS)
            waited += DEPTH_POLL_SECS
            if permission["hit"]:
                break
            if ticker.domBids or ticker.domAsks:
                client._ib.sleep(DEPTH_POLL_SECS)  # one extra tick for more levels
                break

        bids = [_level(lvl) for lvl in ticker.domBids]
        asks = [_level(lvl) for lvl in ticker.domAsks]
        client._ib.cancelMktDepth(contract, is_smart)

        # `entitled` reflects the PERMISSION axis only — never data presence.
        entitled = not permission["hit"]
        note = None
        if permission["hit"]:
            note = "no L2 entitlement"
        elif not bids and not asks:
            note = "depth line budget exhausted (309)" if last_code["code"] == 309 else "no depth returned"

        out = {
            "symbol": args.symbol.upper(),
            "conId": contract.conId,
            "secType": sec_type,
            "isSmartDepth": is_smart,
            "entitled": entitled,
            "numRows": num_rows,
            "asOf": datetime.now(timezone.utc).isoformat(),
            "bids": bids,
            "asks": asks,
        }
        if note:
            out["note"] = note
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
