"""Tests for /futu/* routes and the /health futu block.

Mocks the FutuClient singleton so tests run without a live OpenD. The Futu
display snapshot lives in Postgres (`account_snapshots`, broker FUTU, payload
without `observation`); nothing reads or writes `data/futu_portfolio*.json`.
  1. GET /futu/portfolio returns HTTP 200 + never_synced body on first boot
  2. GET /futu/portfolio returns the newest display snapshot with ok:true,
     never an observation-pass row (bankroll 0, no balances)
  3. /health includes a `futu` block with configured/connected/last_sync_at
     and the configured scope's last observation pass
  4. POST /futu/sync cooldown sentinel: first call proceeds (None → False)
  5. POST /futu/sync partial-failure preservation (tribunal T15) against the
     previous PG row
Account ids, counts and balances are made-up test values.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import func, insert, select

# Boot the server in test mode so lifespan skips IB pool startup.
os.environ["XENON_API_TEST_MODE"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from xenon.api.routes import futu as futu_routes  # noqa: E402
from xenon.db.engine import get_sync_engine  # noqa: E402
from xenon.db.schema import account_snapshots  # noqa: E402
from xenon.execution.account_scope import AccountScope  # noqa: E402

ACC = "281756479345015383"
LIVE = AccountScope(broker="FUTU", account_env="live", broker_account=ACC)


@pytest.fixture()
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point server.DATA_DIR at a tmp directory and reset globals."""
    from xenon.api import server  # type: ignore

    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)
    monkeypatch.delenv("FUTU_TRADE_ACC_ID", raising=False)
    # Request scope with OpenD offline: the last-synced FUTU scope (LIVE).
    from xenon.api import guards

    monkeypatch.setattr(guards, "_futu_scope_from_db", lambda: LIVE)
    # Reset module-global sync state between tests.
    monkeypatch.setattr(futu_routes, "_futu_last_sync_monotonic", None)
    monkeypatch.setattr(futu_routes, "_futu_client", None)
    monkeypatch.setattr(futu_routes, "_futu_lock", None)
    return tmp_path


@pytest.fixture()
def client(isolated_data_dir: Path) -> TestClient:
    from xenon.api import server  # type: ignore

    return TestClient(server.app)


def _store(payload: dict, *, acc: str = ACC, env: str = "live", bankroll: str = "148000") -> None:
    with get_sync_engine().begin() as conn:
        conn.execute(
            insert(account_snapshots).values(
                account=acc,
                bankroll=Decimal(bankroll),
                payload=payload,
                broker="FUTU",
                account_env=env,
                broker_account=acc,
                snapshot_at=func.clock_timestamp(),
            )
        )


def _display_rows() -> list:
    with get_sync_engine().connect() as conn:
        return conn.execute(
            select(account_snapshots).where(account_snapshots.c.broker == "FUTU").order_by(account_snapshots.c.id)
        ).all()


def _connected_client(result: dict) -> MagicMock:
    mock_client = MagicMock()
    mock_client.is_connected.return_value = True
    mock_client.fetch_portfolio.return_value = result
    mock_client._acc_id = int(ACC)
    mock_client.trd_env_of_matched_account.return_value = "REAL"
    return mock_client


# ──────────────────────────────────────────────────────────────────────
# GET /futu/portfolio — never_synced + stored cases
# ──────────────────────────────────────────────────────────────────────


def test_get_futu_portfolio_returns_200_never_synced_when_nothing_stored(
    client: TestClient, isolated_data_dir: Path
) -> None:
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


