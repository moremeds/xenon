"""futu-quotes service tests — unit (quote_map, admission) + service level.

No network: the service runs against ``FakeOpenD`` (a stand-in implementing
the ``OpenDLike`` protocol). Contract fixtures come from the frozen X0
``scripts/infra/ib_realtime/fixtures/futu_quotes/`` directory; frames emitted
by the service are compared field-for-field with them.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from xenon.futu_quotes.admission import (
    Admission,
    futu_code,
    ticker_of,
)
from xenon.futu_quotes import opend as opend_mod
from xenon.futu_quotes.opend import (
    OpenDDownError,
    OpenDNotFoundError,
    OpenDProviderError,
    classify_subscribe_error,
    classify_unsubscribe_error,
)
from xenon.futu_quotes.quote_map import (
    HighWaterMark,
    et_wall_to_iso_z,
    et_wall_to_utc,
    finite_or_none,
    row_to_pricedata,
)
from xenon.futu_quotes.server import Batcher, FutuQuotesService

FIXTURES = (
    Path(__file__).resolve().parents[2]
    / "scripts/infra/ib_realtime/fixtures/futu_quotes"
)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ---------------------------------------------------------------------------
# quote_map
# ---------------------------------------------------------------------------


def test_et_wall_to_utc_edt_fixture():
    fx = load_fixture("_futu_raw_edt.json")
    ts = et_wall_to_iso_z(fx["data_date"], fx["data_time"])
    assert ts == fx["expect_timestamp"]


def test_et_wall_to_utc_dst_fallback_fixture():
    fx = load_fixture("_futu_raw_dst_fallback.json")
    ts = et_wall_to_iso_z(fx["data_date"], fx["data_time"])
    assert ts == fx["expect_timestamp"]


def test_et_wall_to_utc_rejects_bad_input():
    assert et_wall_to_utc("2026-13-01", "10:00:00") is None
    assert et_wall_to_utc("2026-10-23", "25:00:00") is None
    assert et_wall_to_utc(None, "10:00:00") is None
    assert et_wall_to_utc("2026-10-23", ["10:00:00"]) is None
    assert et_wall_to_utc("not-a-date", "10:00:00") is None
    assert et_wall_to_iso_z("2026-10-23", None) is None


def test_finite_or_none():
    assert finite_or_none(1.5) == 1.5
    assert finite_or_none("2.25") == 2.25
    assert finite_or_none("N/A") is None
    assert finite_or_none("") is None
    assert finite_or_none("NaN") is None
    assert finite_or_none(float("nan")) is None
    assert finite_or_none(float("inf")) is None
    assert finite_or_none(None) is None
    assert finite_or_none(True) is None
    assert finite_or_none([1, 2]) is None


def test_row_to_pricedata_normal_fixture():
    """A fully populated row maps to the exact fixture PriceData."""
    fx = load_fixture("batch_normal.json")["updates"]["AAPL"]
    row = {
        "code": "US.AAPL",
        "data_date": "2026-10-23",
        "data_time": "10:30:05",
        "last_price": 101.25,
        "open_price": 100.0,
        "high_price": 101.5,
        "low_price": 99.8,
        "prev_close_price": 100.5,
        "volume": 1200,
    }
    out = row_to_pricedata("AAPL", row)
    assert out is not None
    pd, _ts = out
    assert pd == fx


def test_row_to_pricedata_dst_fixture_sparse_row():
    """Sparse row: fields Futu did not send come out null (fixture)."""
    fx = load_fixture("batch_dst_fallback.json")["updates"]["HYG"]
    row = {
        "code": "US.HYG",
        "data_date": "2026-11-02",
        "data_time": "09:30:01",
        "last_price": 80.1,
    }
    pd, _ts = row_to_pricedata("HYG", row)  # type: ignore[misc]
    assert pd == fx


def test_row_to_pricedata_nan_dropped():
    fx = load_fixture("_futu_raw_nan.json")
    assert fx["expect_forwarded"] == 0
    assert row_to_pricedata("AAPL", fx) is None


def test_row_to_pricedata_bad_time_dropped():
    row = {"code": "US.AAPL", "data_date": "bogus", "data_time": "10:00:00", "last_price": 1.0}
    assert row_to_pricedata("AAPL", row) is None


def test_replay_stale_highwater_fixture():
    """Second (older) row of the sequence is dropped by the high-water mark."""
    fx = load_fixture("_futu_raw_replay_stale.json")
    hw = HighWaterMark()
    forwarded = 0
    for raw in fx["sequence"]:
        out = row_to_pricedata("AAPL", raw)
        assert out is not None
        _pd, ts = out
        if hw.accept("AAPL", ts):
            forwarded += 1
    assert forwarded == fx["expect_forwarded"]


def test_highwater_reset_on_unsubscribe():
    hw = HighWaterMark()
    t2 = datetime(2026, 10, 23, 14, 30, 5, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 23, 14, 29, 50, tzinfo=timezone.utc)
    assert hw.accept("AAPL", t2)
    assert not hw.accept("AAPL", t1)
    hw.reset("AAPL")
    assert hw.accept("AAPL", t1)


def test_ticker_of_round_trip():
    assert futu_code("BRK.B") == "US.BRK.B"
    assert ticker_of("US.AAPL") == "AAPL"
    assert ticker_of("US.BRK.B") == "BRK.B"
    assert ticker_of("HK.00700") is None
    assert ticker_of("US.") is None
    assert ticker_of("") is None


def test_error_classification():
    assert classify_subscribe_error("subscription quota limit exceeded") == "quota"
    assert classify_subscribe_error("code not support") == "not_supported"
    assert classify_subscribe_error("unknown code US.ZZZZ") == "unknown_symbol"
    assert classify_subscribe_error("invalid code US.BRK.B") == "unknown_symbol"
    assert classify_subscribe_error("illegal code US.BRK.B") == "unknown_symbol"
    assert classify_subscribe_error("something odd") == "provider_error"
    assert classify_unsubscribe_error("unsubscribe too early") == "too_early"
    assert classify_unsubscribe_error("code not subscribed") == "not_subscribed"
    assert classify_unsubscribe_error("boom") == "provider_error"


# ---------------------------------------------------------------------------
# admission (pure state machine)
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _admission(clock: Clock, **kw) -> Admission:
    ad = Admission(quota_cap=800, now=clock, **kw)
    ad.opend_connected = True
    ad.session_open = True
    return ad


def test_admission_partition_subscribing_then_ok():
    clock = Clock()
    ad = _admission(clock)
    ad.set_desired(1, ["AAPL", "MSFT"])
    res = ad.evaluate()
    assert res.partition["ok"] == []
    assert {p["symbol"] for p in res.partition["pending"]} == {"AAPL", "MSFT"}
    assert sorted(res.actions.verify) == ["AAPL", "MSFT"]
    ad.verify_result("AAPL", "ok")
    ad.verify_result("MSFT", "ok")
    res = ad.evaluate()
    assert sorted(res.actions.subscribe) == ["AAPL", "MSFT"]
    assert all(p["reason"] == "subscribing" for p in res.partition["pending"])
    ad.subscribe_result("AAPL", None)
    ad.subscribe_result("MSFT", None)
    res = ad.evaluate()
    assert res.partition["ok"] == ["AAPL", "MSFT"]
    assert res.partition["pending"] == []


def test_admission_session_closed_pending():
    clock = Clock()
    ad = _admission(clock)
    ad.set_session_open(False)
    ad.set_desired(1, ["AAPL"])
    ad.verify_result("AAPL", "ok")
    res = ad.evaluate()
    assert res.partition["pending"] == [{"symbol": "AAPL", "reason": "session"}]


def test_admission_opend_down_pending():
    clock = Clock()
    ad = _admission(clock)
    ad.set_desired(1, ["AAPL"])
    ad.verify_result("AAPL", "ok")
    ad.mark_disconnected()
    res = ad.evaluate()
    assert res.partition["pending"] == [{"symbol": "AAPL", "reason": "opend_down"}]


def test_admission_quota_ceiling_partition():
    clock = Clock()
    ad = _admission(clock)
    ad.apply_quota_snapshot({"own_used": 0, "remain": 1, "total_used": 0})
    ad.set_desired(7, ["AAPL", "BRK.B", "HYG"])
    for s in ("AAPL", "BRK.B", "HYG"):
        ad.verify_result(s, "ok")
    res = ad.evaluate()
    # AAPL wins the single slot; the rest are rejected for quota.
    assert res.actions.subscribe == ["AAPL"]
    assert {r["symbol"] for r in res.partition["rejected"]} == {"BRK.B", "HYG"}
    assert all(r["reason"] == "quota" for r in res.partition["rejected"])


def test_admission_provider_rejection_records_backoff():
    clock = Clock()
    ad = _admission(clock)
    ad.set_desired(1, ["HYG"])
    ad.verify_result("HYG", "ok")
    ad.evaluate()
    ad.subscribe_result("HYG", "subscription quota limit exceeded", "quota")
    assert ad.refresh_quota_requested
    res = ad.evaluate()
    assert res.partition["rejected"] == [{"symbol": "HYG", "reason": "quota"}]
    # Retried only after the backoff gate.
    res = ad.evaluate()
    assert res.actions.subscribe == []
    clock.advance(ad.backoff_base_s + 1)
    ad.refresh_quota_requested = False
    res = ad.evaluate()
    assert res.actions.subscribe == ["HYG"]


def test_admission_provider_error_backoff_then_retry():
    clock = Clock()
    ad = _admission(clock)
    ad.set_desired(1, ["HYG"])
    ad.verify_result("HYG", "ok")
    ad.evaluate()
    ad.subscribe_result("HYG", "socket reset", "provider_error")
    res = ad.evaluate()
    assert res.partition["rejected"] == [{"symbol": "HYG", "reason": "provider_error"}]
    res = ad.evaluate()
    assert res.actions.subscribe == []
    clock.advance(ad.backoff_base_s + 1)
    res = ad.evaluate()
    assert res.actions.subscribe == ["HYG"]


def test_admission_60s_unsubscribe_rule():
    """Unsub due = max(subscribed_at+60, unwanted_since+60); early attempt
    rejected → retried at the 60 s mark, then succeeds."""
    clock = Clock(1000.0)
    ad = _admission(clock)
    ad.set_desired(1, ["AAPL", "MSFT"])
    for s in ("AAPL", "MSFT"):
        ad.verify_result(s, "ok")
    ad.evaluate()
    ad.subscribe_result("AAPL", None)
    ad.subscribe_result("MSFT", None)

    clock.advance(10)  # t=1010: MSFT leaves the desired set
    ad.set_desired(2, ["AAPL"])
    res = ad.evaluate()
    assert res.actions.unsubscribe == []  # due at max(1000+60, 1010+60)=1070
    clock.advance(59)
    res = ad.evaluate()
    assert res.actions.unsubscribe == []
    clock.advance(1)  # t=1070 → due
    res = ad.evaluate()
    assert res.actions.unsubscribe == ["MSFT"]

    # Provider says "too early" (its own clock disagrees) → retry later.
    ad.unsubscribe_result("MSFT", "unsubscribe too early", "too_early")
    res = ad.evaluate()
    assert res.actions.unsubscribe == []
    # retry_at = max(sub+60, now+1) = 1071
    clock.advance(2)
    res = ad.evaluate()
    assert res.actions.unsubscribe == ["MSFT"]
    ad.unsubscribe_result("MSFT", None)
    assert not ad.state("MSFT").subscribed


def test_admission_readd_within_grace_cancels_unsub():
    clock = Clock(1000.0)
    ad = _admission(clock)
    ad.set_desired(1, ["AAPL", "MSFT"])
    for s in ("AAPL", "MSFT"):
        ad.verify_result(s, "ok")
    ad.evaluate()
    ad.subscribe_result("AAPL", None)
    ad.subscribe_result("MSFT", None)
    clock.advance(30)
    ad.set_desired(2, ["AAPL"])
    clock.advance(30)  # inside both 60 s windows
    ad.set_desired(3, ["AAPL", "MSFT"])
    res = ad.evaluate()
    assert "MSFT" in res.partition["ok"]
    assert res.actions.unsubscribe == []


def test_admission_release_desired_grace():
    clock = Clock(1000.0)
    ad = _admission(clock)
    ad.set_desired(1, ["AAPL"])
    ad.verify_result("AAPL", "ok")
    ad.evaluate()
    ad.subscribe_result("AAPL", None)
    clock.advance(200)  # sub is old
    ad.release_desired()  # owner conn lost at t=1200
    res = ad.evaluate()
    assert res.actions.unsubscribe == []
    clock.advance(60)  # grace fires at unwanted+60
    res = ad.evaluate()
    assert res.actions.unsubscribe == ["AAPL"]


def test_admission_mark_connected_resubscribes_desired():
    clock = Clock(1000.0)
    ad = _admission(clock)
    ad.set_desired(1, ["AAPL"])
    ad.verify_result("AAPL", "ok")
    ad.evaluate()
    ad.subscribe_result("AAPL", None)
    ad.mark_disconnected()
    ad.mark_connected()
    ad.set_session_open(True)  # session re-polled on recovery
    res = ad.evaluate()
    # Do not assume the subscription survived: re-issue it.
    assert res.actions.subscribe == ["AAPL"]


def test_admission_is_ok_fencing():
    clock = Clock()
    ad = _admission(clock)
    ad.set_desired(1, ["AAPL"])
    ad.verify_result("AAPL", "ok")
    ad.evaluate()
    assert not ad.is_ok("AAPL")
    ad.subscribe_result("AAPL", None)
    assert ad.is_ok("AAPL")
    ad.set_session_open(False)
    assert not ad.is_ok("AAPL")


def test_admission_session_is_open_states():
    assert Admission.session_is_open("MORNING")
    assert Admission.session_is_open("AFTERNOON")
    assert not Admission.session_is_open("CLOSED")
    assert not Admission.session_is_open("PRE_MARKET_BEGIN")
    assert not Admission.session_is_open("OVERNIGHT")
    assert not Admission.session_is_open(None)
    assert not Admission.session_is_open("")


def test_admission_quota_report_disconnected():
    clock = Clock()
    ad = _admission(clock)
    ad.mark_disconnected()
    assert ad.quota_report() == {
        "ceiling": 0,
        "own_used": 0,
        "all_used": None,
        "remain": None,
    }


# ---------------------------------------------------------------------------
# Batcher (coalescing + 100 ms cadence)
# ---------------------------------------------------------------------------


def _pd(symbol: str, last: float, ts_iso: str = "2026-10-23T14:30:05Z") -> dict:
    return {"symbol": symbol, "last": last, "timestamp": ts_iso}


def test_batcher_coalesces_and_caps_rate():
    clock = Clock(0.0)
    b = Batcher(0.1, clock)
    t1 = datetime(2026, 10, 23, 14, 30, 5, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 23, 14, 30, 6, tzinfo=timezone.utc)
    t0 = datetime(2026, 10, 23, 14, 29, 0, tzinfo=timezone.utc)
    b.enqueue(_pd("AAPL", 101.0), t1)
    b.enqueue(_pd("AAPL", 101.5), t2)
    b.enqueue(_pd("MSFT", 420.0), t1)
    # A stale tick never overwrites a newer buffered one.
    b.enqueue(_pd("AAPL", 99.0), t0)
    assert b.pop_ready(clock(), lambda s: True) is None  # not due
    clock.advance(0.11)
    out = b.pop_ready(clock(), lambda s: True)
    assert out is not None
    assert out["updates"]["AAPL"]["last"] == 101.5
    assert out["updates"]["MSFT"]["last"] == 420.0
    assert out["marks"]["AAPL"] == t2


def test_batcher_empty_and_fence():
    clock = Clock(0.0)
    b = Batcher(0.1, clock)
    assert b.pop_ready(clock(), lambda s: True) is None
    t1 = datetime(2026, 10, 23, 14, 30, 5, tzinfo=timezone.utc)
    b.enqueue(_pd("AAPL", 1.0), t1)
    clock.advance(0.2)
    # Fenced out → consumed but nothing emitted.
    assert b.pop_ready(clock(), lambda s: False) is None
    assert b.pending == {}


# ---------------------------------------------------------------------------
# Service level — FakeOpenD + FakeWS over the real service object
# ---------------------------------------------------------------------------


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, s: str) -> None:
        self.sent.append(json.loads(s))

    async def close(self) -> None:
        self.closed = True

    def frames(self, ftype: str) -> list[dict]:
        return [f for f in self.sent if f.get("type") == ftype]

    def last(self, ftype: str) -> dict | None:
        fs = self.frames(ftype)
        return fs[-1] if fs else None


class FakeOpenD:
    """OpenDLike with scripted behaviour; no network."""

    def __init__(
        self,
        *,
        snapshots: dict | None = None,
        market_us: str = "AFTERNOON",
        remain: int = 997,
        own_used: int = 0,
        total_used: int | None = None,
        sub_limit: int | None = None,
    ) -> None:
        self.snapshots = snapshots or {}
        self.market_us = market_us
        self.remain = remain
        self.own_used = own_used
        self.total_used = total_used if total_used is not None else own_used
        self.sub_limit = sub_limit
        self.connected = False
        self.subscribed: set[str] = set()
        self.subscribe_calls: list[str] = []
        self.unsubscribe_calls: list[str] = []
        self.snapshot_calls = 0
        self.quota_calls = 0
        self.subscribe_errors: dict[str, str] = {}
        self.subscribe_raise: dict[str, Exception] = {}
        # Lost reply: OpenD records the subscription, then the caller gets
        # this exception (raised) or error string (returned) instead.
        self.subscribe_lost_reply: dict[str, object] = {}
        self.unsubscribe_errors: list[str] = []
        self.unsubscribe_raise: Exception | None = None
        # Entry hooks: ``hooks[method](n, codes)`` is awaited at the START of
        # the n-th call (1-based) of ``method`` — before anything else, so a
        # parked hook models a call queued in front of the SDK. ``entered``
        # counts calls that reached the fake.
        self.hooks: dict = {}
        self.entered: dict[str, int] = {}
        self.snapshot_errors: dict[str, Exception] = {}
        self.quota_raise: Exception | None = None
        self.defer_all = False
        self._defer_results: dict[str, str | None] = {}
        self._defer_queues: dict[str, list[asyncio.Future]] = {}
        self.on_quote_rows = None
        self.on_disconnected = None
        self.on_reconnected = None

    def _require_conn(self) -> None:
        if not self.connected:
            from xenon.futu_quotes.opend import OpenDDownError

            raise OpenDDownError("fake OpenD down")

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def _enter(self, method: str, codes=()) -> None:
        n = self.entered[method] = self.entered.get(method, 0) + 1
        hook = self.hooks.get(method)
        if hook is not None:
            await hook(n, list(codes))

    def gate(self, method: str) -> asyncio.Event:
        """Park every call of ``method`` at entry until the event is set."""
        ev = asyncio.Event()

        async def _hook(n, codes):
            await ev.wait()

        self.hooks[method] = _hook
        return ev

    async def get_market_snapshot(self, codes: list[str]) -> list[dict]:
        await self._enter("get_market_snapshot", codes)
        self._require_conn()
        self.snapshot_calls += 1
        for c in codes:
            if c in self.snapshot_errors:
                raise self.snapshot_errors[c]
        return [self.snapshots[c] for c in codes if c in self.snapshots and self.snapshots[c]]

    async def subscribe(self, codes: list[str], *, pre=None) -> dict:
        # ``pre`` is accepted only so the red run against the pre-fix
        # service (which passed one) exercises the same schedule.
        await self._enter("subscribe", codes)
        self._require_conn()
        out: dict = {}
        for c in codes:
            if pre is not None and not pre():
                raise getattr(opend_mod, "OpAborted")(f"fake pre declined {c}")
            self.subscribe_calls.append(c)
            if self.defer_all or c in self._defer_results or self._defer_queues.get(c):
                loop = asyncio.get_running_loop()
                fut = loop.create_future()
                self._defer_queues.setdefault(c, []).append(fut)
                if c in self._defer_results:
                    fut.set_result(self._defer_results.pop(c))
                out[c] = await fut
                if out[c] is None and c not in self.subscribed:
                    self.subscribed.add(c)
                    self.own_used = len(self.subscribed)
                continue
            out[c] = self._do_subscribe(c)
        return out

    def _do_subscribe(self, c: str):
        if c in self.subscribe_lost_reply:
            lost = self.subscribe_lost_reply.pop(c)
            self.subscribed.add(c)
            self.own_used = len(self.subscribed)
            if isinstance(lost, Exception):
                raise lost
            return lost
        if c in self.subscribe_raise:
            raise self.subscribe_raise.pop(c)
        if c in self.subscribe_errors:
            return self.subscribe_errors.pop(c)
        if self.sub_limit is not None and len(self.subscribed) >= self.sub_limit:
            return "subscription quota limit exceeded"
        if c in self.subscribed:
            return None
        self.subscribed.add(c)
        self.own_used = len(self.subscribed)
        return None

    def complete_subscribe(self, code: str, err: str | None = None) -> None:
        """Resolve the oldest pending deferred subscribe for ``code``."""
        queue = self._defer_queues.get(code)
        if queue:
            queue.pop(0).set_result(err)
        else:
            self._defer_results[code] = err

    async def unsubscribe(self, codes: list[str], *, pre=None) -> dict:
        await self._enter("unsubscribe", codes)
        self._require_conn()
        out: dict = {}
        for c in codes:
            if pre is not None and not pre():
                raise getattr(opend_mod, "OpAborted")(f"fake pre declined {c}")
            self.unsubscribe_calls.append(c)
            if self.unsubscribe_raise is not None:
                raise self.unsubscribe_raise
            if self.unsubscribe_errors:
                out[c] = self.unsubscribe_errors.pop(0)
            elif c in self.subscribed:
                self.subscribed.discard(c)
                self.own_used = len(self.subscribed)
                out[c] = None
            else:
                out[c] = "code not subscribed"
        return out

    async def query_subscription(self) -> dict:
        await self._enter("query_subscription")
        self._require_conn()
        self.quota_calls += 1
        if self.quota_raise is not None:
            raise self.quota_raise
        return {
            "total_used": self.total_used,
            "own_used": self.own_used,
            "remain": self.remain,
            "sub_list": {"QUOTE": sorted(self.subscribed)},
        }

    async def get_global_state(self) -> dict:
        await self._enter("get_global_state")
        self._require_conn()
        return {"market_us": self.market_us}

    def drop(self) -> None:
        """Simulate OpenD loss."""
        self.connected = False
        if self.on_disconnected:
            self.on_disconnected()

    def push(self, rows: list[dict]) -> None:
        if self.on_quote_rows:
            self.on_quote_rows(rows)


def _snapshot_row(code: str, **extra) -> dict:
    row = {"code": code, "sec_type": "STOCK", "last_price": 1.0}
    row.update(extra)
    return row


DEFAULT_SNAPSHOTS = {
    "US.AAPL": _snapshot_row("US.AAPL"),
    "US.BRK.B": _snapshot_row("US.BRK.B"),
    "US.HYG": _snapshot_row("US.HYG"),
    "US.MSFT": _snapshot_row("US.MSFT"),
}


async def _settled_service(
    fake: FakeOpenD,
    clock=None,
    **svc_kw,
) -> FutuQuotesService:
    """Connect + recover so opend_connected/session/quota are populated."""
    svc = FutuQuotesService(fake, clock=clock or (lambda: 1000.0), **svc_kw)
    await fake.connect()
    await svc.opend_recovered()
    await svc.drain()
    return svc


async def _want(svc: FutuQuotesService, ws: FakeWS, gen: int, symbols: list[str]) -> None:
    await svc.on_ws_message(ws, json.dumps({"action": "want", "gen": gen, "symbols": symbols}))


# ---------------------------------------------------------------------------
# assign fixtures, end to end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_ok_fixture():
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS, remain=997, own_used=0, total_used=0)
    svc = await _settled_service(fake)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    want = load_fixture("want.json")
    await _want(svc, ws, want["gen"], want["symbols"])
    await svc.drain()
    assert ws.last("assign") == load_fixture("assign_ok.json")
    # health: own_used is local truth (3 subs); remain/all_used from provider.
    health = ws.last("health")
    assert health is not None and health["opend_connected"] is True
    assert health["quota"]["ceiling"] == 800


@pytest.mark.asyncio
async def test_assign_partial_quota_fixture():
    """remain=1 → ceiling 1 → AAPL ok, rest quota-rejected."""
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS, remain=1, own_used=0, total_used=1)
    svc = await _settled_service(fake)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    want = load_fixture("want.json")
    await _want(svc, ws, want["gen"], want["symbols"])
    await svc.drain()
    assert ws.last("assign") == load_fixture("assign_partial_quota.json")


@pytest.mark.asyncio
async def test_assign_session_fixture():
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS, market_us="CLOSED")
    svc = await _settled_service(fake)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    want = load_fixture("want.json")
    await _want(svc, ws, want["gen"], want["symbols"])
    await svc.drain()
    assert ws.last("assign") == load_fixture("assign_session.json")


@pytest.mark.asyncio
async def test_opend_loss_fixture_health_and_assign():
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    want = load_fixture("want.json")
    await _want(svc, ws, want["gen"], want["symbols"])
    await svc.drain()
    assert ws.last("assign") == load_fixture("assign_ok.json")

    fake.drop()
    await svc.drain()
    assert ws.last("assign") == load_fixture("assign_opend_lost.json")
    assert ws.last("health") == load_fixture("health_opend_lost.json")


@pytest.mark.asyncio
async def test_health_ok_fixture():
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS, remain=997, own_used=0, total_used=0)
    svc = await _settled_service(fake)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    want = load_fixture("want.json")
    await _want(svc, ws, want["gen"], want["symbols"])
    await svc.drain()
    # Simulate provider-reported totals after 3 subs.
    fake.total_used = 3
    await svc.refresh_quota_once()
    await svc.drain()
    health = ws.last("health")
    assert health is not None
    assert health == load_fixture("health_ok.json")


# ---------------------------------------------------------------------------
# epoch, stale completion, remove/re-add
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_epoch_replacement_closes_old_conn():
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake)
    ws1 = FakeWS()
    await svc.on_ws_connect(ws1)
    await _want(svc, ws1, 7, ["AAPL"])
    await svc.drain()

    ws2 = FakeWS()
    await svc.on_ws_connect(ws2)
    assert ws1.closed
    # New connection: gen restarts at 1; late frames from ws1 are ignored.
    await _want(svc, ws1, 8, ["MSFT"])
    await svc.drain()
    assert ws1.last("assign") is not None and ws1.last("assign")["gen"] == 7
    await _want(svc, ws2, 1, ["AAPL"])
    await svc.drain()
    last = ws2.last("assign")
    assert last is not None and last["gen"] == 1 and last["ok"] == ["AAPL"]


@pytest.mark.asyncio
async def test_stale_subscribe_completion_after_newer_gen():
    """A subscribe issued under gen1 that lands after gen2 must not put the
    dropped symbol into gen2's ok set."""
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    fake.defer_all = True
    svc = await _settled_service(fake)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL", "MSFT"])

    # Ops are serialized: MSFT's call starts once AAPL's returns.
    await _until(lambda: "US.AAPL" in fake.subscribe_calls)
    fake.complete_subscribe("US.AAPL")
    await _until(lambda: "US.MSFT" in fake.subscribe_calls)

    await _want(svc, ws, 2, ["AAPL"])  # MSFT unwanted while subscribe in flight
    await asyncio.sleep(0)

    fake.complete_subscribe("US.MSFT")  # late completion: really did subscribe
    await svc.drain()
    assert svc.admission.state("MSFT").subscribed  # held → released via grace

    last = ws.last("assign")
    assert last is not None
    assert last["gen"] == 2
    assert last["ok"] == ["AAPL"]
    assert all(p["symbol"] != "MSFT" for p in last["pending"])
    assert all(r["symbol"] != "MSFT" for r in last["rejected"])
    assert "US.MSFT" in fake.subscribed  # it did subscribe — pending grace unsub


