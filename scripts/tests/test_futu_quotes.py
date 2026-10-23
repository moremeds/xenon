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
from xenon.futu_quotes.opend import (
    OpenDNotFoundError,
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
    ad.state("AAPL").verify_inflight = False
    res = ad.evaluate()
    assert res.partition["pending"] == [{"symbol": "AAPL", "reason": "session"}]


def test_admission_opend_down_pending():
    clock = Clock()
    ad = _admission(clock)
    ad.set_desired(1, ["AAPL"])
    ad.verify_result("AAPL", "ok")
    ad.state("AAPL").verify_inflight = False
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
        ad.state(s).verify_inflight = False
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
    ad.state("HYG").verify_inflight = False
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
    ad.state("HYG").verify_inflight = False
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
        ad.state(s).verify_inflight = False
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
        ad.state(s).verify_inflight = False
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
    ad.state("AAPL").verify_inflight = False
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
    ad.state("AAPL").verify_inflight = False
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
    ad.state("AAPL").verify_inflight = False
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
        self.unsubscribe_errors: list[str] = []
        self.unsubscribe_raise: Exception | None = None
        self.defer_all = False
        self._defer_results: dict[str, str | None] = {}
        self._defer_waiters: dict[str, asyncio.Future] = {}
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

    async def get_market_snapshot(self, codes: list[str]) -> list[dict]:
        self._require_conn()
        self.snapshot_calls += 1
        return [self.snapshots[c] for c in codes if c in self.snapshots and self.snapshots[c]]

    async def subscribe(self, codes: list[str]) -> dict:
        self._require_conn()
        out: dict = {}
        for c in codes:
            self.subscribe_calls.append(c)
            if self.defer_all or c in self._defer_waiters:
                loop = asyncio.get_running_loop()
                fut = self._defer_waiters.setdefault(c, loop.create_future())
                if c in self._defer_results:
                    fut.set_result(self._defer_results.pop(c))
                out[c] = await fut
                continue
            out[c] = self._do_subscribe(c)
        return out

    def _do_subscribe(self, c: str):
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
        """Resolve a deferred subscribe deterministically."""
        fut = self._defer_waiters.get(code)
        if fut is not None and not fut.done():
            if err is None:
                if self.sub_limit is None or len(self.subscribed) < self.sub_limit:
                    self.subscribed.add(code)
                    self.own_used = len(self.subscribed)
            fut.set_result(err)
        else:
            self._defer_results[code] = err

    async def unsubscribe(self, codes: list[str]) -> dict:
        self._require_conn()
        out: dict = {}
        for c in codes:
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
        self._require_conn()
        self.quota_calls += 1
        return {
            "total_used": self.total_used,
            "own_used": self.own_used,
            "remain": self.remain,
            "sub_list": {"QUOTE": sorted(self.subscribed)},
        }

    async def get_global_state(self) -> dict:
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

    for _ in range(200):
        if {"US.AAPL", "US.MSFT"} <= set(fake.subscribe_calls):
            break
        await asyncio.sleep(0)
    assert "US.MSFT" in fake.subscribe_calls

    await _want(svc, ws, 2, ["AAPL"])  # MSFT unwanted while subscribe in flight
    await asyncio.sleep(0)

    fake.complete_subscribe("US.AAPL")
    fake.complete_subscribe("US.MSFT")  # late completion: really did subscribe
    await svc.drain()

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
