"""Unit tests for xenon.clients.massive_client."""

from __future__ import annotations

import os
from unittest.mock import patch

import pandas as pd
import pytest
import responses

from xenon.clients.massive_client import (
    MassiveAuthError,
    MassiveClient,
    MassiveNoDataError,
    MassiveRateLimitError,
)

_BASE = "https://api.massive.com"
_RESULTS_OK = {
    "ticker": "AAPL",
    "adjusted": True,
    "results": [
        {
            "t": 1744070400000,
            "o": 170.0,
            "h": 172.0,
            "l": 169.5,
            "c": 171.5,
            "v": 50_000_000,
            "vw": 170.8,
            "n": 450_000,
        },
        {
            "t": 1744156800000,
            "o": 171.5,
            "h": 173.0,
            "l": 170.9,
            "c": 172.7,
            "v": 48_000_000,
            "vw": 171.9,
            "n": 430_000,
        },
    ],
}


@pytest.fixture
def client():
    with patch.dict(os.environ, {"MASSIVE_API_KEY": "test-key"}, clear=False):
        yield MassiveClient(base_url=_BASE)


def test_missing_key_raises_at_init():
    with patch.dict(os.environ, {}, clear=True), patch("xenon.clients.massive_client.load_dotenv", lambda: None):
        with pytest.raises(MassiveAuthError):
            MassiveClient()


@responses.activate
def test_get_aggregates_1d_happy_path(client):
    responses.get(
        f"{_BASE}/v2/aggs/ticker/AAPL/range/1/day/2026-04-08/2026-04-09",
        json=_RESULTS_OK,
        status=200,
    )
    df = client.get_aggregates("AAPL", "1d", "2026-04-08", "2026-04-09")
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume", "vwap", "tx_count"]
    assert len(df) == 2
    assert df["close"].iloc[0] == 171.5
    # ET timezone on timestamps
    assert str(df["date"].iloc[0].tz) == "America/New_York"
    # ascending
    assert df["date"].is_monotonic_increasing


@responses.activate
def test_get_aggregates_1h_timeframe_map(client):
    responses.get(
        f"{_BASE}/v2/aggs/ticker/AAPL/range/1/hour/2026-04-08/2026-04-09",
        json=_RESULTS_OK,
        status=200,
    )
    df = client.get_aggregates("AAPL", "1h", "2026-04-08", "2026-04-09")
    assert len(df) == 2


@responses.activate
def test_adjusted_false_propagates(client):
    rsp = responses.get(
        f"{_BASE}/v2/aggs/ticker/AAPL/range/1/day/2026-04-08/2026-04-09",
        json=_RESULTS_OK,
        status=200,
    )
    client.get_aggregates("AAPL", "1d", "2026-04-08", "2026-04-09", adjusted=False)
    assert "adjusted=false" in rsp.calls[0].request.url


def test_invalid_timeframe_raises_value_error(client):
    with pytest.raises(ValueError):
        client.get_aggregates("AAPL", "5m", "2026-04-08", "2026-04-09")


@responses.activate
def test_401_raises_auth_error_no_retry(client):
    responses.get(
        f"{_BASE}/v2/aggs/ticker/AAPL/range/1/day/2026-04-08/2026-04-09",
        status=401,
    )
    with pytest.raises(MassiveAuthError):
        client.get_aggregates("AAPL", "1d", "2026-04-08", "2026-04-09")
    assert len(responses.calls) == 1  # no retry


@responses.activate
def test_404_raises_no_data_error(client):
    responses.get(
        f"{_BASE}/v2/aggs/ticker/ZZZZ/range/1/day/2026-04-08/2026-04-09",
        status=404,
    )
    with pytest.raises(MassiveNoDataError):
        client.get_aggregates("ZZZZ", "1d", "2026-04-08", "2026-04-09")


@responses.activate
def test_empty_results_raises_no_data(client):
    responses.get(
        f"{_BASE}/v2/aggs/ticker/AAPL/range/1/day/2026-04-08/2026-04-09",
        json={"ticker": "AAPL", "results": []},
        status=200,
    )
    with pytest.raises(MassiveNoDataError):
        client.get_aggregates("AAPL", "1d", "2026-04-08", "2026-04-09")


@responses.activate
def test_429_retries_then_raises(client, monkeypatch):
    sleeps = []
    monkeypatch.setattr(
        "xenon.clients.massive_client.time.sleep",
        lambda s: sleeps.append(s),
    )
    for _ in range(4):
        responses.get(
            f"{_BASE}/v2/aggs/ticker/AAPL/range/1/day/2026-04-08/2026-04-09",
            status=429,
        )
    with pytest.raises(MassiveRateLimitError):
        client.get_aggregates("AAPL", "1d", "2026-04-08", "2026-04-09")
    assert len(responses.calls) == 4  # initial + 3 retries
    assert len(sleeps) == 3


@responses.activate
def test_health_check_ok(client):
    responses.get(f"{_BASE}/v1/marketstatus/now", json={"market": "extended-hours"}, status=200)
    assert client.health_check() is True


@responses.activate
def test_health_check_failure(client):
    responses.get(f"{_BASE}/v1/marketstatus/now", status=500)
    assert client.health_check() is False