def test_get_futu_portfolio_returns_stored_snapshot_with_ok_true(client: TestClient, isolated_data_dir: Path) -> None:
    _store(
        {
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
    )

    resp = client.get("/futu/portfolio")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["count"] == 1
    assert body["account_id"] == "12345"
    assert body["positions"][0]["futu_code"] == "US.TSLA"


def test_get_futu_portfolio_ignores_the_json_cache_file(client: TestClient, isolated_data_dir: Path) -> None:
    (isolated_data_dir / "futu_portfolio.json").write_text('{"count": 9, "positions": [], "fetched_at": "x"}')

    body = client.get("/futu/portfolio").json()

    assert body["ok"] is False and body["code"] == "never_synced"


def test_get_futu_portfolio_never_serves_an_observation_row(client: TestClient, isolated_data_dir: Path) -> None:
    """Bankroll condition: an observation-pass snapshot (bankroll 0, no
    balances read) is newer than the display snapshot; the reader still
    serves the display snapshot, so 0 is never shown as a real balance."""
    _store({"fetched_at": "2026-04-07T12:00:00.000Z", "count": 1, "positions": [], "account_summary": {"net_liquidation": 148000}})
    _store(
        {
            "source": "futu",
            "positions": [],
            "account_summary": None,
            "observation": {"as_of": "2026-04-07T12:01:00+00:00", "complete": True, "account": ACC},
        },
        env="paper",
        bankroll="0",
    )

    body = client.get("/futu/portfolio").json()

    assert body["ok"] is True
    assert "observation" not in body
    assert body["account_summary"]["net_liquidation"] == 148000


def test_get_futu_portfolio_with_only_an_observation_row_is_never_synced(
    client: TestClient, isolated_data_dir: Path
) -> None:
    _store({"positions": [], "account_summary": None, "observation": {"complete": True}}, env="paper", bankroll="0")

    body = client.get("/futu/portfolio").json()

    assert body["ok"] is False and body["code"] == "never_synced"


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
    assert futu["observation"] is None
    assert futu["observation_loop"] is False


def test_health_futu_block_surfaces_last_sync_from_postgres(
    client: TestClient, isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUTU_TRADE_ACC_ID", ACC)
    monkeypatch.setenv("FUTU_TRADE_ENV", "REAL")
    (isolated_data_dir / "futu_portfolio.json").write_text('{"fetched_at": "2020-01-01T00:00:00.000Z"}')
    _store({"fetched_at": "2026-04-07T12:00:00.000Z", "positions": [], "count": 0, "account_summary": None})

    body = client.get("/health").json()
    assert body["futu"]["last_sync_at"] == "2026-04-07T12:00:00.000Z"
    assert body["futu"]["last_sync_age_s"] is not None  # a real number


def test_health_futu_block_reports_configured_scope_observation(
    client: TestClient, isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUTU_TRADE_ACC_ID", ACC)
    monkeypatch.setenv("FUTU_TRADE_ENV", "SIMULATE")
    _store(
        {"positions": [], "observation": {"as_of": "2026-04-07T12:01:00+00:00", "complete": True, "account": ACC}},
        env="paper",
        bankroll="0",
    )
    # Another account's pass is not this scope's observation.
    _store({"positions": [], "observation": {"as_of": "2026-04-07T12:02:00+00:00", "complete": False}}, acc="1", env="paper")

    futu = client.get("/health").json()["futu"]

    assert futu["observation"] == {"as_of": "2026-04-07T12:01:00+00:00", "complete": True}
    assert futu["last_sync_at"] is None  # observation rows are not display syncs


# ──────────────────────────────────────────────────────────────────────
# POST /futu/sync — cooldown sentinel + PG persistence
# ──────────────────────────────────────────────────────────────────────


FRESH = {
    "fetched_at": "2026-04-07T12:00:00.000Z",
    "data_as_of": "2026-04-07T12:00:00.000Z",
    "account_id": ACC,
    "source": "futu",
    "positions": [{"futu_code": "US.TSLA"}],
    "count": 1,
    "is_stale": False,
    "warnings": [],
    "account_summary": {"net_liquidation": 148000.5},
}


def test_futu_sync_cooldown_sentinel_first_call_proceeds(
    client: TestClient, isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With _futu_last_sync_monotonic=None, first call MUST hit the client
    even if a snapshot exists. Codex #3: 0.0 init would misfire here."""
    _store({"fetched_at": "2020-01-01T00:00:00.000Z", "count": 0, "positions": []})
    mock_client = _connected_client(FRESH)
    monkeypatch.setattr(futu_routes, "_get_futu_client", lambda: mock_client)
    monkeypatch.setattr(futu_routes, "_futu_last_sync_monotonic", None)

    resp = client.post("/futu/sync")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["count"] == 1
    assert body["fetched_at"] == "2026-04-07T12:00:00.000Z"
    mock_client.fetch_portfolio.assert_called_once()


def test_futu_sync_stores_display_snapshot_in_postgres_not_json(
    client: TestClient, isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(futu_routes, "_get_futu_client", lambda: _connected_client(FRESH))

    assert client.post("/futu/sync").status_code == 200

    rows = _display_rows()
    assert len(rows) == 1
    row = rows[0]
    assert (row.broker, row.account_env, row.broker_account) == ("FUTU", "live", ACC)
    assert row.net_liquidation == Decimal("148000.50") and row.bankroll == Decimal("148000.50")
    assert row.payload["count"] == 1 and "observation" not in row.payload
    assert list(isolated_data_dir.iterdir()) == []  # no data/*.json written
    assert client.get("/futu/portfolio").json()["fetched_at"] == FRESH["fetched_at"]


def test_futu_sync_under_read_only_stores_nothing(
    client: TestClient, isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    monkeypatch.setattr(futu_routes, "_get_futu_client", lambda: _connected_client(FRESH))

    resp = client.post("/futu/sync")

    assert resp.status_code == 200 and resp.json()["count"] == 1
    assert _display_rows() == []


# ──────────────────────────────────────────────────────────────────────
# Partial-failure preservation (tribunal T15) — previous PG row
# ──────────────────────────────────────────────────────────────────────


def test_maybe_preserve_partial_failure_blocks_degraded_overwrite(
    client: TestClient, isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store(
        {
            "fetched_at": "2026-04-07T12:00:00.000Z",
            "count": 27,
            "positions": [{"futu_code": f"US.X{i}"} for i in range(27)],
            "warnings": [],
        }
    )
    degraded = {
        "fetched_at": "2026-04-07T12:05:00.000Z",
        "count": 5,
        "positions": [{"futu_code": f"US.Y{i}"} for i in range(5)],
        "warnings": ["row US.ZZZ: parse error"],
    }

    preserved = futu_routes._maybe_preserve_partial_failure(degraded, LIVE)
    assert preserved is not None
    assert preserved["count"] == 27

    # Through the route: the degraded result is not stored.
    monkeypatch.setattr(futu_routes, "_get_futu_client", lambda: _connected_client(degraded))
    body = client.post("/futu/sync").json()
    assert body["count"] == 27 and "partial_failure_preserved" in body["warnings"]
    assert [r.payload["count"] for r in _display_rows()] == [27]
    assert list(isolated_data_dir.iterdir()) == []  # no error sidecar file


def test_maybe_preserve_partial_failure_allows_clean_overwrite(isolated_data_dir: Path) -> None:
    """If the new snapshot has no warnings, let it overwrite even if count drops
    (e.g. user legitimately closed positions)."""
    _store({"count": 27, "positions": [], "warnings": []})

    assert futu_routes._maybe_preserve_partial_failure({"count": 5, "positions": [], "warnings": []}, LIVE) is None


def test_maybe_preserve_partial_failure_allows_larger_new_snapshot(isolated_data_dir: Path) -> None:
    """If warnings exist but new count >= prev count, allow overwrite."""
    _store({"count": 10, "positions": [], "warnings": []})

    new = {"count": 12, "positions": [], "warnings": ["one bad row skipped"]}
    assert futu_routes._maybe_preserve_partial_failure(new, LIVE) is None


def test_maybe_preserve_partial_failure_ignores_observation_rows(isolated_data_dir: Path) -> None:
    _store({"count": 30, "positions": [], "observation": {"complete": True}}, env="paper", bankroll="0")

    assert futu_routes._maybe_preserve_partial_failure({"count": 1, "warnings": ["x"]}, LIVE) is None


def test_api_package_has_no_futu_portfolio_json_path() -> None:
    """The no-JSON guard matches only literal "data/...json" strings, so it
    never saw `server.DATA_DIR / "futu_portfolio.json"`. Pin the removal."""
    api = Path(__file__).resolve().parents[2] / "src" / "xenon" / "api"
    hits = [str(p) for p in api.rglob("*.py") if ("futu_portfolio.json" in p.read_text() or "futu_portfolio.error" in p.read_text()) and "/tests/" not in str(p)]
    assert hits == []
