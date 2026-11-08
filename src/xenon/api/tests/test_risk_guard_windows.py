"""Risk guard window returns (spec 2026-11-08 §5.1). Prices are synthetic."""

import pytest

from xenon.api.services.risk_guard.windows import PriceWindow, day_return


def _feed(w: PriceWindow, start: float, prices: list[float], step: float = 1.0, session: str = "rth") -> float:
    t = start
    for p in prices:
        w.add(t, p, session)
        t += step
    return t - step


def test_return_over_full_window():
    w = PriceWindow()
    # 301 one-second ticks: 100 -> 99 linearly; 5 min return = -1%.
    last = _feed(w, 1000.0, [100 - i / 300 for i in range(301)])
    assert w.ret(last, 300) == pytest.approx(-0.01)


def test_warm_up_is_unavailable_not_zero():
    w = PriceWindow()
    last = _feed(w, 1000.0, [100.0] * 200)
    assert w.ret(last, 300) is None
    assert w.ret(last, 60) == 0.0


def test_tolerance_two_seconds():
    w = PriceWindow()
    last = _feed(w, 1000.0, [100.0] * 299)  # covers 1000..1298
    assert w.ret(last + 1, 300) is not None  # target 999 is within 2 s of the first sample
    w2 = PriceWindow()
    last2 = _feed(w2, 1000.0, [100.0] * 297)  # covers 1000..1296
    assert w2.ret(last2, 300) is None  # target 996, first sample 4 s later


def test_carry_forward_between_sparse_ticks():
    w = PriceWindow()
    # A tick every 10 s (unchanged prints are not sent): the grid carries the last value.
    last = _feed(w, 1000.0, [100.0] + [99.0] * 30, step=10.0)
    assert w.ret(last, 300) == pytest.approx(-0.01)


def test_reconnect_gap_resets_window_but_keeps_session_low():
    w = PriceWindow()
    _feed(w, 1000.0, [100.0, 98.0, 99.0])
    w.add(1030.0, 99.5, "rth")  # 28 s gap > 20 s
    assert w.ret(1030.0, 15) is None
    assert w.low == 98.0


def test_session_change_resets_everything():
    w = PriceWindow()
    _feed(w, 1000.0, [100.0] * 400, session="extended")
    w.add(1400.0, 101.0, "rth")
    assert w.ret(1400.0, 300) is None
    assert w.low == 101.0 and w.high == 101.0


def test_stale_series_is_unavailable():
    w = PriceWindow()
    last = _feed(w, 1000.0, [100.0] * 400)
    assert w.ret(last + 21, 300) is None


def test_out_of_order_tick_ignored():
    w = PriceWindow()
    _feed(w, 1000.0, [100.0, 101.0])
    w.add(999.0, 50.0, "rth")
    assert w.last == 101.0 and w.low == 100.0


def test_memory_is_bounded():
    w = PriceWindow(max_window_s=60)
    _feed(w, 0.0, [100.0] * 5000)
    assert len(w._samples) <= 64


def test_high_low_track_session():
    w = PriceWindow()
    _feed(w, 1000.0, [100.0, 103.0, 97.0, 99.0])
    assert (w.high, w.low, w.last) == (103.0, 97.0, 99.0)


def test_day_return():
    assert day_return(97.0, 100.0) == pytest.approx(-0.03)
    assert day_return(None, 100.0) is None
    assert day_return(97.0, None) is None
    assert day_return(97.0, 0.0) is None
