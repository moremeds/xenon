"""Thin async wrapper around Futu OpenD quote context.

Wraps ``OpenQuoteContext`` + ``StockQuoteHandlerBase`` + ``query_subscription``
+ ``get_global_state``, all behind one serialized asyncio facade. The sync
Futu SDK calls run via ``asyncio.to_thread``; a single lock keeps OpenD round
trips sequential.

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
        RET_OK,
        OpenQuoteContext,
        StockQuoteHandlerBase,
        SubType,
    )
except ImportError:  # pragma: no cover — futu-api missing
    RET_OK = None  # type: ignore[assignment]
    OpenQuoteContext = None  # type: ignore[assignment]
    StockQuoteHandlerBase = object  # type: ignore[assignment,misc]
    SubType = None  # type: ignore[assignment]

logger = logging.getLogger("xenon.futu_quotes.opend")


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
    "inv code",
    "not found",
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
    """Map a subscribe rejection message to an ``assign`` rejected reason."""
    m = (msg or "").lower()
    if any(k in m for k in _QUOTA_HINTS):
        return "quota"
    if any(k in m for k in _UNSUPPORTED_HINTS):
        return "not_supported"
    if any(k in m for k in _NOT_FOUND_HINTS):
        return "unknown_symbol"
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
    """OpenQuoteContext with asyncio-disconnect/reconnect hooks."""

    def __init__(self, *args: Any, loop: asyncio.AbstractEventLoop, **kwargs: Any) -> None:
        # Attributes first: the base __init__ opens the socket, so a very
        # early disconnect could reach on_disconnect before they exist.
        self._fq_loop = loop
        self._fq_on_disconnect: Optional[Callable[[], None]] = None
        self._fq_on_reconnect: Optional[Callable[[], None]] = None
        super().__init__(*args, **kwargs)

    def on_disconnect(self, conn_id: int, reason: Any, msg: str) -> None:  # noqa: D102
        try:
            super().on_disconnect(conn_id, reason, msg)
        except Exception:  # pragma: no cover - SDK internals
            pass
        cb = self._fq_on_disconnect
        if cb is not None:
            self._fq_loop.call_soon_threadsafe(cb)

    def on_api_socket_reconnected(self) -> None:  # noqa: D102
        try:
            super().on_api_socket_reconnected()
        except Exception:  # pragma: no cover - SDK internals
            pass
        cb = self._fq_on_reconnect
        if cb is not None:
            self._fq_loop.call_soon_threadsafe(cb)


class _StockQuoteHandler(StockQuoteHandlerBase):  # type: ignore[misc]
    """Forwards parsed QUOTE push frames to the asyncio loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop, on_rows: Callable[[list[dict]], None]) -> None:
        super().__init__()
        self._loop = loop
        self._on_rows = on_rows

    def on_recv_rsp(self, rsp_pb: Any) -> tuple:
        ret, content = super().on_recv_rsp(rsp_pb)
        if ret == RET_OK and content is not None:
            try:
                rows = content.to_dict(orient="records")
            except Exception:  # pragma: no cover - defensive
                logger.exception("quote push frame not a DataFrame")
                return ret, content
            self._loop.call_soon_threadsafe(self._on_rows, rows)
        return ret, content


class FutuOpenD:
    """Serialized async facade over one ``OpenQuoteContext`` (SubType.QUOTE only)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 11111, probe_timeout: float = 3.0) -> None:
        self.host = host
        self.port = port
        self.probe_timeout = probe_timeout
        self.on_quote_rows: Optional[Callable[[list[dict]], None]] = None
        self.on_disconnected: Optional[Callable[[], None]] = None
        self.on_reconnected: Optional[Callable[[], None]] = None
        self._ctx: Any = None
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

        def _open() -> Any:
            ctx = _QuoteContext(host=self.host, port=self.port, loop=loop)
            ctx._fq_on_disconnect = self._emit_disconnected
            ctx._fq_on_reconnect = self._emit_reconnected
            if self.on_quote_rows is not None:
                ctx.set_handler(_StockQuoteHandler(loop, self.on_quote_rows))
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
        ctx, self._ctx = self._ctx, None
        if ctx is not None:
            try:
                await asyncio.to_thread(ctx.close)
            except Exception:  # pragma: no cover - best effort
                pass

    def _emit_disconnected(self) -> None:
        cb = self.on_disconnected
        if cb is not None:
            cb()

    def _emit_reconnected(self) -> None:
        cb = self.on_reconnected
        if cb is not None:
            cb()

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
        """Per-code subscribe; returns ``{code: None | error_message}``."""
        out: dict[str, Optional[str]] = {}
        for code in codes:
            def _run(code: str = code) -> Optional[str]:
                ret, msg = self._ctx.subscribe([code], [SubType.QUOTE])
                return None if ret == RET_OK else str(msg)

            try:
                out[code] = await self._call(_run)
            except OpenDError as exc:
                out[code] = str(exc) or exc.__class__.__name__
        return out

    async def unsubscribe(self, codes: list[str]) -> dict[str, Optional[str]]:
        out: dict[str, Optional[str]] = {}
        for code in codes:
            def _run(code: str = code) -> Optional[str]:
                ret, msg = self._ctx.unsubscribe([code], [SubType.QUOTE])
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
