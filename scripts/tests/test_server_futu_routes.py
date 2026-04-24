"""Tests for /futu/* routes and /health futu block in scripts/api/server.py.

Mocks the FutuClient singleton so tests run without a live OpenD. Covers
the 5 behaviors changed in the Codex fix pass:
  1. GET /futu/portfolio returns HTTP 200 + never_synced body on first boot
  2. GET /futu/portfolio returns cached JSON with ok:true when a cache exists
  3. /health includes a `futu` block with configured/connected/last_sync_at
  4. POST /futu/sync cooldown sentinel: first call proceeds (None → False)
  5. POST /futu/sync partial-failure preservation (tribunal T15)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Boot the server in test mode so lifespan skips IB pool startup.
os.environ["XENON_API_TEST_MODE"] = "1"

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point scripts.api.server.DATA_DIR at a tmp directory and reset globals."""
    from xenon.api import server  # type: ignore

    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    # Reset module-global sync state between tests.
    monkeypatch.setattr(server, "_futu_last_sync_monotonic", None, raising=False)
    monkeypatch.setattr(server, "_futu_client", None, raising=False)
    monkeypatch.setattr(server, "_futu_lock", None, raising=False)
    return tmp_path


@pytest.fixture()
def client(isolated_data_dir: Path) -> TestClient:
    from xenon.api import server  # type: ignore

    return TestClient(server.app)


# ──────────────────────────────────────────────────────────────────────
# GET /futu/portfolio — never_synced + cached cases
# ──────────────────────────────────────────────────────────────────────


def test_get_futu_portfolio_returns_200_never_synced_when_no_cache(client: TestClient, isolated_data_dir: Path) -> None:
    # No cache file exists in tmp_path
    resp = client.get("/futu/portfolio")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["code"] == "never_synced"
    assert body["positions"] == []
    assert body["count"] == 0
    assert body["account_summary"] is None
    assert body["fetched_at"] is None
    assert body["data_as_of"] is None


def test_get_futu_portfolio_returns_cached_with_ok_true(client: TestClient, isolated_data_dir: Path) -> None:
    cache = {
        "fetched_at": "2026-04-07T12:00:00.000Z",
        "data_as_of": "2026-04-07T12:00:00.000Z",
        "account_id": "12345",
        "source": "futu",
        "positions": [{"futu_code": "US.TSLA", "quantity": 300}],
        "count": 1,
        "account_summary": {"net_liquidation": 148000},
        "is_stale": False,
        "warnings": [],
    }
    (isolated_data_dir / "futu_portfolio.json").write_text(json.dumps(cache))

    resp = client.get("/futu/portfolio")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["count"] == 1
    assert body["account_id"] == "12345"
    assert body["positions"][0]["futu_code"] == "US.TSLA"


# ──────────────────────────────────────────────────────────────────────
# /health futu block
# ──────────────────────────────────────────────────────────────────────


def test_health_includes_futu_block_with_never_synced(client: TestClient, isolated_data_dir: Path) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert "futu" in body
    futu = body["futu"]
    assert futu["configured"] is True
    assert futu["connected"] is False  # no singleton yet
    assert futu["last_sync_at"] is None
    assert futu["last_sync_age_s"] is None


def test_health_futu_block_surfaces_last_sync_when_cache_exists(client: TestClient, isolated_data_dir: Path) -> None:
    cache = {
        "fetched_at": "2026-04-07T12:00:00.000Z",
        "positions": [],
        "count": 0,
        "account_summary": None,
    }
    (isolated_data_dir / "futu_portfolio.json").write_text(json.dumps(cache))

    resp = client.get("/health")
    body = resp.json()
    assert body["futu"]["last_sync_at"] == "2026-04-07T12:00:00.000Z"
    assert body["futu"]["last_sync_age_s"] is not None  # a real number


# ──────────────────────────────────────────────────────────────────────
# POST /futu/sync — cooldown sentinel
# ──────────────────────────────────────────────────────────────────────


