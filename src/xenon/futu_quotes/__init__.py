"""futu-quotes — Futu OpenD → relay WebSocket quote bridge.

Streams US stock/ETF QUOTE subscriptions from a local Futu OpenD and serves
them to the ib_realtime relay over one internal WebSocket. Contract:
``docs/architecture/futu-quotes.md``.
"""

from xenon.futu_quotes.server import FutuQuotesService, run_forever  # noqa: F401
