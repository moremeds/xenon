"""``xenon-futu-quotes`` entry point.

Env: ``FUTU_QUOTES_HOST``/``FUTU_QUOTES_PORT`` (listen, default
0.0.0.0:8767), ``FUTU_QUOTE_QUOTA`` (admission ceiling, default 800),
``FUTU_OPEND_HOST``/``FUTU_OPEND_PORT`` (OpenD, default 127.0.0.1:11111).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="xenon-futu-quotes",
        description="Stream Futu OpenD US stock/ETF quotes to the ib_realtime relay.",
    )
    p.add_argument("--host", default=os.environ.get("FUTU_QUOTES_HOST", "0.0.0.0"))
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("FUTU_QUOTES_PORT", "8767")),
    )
    p.add_argument("--log-level", default="INFO")
    p.add_argument(
        "--self-test",
        action="store_true",
        help="run the fake-OpenD scenario over a real local WebSocket and exit",
    )
    args = p.parse_args(argv)

    if args.self_test:
        from xenon.futu_quotes._selftest import run_self_test

        return asyncio.run(run_self_test())

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("xenon.futu_quotes")

    # Imported late: the futu SDK touches the filesystem at import time.
    from xenon.futu_quotes.opend import FutuOpenD
    from xenon.futu_quotes.server import run_forever

    quota_cap = int(os.environ.get("FUTU_QUOTE_QUOTA", "800"))
    opend = FutuOpenD(
        host=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"),
        port=int(os.environ.get("FUTU_OPEND_PORT", "11111")),
    )
    try:
        asyncio.run(run_forever(args.host, args.port, opend, quota_cap=quota_cap))
    except KeyboardInterrupt:  # pragma: no cover
        pass
    log.info("futu-quotes stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