@pytest.mark.asyncio
async def test_remove_readd_cancels_unsubscribe():
    clock = Clock(1000.0)
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL", "MSFT"])
    await svc.drain()

    clock.advance(30)
    await _want(svc, ws, 2, ["AAPL"])  # MSFT unwanted at t=1030
    clock.advance(30)
    await svc.tick_unsubscribes_once()
    await svc.drain()
    assert "US.MSFT" not in fake.unsubscribe_calls

    await _want(svc, ws, 3, ["AAPL", "MSFT"])  # re-add inside grace
    await svc.drain()
    last = ws.last("assign")
    assert last is not None and set(last["ok"]) == {"AAPL", "MSFT"}
    assert "US.MSFT" not in fake.unsubscribe_calls


@pytest.mark.asyncio
async def test_60s_unsub_rejected_then_retried():
    clock = Clock(1000.0)
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    fake.unsubscribe_errors = ["unsubscribe too early"]
    svc = await _settled_service(fake, clock=clock)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL", "MSFT"])
    await svc.drain()

    clock.advance(121)  # MSFT unwanted at t=1121; due = max(1060, 1181) = 1181
    await _want(svc, ws, 2, ["AAPL"])
    await svc.drain()
    assert "US.MSFT" not in fake.unsubscribe_calls

    clock.advance(1)  # wait — actually due: subs were made at ~t≈1000+
    for _ in range(120):
        if "US.MSFT" in fake.unsubscribe_calls:
            break
        clock.advance(1)
        await svc.tick_unsubscribes_once()
        await svc.drain()
    assert "US.MSFT" in fake.unsubscribe_calls

    # First attempt rejected as too early → retried later, then succeeds.
    for _ in range(120):
        if "US.MSFT" not in fake.subscribed:
            break
        clock.advance(2)
        await svc.tick_unsubscribes_once()
        await svc.drain()
    assert "US.MSFT" not in fake.subscribed
    assert fake.unsubscribe_calls.count("US.MSFT") >= 2


