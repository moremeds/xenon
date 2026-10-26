"""Thin async wrapper around Futu OpenD quote context.

Wraps ``OpenQuoteContext`` + ``StockQuoteHandlerBase`` + ``query_subscription``
+ ``get_global_state``, all behind one serialized asyncio facade. The sync
Futu SDK calls run via ``asyncio.to_thread``; a single lock keeps OpenD round
trips sequential (the service also issues them from one worker).

SDK callbacks (quote pushes, disconnect, reconnect) are fenced by origin:
each context carries a generation, and each push carries the context's
link counter read on the SDK thread. Callbacks from a closed/replaced
context, and pushes received before a later disconnect/reconnect of the
same context, are dropped on the loop thread.

The service only consumes the ``OpenDLike`` protocol, so tests inject a fake
with the same async surface and no network.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional, Protocol

from xenon.clients.futu_client import _probe_opend

try:
    from futu import (
        RET_ERROR,
        RET_OK,
        OpenQuoteContext,
        OrderBookHandlerBase,
        StockQuoteHandlerBase,
        SubType,
    )
except ImportError:  # pragma: no cover — futu-api missing
    RET_ERROR = None  # type: ignore[assignment]
    RET_OK = None  # type: ignore[assignment]
    OpenQuoteContext = None  # type: ignore[assignment]
    OrderBookHandlerBase = object  # type: ignore[assignment,misc]
    StockQuoteHandlerBase = object  # type: ignore[assignment,misc]
    SubType = None  # type: ignore[assignment]

logger = logging.getLogger("xenon.futu_quotes.opend")

# Subtypes every subscription holds together: QUOTE alone has no bid/ask, so
# the pair is atomic — a symbol is ready only when both are subscribed.
_SUBTYPES = [SubType.QUOTE, SubType.ORDER_BOOK] if SubType is not None else []


class OpenDError(Exception):
    """Base for OpenD call failures."""


class OpenDDownError(OpenDError):
    """OpenD unreachable / connection dead."""


class OpenDTransientError(OpenDError):
    """Rate limit, timeout or other retryable provider failure."""


class OpenDNotFoundError(OpenDError):
    """Provider says the code does not exist."""


class OpenDProviderError(OpenDError):
    """Provider rejected the call (message preserved for classification)."""


_TRANSIENT_HINTS = (
    "frequent",
    "rate",
    "too many",
    "busy",
    "timeout",
    "timed out",
    "try again",
    "throttl",
)
_NOT_FOUND_HINTS = (
    "not exist",
    "no such",
    "unknown code",
    "invalid code",
    "illegal code",
    "invalid security",
    "inv code",
    "not found",
    "代码",
)


def _classify_call_error(msg: str) -> OpenDError:
    m = (msg or "").lower()
    if any(k in m for k in _TRANSIENT_HINTS):
        return OpenDTransientError(msg)
    if any(k in m for k in _NOT_FOUND_HINTS):
        return OpenDNotFoundError(msg)
    return OpenDProviderError(msg)


_QUOTA_HINTS = ("quota", "exceed", "over the limit", "reached the limit", "subscription limit", "额度")
_UNSUPPORTED_HINTS = (
    "not support",
    "unsubscrib",
    "cannot subscribe",
    "illegal",
    "not allow",
    "不支持",
)


def classify_subscribe_error(msg: str) -> str:
    """Map a subscribe rejection message to an ``assign`` rejected reason.

    Not-found/invalid-code is checked before "not supported": a code that
    does not exist at all (e.g. a class-share spelling the provider cannot
    parse) must land in ``rejected/unknown_symbol``, never a retryable
    bucket.
    """
    m = (msg or "").lower()
    if any(k in m for k in _QUOTA_HINTS):
        return "quota"
    if any(k in m for k in _NOT_FOUND_HINTS):
        return "unknown_symbol"
    if any(k in m for k in _UNSUPPORTED_HINTS):
        return "not_supported"
    return "provider_error"


_UNSUB_EARLY_HINTS = ("too early", "too soon", "too short", "too frequent", "interval", "minute")
_UNSUB_GONE_HINTS = ("not subscribed", "no subscription", "never subscribed", "no sub")


def classify_unsubscribe_error(msg: str) -> str:
    """Map an unsubscribe rejection to ``too_early`` | ``not_subscribed`` |
    ``provider_error``."""
    m = (msg or "").lower()
    if any(k in m for k in _UNSUB_GONE_HINTS):
        return "not_subscribed"
    if any(k in m for k in _UNSUB_EARLY_HINTS):
        return "too_early"
    return "provider_error"


class OpenDLike(Protocol):
    """Async surface the service depends on (fake-compatible)."""

    on_quote_rows: Optional[Callable[[list[dict]], None]]
    on_book_rows: Optional[Callable[[list[dict]], None]]
    on_disconnected: Optional[Callable[[], None]]
    on_reconnected: Optional[Callable[[], None]]

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def get_market_snapshot(self, codes: list[str]) -> list[dict]: ...

    async def subscribe(self, codes: list[str]) -> dict[str, Optional[str]]: ...

    async def unsubscribe(self, codes: list[str]) -> dict[str, Optional[str]]: ...

    async def query_subscription(self) -> dict: ...

    async def get_global_state(self) -> dict: ...


class _QuoteContext(OpenQuoteContext if OpenQuoteContext is not None else object):  # type: ignore[misc]
    """OpenQuoteContext whose callbacks carry their origin to the loop.

    ``_fq_gen`` names this context; ``_fq_link`` counts its socket
    transitions (bumped on the SDK thread at every disconnect/recovery).
    """

    def __init__(
        self, *args: Any, loop: asyncio.AbstractEventLoop, sink: "FutuOpenD", gen: int, **kwargs: Any
    ) -> None:
        # Attributes first: the base __init__ opens the socket, so a very
        # early disconnect could reach on_disconnect before they exist.
        self._fq_loop = loop
        self._fq_sink = sink
        self._fq_gen = gen
        self._fq_link = 0
        # The base __init__ calls on_api_socket_reconnected on the FIRST
        # connect too; that is not a recovery (the supervisor handles it).
        self._fq_ready = False
        super().__init__(*args, **kwargs)
        self._fq_ready = True

    def on_disconnect(self, conn_id: int, reason: Any, msg: str) -> None:  # noqa: D102
        self._fq_link += 1
        try:
            super().on_disconnect(conn_id, reason, msg)
        except Exception:  # pragma: no cover - SDK internals
            pass
        self._fq_loop.call_soon_threadsafe(self._fq_sink._deliver_lifecycle, self._fq_gen, False)

    def on_api_socket_reconnected(self) -> tuple:  # noqa: D102
        # The base returns (ret, msg) — OpenContextBase._init_connect_sync
        # unpacks it even on the first connect. Preserve the contract; only
        # a successful SDK resubscribe after a real reconnect is a recovery.
        try:
            ret, msg = super().on_api_socket_reconnected()
        except Exception as exc:  # pragma: no cover - SDK internals
            logger.exception("OpenD reconnect resubscribe raised")
            return RET_ERROR, f"xenon reconnect callback: {exc}"
        if ret == RET_OK:
            if self._fq_ready:
                self._fq_link += 1
                self._fq_loop.call_soon_threadsafe(
                    self._fq_sink._deliver_lifecycle, self._fq_gen, True
                )
        else:
            logger.warning("OpenD reconnect resubscribe failed: %s", msg)
        return ret, msg


class _StockQuoteHandler(StockQuoteHandlerBase):  # type: ignore[misc]
    """Forwards parsed QUOTE push frames, tagged with their origin."""

    def __init__(self, ctx: _QuoteContext) -> None:
        super().__init__()
        self._ctx = ctx

    def on_recv_rsp(self, rsp_pb: Any) -> tuple:
        ret, content = super().on_recv_rsp(rsp_pb)
        if ret == RET_OK and content is not None:
            ctx = self._ctx
            origin = (ctx._fq_gen, ctx._fq_link)  # read on the SDK thread
            try:
                rows = content.to_dict(orient="records")
            except Exception:  # pragma: no cover - defensive
                logger.exception("quote push frame not a DataFrame")
                return ret, content
            ctx._fq_loop.call_soon_threadsafe(ctx._fq_sink._deliver_rows, origin, rows)
        return ret, content


class _OrderBookHandler(OrderBookHandlerBase):  # type: ignore[misc]
    """Forwards parsed ORDER_BOOK push frames, tagged with their origin.

    ``content`` is one ``{code, Bid, Ask, ...}`` dict per frame (not a
    DataFrame), so it is wrapped in a single-item list to match the
    ``on_book_rows`` rows convention.
    """

    def __init__(self, ctx: _QuoteContext) -> None:
        super().__init__()
        self._ctx = ctx

    def on_recv_rsp(self, rsp_pb: Any) -> tuple:
        ret, content = super().on_recv_rsp(rsp_pb)
        if ret == RET_OK and isinstance(content, dict):
            ctx = self._ctx
            origin = (ctx._fq_gen, ctx._fq_link)  # read on the SDK thread
            ctx._fq_loop.call_soon_threadsafe(
                ctx._fq_sink._deliver_book, origin, [content]
            )
        return ret, content


class FutuOpenD:
    """Serialized async facade over one ``OpenQuoteContext``.

    Subscriptions are the QUOTE + ORDER_BOOK pair: a symbol counts as held
    only when both subtypes are subscribed in the current session, and
    unsubscribe releases both together.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 11111, probe_timeout: float = 3.0) -> None:
        self.host = host
        self.port = port
        self.probe_timeout = probe_timeout
        self.on_quote_rows: Optional[Callable[[list[dict]], None]] = None
        self.on_book_rows: Optional[Callable[[list[dict]], None]] = None
        self.on_disconnected: Optional[Callable[[], None]] = None
        self.on_reconnected: Optional[Callable[[], None]] = None
        self._ctx: Any = None
        # Generation of the live context; bumped on every connect and close
        # so callbacks from a replaced context are dropped.
        self._gen = 0
        self._lock = asyncio.Lock()

    async def probe(self) -> None:
        try:
            await asyncio.to_thread(_probe_opend, self.host, self.port, self.probe_timeout)
        except Exception as exc:
            raise OpenDDownError(str(exc)) from exc

    async def connect(self) -> None:
        if OpenQuoteContext is None:
            raise OpenDDownError("futu-api is not installed")
        await self.probe()
        loop = asyncio.get_running_loop()
        self._gen += 1
        gen = self._gen

        def _open() -> Any:
            ctx = _QuoteContext(host=self.host, port=self.port, loop=loop, sink=self, gen=gen)
            ctx.set_handler(_StockQuoteHandler(ctx))
            ctx.set_handler(_OrderBookHandler(ctx))
            ctx.start()
            return ctx

        try:
            async with self._lock:
                self._ctx = await asyncio.to_thread(_open)
        except OpenDError:
            raise
        except Exception as exc:
            raise OpenDDownError(str(exc)) from exc
        logger.info("futu-quotes connected to OpenD %s:%s", self.host, self.port)

    async def close(self) -> None:
        self._gen += 1  # fence callbacks from the closing context
        ctx, self._ctx = self._ctx, None
        if ctx is not None:
            try:
                await asyncio.to_thread(ctx.close)
            except Exception:  # pragma: no cover - best effort
                pass

    def _deliver_lifecycle(self, gen: int, up: bool) -> None:
        """Loop thread: drop lifecycle callbacks of a replaced context."""
        if gen != self._gen:
            return
        cb = self.on_reconnected if up else self.on_disconnected
        if cb is not None:
            cb()

    def _deliver_rows(self, origin: tuple[int, int], rows: list[dict]) -> None:
        """Loop thread: drop pushes from a replaced context or from before a
        later disconnect/reconnect of the live one."""
        gen, link = origin
        ctx = self._ctx
        if gen != self._gen or ctx is None or link != ctx._fq_link:
            return
        cb = self.on_quote_rows
        if cb is not None:
            cb(rows)

    def _deliver_book(self, origin: tuple[int, int], rows: list[dict]) -> None:
        """Loop thread: same origin fence as ``_deliver_rows``, for books."""
        gen, link = origin
        ctx = self._ctx
        if gen != self._gen or ctx is None or link != ctx._fq_link:
            return
        cb = self.on_book_rows
        if cb is not None:
            cb(rows)

    async def _call(self, fn: Callable[[], Any]) -> Any:
        """Run a blocking SDK call serialized under one lock."""
        if self._ctx is None:
            raise OpenDDownError("not connected")
        async with self._lock:
            try:
                return await asyncio.to_thread(fn)
            except OpenDError:
                raise
            except Exception as exc:
                raise _classify_call_error(str(exc)) from exc

    async def get_market_snapshot(self, codes: list[str]) -> list[dict]:
        def _run() -> list[dict]:
            ret, data = self._ctx.get_market_snapshot(list(codes))
            if ret != RET_OK:
                raise _classify_call_error(str(data))
            if data is None or getattr(data, "empty", True):
                return []
            return data.to_dict(orient="records")

        return await self._call(_run)

    async def subscribe(self, codes: list[str]) -> dict[str, Optional[str]]:
        """Per-code QUOTE+ORDER_BOOK subscribe (one atomic SDK call each);
        returns ``{code: None | error_message}``."""
        out: dict[str, Optional[str]] = {}
        for code in codes:

            def _run(code: str = code) -> Optional[str]:
                ret, msg = self._ctx.subscribe([code], _SUBTYPES)
                return None if ret == RET_OK else str(msg)

            try:
                out[code] = await self._call(_run)
            except OpenDError as exc:
                out[code] = str(exc) or exc.__class__.__name__
        return out

    async def unsubscribe(self, codes: list[str]) -> dict[str, Optional[str]]:
        """Per-code unsubscribe of both subtypes together."""
        out: dict[str, Optional[str]] = {}
        for code in codes:

            def _run(code: str = code) -> Optional[str]:
                ret, msg = self._ctx.unsubscribe([code], _SUBTYPES)
                return None if ret == RET_OK else str(msg)

            try:
                out[code] = await self._call(_run)
            except OpenDError as exc:
                out[code] = str(exc) or exc.__class__.__name__
        return out

    async def query_subscription(self) -> dict:
        def _run() -> dict:
            ret, data = self._ctx.query_subscription(is_all_conn=True)
            if ret != RET_OK:
                raise _classify_call_error(str(data))
            return dict(data)

        return await self._call(_run)

    async def get_global_state(self) -> dict:
        def _run() -> dict:
            ret, data = self._ctx.get_global_state()
            if ret != RET_OK:
                raise _classify_call_error(str(data))
            return dict(data)

        return await self._call(_run)
