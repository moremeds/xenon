"""DuckDB storage for trend scan history and backtesting."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import duckdb

logger = logging.getLogger(__name__)
DEFAULT_DB_PATH = "data/trend_scan.duckdb"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scan_runs (
    scan_id        VARCHAR PRIMARY KEY,
    scan_timestamp TIMESTAMPTZ NOT NULL,
    universe_size  INTEGER,
    stage_a_pass   INTEGER,
    stage_b_pass   INTEGER,
    candidates_out INTEGER,
    spy_close      DOUBLE,
    vix_close      DOUBLE,
    regime         VARCHAR,
    duration_secs  DOUBLE
);

CREATE TABLE IF NOT EXISTS scan_candidates (
    scan_id            VARCHAR NOT NULL,
    ticker             VARCHAR NOT NULL,
    snapshot_timestamp  TIMESTAMPTZ NOT NULL,
    spot_price         DOUBLE,
    direction          VARCHAR,
    final_score        DOUBLE,
    trend_score        DOUBLE,
    structure_score    DOUBLE,
    vol_score          DOUBLE,
    flow_score         DOUBLE,
    ma_20              DOUBLE,
    ma_50              DOUBLE,
    ma_200             DOUBLE,
    rsi                DOUBLE,
    adx                DOUBLE,
    macd_histogram     DOUBLE,
    bbw                DOUBLE,
    rs_vs_spy          DOUBLE,
    iv_rank            DOUBLE,
    gamma_flip         DOUBLE,
    call_wall          DOUBLE,
    put_wall           DOUBLE,
    suggested_trade    VARCHAR,
    invalidation       DOUBLE,
    flags              VARCHAR[],
    trend_summary      VARCHAR,
    structure_summary  VARCHAR,
    vol_summary        VARCHAR,
    flow_summary       VARCHAR,
    PRIMARY KEY (scan_id, ticker)
);
"""


def get_connection(db_path: str = DEFAULT_DB_PATH) -> duckdb.DuckDBPyConnection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(db_path)


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(SCHEMA_SQL)


def write_scan_run(conn: duckdb.DuckDBPyConnection, run: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO scan_runs (scan_id, scan_timestamp, universe_size, stage_a_pass, stage_b_pass, candidates_out, spy_close, vix_close, regime, duration_secs) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            run["scan_id"],
            run["scan_timestamp"],
            run.get("universe_size", 0),
            run.get("stage_a_pass", 0),
            run.get("stage_b_pass", 0),
            run.get("candidates_out", 0),
            run.get("spy_close", 0),
            run.get("vix_close", 0),
            run.get("regime", "unknown"),
            run.get("duration_secs", 0),
        ],
    )


def write_scan_candidates(conn: duckdb.DuckDBPyConnection, candidates: list[dict[str, Any]]) -> None:
    for c in candidates:
        conn.execute(
            "INSERT INTO scan_candidates (scan_id, ticker, snapshot_timestamp, spot_price, direction, final_score, trend_score, structure_score, vol_score, flow_score, ma_20, ma_50, ma_200, rsi, adx, macd_histogram, bbw, rs_vs_spy, iv_rank, gamma_flip, call_wall, put_wall, suggested_trade, invalidation, flags, trend_summary, structure_summary, vol_summary, flow_summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                c["scan_id"],
                c["ticker"],
                c["snapshot_timestamp"],
                c.get("spot_price", 0),
                c.get("direction", "bullish"),
                c.get("final_score", 0),
                c.get("trend_score", 0),
                c.get("structure_score", 0),
                c.get("vol_score", 0),
                c.get("flow_score", 0),
                c.get("ma_20", 0),
                c.get("ma_50", 0),
                c.get("ma_200", 0),
                c.get("rsi", 0),
                c.get("adx", 0),
                c.get("macd_histogram", 0),
                c.get("bbw", 0),
                c.get("rs_vs_spy", 0),
                c.get("iv_rank", 0),
                c.get("gamma_flip", 0),
                c.get("call_wall", 0),
                c.get("put_wall", 0),
                c.get("suggested_trade", ""),
                c.get("invalidation", 0),
                c.get("flags", []),
                c.get("trend_summary", ""),
                c.get("structure_summary", ""),
                c.get("vol_summary", ""),
                c.get("flow_summary", ""),
            ],
        )
