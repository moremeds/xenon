"""FutuApiStats singleton + record/snapshot tests."""

from xenon.utils.futu_api_stats import FutuApiStats


def test_singleton():
    a = FutuApiStats.instance()
    b = FutuApiStats.instance()
    assert a is b


def test_record_and_snapshot():
    stats = FutuApiStats.instance()
    stats.reset()
    stats.record_call("history_deal_list_query", latency_s=0.42, ok=True)
    stats.record_call("history_deal_list_query", latency_s=0.50, ok=True)
    stats.record_call(
        "history_deal_list_query",
        latency_s=1.0,
        ok=False,
        error_code="rate_limit",
    )
    snap = stats.snapshot()
    fn = snap["history_deal_list_query"]
    assert fn["calls"] == 3
    assert fn["errors"] == 1
    assert abs(fn["mean_latency_s"] - 0.64) < 0.01
    assert fn["error_codes"]["rate_limit"] == 1


def test_reset_clears_state():
    stats = FutuApiStats.instance()
    stats.record_call("foo", latency_s=0.1, ok=True)
    stats.reset()
    assert stats.snapshot() == {}
