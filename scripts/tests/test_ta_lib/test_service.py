"""Integration tests for TAService with mocked IB, real in-memory DuckDB."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest


def _make_bar_data(n: int = 260, start_date: str = "20250501") -> list:
    """Create mock BarData with a gentle uptrend."""
    np.random.seed(42)
    bars = []
    base = 100.0
    dt = datetime.strptime(start_date, "%Y%m%d")
    for i in range(n):
        # Skip weekends
        current = dt.replace(day=1) + pd.tseries.offsets.BDay(i)
        close = base + i * 0.1 + np.random.randn() * 0.3
        bars.append(
            SimpleNamespace(
                date=current.strftime("%Y%m%d"),
                open=close - 0.1,
                high=close + 0.5,
                low=close - 0.5,
                close=close,
                volume=1_000_000,
            )
        )
    return bars


@pytest.fixture
def mock_ib():
    ib = MagicMock()
    ib.get_historical_data.return_value = _make_bar_data(n=260)
    ib._ib = MagicMock()
    ib._ib.qualifyContracts.return_value = [MagicMock()]
    return ib


@pytest.fixture
def ta_service(mock_ib):
    from scripts.ta_lib.service import TAService

    svc = TAService(db_path=":memory:", ib_client=mock_ib)
    return svc


class TestGetIndicators:
    def test_cache_miss_fetches_from_ib(self, ta_service, mock_ib):
        result = ta_service.get_indicators("AAPL")
        assert isinstance(result, pd.DataFrame)
        assert len(result) > 0
        assert "sma_20" in result.columns
        assert "rsi_14" in result.columns
        mock_ib.get_historical_data.assert_called_once()

    def test_cache_hit_skips_ib(self, ta_service, mock_ib):
        # First call — cache miss
        ta_service.get_indicators("AAPL")
        mock_ib.get_historical_data.reset_mock()

        # Patch freshness to say cache is current
        with patch.object(ta_service, "_is_stale", return_value=False):
            ta_service.get_indicators("AAPL")
        mock_ib.get_historical_data.assert_not_called()

    def test_returns_ohlc_plus_indicators(self, ta_service):
        result = ta_service.get_indicators("AAPL")
        expected_cols = {
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "sma_20",
            "sma_50",
            "sma_200",
            "rsi_14",
            "macd",
            "macd_signal",
            "macd_histogram",
            "adx_14",
            "bb_upper",
            "bb_middle",
            "bb_lower",
            "bb_width",
            "atr_14",
        }
        assert expected_cols.issubset(set(result.columns))


class TestGetSnapshot:
    def test_returns_dict_with_mapped_keys(self, ta_service):
        result = ta_service.get_snapshot("AAPL")
        assert isinstance(result, dict)
        # Check mapped field names (not DB names)
        assert "ma_20" in result
        assert "rsi" in result
        assert "adx" in result
        assert "bbw" in result
        assert "atr_pct" in result
        assert "ticker" in result
        assert result["ticker"] == "AAPL"

    def test_snapshot_has_derived_fields(self, ta_service):
        result = ta_service.get_snapshot("AAPL")
        assert "ma_20_series" in result
        assert isinstance(result["ma_20_series"], list)
        assert len(result["ma_20_series"]) <= 5
        assert "recent_avg_volume" in result
        assert "avg_20d_volume" in result
        assert "recent_up_ratio" in result
        assert "high_52w" in result
        assert "range_20d_pct" in result
        assert "dollar_volume" in result
        assert "price" in result

    def test_price_equals_close(self, ta_service):
        result = ta_service.get_snapshot("AAPL")
        assert result["price"] == result["close"]

    def test_snapshot_does_not_include_market_cap(self, ta_service):
        result = ta_service.get_snapshot("AAPL")
        assert "market_cap" not in result


class TestStaleness:
    def test_stale_when_no_data(self, ta_service):
        assert ta_service._is_stale("NEWSTOCK", "1d") is True

    @patch("scripts.ta_lib.service.get_last_n_trading_days")
    def test_not_stale_when_current(self, mock_cal, ta_service):
        mock_cal.return_value = ["2026-04-10"]
        # Populate cache with data
        ta_service.get_indicators("AAPL")
        # After fetch, cache should be current
        result = ta_service._is_stale("AAPL", "1d")
        assert result is False, "Cache should not be stale after fresh fetch"

    @patch("scripts.ta_lib.service.get_last_n_trading_days")
    def test_stale_when_indicators_missing(self, mock_cal, ta_service):
        """OHLC exists but indicators were deleted → stale (partial cache)."""
        mock_cal.return_value = ["2026-04-10"]
        ta_service.get_indicators("AAPL")
        # Delete indicators but keep OHLC
        ta_service._conn.execute("DELETE FROM ta_indicators WHERE ticker = 'AAPL'")
        result = ta_service._is_stale("AAPL", "1d")
        assert result is True, "Missing indicators should be treated as stale"

    def test_allow_fetch_false_raises_on_miss(self, ta_service):
        with pytest.raises(RuntimeError, match="allow_fetch=False"):
            ta_service.get_snapshot("NEWSTOCK", allow_fetch=False)


class TestSplitDetection:
    def test_split_triggers_full_refetch(self, mock_ib):
        from scripts.ta_lib.service import TAService

        svc = TAService(db_path=":memory:", ib_client=mock_ib)
        # First fetch — populates cache
        svc.get_indicators("AAPL")
        mock_ib.get_historical_data.reset_mock()

        # Simulate split: next fetch returns bars at half price
        split_bars = _make_bar_data(n=5, start_date="20260501")
        for bar in split_bars:
            bar.close = bar.close / 2
            bar.open = bar.open / 2
            bar.high = bar.high / 2
            bar.low = bar.low / 2
        mock_ib.get_historical_data.return_value = split_bars

        # Force stale
        with patch.object(svc, "_is_stale", return_value=True):
            svc.get_indicators("AAPL")
            # Should be exactly 2 calls: first incremental (detects gap),
            # then full re-fetch (1 Y)
            assert mock_ib.get_historical_data.call_count == 2
            # Verify the second call used "1 Y" duration
            second_call = mock_ib.get_historical_data.call_args_list[1]
            assert second_call.kwargs.get("duration") == "1 Y" or "1 Y" in str(second_call)


class TestBulkRefresh:
    def test_refreshes_stale_tickers(self, ta_service, mock_ib):
        with patch.object(ta_service, "_is_stale", side_effect=lambda t, tf, cursor=None: t in ("AAPL", "MSFT")):
            ta_service.bulk_refresh(["AAPL", "MSFT", "GOOG"])
        # AAPL and MSFT refreshed, GOOG skipped
        assert mock_ib.get_historical_data.call_count == 2

    def test_all_current_skips(self, ta_service, mock_ib):
        with patch.object(ta_service, "_is_stale", return_value=False):
            ta_service.bulk_refresh(["AAPL", "MSFT"])
        mock_ib.get_historical_data.assert_not_called()


class TestHourlyTimeframe:
    def test_1h_fetches_with_hourly_bar_size(self, mock_ib):
        from scripts.ta_lib.service import TAService

        svc = TAService(db_path=":memory:", ib_client=mock_ib)
        svc.get_indicators("AAPL", timeframe="1h")
        call_kwargs = mock_ib.get_historical_data.call_args.kwargs
        assert call_kwargs["bar_size"] == "1 hour"
        assert call_kwargs["duration"] == "1 M"

    def test_unsupported_timeframe_raises(self, mock_ib):
        from scripts.ta_lib.service import TAService

        svc = TAService(db_path=":memory:", ib_client=mock_ib)
        with pytest.raises(ValueError, match="Unsupported timeframe"):
            svc.get_indicators("AAPL", timeframe="15m")

    def test_1h_snapshot_works(self, mock_ib):
        from scripts.ta_lib.service import TAService

        svc = TAService(db_path=":memory:", ib_client=mock_ib)
        snap = svc.get_snapshot("AAPL", timeframe="1h")
        assert snap["ticker"] == "AAPL"
        assert snap["close"] > 0


class TestIBErrorHandling:
    def test_invalid_contract_raises(self):
        from scripts.ta_lib.service import TAService

        mock_ib = MagicMock()
        mock_ib._ib = MagicMock()
        mock_ib._ib.qualifyContracts.return_value = []

        svc = TAService(db_path=":memory:", ib_client=mock_ib)
        with pytest.raises(ValueError, match="qualify"):
            svc.get_indicators("INVALIDTICKER")

    def test_empty_response_raises(self):
        from scripts.ta_lib.service import TAService

        mock_ib = MagicMock()
        mock_ib._ib = MagicMock()
        mock_ib._ib.qualifyContracts.return_value = [MagicMock()]
        mock_ib.get_historical_data.return_value = []

        svc = TAService(db_path=":memory:", ib_client=mock_ib)
        with pytest.raises(RuntimeError, match="No historical data"):
            svc.get_indicators("DELISTED")

    def test_bulk_refresh_skips_after_5_failures(self):
        from scripts.ta_lib.service import TAService

        mock_ib = MagicMock()
        mock_ib._ib = MagicMock()
        mock_ib._ib.qualifyContracts.return_value = [MagicMock()]
        mock_ib.get_historical_data.side_effect = RuntimeError("IB pacing")

        svc = TAService(db_path=":memory:", ib_client=mock_ib)
        tickers = [f"TICK{i}" for i in range(10)]

        with patch.object(svc, "_is_stale", return_value=True):
            with patch("scripts.ta_lib.service.time.sleep"):
                svc.bulk_refresh(tickers)

        # Should have attempted 5 then stopped the batch
        assert mock_ib.get_historical_data.call_count == 5
