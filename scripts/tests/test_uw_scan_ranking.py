from scripts.scanners.uw.models import SignalHit, ContextFlag, ScanCandidate
from scripts.scanners.uw.ranking import build_candidate, rank_candidates, RAW_RANKING_EXCLUDE


def _hit(ticker, sig_type, tier, score=0.8):
    return SignalHit(ticker=ticker, signal_type=sig_type, tier=tier, score=score, evidence={})


def _ctx(ticker, label="Elevated Fear", value=1.3):
    return ContextFlag(ticker=ticker, layer="pcr_sentiment", label=label, value=value)


def test_dark_pool_excluded_from_raw_score():
    assert "dark_pool_accumulation" in RAW_RANKING_EXCLUDE


def test_build_candidate_raw_score_excludes_dark_pool():
    hits = [
        _hit("T", "deep_conviction_flow", 1, score=1.0),
        _hit("T", "dark_pool_accumulation", 2, score=1.0),
    ]
    c = build_candidate("T", hits, [])
    assert c is not None
    assert c.raw_score == 3.0
    assert c.confluence_score == 4.5
    assert c.is_type_f is False
    assert c.final_score == 7.5


def test_build_candidate_returns_none_when_only_dark_pool():
    hits = [_hit("T", "dark_pool_accumulation", 2, score=1.0)]
    assert build_candidate("T", hits, []) is None


def test_context_flags_do_not_affect_final_score():
    hits = [_hit("T", "gex_pinning", 1, score=1.0)]
    c_no_ctx = build_candidate("T", hits, [])
    c_with_ctx = build_candidate("T", hits, [_ctx("T")])
    assert c_no_ctx.final_score == c_with_ctx.final_score


def test_rank_type_f_first_then_by_final_score():
    a = ScanCandidate("A", [], [], 100.0, 0.0, 100.0, is_type_f=False)
    b = ScanCandidate("B", [], [], 5.0, 0.0, 5.0, is_type_f=True)
    c = ScanCandidate("C", [], [], 10.0, 0.0, 10.0, is_type_f=True)
    ranked = rank_candidates([a, b, c])
    assert [r.ticker for r in ranked] == ["C", "B", "A"]


def test_rank_ticker_asc_tiebreak():
    x = ScanCandidate("XYZ", [], [], 10.0, 0.0, 10.0, is_type_f=False)
    a = ScanCandidate("ABC", [], [], 10.0, 0.0, 10.0, is_type_f=False)
    ranked = rank_candidates([x, a])
    assert [r.ticker for r in ranked] == ["ABC", "XYZ"]
