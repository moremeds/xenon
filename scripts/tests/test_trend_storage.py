"""Tests for trend scanner DuckDB storage."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


def test_init_schema_creates_tables():
    import duckdb

    from xenon.scanners.trend.storage import init_schema

    conn = duckdb.connect(":memory:")
    init_schema(conn)
    tables = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()
    table_names = {t[0] for t in tables}
    assert "scan_runs" in table_names
    assert "scan_candidates" in table_names


def test_init_schema_idempotent():
    import duckdb

    from xenon.scanners.trend.storage import init_schema

    conn = duckdb.connect(":memory:")
    init_schema(conn)
    init_schema(conn)
    count = conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0]
    assert count == 0


def test_write_scan_run():
    import duckdb

    from xenon.scanners.trend.storage import init_schema, write_scan_run

    conn = duckdb.connect(":memory:")
    init_schema(conn)
    run = {
        "scan_id": "trend_20260410_0845",
        "scan_timestamp": datetime(2026, 4, 10, 8, 45, 0, tzinfo=timezone.utc),
        "universe_size": 743,
        "stage_a_pass": 187,
        "stage_b_pass": 92,
        "candidates_out": 25,
        "spy_close": 523.45,
        "vix_close": 18.2,
        "regime": "bullish",
        "duration_secs": 42.5,
    }
    write_scan_run(conn, run)
    row = conn.execute("SELECT scan_id, universe_size, regime FROM scan_runs").fetchone()
    assert row[0] == "trend_20260410_0845"
    assert row[1] == 743
    assert row[2] == "bullish"


def test_write_scan_candidate():
    import duckdb

    from xenon.scanners.trend.storage import init_schema, write_scan_candidates, write_scan_run

    conn = duckdb.connect(":memory:")
    init_schema(conn)
    run = {
        "scan_id": "test_001",
        "scan_timestamp": datetime(2026, 4, 10, 8, 45, 0, tzinfo=timezone.utc),
        "universe_size": 100,
        "stage_a_pass": 50,
        "stage_b_pass": 30,
        "candidates_out": 10,
        "spy_close": 520.0,
        "vix_close": 17.0,
        "regime": "bullish",
        "duration_secs": 30.0,
    }
    write_scan_run(conn, run)
    candidates = [
        {
            "scan_id": "test_001",
            "ticker": "NVDA",
            "snapshot_timestamp": datetime(2026, 4, 10, 8, 45, 0, tzinfo=timezone.utc),
            "spot_price": 148.30,
            "direction": "bullish",
            "final_score": 0.82,
            "trend_score": 0.91,
            "structure_score": 0.75,
            "vol_score": 0.68,
            "flow_score": 0.85,
            "ma_20": 142.50,
            "ma_50": 138.20,
            "ma_200": 125.80,
            "rsi": 62.3,
            "adx": 32.1,
            "macd_histogram": 1.45,
            "bbw": 0.08,
            "rs_vs_spy": 1.15,
            "iv_rank": 22.0,
            "gamma_flip": 145.0,
            "call_wall": 160.0,
            "put_wall": 140.0,
            "structure_hint": "long_call",
            "catalysts": [],
            "invalidation": 142.50,
            "flags": ["breakout"],
            "trend_summary": "Full MA stack",
            "structure_summary": "Above gamma flip",
            "vol_summary": "IV rank 22",
            "flow_summary": "4 ask-side prints",
        }
    ]
    write_scan_candidates(conn, candidates)
    row = conn.execute("SELECT ticker, final_score, rsi, flags FROM scan_candidates").fetchone()
    assert row[0] == "NVDA"
    assert abs(row[1] - 0.82) < 0.01
    assert abs(row[2] - 62.3) < 0.1
    assert row[3] == ["breakout"]


def test_write_multiple_candidates():
    import duckdb

    from xenon.scanners.trend.storage import init_schema, write_scan_candidates, write_scan_run

    conn = duckdb.connect(":memory:")
    init_schema(conn)
    run = {
        "scan_id": "test_002",
        "scan_timestamp": datetime(2026, 4, 10, 9, 0, 0, tzinfo=timezone.utc),
        "universe_size": 100,
        "stage_a_pass": 50,
        "stage_b_pass": 30,
        "candidates_out": 2,
        "spy_close": 520.0,
        "vix_close": 17.0,
        "regime": "bullish",
        "duration_secs": 25.0,
    }
    write_scan_run(conn, run)
    candidates = [_make_candidate("test_002", "AAPL", 0.9), _make_candidate("test_002", "MSFT", 0.8)]
    write_scan_candidates(conn, candidates)
    count = conn.execute("SELECT COUNT(*) FROM scan_candidates WHERE scan_id='test_002'").fetchone()[0]
    assert count == 2


def test_query_historical_scores():
    import duckdb

    from xenon.scanners.trend.storage import init_schema, write_scan_candidates, write_scan_run

    conn = duckdb.connect(":memory:")
    init_schema(conn)
    for sid, ts_hour in [("run_1", 8), ("run_2", 9)]:
        write_scan_run(
            conn,
            {
                "scan_id": sid,
                "scan_timestamp": datetime(2026, 4, 10, ts_hour, 0, 0, tzinfo=timezone.utc),
                "universe_size": 100,
                "stage_a_pass": 50,
                "stage_b_pass": 30,
                "candidates_out": 1,
                "spy_close": 520.0,
                "vix_close": 17.0,
                "regime": "bullish",
                "duration_secs": 20.0,
            },
        )
        write_scan_candidates(conn, [_make_candidate(sid, "NVDA", 0.85)])
    rows = conn.execute(
        "SELECT r.scan_id, c.ticker, c.final_score FROM scan_candidates c JOIN scan_runs r ON c.scan_id = r.scan_id ORDER BY r.scan_timestamp"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0][0] == "run_1"
    assert rows[1][0] == "run_2"


def test_file_based_db(tmp_path):
    import duckdb

    from xenon.scanners.trend.storage import get_connection, init_schema

    db_path = tmp_path / "test.duckdb"
    conn = get_connection(str(db_path))
    init_schema(conn)
    assert db_path.exists()
    conn.close()
    conn2 = get_connection(str(db_path))
    tables = conn2.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()
    assert len(tables) >= 2
    conn2.close()


def _make_candidate(scan_id: str, ticker: str, score: float) -> dict:
    return {
        "scan_id": scan_id,
        "ticker": ticker,
        "snapshot_timestamp": datetime(2026, 4, 10, 8, 45, 0, tzinfo=timezone.utc),
        "spot_price": 100.0,
        "direction": "bullish",
        "final_score": score,
        "trend_score": score,
        "structure_score": score * 0.9,
        "vol_score": score * 0.8,
        "flow_score": score * 0.7,
        "ma_20": 95.0,
        "ma_50": 90.0,
        "ma_200": 80.0,
        "rsi": 60.0,
        "adx": 30.0,
        "macd_histogram": 1.0,
        "bbw": 0.05,
        "rs_vs_spy": 1.1,
        "iv_rank": 25.0,
        "gamma_flip": 98.0,
        "call_wall": 110.0,
        "put_wall": 95.0,
        "structure_hint": "long_call",
        "catalysts": [],
        "invalidation": 95.0,
        "flags": [],
        "trend_summary": "Good",
        "structure_summary": "Good",
        "vol_summary": "Good",
        "flow_summary": "Good",
    }
