from scripts.uw_scan_lib.models import SignalHit, ScanCandidate


def test_signal_hit_basic():
    h = SignalHit(
        ticker="TSLA", signal_type="deep_conviction_flow", tier=1, score=0.8,
        evidence={"premium": 2_000_000},
    )
    assert h.ticker == "TSLA"
    assert h.tier == 1


def test_scan_candidate_is_type_f_default_false():
    c = ScanCandidate(
        ticker="TSLA", hits=[], context_flags=[],
        raw_score=0.0, confluence_score=0.0, final_score=0.0,
        is_type_f=False,
    )
    assert c.is_type_f is False