def test_futu_sync_cooldown_sentinel_first_call_proceeds(
    client: TestClient, isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With _futu_last_sync_monotonic=None, first call MUST hit the client
    even if a cache file exists. Codex #3: 0.0 init would misfire here."""
    from xenon.api import server  # type: ignore

    # Cache exists but should NOT be served — first call must fetch fresh.
    stale_cache = {
        "fetched_at": "2020-01-01T00:00:00.000Z",
        "count": 0,
        "positions": [],
    }
    (isolated_data_dir / "futu_portfolio.json").write_text(json.dumps(stale_cache))

    fresh_result = {
        "fetched_at": "2026-04-07T12:00:00.000Z",
        "data_as_of": "2026-04-07T12:00:00.000Z",
        "account_id": "12345",
        "source": "futu",
        "positions": [{"futu_code": "US.TSLA"}],
        "count": 1,
        "is_stale": False,
        "warnings": [],
        "account_summary": {"net_liquidation": 148000},
    }

    mock_client = MagicMock()
    mock_client.is_connected.return_value = True
    mock_client.fetch_portfolio.return_value = fresh_result

    monkeypatch.setattr(server, "_get_futu_client", lambda: mock_client)
    monkeypatch.setattr(server, "_futu_last_sync_monotonic", None, raising=False)

    resp = client.post("/futu/sync")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["count"] == 1
    assert body["fetched_at"] == "2026-04-07T12:00:00.000Z"
    mock_client.fetch_portfolio.assert_called_once()


# ──────────────────────────────────────────────────────────────────────
# Partial-failure preservation (tribunal T15)
# ──────────────────────────────────────────────────────────────────────


def test_maybe_preserve_partial_failure_blocks_degraded_overwrite(
    isolated_data_dir: Path,
) -> None:
    from xenon.api import server  # type: ignore

    # Existing snapshot: 27 positions, clean
    good_cache = {
        "fetched_at": "2026-04-07T12:00:00.000Z",
        "count": 27,
        "positions": [{"futu_code": f"US.X{i}"} for i in range(27)],
        "warnings": [],
    }
    (isolated_data_dir / "futu_portfolio.json").write_text(json.dumps(good_cache))

    # Degraded new result: 5 positions with warnings
    degraded = {
        "fetched_at": "2026-04-07T12:05:00.000Z",
        "count": 5,
        "positions": [{"futu_code": f"US.Y{i}"} for i in range(5)],
        "warnings": ["row US.ZZZ: parse error"],
    }

    preserved = server._maybe_preserve_partial_failure(degraded)

    # Previous cache returned, error sidecar written
    assert preserved is not None
    assert preserved["count"] == 27
    assert (isolated_data_dir / "futu_portfolio.error.json").exists()

    # Good cache untouched
    reloaded = json.loads((isolated_data_dir / "futu_portfolio.json").read_text())
    assert reloaded["count"] == 27


def test_maybe_preserve_partial_failure_allows_clean_overwrite(
    isolated_data_dir: Path,
) -> None:
    """If the new snapshot has no warnings, let it overwrite even if count drops
    (e.g. user legitimately closed positions)."""
    from xenon.api import server  # type: ignore

    good_cache = {"count": 27, "positions": [], "warnings": []}
    (isolated_data_dir / "futu_portfolio.json").write_text(json.dumps(good_cache))

    clean_new = {"count": 5, "positions": [], "warnings": []}
    preserved = server._maybe_preserve_partial_failure(clean_new)

    assert preserved is None  # caller should proceed to normal save


def test_maybe_preserve_partial_failure_allows_larger_new_snapshot(
    isolated_data_dir: Path,
) -> None:
    """If warnings exist but new count >= prev count, allow overwrite."""
    from xenon.api import server  # type: ignore

    good_cache = {"count": 10, "positions": [], "warnings": []}
    (isolated_data_dir / "futu_portfolio.json").write_text(json.dumps(good_cache))

    new_with_warnings_but_same_count = {
        "count": 12,
        "positions": [],
        "warnings": ["one bad row skipped"],
    }
    preserved = server._maybe_preserve_partial_failure(new_with_warnings_but_same_count)

    assert preserved is None
