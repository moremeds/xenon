"""futu-quotes WebSocket service.

One internal WebSocket for the ib_realtime relay (its only client). Frames
per ``docs/architecture/futu-quotes.md``:

- in:  ``{"action": "want", "gen": N, "symbols": [...]}``
- out: ``assign`` / ``batch`` / ``health``

The service owns an ``OpenDLike`` (real ``FutuOpenD`` in prod, fake in
tests), runs admission evaluation, streams coalesced ``batch`` frames
(≤1 per 100 ms, last value wins) and ``health`` every 5 s + on change.

Liveness on the link uses WebSocket protocol ping/pong — the caller
configures ``serve(..., ping_interval=10, ping_timeout=20)``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any, Callable, Optional

from xenon.futu_quotes.admission import (
    Admission,
    ticker_of,
)
from xenon.futu_quotes.opend import (
    OpenDDownError,
    OpenDError,
    OpenDLike,
    OpenDNotFoundError,
    classify_subscribe_error,
    classify_unsubscribe_error,
)
from xenon.futu_quotes.quote_map import (
    HighWaterMark,
    row_to_pricedata,
)

logger = logging.getLogger("xenon.futu_quotes")

_NOT_STOCK_TYPES = frozenset({"IDX", "INDEX", "BOND", "WARRANT", "DRVT", "FUTURE"})


class Batcher:
    """Last-value coalescer: at most one flush per ``interval_s``.

    Timestamps ride along with each PriceData so a stale tick never
    overwrites a newer buffered one, and the high-water mark advances only
    for updates actually flushed.
    """

    def __init__(self, interval_s: float = 0.1, now: Callable[[], float] = time.monotonic) -> None:
        self.interval_s = interval_s
        self.now = now
        self.pending: dict[str, tuple[dict, datetime]] = {}
        self.window_start: Optional[float] = None

    def enqueue(self, pd: dict, ts: datetime) -> None:
        sym = pd["symbol"]
        cur = self.pending.get(sym)
        if cur is not None and ts < cur[1]:
            return
        self.pending[sym] = (pd, ts)
        if self.window_start is None:
            self.window_start = self.now()

    def due_at(self) -> Optional[float]:
        if self.window_start is None:
            return None
        return self.window_start + self.interval_s

    def pop_ready(self, now: float, fence: Callable[[str], bool]) -> Optional[dict]:
        """Consume the window when due; returns ``{updates, marks}`` or None.

        ``fence(sym)`` is the producer fence: buffered ticks for symbols no
        longer in ``ok`` are dropped here.
        """
        due = self.due_at()
        if not self.pending or due is None or now < due:
            return None
        updates = {s: pd for s, (pd, ts) in self.pending.items() if fence(s)}
        marks = {s: ts for s, (pd, ts) in self.pending.items() if s in updates}
        self.pending.clear()
        self.window_start = None
        if not updates:
            return None
        return {"updates": updates, "marks": marks}


class FutuQuotesService:
    """Core service; WS/socket plumbing is injected for tests."""

    def __init__(
        self,
        opend: OpenDLike,
        *,
        quota_cap: int = 800,
        clock: Callable[[], float] = time.monotonic,
        batch_window_s: float = 0.1,
    ) -> None:
        self.opend = opend
        opend.on_quote_rows = self._on_quote_rows
        opend.on_disconnected = self._on_opend_down
        opend.on_reconnected = self._on_opend_up

        self.clock = clock
        self.admission = Admission(quota_cap=quota_cap, now=clock)
        self.batcher = Batcher(batch_window_s, clock)
        self.highwater = HighWaterMark()

        self.conn: Any = None
        self._tasks: set[asyncio.Task] = set()
        self._eval_task: Optional[asyncio.Task] = None
        self._eval_again = False
        self._eval_emit_always = False
        self._flush_handle: Optional[asyncio.Handle] = None
        self._last_health: Optional[dict] = None
        self._opend_down = asyncio.Event()
        self._opend_down.set()

    # ------------------------------------------------------------------
    # ws plumbing (fake-socket friendly)
    # ------------------------------------------------------------------

    async def on_ws_connect(self, ws: Any) -> None:
        """New relay connection replaces the old one (epoch boundary)."""
        old = self.conn
        self.conn = ws
        self.admission.gen = 0
        self.admission.release_desired()
        if old is not None and old is not ws:
            try:
                await old.close()
            except Exception:
                pass
        logger.info("relay connected")

    async def on_ws_message(self, ws: Any, raw: Any) -> None:
        if ws is not self.conn:
            return  # frame from a replaced connection
        try:
            msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except Exception:
            logger.warning("relay frame not JSON: %r", raw[:200] if hasattr(raw, "__getitem__") else raw)
            return
        if not isinstance(msg, dict) or msg.get("action") != "want":
            return  # pong and anything else: liveness only
        try:
            gen = int(msg["gen"])
        except (KeyError, TypeError, ValueError):
            logger.warning("want without integer gen: %r", msg)
            return
        if gen < self.admission.gen:
            logger.debug("stale want gen=%s (last=%s) ignored", gen, self.admission.gen)
            return
        symbols = msg.get("symbols")
        if not isinstance(symbols, list):
            symbols = []
        self.admission.set_desired(gen, [s for s in symbols if isinstance(s, str)])
        self._kick(emit_always=True)

    async def on_ws_close(self, ws: Any) -> None:
        if ws is not self.conn:
            return
        self.conn = None
        self.admission.gen = 0
        self.admission.release_desired()
        self._kick()
        logger.info("relay disconnected; desired set released to grace")

    async def ws_entry(self, ws: Any) -> None:
        """websockets.asyncio.server handler."""
        await self.on_ws_connect(ws)
        try:
            async for raw in ws:
                await self.on_ws_message(ws, raw)
        finally:
            await self.on_ws_close(ws)

    async def _send(self, frame: dict) -> None:
        conn = self.conn
        if conn is None:
            return
        try:
            await conn.send(json.dumps(frame))
        except Exception as exc:
            logger.warning("relay send failed: %s", exc)

    # ------------------------------------------------------------------
    # opend lifecycle callbacks
    # ------------------------------------------------------------------

    def _on_opend_down(self) -> None:
        """SDK disconnect callback (loop thread)."""
        logger.warning("OpenD connection lost")
        self.admission.mark_disconnected()
        self._opend_down.set()
        self._kick()

    def _on_opend_up(self) -> None:
        """SDK socket-reconnected callback → treat as reconnect."""
        try:
            asyncio.get_running_loop().create_task(self.opend_recovered())
        except RuntimeError:
            pass

    async def opend_recovered(self) -> None:
        """After a (re)connect: reconcile quota, session, resubscribe.

        The down-event is cleared before marking connected so a
        disconnect that lands mid-recovery is not lost.
        """
        self._opend_down.clear()
        self.admission.mark_connected()
        await self.refresh_quota_once()
        await self.poll_session_once()
        self._kick()

    async def wait_opend_down(self) -> None:
        await self._opend_down.wait()

    # ------------------------------------------------------------------
    # evaluation pump
    # ------------------------------------------------------------------

    def _kick(self, emit_always: bool = False) -> None:
        self._eval_emit_always = self._eval_emit_always or emit_always
        if self._eval_task is not None and not self._eval_task.done():
            self._eval_again = True
            return
        try:
            self._eval_task = asyncio.create_task(self._eval_loop())
            self._tasks.add(self._eval_task)
            self._eval_task.add_done_callback(self._tasks.discard)
        except RuntimeError:  # no running loop (pure-unit tests)
            self._eval_task = None

    async def _eval_loop(self) -> None:
        try:
            for _ in range(200):
                res = self.admission.evaluate(emit_always=self._eval_emit_always)
                self._eval_emit_always = False
                if res.changed and self.conn is not None and self.admission.gen > 0:
                    await self._send(self.admission.assign_frame(res.partition))
                if res.actions.verify:
                    self._spawn(self._do_verify(res.actions.verify))
                for sym in res.actions.subscribe:
                    self._spawn(self._do_subscribe(sym))
                for sym in res.actions.unsubscribe:
                    self._spawn(self._do_unsubscribe(sym))
                await self._emit_health()
                if not res.changed and not res.actions.any() and not self._eval_again:
                    break
                self._eval_again = False
        finally:
            self._eval_task = None

    def _spawn(self, coro: Any) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    # ------------------------------------------------------------------
    # provider actions
    # ------------------------------------------------------------------

    async def _do_verify(self, symbols: list[str]) -> None:
        codes = [self.admission.state(s).code for s in symbols]
        try:
            rows = await self.opend.get_market_snapshot(codes)
        except OpenDDownError:
            self._on_opend_down()
            for s in symbols:
                self.admission.verify_result(s, "transient")
            return
        except OpenDNotFoundError:
            for s in symbols:
                self.admission.verify_result(s, "unknown_symbol")
            self._kick()
            return
        except OpenDError:
            for s in symbols:
                self.admission.verify_result(s, "transient")
            self._kick()
            return
        found = {str(r.get("code")): r for r in rows if isinstance(r, dict)}
        for s in symbols:
            row = found.get(self.admission.state(s).code)
            if row is None:
                self.admission.verify_result(s, "unknown_symbol")
            else:
                self.admission.verify_result(s, _classify_snapshot_row(row))
        self._kick()

    async def _do_subscribe(self, symbol: str) -> None:
        code = self.admission.state(symbol).code
        try:
            res = await self.opend.subscribe([code])
        except OpenDDownError:
            self._on_opend_down()
            return
        except OpenDError as exc:
            self.admission.subscribe_result(symbol, str(exc), classify_subscribe_error(str(exc)))
            self._kick()
            return
        err = res.get(code)
        if err is None:
            self.admission.subscribe_result(symbol, None)
        else:
            self.admission.subscribe_result(symbol, err, classify_subscribe_error(err))
        if self.admission.refresh_quota_requested:
            # Provider rejection is authoritative — re-pull quota numbers.
            self.admission.refresh_quota_requested = False
            self._spawn(self.refresh_quota_once())
        self._kick()

    async def _do_unsubscribe(self, symbol: str) -> None:
        code = self.admission.state(symbol).code
        try:
            res = await self.opend.unsubscribe([code])
        except OpenDDownError:
            self._on_opend_down()
            return
        except OpenDError as exc:
            self.admission.unsubscribe_result(symbol, str(exc), "provider_error")
            self._kick()
            return
        err = res.get(code)
        kind = None if err is None else classify_unsubscribe_error(err)
        self.admission.unsubscribe_result(symbol, err, kind)
        if not self.admission.state(symbol).subscribed:
            self.highwater.reset(symbol)
        self._kick()

    # ------------------------------------------------------------------
    # periodic jobs
    # ------------------------------------------------------------------

    async def poll_session_once(self) -> None:
        """get_global_state().market_us ∈ {MORNING, AFTERNOON} ⇒ open."""
        try:
            gs = await self.opend.get_global_state()
            open_ = Admission.session_is_open(gs.get("market_us"))
        except OpenDDownError:
            self._on_opend_down()
            return
        except OpenDError:
            open_ = False
        self.admission.set_session_open(open_)
        self._kick()

    async def refresh_quota_once(self) -> None:
        try:
            data = await self.opend.query_subscription()
        except OpenDDownError:
            self._on_opend_down()
            return
        except OpenDError:
            return
        self.admission.apply_quota_snapshot(data)
        self.admission.refresh_quota_requested = False
        await self._emit_health()
        self._kick()

    async def tick_unsubscribes_once(self) -> None:
        self._kick()

    async def emit_health(self, force: bool = False) -> None:
        await self._emit_health(force=force)

    async def _emit_health(self, force: bool = False) -> None:
        frame = self.health_frame()
        if force or frame != self._last_health:
            self._last_health = frame
            await self._send(frame)

    def health_frame(self) -> dict:
        return {
            "type": "health",
            "opend_connected": self.admission.opend_connected,
            "quota": self.admission.quota_report(),
        }

    # ------------------------------------------------------------------
    # tick intake → coalesced batch
    # ------------------------------------------------------------------

    def _on_quote_rows(self, rows: list[dict]) -> None:
        """QUOTE push rows (already on the loop thread)."""
        for row in rows:
            sym = ticker_of(row.get("code"))
            if sym is None:
                continue
            out = row_to_pricedata(sym, row)
            if out is None:
                continue
            pd, ts = out
            if self.highwater.seen(sym, ts):
                continue
            if not self.admission.is_ok(sym):
                continue
            self.batcher.enqueue(pd, ts)
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if not self.batcher.pending or self._flush_handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        delay = max(0.0, (self.batcher.due_at() or 0.0) - self.batcher.now())
        self._flush_handle = loop.call_later(
            delay, lambda: self._spawn(self.flush_now())
        )

    async def flush_now(self) -> None:
        """Flush the coalesce window if due (callable directly in tests)."""
        self._flush_handle = None
        out = self.batcher.pop_ready(self.batcher.now(), self.admission.is_ok)
        if out is None:
            return
        updates = out["updates"]
        for sym, ts in out["marks"].items():
            self.highwater.accept(sym, ts)
        await self._send({"type": "batch", "updates": updates})
        if self.batcher.pending:
            self._schedule_flush()

    # ------------------------------------------------------------------
    # test seam
    # ------------------------------------------------------------------

    async def drain(self, rounds: int = 50) -> None:
        """Await pending tasks until quiescent. Test helper."""
        for _ in range(rounds):
            tasks = [t for t in self._tasks if not t.done()]
            if not tasks:
                break
            await asyncio.gather(*tasks, return_exceptions=True)


def _classify_snapshot_row(row: dict) -> str:
    for key in ("sec_type", "stock_type", "index_option_type"):
        val = row.get(key)
        if val is not None and str(val).upper() in _NOT_STOCK_TYPES:
            return "not_supported"
    return "ok"


# ----------------------------------------------------------------------
# real server
# ----------------------------------------------------------------------

RECONNECT_MIN_S = 1.0
RECONNECT_MAX_S = 30.0


async def _every(interval_s: float, fn: Callable[[], Any], name: str) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            await fn()
        except Exception:
            logger.exception("%s tick failed", name)


async def _opend_supervisor(svc: FutuQuotesService, opend: OpenDLike) -> None:
    backoff = RECONNECT_MIN_S
    while True:
        try:
            await opend.connect()
        except Exception as exc:
            logger.warning("OpenD connect failed (%s); retry in %.0fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_S)
            continue
        backoff = RECONNECT_MIN_S
        try:
            await svc.opend_recovered()
        except Exception:
            logger.exception("post-connect reconcile failed")
        await svc.wait_opend_down()
        try:
            await opend.close()
        except Exception:
            pass


async def run_forever(
    host: str,
    port: int,
    opend: OpenDLike,
    *,
    quota_cap: int = 800,
    clock: Callable[[], float] = time.monotonic,
    ping_interval_s: float = 10.0,
    ping_timeout_s: float = 20.0,
    session_poll_s: float = 30.0,
    quota_poll_s: float = 300.0,
    health_s: float = 5.0,
    unsub_tick_s: float = 1.0,
) -> None:
    """Serve the internal relay WebSocket until cancelled."""
    try:
        from websockets.asyncio.server import serve
    except ImportError:  # pragma: no cover
        from websockets import serve  # type: ignore

    svc = FutuQuotesService(opend, quota_cap=quota_cap, clock=clock)

    supervisor = asyncio.create_task(_opend_supervisor(svc, opend))
    timers = [
        asyncio.create_task(_every(session_poll_s, svc.poll_session_once, "session")),
        asyncio.create_task(_every(quota_poll_s, svc.refresh_quota_once, "quota")),
        asyncio.create_task(_every(health_s, lambda: svc.emit_health(force=True), "health")),
        asyncio.create_task(_every(unsub_tick_s, svc.tick_unsubscribes_once, "unsub")),
    ]
    try:
        async with serve(
            svc.ws_entry,
            host,
            port,
            ping_interval=ping_interval_s,
            ping_timeout=ping_timeout_s,
        ):
            logger.info(
                "futu-quotes listening on %s:%s (quota cap %s)", host, port, quota_cap
            )
            await asyncio.Event().wait()
    finally:
        supervisor.cancel()
        for t in timers:
            t.cancel()
