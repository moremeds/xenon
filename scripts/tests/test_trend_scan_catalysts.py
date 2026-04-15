"""Stage C catalyst stage tests."""

from unittest.mock import MagicMock


def test_catalyst_stage_degrades_gracefully_without_uw_client():
    """If UW client is None (unavailable / budget exhausted), stage returns
    empty catalysts and neutral score — never raises."""
    from scripts.trend_scan_lib.stages.catalysts import fetch_catalysts

    catalysts, score = fetch_catalysts(
        ticker="AAPL",
        direction="bullish",
        uw_client=None,
        earnings_days=30,
    )
    assert catalysts == []
    assert score == 0.5


def test_catalyst_stage_flags_imminent_earnings():
    """Earnings within 7 days is always a catalyst (direction-agnostic —
    creates event risk either way). Scored as neutral (0.5) since we
    don't predict direction of the move."""
    from scripts.trend_scan_lib.stages.catalysts import fetch_catalysts

    catalysts, score = fetch_catalysts(
        ticker="AAPL",
        direction="bullish",
        uw_client=None,
        earnings_days=3,
    )
    assert "earnings_within_7d" in catalysts
    assert score == 0.5


def test_catalyst_stage_rewards_bullish_aligned_headlines():
    from scripts.trend_scan_lib.stages.catalysts import fetch_catalysts

    fake_uw = MagicMock()
    fake_uw.get_headlines.return_value = [
        {"type": "analyst_upgrade", "ts": "2026-04-14T09:00:00Z"},
        {"type": "guidance_raise", "ts": "2026-04-14T08:00:00Z"},
    ]

    catalysts, score = fetch_catalysts(
        ticker="AAPL",
        direction="bullish",
        uw_client=fake_uw,
        earnings_days=30,
    )
    assert "analyst_upgrade" in catalysts
    assert "guidance_raise" in catalysts
    assert score > 0.6


def test_catalyst_stage_penalizes_bullish_against_bearish_headlines():
    from scripts.trend_scan_lib.stages.catalysts import fetch_catalysts

    fake_uw = MagicMock()
    fake_uw.get_headlines.return_value = [
        {"type": "analyst_downgrade", "ts": "2026-04-14T09:00:00Z"},
    ]

    catalysts, score = fetch_catalysts(
        ticker="AAPL",
        direction="bullish",
        uw_client=fake_uw,
        earnings_days=30,
    )
    assert "analyst_downgrade" in catalysts
    assert score < 0.4