@pytest.mark.asyncio
async def test_provider_quota_rejection_below_local_ceiling():
    """Ceiling has room locally but the provider rejects → rejected quota,
    and the service refreshes quota afterwards."""
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS, remain=997)
    fake.subscribe_errors["US.HYG"] = "subscription quota limit exceeded"
    svc = await _settled_service(fake)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL", "HYG"])
    await svc.drain()
    last = ws.last("assign")
    assert last is not None
    assert last["ok"] == ["AAPL"]
    assert {"symbol": "HYG", "reason": "quota"} in last["rejected"]
    assert fake.quota_calls >= 2  # initial + post-rejection refresh


# ---------------------------------------------------------------------------
# ticks → batch frames
# ---------------------------------------------------------------------------


def _raw_row(code: str, last: float, date="2026-10-23", time_="10:30:05", **kw) -> dict:
    row = {
        "code": code,
        "data_date": date,
        "data_time": time_,
        "last_price": last,
    }
    row.update(kw)
    return row


@pytest.mark.asyncio
async def test_batch_normal_fixture_end_to_end():
    clock = Clock(1000.0)
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain()

    fake.push([
        _raw_row(
            "US.AAPL",
            101.25,
            open_price=100.0,
            high_price=101.5,
            low_price=99.8,
            prev_close_price=100.5,
            volume=1200,
        )
    ])
    clock.advance(0.11)
    await svc.flush_now()
    assert ws.last("batch") == load_fixture("batch_normal.json")


