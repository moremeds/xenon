"""futu-quotes WebSocket service.

One internal WebSocket for the ib_realtime relay (its only client). Frames
per ``docs/architecture/futu-quotes.md``:

- in:  ``{"action": "want", "gen": N, "symbols": [...],
         "tiers": {"<sym>": "held"|"view"}}`` (``tiers`` optional;
         a symbol missing from it = ``held``)
- in:  ``{"action": "config", "values": {"FUTU_QUOTE_QUOTA": n|null, ...}}``
       runtime-settings overrides forwarded by the relay; null, missing or
       invalid = this process's env value (``apply_config``)
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
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from xenon.futu_quotes.admission import (
    EXTENDED_SESSIONS,
    Admission,
    option_underlying,
    ticker_of,
    us_session,
)
from xenon.futu_quotes.opend import (
    OpenDDownError,
    OpenDError,
    OpenDLike,
    OpenDNotFoundError,
    OpenDTransientError,
    classify_subscribe_error,
    classify_unsubscribe_error,
)
from xenon.futu_quotes.quote_map import (
    ExtendedBaseline,
    HighWaterMark,
    extended_row_to_pricedata,
    option_quote_fields,
    order_book_top,
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

    def drop(self, symbol: str) -> None:
        """Discard a buffered value the moment its symbol leaves ``ok`` —
        a flush-time fence alone cannot catch remove→re-add in one window."""
        self.pending.pop(symbol, None)

    def clear(self) -> None:
        """Drop every buffered value (owner/session boundary)."""
        self.pending.clear()
        self.window_start = None


@dataclass
class _Op:
    """One provider operation, captured at enqueue.

    ``epoch`` is the OpenD session it belongs to; ``items`` are the
    ``(symbol, op_id)`` reservations it acts on. The worker re-checks both
    right before the SDK call and right after it returns; on mismatch the
    result is discarded and no state is touched.
    """

    kind: str  # verify | subscribe | unsubscribe | session | quota
    epoch: int
    items: tuple[tuple[str, int], ...] = ()
    done: Optional[asyncio.Future] = None


class FutuQuotesService:
    """Core service; WS/socket plumbing is injected for tests.

    Every provider operation (verify, subscribe, unsubscribe, session and
    quota polls) runs on ONE worker, one at a time, in enqueue order.
    """

    def __init__(
        self,
        opend: OpenDLike,
        *,
        quota_cap: int = 800,
        clock: Callable[[], float] = time.monotonic,
        batch_window_s: float = 0.1,
        wall: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        book_stale_s: float = 0.0,
        units_per_symbol: int = 2,
        greek_factors: Optional[dict] = None,
    ) -> None:
        self.opend = opend
        opend.on_quote_rows = self._on_quote_rows
        opend.on_book_rows = self._on_book_rows
        opend.on_disconnected = self._on_opend_down
        opend.on_reconnected = self._on_opend_up

        self.clock = clock
        self.admission = Admission(
            quota_cap=quota_cap, now=clock, units_per_symbol=units_per_symbol
        )
        self.batcher = Batcher(batch_window_s, clock)
        self.highwater = HighWaterMark()
        # Wall clock (aware UTC): session rule + extended-session receipt stamps.
        self.wall = wall
        # ORDER_BOOK age cutoff (Amendment A1): 0 = off — a held pair keeps
        # its book top until a push, disconnect or unsubscribe replaces it;
        # a positive value re-enables the receipt-age null.
        self.book_stale_s = book_stale_s
        # Per-symbol last ORDER_BOOK top {fields, recv} and last emitted
        # RTH quote (pd, quote_ts) — the template book pushes rebuild from.
        self._book: dict[str, dict] = {}
        self._last_quote: dict[str, tuple[dict, datetime]] = {}
        # Parsed FUTU_GREEK_FACTORS; None = greeks/IV stay NULL (contract).
        self.greek_factors = greek_factors
        # Env/start values the runtime-settings config frame falls back to.
        self._env_config = {
            "FUTU_QUOTE_QUOTA": quota_cap,
            "FUTU_UNITS_PER_SYMBOL": units_per_symbol,
            "FUTU_BOOK_STALE_S": book_stale_s,
            "FUTU_GREEK_FACTORS": greek_factors,
        }
        # US session from the last poll: "rth" | "pre" | "after" | "overnight" | None (closed).
        self.session: Optional[str] = None
        self.baseline = ExtendedBaseline()

        self.conn: Any = None
        self._tasks: set[asyncio.Task] = set()
        self._eval_task: Optional[asyncio.Task] = None
        self._eval_again = False
        self._eval_emit_always = False
        self._ops: deque[_Op] = deque()
        self._worker: Optional[asyncio.Task] = None
        self._queued_polls: dict[str, _Op] = {}
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
        # The new owner must not inherit a prior owner's buffered quotes
        # or replay judgement.
        self.batcher.clear()
        self.highwater.reset_all()
        self.baseline.reset_all()
        self._book.clear()
        self._last_quote.clear()
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
        if isinstance(msg, dict) and msg.get("action") == "config":
            self.apply_config(msg.get("values"))
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
        tiers = msg.get("tiers")
        if not isinstance(tiers, dict):
            tiers = None
        prev = set(self.admission.desired)
        self.admission.set_desired(
            gen, [s for s in symbols if isinstance(s, str)], tiers
        )
        # A removed symbol loses its buffered quote AT the transition — a
        # later re-add before any evaluation must not revive it.
        for s in prev.difference(self.admission.desired):
            self.batcher.drop(s)
        self._kick(emit_always=True)

    def effective_config(self) -> dict:
        return {
            "FUTU_QUOTE_QUOTA": self.admission.quota_cap,
            "FUTU_UNITS_PER_SYMBOL": self.admission.units_per_symbol,
            "FUTU_BOOK_STALE_S": self.book_stale_s,
            "FUTU_GREEK_FACTORS": self.greek_factors,
        }

    def apply_config(self, values: Any) -> None:
        """Apply a relay ``config`` frame (runtime settings, D6): each key's
        value when valid per the registry (`xenon.runtime_settings`), else
        the env value. Quota and units take effect on the next admission
        evaluation (kicked here), book staleness and greek factors on the
        next emit."""
        from xenon import runtime_settings

        if not isinstance(values, dict):
            logger.warning("config frame without a values object ignored")
            return
        nxt = {}
        for key, env_value in self._env_config.items():
            raw = values.get(key)
            value = env_value
            if raw is not None:
                try:
                    value = runtime_settings.validate(key, raw)[1]
                except runtime_settings.SettingError as exc:
                    logger.warning("config %s ignored: %s", key, exc.detail)
            nxt[key] = value
        if nxt == self.effective_config():
            return
        self.admission.quota_cap = nxt["FUTU_QUOTE_QUOTA"]
        self.admission.units_per_symbol = nxt["FUTU_UNITS_PER_SYMBOL"]
        self.book_stale_s = nxt["FUTU_BOOK_STALE_S"]
        self.greek_factors = nxt["FUTU_GREEK_FACTORS"]
        logger.info("runtime config applied: %s", nxt)
        self._kick(emit_always=True)

    async def on_ws_close(self, ws: Any) -> None:
        if ws is not self.conn:
            return
        self.conn = None
        self.admission.gen = 0
        self.admission.release_desired()
        self.batcher.clear()
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
        """OpenD session lost (SDK callback or a current-session op)."""
        logger.warning("OpenD connection lost")
        self.admission.mark_disconnected()
        self.batcher.clear()  # dead-session ticks must not carry over
        self.session = None  # mark_disconnected also drops extended readiness
        self.baseline.reset_all()  # extended baseline is per OpenD session
        self._book.clear()  # book receipt belongs to the dead session
        self._last_quote.clear()
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
        self.admission.mark_connected()  # new epoch: nothing is ready yet
        self.batcher.clear()
        self.baseline.reset_all()
        self._book.clear()
        self._last_quote.clear()
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
                ad = self.admission
                res = ad.evaluate(emit_always=self._eval_emit_always)
                self._eval_emit_always = False
                # Enqueue BEFORE any await: the ops carry the epoch and
                # reservations the evaluation was made under.
                epoch = ad.epoch
                if res.actions.verify:
                    self._enqueue(
                        _Op("verify", epoch, tuple((s, ad.state(s).op_id) for s in res.actions.verify))
                    )
                for sym in res.actions.subscribe:
                    self._enqueue(_Op("subscribe", epoch, ((sym, ad.state(sym).op_id),)))
                for sym in res.actions.unsubscribe:
                    self._enqueue(_Op("unsubscribe", epoch, ((sym, ad.state(sym).op_id),)))
                if res.changed and self.conn is not None and ad.gen > 0:
                    await self._send(ad.assign_frame(res.partition))
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
    # the provider-op worker
    # ------------------------------------------------------------------

    def _enqueue(self, op: _Op) -> None:
        self._ops.append(op)
        if self._worker is None or self._worker.done():
            self._worker = self._spawn(self._op_worker())

    async def _op_worker(self) -> None:
        while self._ops:
            op = self._ops.popleft()
            if self._queued_polls.get(op.kind) is op:
                del self._queued_polls[op.kind]
            try:
                if op.epoch == self.admission.epoch:  # else: stale, nothing sent
                    await _OP_HANDLERS[op.kind](self, op)
            except Exception:
                logger.exception("futu-quotes %s op failed", op.kind)
                # Never leave a symbol wedged behind a dead reservation.
                for sym, oid in op.items:
                    self.admission.release_op(sym, oid)
                self._kick()
            finally:
                if op.done is not None and not op.done.done():
                    op.done.set_result(None)

    def _stale(self, op: _Op) -> bool:
        return op.epoch != self.admission.epoch

    def _down_from(self, op: _Op) -> None:
        """OpenDDownError from an op: only a current-session op may declare
        the current session dead."""
        if not self._stale(op):
            self._on_opend_down()

    async def _op_verify(self, op: _Op) -> None:
        ad = self.admission
        todo: list[tuple[str, int]] = []
        for sym, oid in op.items:
            if not ad.op_current(sym, oid, op.epoch):
                continue
            if ad.verify_needed(sym):
                todo.append((sym, oid))
            else:
                ad.release_op(sym, oid)
        if not todo:
            self._kick()
            return
        codes = [ad.state(s).code for s, _ in todo]
        try:
            rows = await self.opend.get_market_snapshot(codes)
        except OpenDDownError:
            self._down_from(op)
            return
        except OpenDError as exc:
            if self._stale(op):
                return
            if len(todo) > 1 and not isinstance(exc, OpenDTransientError):
                # Whole-call failure with no per-code attribution: verify
                # each code alone (queued, so other ops interleave).
                for item in todo:
                    self._enqueue(_Op("verify", op.epoch, (item,)))
                return
            # Only a definite not-found is permanent; anything else retries.
            outcome = "unknown_symbol" if isinstance(exc, OpenDNotFoundError) else "transient"
            for sym, oid in todo:
                if ad.op_current(sym, oid, op.epoch):
                    ad.verify_result(sym, outcome)
            self._kick()
            return
        if self._stale(op):
            return  # stale-session result
        found = {str(r.get("code")): r for r in rows if isinstance(r, dict)}
        for sym, oid in todo:
            if not ad.op_current(sym, oid, op.epoch):
                continue
            row = found.get(ad.state(sym).code)
            ad.verify_result(sym, "unknown_symbol" if row is None else _classify_snapshot_row(row))
        self._kick()

    async def _op_subscribe(self, op: _Op) -> None:
        ad = self.admission
        ((sym, oid),) = op.items
        if not ad.op_current(sym, oid, op.epoch):
            return
        if not ad.subscribe_still_valid(sym):
            ad.release_op(sym, oid)  # declined: nothing was sent
            self._kick()
            return
        code = ad.state(sym).code
        ad.subscribe_issued(sym)  # uncertain from here on ⇒ counted as held
        try:
            res = await self.opend.subscribe([code])
        except OpenDDownError:
            self._down_from(op)
            return
        except OpenDError as exc:
            if not ad.op_current(sym, oid, op.epoch):
                return
            reason = "unknown_symbol" if isinstance(exc, OpenDNotFoundError) else classify_subscribe_error(str(exc))
            ad.subscribe_result(sym, str(exc), reason)
        else:
            if not ad.op_current(sym, oid, op.epoch):
                return  # stale-session completion — never certifies the new one
            err = res.get(code)
            ad.subscribe_result(sym, err, None if err is None else classify_subscribe_error(err))
        if ad.refresh_quota_requested:
            # Provider rejection is authoritative — re-pull quota numbers.
            ad.refresh_quota_requested = False
            self._spawn(self.refresh_quota_once())
        self._kick()

    async def _op_unsubscribe(self, op: _Op) -> None:
        ad = self.admission
        ((sym, oid),) = op.items
        if not ad.op_current(sym, oid, op.epoch):
            return
        if not ad.unsubscribe_still_due(sym):
            # Re-added, renewed grace, or not held any more: nothing sent.
            ad.release_op(sym, oid)
            self._kick()
            return
        code = ad.state(sym).code
        try:
            res = await self.opend.unsubscribe([code])
        except OpenDDownError:
            self._down_from(op)
            return
        except OpenDError as exc:
            if not ad.op_current(sym, oid, op.epoch):
                return
            # Code unknown to OpenD ⇒ nothing is subscribed there.
            kind = "not_subscribed" if isinstance(exc, OpenDNotFoundError) else "provider_error"
            ad.unsubscribe_result(sym, str(exc), kind)
        else:
            if not ad.op_current(sym, oid, op.epoch):
                return  # stale-session completion
            err = res.get(code)
            ad.unsubscribe_result(sym, err, None if err is None else classify_unsubscribe_error(err))
        if not ad.state(sym).subscribed:
            # Released: its buffered quote belongs to the old subscription —
            # drop it now, before an evaluation can resubscribe the symbol.
            self.highwater.reset(sym)
            self.baseline.reset(sym)
            self.batcher.drop(sym)
            self._book.pop(sym, None)
            self._last_quote.pop(sym, None)
        self._kick()

    async def _op_session(self, op: _Op) -> None:
        try:
            gs = await self.opend.get_global_state()
            session = us_session(gs.get("market_us"), self.wall())
        except OpenDDownError:
            self._down_from(op)
            return
        except OpenDError:
            session = None
        if self._stale(op):
            return  # stale-session poll result
        if session is None or session != self.session:
            # Closed (nothing is ok any more) or a session change: buffered
            # rows were mapped for the old session.
            self.batcher.clear()
        if session != self.session:
            self.baseline.reset_all()  # first row of a new session is a baseline
            # A1: the stored book top and the RTH quote template are
            # session-bound — a quiet book must not ride into the next
            # session (book age rule off by default means nothing else
            # would ever expire them).
            self._book.clear()
            self._last_quote.clear()
        self.session = session
        self.admission.set_session_open(session is not None)
        self.admission.extended_trade_seen = (
            self.baseline.seen if session in EXTENDED_SESSIONS else None
        )
        self._kick()

    async def _op_quota(self, op: _Op) -> None:
        try:
            data = await self.opend.query_subscription()
        except OpenDDownError:
            self._down_from(op)
            return
        except OpenDError:
            return
        if self._stale(op):
            return  # stale-session poll result
        self.admission.apply_quota_snapshot(data)
        self.admission.refresh_quota_requested = False
        await self._emit_health()
        self._kick()

    # ------------------------------------------------------------------
    # periodic jobs (enqueued on the worker; a queued poll is coalesced)
    # ------------------------------------------------------------------

    async def _poll(self, kind: str) -> None:
        op = self._queued_polls.get(kind)
        if op is None or op.epoch != self.admission.epoch:
            op = _Op(kind, self.admission.epoch, done=asyncio.get_running_loop().create_future())
            self._queued_polls[kind] = op
            self._enqueue(op)
        assert op.done is not None
        await asyncio.shield(op.done)

    async def poll_session_once(self) -> None:
        """``us_session(market_us, wall)`` is rth or an extended session ⇒ open."""
        await self._poll("session")

    async def refresh_quota_once(self) -> None:
        await self._poll("quota")

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
        received_at = self.wall() if self.session in EXTENDED_SESSIONS else None
        for row in rows:
            sym = ticker_of(row.get("code"))
            if sym is None:
                continue
            if received_at is not None:
                if "_" in sym:
                    continue  # option relay keys are RTH only
                out = extended_row_to_pricedata(sym, row, received_at, self.session)
                if out is None:
                    continue  # no trade in this session: not ready, not published
                if not self.baseline.seen(sym):
                    self._kick()  # first trade row: symbol may become ok
                # Receipt-time stamps: publish only a changed session
                # volume/price; the first row after (re)subscribe or a reset
                # is the baseline (see ExtendedBaseline ponytail ceiling).
                if not self.baseline.changed(sym, row, self.session):
                    continue
            else:
                out = row_to_pricedata(sym, row)
            if out is None:
                continue
            pd, ts = out
            if self.highwater.seen(sym, ts):
                continue
            if not self.admission.is_ok(sym):
                continue
            if received_at is None:
                # RTH row: remember it as the template book pushes rebuild
                # from — unless an older row arrived inside this coalesce
                # window, which must not walk the template backwards —
                # then overlay the fresh book top if we have one.
                tpl = self._last_quote.get(sym)
                if tpl is None or ts >= tpl[1]:
                    self._last_quote[sym] = (pd, ts)
                self._apply_book(pd, ts)
                self._apply_option(pd, sym, row)
            self.batcher.enqueue(pd, ts)  # ordering/mark stays on QUOTE ts
        self._schedule_flush()

    def _apply_book(self, pd: dict, quote_ts: datetime) -> None:
        """Overlay the ORDER_BOOK top onto an RTH row, in place.

        The four book fields stay NULL when the session is not RTH or —
        only when ``book_stale_s`` > 0 (A1: default 0 = age rule off) — the
        book receipt is older than the cutoff. A fresh book also lifts
        ``timestamp`` to ``max(quote_ts, book receipt)``.
        """
        book = self._book.get(pd["symbol"])
        if book is None or self.session != "rth":
            return
        if (
            self.book_stale_s > 0
            and (self.wall() - book["recv"]).total_seconds() > self.book_stale_s
        ):
            return
        pd.update(book["fields"])
        merged = max(quote_ts, book["recv"])
        pd["timestamp"] = merged.strftime("%Y-%m-%dT%H:%M:%S") + "Z"

    def _apply_option(self, pd: dict, sym: str, row: Optional[dict] = None) -> None:
        """Option relay keys: factored greeks/IV + underlying last, in place.

        Greeks/IV stamp only from a QUOTE ``row`` and only when
        ``greek_factors`` is set (else they stay NULL). ``undPrice`` is
        the latest Futu last of the underlying while the service holds it,
        else NULL — refreshed at emit time so book-driven rebuilds see the
        newest underlying.
        """
        if row is not None and self.greek_factors is not None:
            pd.update(option_quote_fields(row, self.greek_factors))
        und = option_underlying(sym)
        if und is not None:
            base = self._last_quote.get(und)
            pd["undPrice"] = base[0]["last"] if base is not None else None

    def _on_book_rows(self, rows: list[dict]) -> None:
        """ORDER_BOOK push rows (already on the loop thread).

        US books carry no source timestamp, so freshness is the local
        receipt time. A push re-emits the last RTH quote with the new top;
        the coalescer ordering and the high-water mark stay on the QUOTE
        source ts — book receipt time must never advance them.
        """
        recv = self.wall()
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = ticker_of(row.get("code"))
            if sym is None:
                continue
            if self.session != "rth":
                continue  # session-bound state: nothing is stored outside RTH
            fields = order_book_top(row)
            self._book[sym] = {"recv": recv, "fields": fields}
            base = self._last_quote.get(sym)
            if base is None or not self.admission.is_ok(sym):
                continue
            pd, quote_ts = base
            if self.highwater.seen(sym, quote_ts):
                # The template slipped behind the flushed mark (e.g. the
                # mark advanced on an extended-session receipt): re-emitting it
                # would publish an older `last` stamped fresh. Skip the
                # rebuild; the stored top rides the next real quote.
                continue
            out = dict(pd)
            out.update(fields)
            merged = max(quote_ts, recv)
            out["timestamp"] = merged.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
            self._apply_option(out, sym)  # refresh undPrice for option keys
            self.batcher.enqueue(out, quote_ts)
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if not self.batcher.pending or self._flush_handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        delay = max(0.0, (self.batcher.due_at() or 0.0) - self.batcher.now())
        self._flush_handle = loop.call_later(delay, lambda: self._spawn(self.flush_now()))

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

    async def drain(self, rounds: int = 50, timeout: float = 5.0) -> None:
        """Await pending tasks until quiescent. Test helper.

        ``asyncio.wait`` (not gather): a parked deferred provider call must
        not hang the test — and unlike wait_for+gather it never cancels the
        awaited task.
        """
        for _ in range(rounds):
            tasks = [t for t in self._tasks if not t.done()]
            if not tasks:
                break
            done, pending = await asyncio.wait(tasks, timeout=timeout)
            for t in done:
                _ = t.exception() if not t.cancelled() else None
            if not done:
                break  # everything left is parked (e.g. a deferred provider call)


_OP_HANDLERS: dict[str, Callable[[FutuQuotesService, _Op], Any]] = {
    "verify": FutuQuotesService._op_verify,
    "subscribe": FutuQuotesService._op_subscribe,
    "unsubscribe": FutuQuotesService._op_unsubscribe,
    "session": FutuQuotesService._op_session,
    "quota": FutuQuotesService._op_quota,
}


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


def healthz(connection: Any, request: Any) -> Any:
    """``process_request`` hook: plain HTTP ``GET /healthz`` gets 200, no upgrade.

    A WS probe would replace the relay connection (see ``on_ws_connect``); a bare
    TCP probe makes websockets log a handshake traceback per check.
    """
    if request.path == "/healthz":
        return connection.respond(200, "ok\n")
    return None


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
    book_stale_s: float = 0.0,
    units_per_symbol: int = 2,
    greek_factors: Optional[dict] = None,
) -> None:
    """Serve the internal relay WebSocket until cancelled."""
    try:
        from websockets.asyncio.server import serve
    except ImportError:  # pragma: no cover
        from websockets import serve  # type: ignore

    svc = FutuQuotesService(
        opend,
        quota_cap=quota_cap,
        clock=clock,
        book_stale_s=book_stale_s,
        units_per_symbol=units_per_symbol,
        greek_factors=greek_factors,
    )

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
            process_request=healthz,
            ping_interval=ping_interval_s,
            ping_timeout=ping_timeout_s,
        ):
            logger.info("futu-quotes listening on %s:%s (quota cap %s)", host, port, quota_cap)
            await asyncio.Event().wait()
    finally:
        supervisor.cancel()
        for t in timers:
            t.cancel()
