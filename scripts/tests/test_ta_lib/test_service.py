"""Integration tests for TAService with mocked Massive client, real in-memory DuckDB."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest


def _make_massive_df(n: int = 260, start: str = "2025-05-01") -> pd.DataFrame:
    """Create a Massive-shaped DataFrame (date tz-aware ET, with VWAP + tx_count)
    that fetch_bars will reduce to the store columns."""
    np.random.seed(42)
    rows = []
    start_ts = pd.Timestamp(start, tz="America/New_York")
    base = 100.0
    for i in range(n):
        # Business-day increments keep weekends out.
        current = start_ts + pd.tseries.offsets.BDay(i)
        close = base + i * 0.1 + np.random.randn() * 0.3
        rows.append(
            {
                "date": current,
                "open": close - 0.1,
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
                "volume": 1_000_000,
                "vwap": close,
                "tx_count": 10_000,
            }
        )
    return pd.DataFrame(rows)


def _massive_mock_ok() -> MagicMock:
    """A Massive client whose get_aggregates returns a minimal one-row frame."""
    mc = MagicMock()
    mc.get_aggregates.return_value = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2026-04-09", tz="America/New_York"),
                "open": 170.0,
                "high": 172.0,
                "low": 169.5,
                "close": 171.5,
                "volume": 50_000_000,
                "vwap": 170.8,
                "tx_count": 450_000,
            }
        ]
    )
    return mc


@pytest.fixture
def mock_massive():
    mc = MagicMock()
    mc.get_aggregates.return_value = _make_massive_df(n=260)
    return mc


@pytest.fixture
def ta_service(mock_massive):
    from scripts.ta_lib.service import TAService

    svc = TAService(db_path=":memory:", massive_client=mock_massive)
    return svc


class TestGetIndicators:
    def test_cache_miss_fetches_from_massive(self, ta_service, mock_massive):
        result = ta_service.get_indicators("AAPL")
        assert isinstance(result, pd.DataFrame)
        assert len(result) > 0
        assert "sma_20" in result.columns
        assert "rsi_14" in result.columns
        mock_massive.get_aggregates.assert_called_once()

    def test_cache_hit_skips_massive(self, ta_service, mock_massive):
        # First call - cache miss
        ta_service.get_indicators("AAPL")
        mock_massive.get_aggregates.reset_mock()

        # Patch freshness to say cache is current
        with patch.object(ta_service, "_is_stale", return_value=False):
            ta_service.get_indicators("AAPL")
        mock_massive.get_aggregates.assert_not_called()

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
        """OHLC exists but indicators were deleted -> stale (partial cache)."""
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
    def test_split_triggers_full_refetch(self, mock_massive):
        from scripts.ta_lib.service import TAService

        svc = TAService(db_path=":memory:", massive_client=mock_massive)
        # First fetch - populates cache
        svc.get_indicators("AAPL")
        mock_massive.get_aggregates.reset_mock()

        # Simulate a split: Massive returns bars at half price for dates past
        # the cached series. We use a start far beyond the seeded 260 business
        # days to guarantee the new bars are "after" the latest cached date.
        split_df = _make_massive_df(n=5, start="2027-05-01")
        for col in ("open", "high", "low", "close"):
            split_df[col] = split_df[col] / 2
        mock_massive.get_aggregates.return_value = split_df

        # Force stale
        with patch.object(svc, "_is_stale", return_value=True):
            svc.get_indicators("AAPL")
            # Should be exactly 2 calls: first incremental (detects gap),
            # then full re-fetch (cold lookback)
            assert mock_massive.get_aggregates.call_count == 2
            # Verify the second call used cold lookback (365 for 1d)
            second_call = mock_massive.get_aggregates.call_args_list[1]
            # from_date should be ~365 + overlap days before to_date
            from_date = second_call.kwargs["from_date"]
            to_date = second_call.kwargs["to_date"]
            span = (
                datetime.strptime(to_date, "%Y-%m-%d").date() - datetime.strptime(from_date, "%Y-%m-%d").date()
            ).days
            assert span >= 365  # cold lookback


class TestBulkRefreshResult:
    def test_returns_counts_on_success(self, tmp_path):
        from scripts.ta_lib.service import TAService

        mc = _massive_mock_ok()
        svc = TAService(db_path=str(tmp_path / "ta.duckdb"), massive_client=mc)
        with (
            patch.object(svc, "_is_stale", return_value=True),
            patch.object(svc, "_refresh"),
        ):
            result = svc.bulk_refresh(["AAPL", "SPY"], timeframe="1d")
        assert result.attempted == 2
        assert result.succeeded == 2
        assert result.failed == 0
        assert result.failure_ratio == 0.0

    def test_failure_ratio_on_partial(self, tmp_path):
        from scripts.ta_lib.service import TAService

        mc = _massive_mock_ok()
        svc = TAService(db_path=str(tmp_path / "ta.duckdb"), massive_client=mc)

        def _refresh_side_effect(ticker, tf):
            if ticker == "BAD":
                raise RuntimeError("simulated")

        with (
            patch.object(svc, "_is_stale", return_value=True),
            patch.object(svc, "_refresh", side_effect=_refresh_side_effect),
        ):
            result = svc.bulk_refresh(["AAPL", "BAD", "SPY"], timeframe="1d")
        assert result.attempted == 3
        assert result.succeeded == 2
        assert result.failed == 1
        assert result.failed_tickers == ["BAD"]
        assert 0.33 < result.failure_ratio < 0.34

    def test_loops_both_timeframes_when_none(self, tmp_path):
        from scripts.ta_lib.service import TAService

        mc = _massive_mock_ok()
        svc = TAService(db_path=str(tmp_path / "ta.duckdb"), massive_client=mc)
        seen: list[str] = []

        def _refresh(ticker, tf):
            seen.append(tf)

        with (
            patch.object(svc, "_is_stale", return_value=True),
            patch.object(svc, "_refresh", side_effect=_refresh),
        ):
            svc.bulk_refresh(["AAPL"])  # no timeframe - loops both
        assert set(seen) == {"1d", "1h"}


class TestBulkRefresh:
    def test_refreshes_stale_tickers(self, ta_service, mock_massive):
        with patch.object(
            ta_service,
            "_is_stale",
            side_effect=lambda t, tf, cursor=None: t in ("AAPL", "MSFT"),
        ):
            ta_service.bulk_refresh(["AAPL", "MSFT", "GOOG"], timeframe="1d")
        # AAPL and MSFT refreshed, GOOG skipped
        assert mock_massive.get_aggregates.call_count == 2

    def test_all_current_skips(self, ta_service, mock_massive):
        with patch.object(ta_service, "_is_stale", return_value=False):
            ta_service.bulk_refresh(["AAPL", "MSFT"], timeframe="1d")
        mock_massive.get_aggregates.assert_not_called()

    def test_no_client_returns_empty_result(self, tmp_path):
        from scripts.ta_lib.service import BulkRefreshResult, TAService

        svc = TAService(db_path=str(tmp_path / "ta.duckdb"), massive_client=None)
        result = svc.bulk_refresh(["AAPL"], timeframe="1d")
        assert isinstance(result, BulkRefreshResult)
        assert result.attempted == 0
        assert result.succeeded == 0
        assert result.failed == 0


class TestHourlyTimeframe:
    def test_1h_fetches_with_hourly_timeframe(self, mock_massive):
        from scripts.ta_lib.service import TAService

        svc = TAService(db_path=":memory:", massive_client=mock_massive)
        svc.get_indicators("AAPL", timeframe="1h")
        call_kwargs = mock_massive.get_aggregates.call_args.kwargs
        assert call_kwargs["timeframe"] == "1h"
        # Cold start -> 30 calendar days + 5 overlap
        from_date = call_kwargs["from_date"]
        to_date = call_kwargs["to_date"]
        span = (datetime.strptime(to_date, "%Y-%m-%d").date() - datetime.strptime(from_date, "%Y-%m-%d").date()).days
        assert span == 35  # 30 + 5

    def test_unsupported_timeframe_raises(self, mock_massive):
        from scripts.ta_lib.service import TAService

        svc = TAService(db_path=":memory:", massive_client=mock_massive)
        with pytest.raises(ValueError, match="Unsupported timeframe"):
            svc.get_indicators("AAPL", timeframe="15m")

    def test_1h_snapshot_works(self, mock_massive):
        from scripts.ta_lib.service import TAService

        svc = TAService(db_path=":memory:", massive_client=mock_massive)
        snap = svc.get_snapshot("AAPL", timeframe="1h")
        assert snap["ticker"] == "AAPL"
        assert snap["close"] > 0


class TestIsStale1hNewContract:
    @staticmethod
    def _make_svc_with_row(tmp_path):
        from scripts.ta_lib.service import TAService
        from scripts.ta_lib.store import get_connection, init_schema, write_ohlc

        db = tmp_path / "ta.duckdb"
        conn = get_connection(str(db))
        init_schema(conn)
        row = pd.DataFrame(
            [
                {
                    "date": pd.Timestamp("2026-04-08 15:30", tz="America/New_York"),
                    "open": 1,
                    "high": 1,
                    "low": 1,
                    "close": 1,
                    "volume": 1,
                }
            ]
        )
        write_ohlc(conn, "AAPL", "1h", row)
        conn.commit()
        svc = TAService(db_path=str(db), massive_client=MagicMock())
        return svc

    def test_fresh_when_latest_is_prior_1530(self, tmp_path, monkeypatch):
        from datetime import datetime

        monkeypatch.setattr(
            "scripts.ta_lib.service.get_last_n_trading_days",
            lambda n: ["2026-04-08", "2026-04-09"],
        )
        svc = self._make_svc_with_row(tmp_path)
        with patch(
            "scripts.ta_lib.service.read_indicators",
            return_value=pd.DataFrame({"x": [1]}),
        ):
            assert svc._is_stale("AAPL", "1h") is False

    def test_stale_when_multiple_days_behind(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "scripts.ta_lib.service.get_last_n_trading_days",
            lambda n: ["2026-04-14", "2026-04-15"],
        )
        svc = self._make_svc_with_row(tmp_path)
        with patch(
            "scripts.ta_lib.service.read_indicators",
            return_value=pd.DataFrame({"x": [1]}),
        ):
            assert svc._is_stale("AAPL", "1h") is True


class TestMassiveErrorHandling:
    def test_refresh_propagates_massive_error(self):
        from scripts.ta_lib.service import TAService

        mc = MagicMock()
        mc.get_aggregates.side_effect = RuntimeError("Massive 429 rate limit")

        svc = TAService(db_path=":memory:", massive_client=mc)
        with pytest.raises(RuntimeError, match="Massive"):
            svc.get_indicators("AAPL")

    def test_bulk_refresh_records_failures(self):
        from scripts.ta_lib.service import TAService

        mc = MagicMock()
        mc.get_aggregates.side_effect = RuntimeError("Massive 500")

        svc = TAService(db_path=":memory:", massive_client=mc)
        tickers = [f"TICK{i}" for i in range(3)]

        with patch.object(svc, "_is_stale", return_value=True):
            result = svc.bulk_refresh(tickers, timeframe="1d")

        assert result.attempted == 3
        assert result.succeeded == 0
        assert result.failed == 3
        assert set(result.failed_tickers) == set(tickers)