@pytest.mark.asyncio
async def test_batch_dst_fixture_end_to_end():
    clock = Clock(1000.0)
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["HYG"])
    await svc.drain()

    raw = load_fixture("_futu_raw_dst_fallback.json")
    fake.push([raw])
    clock.advance(0.11)
    await svc.flush_now()
    assert ws.last("batch") == load_fixture("batch_dst_fallback.json")


@pytest.mark.asyncio
async def test_nan_push_forwards_nothing():
    clock = Clock(1000.0)
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain()
    fake.push([load_fixture("_futu_raw_nan.json")])
    clock.advance(0.2)
    await svc.flush_now()
    assert ws.frames("batch") == []


@pytest.mark.asyncio
async def test_producer_fence_and_stale_replay():
    """Ticks for non-ok symbols never leave; replayed cached quotes are
    dropped by the high-water mark."""
    clock = Clock(1000.0)
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain()

    # Fence: MSFT not in ok → dropped before buffering.
    fake.push([_raw_row("US.MSFT", 420.0, time_="10:30:00")])
    clock.advance(0.11)
    await svc.flush_now()
    assert ws.frames("batch") == []

    # Replay: fresh quote, then stale cached quote; only fresh goes out.
    fx = load_fixture("_futu_raw_replay_stale.json")["sequence"]
    fake.push([fx[0]])
    clock.advance(0.11)
    await svc.flush_now()
    fake.push([fx[1]])
    clock.advance(0.11)
    await svc.flush_now()
    batches = ws.frames("batch")
    assert len(batches) == 1
    assert batches[0]["updates"]["AAPL"]["last"] == 101.25


