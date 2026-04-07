from scripts.uw_scan_lib.models import SignalHit
from scripts.uw_scan_lib.confluence import compute_confluence, is_type_f


def _hit(sig_type, tier, score=0.8):
    return SignalHit(ticker="X", signal_type=sig_type, tier=tier, score=score, evidence={})


def test_type_f_requires_two_independent_hits():
    hits = [_hit("deep_conviction_flow", 1), _hit("gex_pinning", 1)]
    assert is_type_f(hits) is True


def test_same_signal_type_twice_is_not_type_f():
    hits = [_hit("deep_conviction_flow", 1), _hit("deep_conviction_flow", 1)]
    assert is_type_f(hits) is False


def test_dark_pool_alone_is_not_type_f():
    assert is_type_f([_hit("dark_pool_accumulation", 2)]) is False


def test_dark_pool_plus_tier1_is_NOT_type_f():
    hits = [_hit("deep_conviction_flow", 1), _hit("dark_pool_accumulation", 2)]
    assert is_type_f(hits) is False


def test_type_f_requires_two_non_darkpool_signals():
    hits = [
        _hit("deep_conviction_flow", 1),
        _hit("gex_pinning", 1),
        _hit("dark_pool_accumulation", 2),
    ]
    assert is_type_f(hits) is True


def test_confluence_score_includes_dark_pool():
    hits = [_hit("deep_conviction_flow", 1), _hit("dark_pool_accumulation", 2)]
    assert compute_confluence(hits) == 4.5
