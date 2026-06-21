"""Dev/debug CLI for the option chain snapshotter.

In production the snapshotter runs as a background task inside the FastAPI
lifespan (server.py → _maybe_start_snapshotter).

This entry point is useful for testing the daemon in isolation without
starting the full API stack:

    DATABASE_URL=postgresql://... IB_GATEWAY_HOST=... \\
        python -m xenon.option_chain_snapshotter

Set XENON_OPTION_CHAIN_SNAPSHOTTER=0 in the API environment to avoid running
two concurrent instances when also using this CLI against the same DB.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from xenon.clients.ib_client import DEFAULT_GATEWAY_PORT, DEFAULT_HOST

from .daemon import run_forever

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("xenon.option_chain_snapshotter")


def main() -> int:
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        log.error("DATABASE_URL is not set — exiting")
        return 1

    ib_host = os.environ.get("IB_GATEWAY_HOST", DEFAULT_HOST)
    ib_port = int(os.environ.get("IB_GATEWAY_PORT", str(DEFAULT_GATEWAY_PORT)))
    ib_client_id = int(os.environ.get("IB_CLIENT_ID_SNAPSHOTTER", "901"))

    log.info("option_chain_snapshotter dev CLI (IB=%s:%d clientId=%d)", ib_host, ib_port, ib_client_id)
    log.warning("Production path: set XENON_OPTION_CHAIN_SNAPSHOTTER=0 in API env to avoid dual instances")

    loop = asyncio.new_event_loop()

    def _stop(sig: int, _frame: object) -> None:
        log.info("Signal %d — cancelling", sig)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    try:
        loop.run_until_complete(
            run_forever(
                ib_host=ib_host,
                ib_port=ib_port,
                ib_client_id=ib_client_id,
                database_url=db_url,
            )
        )
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        loop.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