@pytest.mark.asyncio
async def test_tick_cadence_coalesces_real_time():
    """Real-time path: multiple pushes inside one 100 ms window → 1 batch."""
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=time.monotonic)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain()

    fake.push([_raw_row("US.AAPL", 101.0, time_="10:30:05")])
    fake.push([_raw_row("US.AAPL", 101.25, time_="10:30:05")])
    await asyncio.sleep(0.05)
    fake.push([_raw_row("US.AAPL", 101.3, time_="10:30:06")])
    await asyncio.sleep(0.12)
    batches = ws.frames("batch")
    assert len(batches) == 1
    assert batches[0]["updates"]["AAPL"]["last"] == 101.3


@pytest.mark.asyncio
async def test_class_share_unknown_and_index_not_supported():
    fake = FakeOpenD(
        snapshots={
            **_snapshot_map(),
            "US.SPX": _snapshot_row("US.SPX", sec_type="IDX"),
        }
    )
    svc = await _settled_service(fake)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["SPX", "ZZZZ", "BRK.B"])
    await svc.drain()
    last = ws.last("assign")
    assert last is not None
    reasons = {r["symbol"]: r["reason"] for r in last["rejected"]}
    assert reasons["SPX"] == "not_supported"
    assert reasons["ZZZZ"] == "unknown_symbol"
    assert last["ok"] == ["BRK.B"]


def _snapshot_map() -> dict:
    return dict(DEFAULT_SNAPSHOTS)


@pytest.mark.asyncio
async def test_gen_stale_ignored():
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 5, ["AAPL"])
    await svc.drain()
    assigns_before = len(ws.frames("assign"))
    await _want(svc, ws, 3, ["MSFT"])  # older gen → ignored
    await svc.drain()
    assert len(ws.frames("assign")) == assigns_before
    assert ws.last("assign")["gen"] == 5


@pytest.mark.asyncio
async def test_subscribe_rejected_unknown_code_via_error_string():
    """Verified at snapshot, then subscribe rejects the code as invalid →
    rejected/unknown_symbol permanently (class-share passthrough)."""
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    fake.subscribe_errors["US.BRK.B"] = "invalid code US.BRK.B"
    svc = await _settled_service(fake)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL", "BRK.B"])
    await svc.drain()
    last = ws.last("assign")
    assert last is not None
    assert last["ok"] == ["AAPL"]
    assert {"symbol": "BRK.B", "reason": "unknown_symbol"} in last["rejected"]
    # Permanent: no re-subscribe storms.
    await svc.tick_unsubscribes_once()
    await svc.drain()
    assert fake.subscribe_calls.count("US.BRK.B") == 1


@pytest.mark.asyncio
async def test_subscribe_raises_not_found_unknown_symbol():
    """OpenDNotFoundError on subscribe classifies by type, not message."""
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    fake.subscribe_raise["US.BRK.B"] = OpenDNotFoundError("US.BRK.B")
    svc = await _settled_service(fake)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL", "BRK.B"])
    await svc.drain()
    last = ws.last("assign")
    assert last is not None
    assert last["ok"] == ["AAPL"]
    assert {"symbol": "BRK.B", "reason": "unknown_symbol"} in last["rejected"]
    assert fake.subscribe_calls.count("US.BRK.B") == 1


@pytest.mark.asyncio
async def test_unsubscribe_raises_not_found_clears_record():
    """OpenDNotFoundError on unsubscribe resolves the sub the same as a
    'not subscribed' reply."""
    clock = Clock(1000.0)
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL", "MSFT"])
    await svc.drain()
    clock.advance(200)
    await _want(svc, ws, 2, ["AAPL"])
    fake.unsubscribe_raise = OpenDNotFoundError("US.MSFT")
    for _ in range(120):
        if "US.MSFT" in fake.unsubscribe_calls:
            break
        clock.advance(1)
        await svc.tick_unsubscribes_once()
        await svc.drain()
    assert "US.MSFT" in fake.unsubscribe_calls
    assert not svc.admission.state("MSFT").subscribed


# ---------------------------------------------------------------------------
# Astra round-1 regressions (R235-1..7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_reconnect_callback_preserves_tuple_contract(monkeypatch):
    """R235-1: OpenContextBase._init_connect_sync unpacks
    ``on_api_socket_reconnected()`` → the override must return ``(ret, msg)``
    and notify recovery only when the SDK resubscribe succeeded."""
    import threading

    from futu import OpenQuoteContext
    from futu.common.constant import RET_ERROR, RET_OK
    from futu.common.open_context_base import ContextStatus

    from xenon.futu_quotes.opend import _QuoteContext

    fired: list[bool] = []

    class _Sink:
        def _deliver_lifecycle(self, gen: int, up: bool) -> None:
            if up:
                fired.append(True)

    ctx = _QuoteContext.__new__(_QuoteContext)
    ctx._fq_loop = asyncio.get_running_loop()
    ctx._fq_sink = _Sink()
    ctx._fq_gen = 1
    ctx._fq_link = 0
    ctx._fq_ready = True  # past the base __init__ (first connect)
    ctx._lock = threading.RLock()
    ctx._status = ContextStatus.START
    ctx._connect_sync = lambda: RET_OK
    ctx._send_init_connect_sync = lambda: (RET_OK, "")
    ctx.conn_str = lambda: "stub:0"

    # Success: tuple preserved → unpackable; recovery notified.
    monkeypatch.setattr(
        OpenQuoteContext, "on_api_socket_reconnected", lambda self: (RET_OK, "")
    )
    ret, msg = ctx.on_api_socket_reconnected()
    assert (ret, msg) == (RET_OK, "")
    await asyncio.sleep(0)
    assert fired == [True]
    fired.clear()
    assert ctx._init_connect_sync() == RET_OK
    await asyncio.sleep(0)
    assert fired == [True]

    # SDK resubscribe failure: tuple still returned; recovery NOT notified.
    ctx._status = ContextStatus.START
    fired.clear()
    monkeypatch.setattr(
        OpenQuoteContext, "on_api_socket_reconnected", lambda self: (RET_ERROR, "boom")
    )
    assert ctx.on_api_socket_reconnected() == (RET_ERROR, "boom")
    await asyncio.sleep(0)
    assert fired == []


async def _until(pred, rounds: int = 400) -> None:
    for _ in range(rounds):
        if pred():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


@pytest.mark.asyncio
async def test_due_unsub_runs_once_and_readd_aborts():
    """R235-2: a due unsubscribe is reserved before it is queued — exactly
    one provider call across many eval passes; re-adding the symbol while
    the op waits for the worker declines it before it reaches the provider."""
    clock = Clock(1000.0)
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL", "MSFT"])
    await svc.drain()
    assert "US.MSFT" in fake.subscribed

    clock.advance(200)
    await _want(svc, ws, 2, ["AAPL"])  # MSFT unwanted at t=1200
    await svc.drain()
    clock.advance(61)                 # past max(sub+60, unwanted+60) = 1260
    held = fake.gate("get_global_state")  # worker busy in front of the op
    poll = asyncio.create_task(svc.poll_session_once())
    await _until(lambda: fake.entered.get("get_global_state", 0) >= 2)
    for _ in range(5):
        await svc.tick_unsubscribes_once()
        await svc.drain(timeout=0.05)
    assert svc.admission.state("MSFT").op == "unsubscribe"  # one reservation
    assert fake.entered.get("unsubscribe", 0) == 0

    await _want(svc, ws, 3, ["AAPL", "MSFT"])  # re-add while queued
    held.set()
    await poll
    await svc.drain()
    assert fake.entered.get("unsubscribe", 0) == 0
    assert "US.MSFT" in fake.subscribed
    assert svc.admission.is_ok("MSFT")


