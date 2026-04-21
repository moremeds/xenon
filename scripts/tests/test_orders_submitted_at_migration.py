"""D2 — smoke test for the pre-upgrade timestamp TZ migration.

Seeds a DuckDB with LA-wall-clock naive timestamps that should, after the
migration, equal a known UTC instant. Asserts that ``_submitted_at_epoch``
reads the post-migration value as the correct UTC epoch.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "migrations" / "2026_04_21_orders_submitted_at_to_utc.py"
)


def _seed_pre_patch_row(db_path: Path, naive_local_ts: datetime) -> str:
    """Simulate a row written by pre-patch orders_store on an LA host.

    Pre-patch code wrote ``datetime.now(timezone.utc)``; without a pinned
    session TZ, DuckDB converted that to local wall-clock then stripped
    tzinfo. So the stored naive value equals the local wall-clock.
    """
    # First ensure schema exists — use orders_store.init_store.
    from xenon.execution import orders_store

    orders_store.init_store(db_path)

    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            """
            INSERT INTO orders_submissions (
                submission_id, user_id, client_attempt_id, ticker, security_type,
                action, quantity, multiplier, limit_price, state, ib_order_id,
                modify_sequence, submitted_at, updated_at
            ) VALUES (?, 'local', 'cid-mig-1', 'AAPL', 'STK', 'BUY', 1, 100, '1.23',
                      'PENDING', 'ib-1', 0, ?, ?)
            """,
            ["sub-mig-1", naive_local_ts, naive_local_ts],
        )
    finally:
        con.close()
    return "sub-mig-1"


def test_migration_rewrites_local_wall_clock_to_utc(tmp_path, monkeypatch):
    """After --apply, _submitted_at_epoch must read UTC epoch matching the
    original UTC instant the pre-patch code intended."""
    # Known UTC instant: 2024-03-15 14:30:00 UTC == 2024-03-15 07:30:00 LA (PDT).
    intended_utc = datetime(2024, 3, 15, 14, 30, 0, tzinfo=timezone.utc)
    la_wall_clock_naive = datetime(2024, 3, 15, 7, 30, 0)  # naive, LA local

    db_path = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(db_path))

    _seed_pre_patch_row(db_path, la_wall_clock_naive)

    # Sanity precondition: a post-patch reader (treats naive as UTC) would see
    # 07:30 UTC, not 14:30 UTC — that's the bug we're fixing.
    from xenon.execution.single_leg_rehydrate import _submitted_at_epoch

    con = duckdb.connect(str(db_path))
    try:
        row = con.execute("SELECT submitted_at FROM orders_submissions WHERE submission_id = 'sub-mig-1'").fetchone()
    finally:
        con.close()
    pre_epoch = _submitted_at_epoch(row[0])
    # Confirms the hazard: reader sees 07:30 UTC (= intended - 7h).
    assert pre_epoch == datetime(2024, 3, 15, 7, 30, 0, tzinfo=timezone.utc).timestamp()

    # Run the migration with --apply --from-tz America/Los_Angeles.
    result = subprocess.run(
        [
            sys.executable,
            str(MIGRATION_PATH),
            "--db",
            str(db_path),
            "--from-tz",
            "America/Los_Angeles",
            "--apply",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"migration failed: {result.stderr}\n{result.stdout}"

    # After migration the stored naive value should equal 14:30 UTC wall-clock.
    con = duckdb.connect(str(db_path))
    try:
        row2 = con.execute("SELECT submitted_at FROM orders_submissions WHERE submission_id = 'sub-mig-1'").fetchone()
    finally:
        con.close()
    post_epoch = _submitted_at_epoch(row2[0])
    assert post_epoch == intended_utc.timestamp()


def test_migration_idempotency_sentinel(tmp_path, monkeypatch):
    """A second run on the same DB must be a no-op (sentinel present)."""
    db_path = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(db_path))
    _seed_pre_patch_row(db_path, datetime(2024, 3, 15, 7, 30, 0))

    def _run(apply: bool):
        argv = [
            sys.executable,
            str(MIGRATION_PATH),
            "--db",
            str(db_path),
            "--from-tz",
            "America/Los_Angeles",
        ]
        if apply:
            argv.append("--apply")
        return subprocess.run(argv, capture_output=True, text=True, check=False)

    first = _run(apply=True)
    assert first.returncode == 0
    # Second run should detect the sentinel and skip.
    second = _run(apply=True)
    assert second.returncode == 0
    assert "SKIP" in (second.stderr + second.stdout)
