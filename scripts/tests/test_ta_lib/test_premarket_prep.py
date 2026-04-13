"""Tests for ta_premarket_prep — audit, refresh, and JSON output."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_project_root = str(Path(__file__).resolve().parent.parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from scripts.ta_premarket_prep import classify_tickers, main

# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture()
def mock_conn():
    return MagicMock()


@pytest.fixture()
def ref_date() -> date:
    return date(2026, 4, 10)  # a Friday


# ── classify_tickers ─────────────────────────────────────────────────────


def test_classify_current(mock_conn, ref_date):
    """Ticker with latest bar_date == ref_date is current."""
    with patch("scripts.ta_premarket_prep.get_latest_bar_date", return_value=ref_date):
        result = classify_tickers(mock_conn, ["AAPL"], ref_date)
    assert result == {"current": ["AAPL"], "stale": [], "missing": []}


def test_classify_stale(mock_conn, ref_date):
    """Ticker with latest bar_date < ref_date is stale."""
    with patch("scripts.ta_premarket_prep.get_latest_bar_date", return_value=date(2026, 4, 8)):
        result = classify_tickers(mock_conn, ["MSFT"], ref_date)
    assert result == {"current": [], "stale": ["MSFT"], "missing": []}


def test_classify_missing(mock_conn, ref_date):
    """Ticker with no bar data is missing."""
    with patch("scripts.ta_premarket_prep.get_latest_bar_date", return_value=None):
        result = classify_tickers(mock_conn, ["XYZ"], ref_date)
    assert result == {"current": [], "stale": [], "missing": ["XYZ"]}


def test_classify_mixed(mock_conn, ref_date):
    """Multiple tickers get classified correctly."""

    def fake_latest(conn, ticker, timeframe):
        return {"AAPL": ref_date, "MSFT": date(2026, 4, 7), "NEWCO": None}[ticker]

    with patch("scripts.ta_premarket_prep.get_latest_bar_date", side_effect=fake_latest):
        result = classify_tickers(mock_conn, ["AAPL", "MSFT", "NEWCO"], ref_date)
    assert result["current"] == ["AAPL"]
    assert result["stale"] == ["MSFT"]
    assert result["missing"] == ["NEWCO"]


# ── --audit-only ─────────────────────────────────────────────────────────


@patch("scripts.ta_premarket_prep.build_static_universe", return_value=["AAPL", "SPY"])
@patch("scripts.ta_premarket_prep.get_connection")
@patch("scripts.ta_premarket_prep.init_schema")
@patch("scripts.ta_premarket_prep._last_trading_date", return_value=date(2026, 4, 10))
@patch("scripts.ta_premarket_prep.get_latest_bar_date", return_value=date(2026, 4, 10))
def test_audit_only_never_creates_ib(mock_bar, mock_ltd, mock_init, mock_conn_fn, mock_universe, capsys):
    """--audit-only should print JSON and never import IBClient."""
    mock_conn_fn.return_value = MagicMock()

    with patch.dict(sys.modules, {"scripts.clients.ib_client": None}):
        with pytest.raises(SystemExit) as exc_info:
            main(["--audit-only", "--db", ":memory:"])
        assert exc_info.value.code == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "before" in data
    assert data["before"]["current"] == 2  # AAPL + SPY both current


# ── JSON output structure ────────────────────────────────────────────────


@patch("scripts.ta_premarket_prep.build_static_universe", return_value=["AAPL"])
@patch("scripts.ta_premarket_prep.get_connection")
@patch("scripts.ta_premarket_prep.init_schema")
@patch("scripts.ta_premarket_prep._last_trading_date", return_value=date(2026, 4, 10))
@patch("scripts.ta_premarket_prep.get_latest_bar_date", return_value=date(2026, 4, 9))
def test_json_output_structure(mock_bar, mock_ltd, mock_init, mock_conn_fn, mock_universe, capsys):
    """Full run with mocked IB produces the expected JSON keys."""
    mock_conn_fn.return_value = MagicMock()

    mock_ib_cls = MagicMock()
    mock_ib_inst = MagicMock()
    mock_ib_cls.return_value = mock_ib_inst

    mock_ta_cls = MagicMock()
    mock_ta_inst = MagicMock()
    mock_ta_cls.return_value = mock_ta_inst

    with (
        patch("scripts.ta_premarket_prep.get_latest_bar_date", return_value=date(2026, 4, 9)),
        patch.dict(
            sys.modules,
            {
                "scripts.clients.ib_client": MagicMock(IBClient=mock_ib_cls),
                "scripts.ta_lib.service": MagicMock(TAService=mock_ta_cls),
            },
        ),
    ):
        # After refresh, make bar_date current
        original = get_latest_bar_date_side = [date(2026, 4, 9)]  # noqa: F841

        # Re-patch for the post-refresh audit to show current
        def bar_after_refresh(conn, ticker, tf):
            return date(2026, 4, 10)

        with patch("scripts.ta_premarket_prep.get_latest_bar_date", side_effect=bar_after_refresh):
            # Need to re-patch classify for phase 3
            pass

        main(["--db", ":memory:"])

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "before" in data
    assert "after" in data
    assert "refreshed" in data
    assert "failed_tickers" in data
    assert "elapsed_s" in data
    assert isinstance(data["failed_tickers"], list)


# ── --force passes all tickers ───────────────────────────────────────────


@patch("scripts.ta_premarket_prep.build_static_universe", return_value=["AAPL", "MSFT"])
@patch("scripts.ta_premarket_prep.get_connection")
@patch("scripts.ta_premarket_prep.init_schema")
@patch("scripts.ta_premarket_prep._last_trading_date", return_value=date(2026, 4, 10))
@patch("scripts.ta_premarket_prep.get_latest_bar_date", return_value=date(2026, 4, 10))
def test_force_passes_all_tickers(mock_bar, mock_ltd, mock_init, mock_conn_fn, mock_universe, capsys):
    """--force should pass ALL tickers to bulk_refresh, even if current."""
    mock_conn_fn.return_value = MagicMock()

    mock_ib_cls = MagicMock()
    mock_ta_cls = MagicMock()
    mock_ta_inst = MagicMock()
    mock_ta_cls.return_value = mock_ta_inst

    with patch.dict(
        sys.modules,
        {
            "scripts.clients.ib_client": MagicMock(IBClient=mock_ib_cls),
            "scripts.ta_lib.service": MagicMock(TAService=mock_ta_cls),
        },
    ):
        main(["--force", "--db", ":memory:"])

    # bulk_refresh should have been called with all tickers including SPY
    mock_ta_inst.bulk_refresh.assert_called_once()
    call_tickers = mock_ta_inst.bulk_refresh.call_args[0][0]
    assert "AAPL" in call_tickers
    assert "MSFT" in call_tickers
    assert "SPY" in call_tickers
    assert len(call_tickers) == 3


# ── IB connection failure ────────────────────────────────────────────────


@patch("scripts.ta_premarket_prep.build_static_universe", return_value=["AAPL"])
@patch("scripts.ta_premarket_prep.get_connection")
@patch("scripts.ta_premarket_prep.init_schema")
@patch("scripts.ta_premarket_prep._last_trading_date", return_value=date(2026, 4, 10))
@patch("scripts.ta_premarket_prep.get_latest_bar_date", return_value=None)
def test_ib_connection_failure_prints_audit(mock_bar, mock_ltd, mock_init, mock_conn_fn, mock_universe, capsys):
    """If IB connection fails, output audit JSON with error field."""
    mock_conn_fn.return_value = MagicMock()

    mock_ib_cls = MagicMock()
    mock_ib_cls.return_value.connect.side_effect = ConnectionError("no gateway")

    with patch.dict(
        sys.modules,
        {
            "scripts.clients.ib_client": MagicMock(IBClient=mock_ib_cls),
            "scripts.ta_lib.service": MagicMock(),
        },
    ):
        main(["--db", ":memory:"])

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "before" in data
    assert "error" in data