@pytest.mark.asyncio
async def test_failed_unsub_retries_once_per_backoff():
    """R235-2: after a failed unsubscribe the next attempt runs only after
    the backoff — never a second concurrent op."""
    clock = Clock(1000.0)
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL", "MSFT"])
    await svc.drain()

    fake.unsubscribe_errors = ["provider blew up"]
    clock.advance(200)
    await _want(svc, ws, 2, ["AAPL"])
    clock.advance(61)  # past the 60 s unwanted grace
    await svc.tick_unsubscribes_once()
    await svc.drain()
    assert fake.unsubscribe_calls == ["US.MSFT"]
    assert svc.admission.state("MSFT").subscribed  # still owned — kept counted

    await svc.tick_unsubscribes_once()  # same instant — inside backoff
    await svc.drain()
    assert fake.unsubscribe_calls == ["US.MSFT"]

    clock.advance(16)  # past the 15 s retry
    await svc.tick_unsubscribes_once()
    await svc.drain()
    assert fake.unsubscribe_calls == ["US.MSFT", "US.MSFT"]
    assert not svc.admission.state("MSFT").subscribed


@pytest.mark.asyncio
async def test_failed_unsub_never_releases_owned_quota():
    """R235-3: repeated unsubscribe failures + a stale/failed quota refresh
    must not free the record — a new admission stays rejected until the
    unsubscribe actually succeeds."""
    clock = Clock(1000.0)
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS, remain=1, own_used=0, total_used=0)
    svc = await _settled_service(fake, clock=clock, quota_cap=1)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain()
    assert svc.admission.is_ok("AAPL")  # ceiling == 1, own == 1

    # OLD leaves, NEW arrives; unsubscribe keeps failing, quota refresh too.
    fake.unsubscribe_raise = OpenDProviderError("provider blew up")
    fake.quota_raise = OpenDProviderError("quota unavailable")
    clock.advance(200)
    await _want(svc, ws, 2, ["MSFT"])
    clock.advance(61)  # past the 60 s unwanted grace
    await svc.tick_unsubscribes_once()
    await svc.drain()
    assert fake.unsubscribe_calls == ["US.AAPL"]
    assert svc.admission.state("AAPL").subscribed  # still owned

    # Many retry rounds (>20 failures): AAPL remains counted, MSFT can
    # never be admitted while the sub is unresolved.
    for _ in range(25):
        clock.advance(120)
        await svc.tick_unsubscribes_once()
        await svc.drain()
    assert svc.admission.state("AAPL").subscribed
    last = ws.last("assign")
    assert {"symbol": "MSFT", "reason": "quota"} in last["rejected"]
    assert svc.admission.own_used() == 1

    # Recovery: the unsubscribe finally lands → ownership released → NEW
    # admitted once its stale quota backoff expires.
    fake.unsubscribe_raise = None
    clock.advance(150)
    await svc.tick_unsubscribes_once()
    await svc.drain()
    assert not svc.admission.state("AAPL").subscribed
    clock.advance(125)  # past MSFT's stale rejected-quota backoff
    await svc.tick_unsubscribes_once()
    await svc.drain()
    assert svc.admission.is_ok("MSFT")


@pytest.mark.asyncio
async def test_stale_session_subscribe_cannot_certify_new_session():
    """R235-4: a subscribe completing on a dead OpenD session must not mark
    the symbol subscribed in the replacement session."""
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    fake.defer_all = True
    svc = await _settled_service(fake)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    for _ in range(200):
        if fake.subscribe_calls.count("US.AAPL") >= 1:
            break
        await asyncio.sleep(0)
    epoch_a = svc.admission.epoch

    fake.drop()  # session A dies while call A is pending
    await fake.connect()
    rec = asyncio.create_task(svc.opend_recovered())  # session B
    await asyncio.sleep(0)
    assert svc.admission.epoch > epoch_a
    # Ops are serialized: session B's work waits for call A to return.
    fake.complete_subscribe("US.AAPL", None)  # session-A call succeeds late
    await rec
    await _until(lambda: fake.subscribe_calls.count("US.AAPL") >= 2)
    assert not svc.admission.is_ok("AAPL")  # A's success certified nothing
    fake.complete_subscribe("US.AAPL", "quota burst")  # session-B call rejected
    await svc.drain()

    st = svc.admission.state("AAPL")
    assert st.subscribed_at is None
    assert not svc.admission.is_ok("AAPL")
    last = ws.last("assign")
    assert "AAPL" not in last["ok"]
    assert {"symbol": "AAPL", "reason": "quota"} in last["rejected"]


@pytest.mark.asyncio
async def test_reconnect_reconfirms_owned_subs_without_double_charging():
    """R235-5: recovery at the ceiling reconfirms believed-owned subs
    without reserving a second quota unit; genuinely new symbols still hit
    the ceiling."""
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS, remain=1, own_used=0, total_used=0)
    svc = await _settled_service(fake, quota_cap=1)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain()
    assert svc.admission.is_ok("AAPL")

    fake.drop()
    await fake.connect()
    # Provider still reports own_used=1 (the SDK restores its own subs).
    await svc.opend_recovered()
    await svc.drain()

    st = svc.admission.state("AAPL")
    assert fake.subscribe_calls.count("US.AAPL") == 2  # reconfirm issued
    assert st.subscribed
    assert svc.admission.is_ok("AAPL")
    assert st.rejected_reason != "quota"

    # A genuinely NEW symbol still hits the ceiling.
    await _want(svc, ws, 2, ["AAPL", "MSFT"])
    await svc.drain()
    last = ws.last("assign")
    assert {"symbol": "MSFT", "reason": "quota"} in last["rejected"]


@pytest.mark.asyncio
async def test_buffered_tick_dropped_on_remove_readd():
    """R235-6: a tick buffered before remove→re-add inside one coalesce
    window must not flush."""
    clock = Clock(1000.0)
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain()

    fake.push([_raw_row("US.AAPL", 123.45)])

    # Remove + re-add within the same 100 ms window — the buffered tick is
    # obsolete even though AAPL is ok again at flush time.
    await _want(svc, ws, 2, ["MSFT"])
    await svc.drain()
    await _want(svc, ws, 3, ["AAPL"])
    await svc.drain()
    clock.advance(0.2)
    await svc.flush_now()
    assert ws.frames("batch") == []

    # A fresh tick flows normally afterwards.
    fake.push([_raw_row("US.AAPL", 123.45, time_="10:30:06.500")])
    clock.advance(0.2)
    await svc.flush_now()
    batches = ws.frames("batch")
    assert len(batches) == 1
    assert "AAPL" in batches[0]["updates"]


@pytest.mark.asyncio
async def test_owner_replacement_clears_buffered_ticks():
    """R235-6: a prior owner's buffered quote must not flush to the
    replacement connection."""
    clock = Clock(1000.0)
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock)
    ws1 = FakeWS()
    await svc.on_ws_connect(ws1)
    await _want(svc, ws1, 1, ["AAPL"])
    await svc.drain()
    fake.push([_raw_row("US.AAPL", 123.45)])

    ws2 = FakeWS()
    await svc.on_ws_connect(ws2)  # epoch boundary — buffer cleared
    await _want(svc, ws2, 1, ["AAPL"])
    await svc.drain()
    clock.advance(0.2)
    await svc.flush_now()
    assert ws2.frames("batch") == []


@pytest.mark.asyncio
async def test_snapshot_batch_failure_isolates_bad_code():
    """R235-7: a whole-call snapshot failure for one bad code must not
    reject its batch-mates — valid symbols still verify and subscribe."""
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    fake.snapshot_errors["US.INVALID"] = OpenDNotFoundError("invalid code US.INVALID")
    fake.snapshots["US.INVALID"] = _snapshot_row("US.INVALID")
    svc = await _settled_service(fake)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL", "INVALID"])
    await svc.drain()

    last = ws.last("assign")
    assert last is not None
    assert "AAPL" in last["ok"]
    assert {"symbol": "INVALID", "reason": "unknown_symbol"} in last["rejected"]
    assert "US.AAPL" in fake.subscribed
    # Batch fast path preserved: one failing batch call, then per-code calls.
    assert fake.snapshot_calls >= 3


# ---------------------------------------------------------------------------
# Astra round-2 regressions (exact failing schedules from the review)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queued_unsub_honours_renewed_grace():
    """R235-2: a queued unsubscribe re-checks the CURRENT deadlines. A
    re-add followed by another removal restarts the 60 s grace."""
    clock = Clock(1000.0)
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL", "MSFT"])
    await svc.drain()
    clock.advance(200)
    await _want(svc, ws, 2, ["AAPL"])  # MSFT unwanted at t=1200
    await svc.drain()
    clock.advance(61)  # t=1261: due

    worker_gate = fake.gate("get_global_state")  # worker busy in front of the op
    unsub_gate = fake.gate("unsubscribe")  # pre-fix code: op parked before its pre-check
    poll = asyncio.create_task(svc.poll_session_once())
    await _until(lambda: fake.entered.get("get_global_state", 0) >= 2)
    await svc.tick_unsubscribes_once()
    await svc.drain(timeout=0.05)

    await _want(svc, ws, 3, ["AAPL", "MSFT"])  # re-add ...
    await _want(svc, ws, 4, ["AAPL"])  # ... and remove again at t=1261
    worker_gate.set()
    unsub_gate.set()
    await poll
    await svc.drain()
    assert fake.unsubscribe_calls == []  # 0 s after the latest removal
    assert "US.MSFT" in fake.subscribed

    clock.advance(59)  # t=1320: still inside the renewed grace
    await svc.tick_unsubscribes_once()
    await svc.drain()
    assert fake.unsubscribe_calls == []
    clock.advance(2)  # t=1322 > 1261 + 60
    await svc.tick_unsubscribes_once()
    await svc.drain()
    assert fake.unsubscribe_calls == ["US.MSFT"]
    assert not svc.admission.state("MSFT").subscribed


@pytest.mark.asyncio
async def test_stale_split_verify_cannot_overwrite_current_session():
    """R235-4: a session-A split-verify call that completes after a
    reconnect must not overwrite the session-B verification."""
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    release_a = asyncio.Event()

    async def snap_hook(n, codes):
        if n == 1:
            raise OpenDProviderError("batch failed")  # session-A batch → split
        if n == 2:
            await release_a.wait()  # session-A singleton parked
            raise OpenDNotFoundError("late session-A not found")

    fake.hooks["get_market_snapshot"] = snap_hook
    svc = await _settled_service(fake)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL", "MSFT"])
    await _until(lambda: fake.entered.get("get_market_snapshot", 0) >= 2)

    fake.drop()
    await fake.connect()
    rec = asyncio.create_task(svc.opend_recovered())  # session B
    await svc.drain(timeout=0.05)
    release_a.set()
    await rec
    await svc.drain()

    assert svc.admission.state("AAPL").verify == "ok"
    last = ws.last("assign")
    assert sorted(last["ok"]) == ["AAPL", "MSFT"]
    assert last["rejected"] == []


@pytest.mark.asyncio
async def test_stale_op_cannot_clear_replacement_reservation():
    """R235-4: an old-session subscribe that resumes after the reconnect
    must not clear the replacement session's reservation (no extra
    admission)."""
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    gate_a, gate_b = asyncio.Event(), asyncio.Event()

    async def sub_hook(n, codes):
        if n == 1:
            await gate_a.wait()
        elif n == 2:
            await gate_b.wait()

    fake.hooks["subscribe"] = sub_hook
    svc = await _settled_service(fake)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await _until(lambda: fake.entered.get("subscribe", 0) >= 1)

    fake.drop()
    await fake.connect()
    rec = asyncio.create_task(svc.opend_recovered())
    await svc.drain(timeout=0.05)
    gate_a.set()  # the session-A op resumes
    await rec
    await _until(lambda: fake.entered.get("subscribe", 0) >= 2)  # B reserved + in flight
    for _ in range(3):
        await svc.tick_unsubscribes_once()
        await svc.drain(timeout=0.05)
    assert fake.entered["subscribe"] == 2  # no extra AAPL admission

    gate_b.set()
    await svc.drain()
    assert fake.entered["subscribe"] == 2
    assert svc.admission.is_ok("AAPL")


@pytest.mark.asyncio
async def test_stale_op_down_error_does_not_kill_new_session():
    """R235-4: an OpenDDownError from an old-session op must not mark the
    replacement session down."""
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    gate_a = asyncio.Event()

    async def sub_hook(n, codes):
        if n == 1:
            await gate_a.wait()
            raise OpenDDownError("session-A socket gone")

    fake.hooks["subscribe"] = sub_hook
    svc = await _settled_service(fake)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await _until(lambda: fake.entered.get("subscribe", 0) >= 1)

    fake.drop()
    await fake.connect()
    rec = asyncio.create_task(svc.opend_recovered())
    await svc.drain(timeout=0.05)
    gate_a.set()
    await rec
    await svc.drain()

    assert svc.admission.opend_connected
    assert svc.admission.is_ok("AAPL")
    assert ws.last("health")["opend_connected"] is True


@pytest.mark.asyncio
async def test_sdk_callbacks_from_replaced_context_are_dropped(monkeypatch):
    """R235-4: SDK push/lifecycle callbacks are fenced by their originating
    context. Late callbacks of a closed context never reach the service;
    a push received before a later disconnect is not delivered after it.
    The SDK socket layer is stubbed — no network."""
    import pandas as pd
    from futu import OpenQuoteContext, StockQuoteHandlerBase
    from futu.common.constant import RET_OK

    monkeypatch.setattr(opend_mod, "_probe_opend", lambda *a, **k: None)
    monkeypatch.setattr(OpenQuoteContext, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(
        OpenQuoteContext, "set_handler", lambda self, h: setattr(self, "_t_handler", h)
    )
    monkeypatch.setattr(OpenQuoteContext, "start", lambda self: None)
    monkeypatch.setattr(OpenQuoteContext, "close", lambda self: None)
    monkeypatch.setattr(OpenQuoteContext, "on_disconnect", lambda self, c, r, m: None)
    monkeypatch.setattr(OpenQuoteContext, "on_api_socket_reconnected", lambda self: (RET_OK, ""))
    monkeypatch.setattr(
        StockQuoteHandlerBase, "on_recv_rsp", lambda self, rsp: (RET_OK, pd.DataFrame(rsp))
    )

    od = opend_mod.FutuOpenD()
    rows: list = []
    downs: list = []
    ups: list = []
    od.on_quote_rows = rows.append
    od.on_disconnected = lambda: downs.append(1)
    od.on_reconnected = lambda: ups.append(1)

    async def settle() -> None:
        for _ in range(3):
            await asyncio.sleep(0)

    def seen() -> list:
        return [int(r[0]["n"]) for r in rows]

    await od.connect()
    ctx_a = od._ctx
    ctx_a._t_handler.on_recv_rsp([{"code": "US.AAPL", "n": 1}])
    await settle()
    assert seen() == [1]

    await od.close()
    await od.connect()  # context B replaces A
    ctx_b = od._ctx
    assert ctx_b is not ctx_a
    # Context A's SDK threads are still draining.
    ctx_a._t_handler.on_recv_rsp([{"code": "US.AAPL", "n": 2}])
    ctx_a.on_api_socket_reconnected()
    ctx_a.on_disconnect(1, None, "late")
    ctx_b._t_handler.on_recv_rsp([{"code": "US.MSFT", "n": 3}])
    await settle()
    assert seen() == [1, 3]
    assert downs == [] and ups == []

    # Same context: push read before a later disconnect is not delivered after it.
    ctx_b._t_handler.on_recv_rsp([{"code": "US.MSFT", "n": 4}])
    ctx_b.on_disconnect(2, None, "drop")
    await settle()
    assert seen() == [1, 3]
    assert downs == [1]


@pytest.mark.asyncio
async def test_reconnect_with_failed_quota_refresh_never_over_admits():
    """R235-5: a held symbol stays counted across reconnect even when the
    quota refresh fails; only its reconfirm is sent, never a new admission
    into its slot."""
    clock = Clock(1000.0)
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS, remain=1, own_used=0, total_used=0)
    svc = await _settled_service(fake, clock=clock, quota_cap=1)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain()
    assert svc.admission.is_ok("AAPL")
    fake.remain = 0
    await _want(svc, ws, 2, ["AAPL", "MSFT"])
    await svc.drain()
    assert {"symbol": "MSFT", "reason": "quota"} in ws.last("assign")["rejected"]

    clock.advance(300)  # MSFT's quota backoff long expired
    fake.quota_raise = OpenDProviderError("quota unavailable")
    fake.drop()
    await fake.connect()
    await svc.opend_recovered()
    await svc.drain()

    assert fake.subscribe_calls.count("US.MSFT") == 0
    assert fake.subscribe_calls.count("US.AAPL") == 2  # reconfirm only
    assert svc.admission.is_ok("AAPL")
    assert svc.admission.controlled_used() <= svc.admission.ceiling()
    last = ws.last("assign")
    assert last["ok"] == ["AAPL"]
    assert {"symbol": "MSFT", "reason": "quota"} in last["rejected"]


@pytest.mark.asyncio
async def test_failed_reconfirm_keeps_retry_exemption():
    """R235-5: a failed reconfirm keeps its intent — after backoff it is
    retried as a reconfirm, not rejected for quota at a truthful ceiling."""
    clock = Clock(1000.0)
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS, remain=1, own_used=0, total_used=0)
    svc = await _settled_service(fake, clock=clock, quota_cap=1)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain()
    assert svc.admission.is_ok("AAPL")

    fake.remain = 0  # truthful: own_used=1, remain=0 → ceiling 1
    fake.drop()
    await fake.connect()
    fake.subscribe_errors["US.AAPL"] = "provider internal failure"
    await svc.opend_recovered()
    await svc.drain()
    assert fake.subscribe_calls.count("US.AAPL") == 2  # failed reconfirm
    assert not svc.admission.is_ok("AAPL")

    clock.advance(10)  # past the 5 s backoff
    await svc.tick_unsubscribes_once()
    await svc.drain()
    assert fake.subscribe_calls.count("US.AAPL") == 3
    assert svc.admission.is_ok("AAPL")
    assert svc.admission.own_used() == 1


@pytest.mark.asyncio
async def test_buffered_tick_dropped_on_back_to_back_remove_readd():
    """R235-6: want(gen=2, []) then want(gen=3, [AAPL]) with no evaluation
    in between — the removal itself drops the buffered tick."""
    clock = Clock(1000.0)
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain()

    fake.push([_raw_row("US.AAPL", 123.45)])
    await _want(svc, ws, 2, [])
    await _want(svc, ws, 3, ["AAPL"])
    await svc.drain()
    assert svc.admission.is_ok("AAPL")
    clock.advance(0.2)
    await svc.flush_now()
    assert ws.frames("batch") == []


@pytest.mark.asyncio
async def test_generic_snapshot_error_is_retryable_not_unknown():
    """R235-7: only a definite not-found makes unknown_symbol. A generic
    provider error in a mixed batch isolates the good code and retries the
    failing one after backoff."""
    clock = Clock(1000.0)
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    fake.snapshot_errors["US.MSFT"] = OpenDProviderError("provider internal failure")
    svc = await _settled_service(fake, clock=clock)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL", "MSFT"])
    await svc.drain()
    last = ws.last("assign")
    assert last["ok"] == ["AAPL"]
    assert {"symbol": "MSFT", "reason": "unknown_symbol"} not in last["rejected"]
    assert {"symbol": "MSFT", "reason": "backoff"} in last["pending"]

    del fake.snapshot_errors["US.MSFT"]
    clock.advance(500)
    await svc.tick_unsubscribes_once()
    await svc.drain()
    assert sorted(ws.last("assign")["ok"]) == ["AAPL", "MSFT"]


# ---------------------------------------------------------------------------
# Astra round-3 regressions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lost",
    [
        pytest.param("Timeout", id="error-string"),  # real adapter: per-code string
        pytest.param(opend_mod.OpenDTransientError("timed out"), id="raised"),
    ],
)
async def test_lost_subscribe_reply_keeps_slot_held(lost):
    """R235-5: OpenD accepted AAPL but the reply was lost. AAPL stays
    believed held: MSFT is never admitted into its slot at cap 1, and AAPL
    is unsubscribed after the normal grace once demand moves away."""
    clock = Clock(1000.0)
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS, remain=5, own_used=0, total_used=0)
    fake.subscribe_lost_reply["US.AAPL"] = lost
    svc = await _settled_service(fake, clock=clock, quota_cap=1)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain()
    assert "US.AAPL" in fake.subscribed  # provider holds it
    assert not svc.admission.is_ok("AAPL")

    await _want(svc, ws, 2, ["MSFT"])  # demand moves before the retry
    await svc.drain()
    assert fake.subscribe_calls.count("US.MSFT") == 0
    assert {"symbol": "MSFT", "reason": "quota"} in ws.last("assign")["rejected"]
    assert svc.admission.controlled_used() <= svc.admission.ceiling()

    for _ in range(4):  # past lifetime + grace
        clock.advance(40)
        await svc.tick_unsubscribes_once()
        await svc.drain()
    assert fake.unsubscribe_calls == ["US.AAPL"]
    assert "US.AAPL" not in fake.subscribed
    clock.advance(200)  # MSFT's quota backoff expires; the slot is free
    await svc.tick_unsubscribes_once()
    await svc.drain()
    assert svc.admission.is_ok("MSFT")
    assert fake.subscribed == {"US.MSFT"}


@pytest.mark.asyncio
async def test_late_old_session_success_keeps_ownership():
    """R235-5: a session-A subscribe that succeeds after the reconnect must
    not certify session B, but its provider-held slot stays owned: AAPL is
    reconfirmed (no second slot) instead of being stuck at rejected/quota."""
    clock = Clock(1000.0)
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS, remain=1, own_used=0, total_used=0)
    gate_a = asyncio.Event()

    async def sub_hook(n, codes):
        if n == 1:
            await gate_a.wait()

    fake.hooks["subscribe"] = sub_hook
    svc = await _settled_service(fake, clock=clock, quota_cap=1)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await _until(lambda: fake.entered.get("subscribe", 0) >= 1)

    fake.drop()
    await fake.connect()
    fake.remain = 0  # truthful after AAPL lands: own_used=1, remain=0
    rec = asyncio.create_task(svc.opend_recovered())
    await svc.drain(timeout=0.05)
    gate_a.set()  # old-session success; OpenD retains AAPL
    await rec
    await svc.drain()

    clock.advance(500)
    await svc.refresh_quota_once()
    await svc.tick_unsubscribes_once()
    await svc.drain()
    assert svc.admission.is_ok("AAPL")
    assert fake.subscribe_calls.count("US.AAPL") == 2  # original + reconfirm
    assert svc.admission.own_used() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "not_subscribed"])
async def test_unsub_release_drops_buffer_before_resubscribe(outcome):
    """R235-6: an in-flight unsubscribe completes after a re-add. Releasing
    the subscription drops its buffered tick at once, so the resubscribe
    cannot flush a quote from the released subscription."""
    clock = Clock(1000.0)
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL", "MSFT"])
    await svc.drain()
    clock.advance(200)
    await _want(svc, ws, 2, ["MSFT"])  # AAPL unwanted at t=1200
    await svc.drain()
    clock.advance(61)  # t=1261: due

    reply = asyncio.Event()

    async def unsub_hook(n, codes):
        await reply.wait()  # past the pre-check: the call is at OpenD
        if outcome == "not_subscribed":
            fake.subscribed.discard("US.AAPL")

    fake.hooks["unsubscribe"] = unsub_hook
    await svc.tick_unsubscribes_once()
    await _until(lambda: fake.entered.get("unsubscribe", 0) >= 1)
    await _want(svc, ws, 3, ["AAPL", "MSFT"])  # re-add while in flight
    await svc.drain(timeout=0.05)
    fake.push([_raw_row("US.AAPL", 123.45)])
    reply.set()
    await svc.drain()
    assert fake.subscribe_calls.count("US.AAPL") == 2  # resubscribed
    assert svc.admission.is_ok("AAPL")
    clock.advance(0.2)
    await svc.flush_now()
    assert ws.frames("batch") == []
