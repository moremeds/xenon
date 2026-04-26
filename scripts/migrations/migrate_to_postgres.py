"""One-time migration: JSON/DuckDB → PostgreSQL.

Usage:
    uv run python scripts/migrations/migrate_to_postgres.py

Requires DATABASE_URL env var and existing data/ directory.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    import duckdb
except ImportError:
    duckdb = None

import logging

from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)

DATA_DIR = Path("data")


def _extract_cri_level(data: dict) -> float:
    """Extract numeric CRI level from various JSON shapes."""
    raw = data.get("cri_level", data.get("cri", 0))
    if isinstance(raw, dict):
        return float(raw.get("score", raw.get("value", 0)))
    return float(raw)


def get_engine():
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql+psycopg://")
    return create_engine(url)


def migrate_portfolio(engine):
    path = DATA_DIR / "portfolio.json"
    if not path.exists():
        print("  SKIP portfolio.json (not found)")
        return 0
    with engine.connect() as conn:
        existing = conn.execute(text("SELECT count(*) FROM xenon.positions")).scalar()
        if existing > 0:
            print(f"  SKIP portfolio.json (positions already has {existing} rows)")
            return 0
    with open(path) as f:
        data = json.load(f)
    data.pop("_checksum", None)

    count = 0
    with engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO xenon.account_snapshots (account, bankroll, peak_value, net_liquidation)
            VALUES (:account, :bankroll, :peak_value, :net_liq)
        """),
            {
                "account": "IB",
                "bankroll": data.get("bankroll", 0),
                "peak_value": data.get("peak_value"),
                "net_liq": data.get("net_liquidation"),
            },
        )

        for pos in data.get("positions", []):
            conn.execute(
                text("""
                INSERT INTO xenon.positions
                    (ticker, security_type, expiry, strike, "right", quantity,
                     avg_cost, current_price, unrealized_pnl, account)
                VALUES (:ticker, :sec_type, :expiry, :strike, :right, :qty,
                        :avg_cost, :cur_price, :pnl, :account)
            """),
                {
                    "ticker": pos.get("ticker", pos.get("symbol", "")),
                    "sec_type": pos.get("security_type", pos.get("secType", "STK")),
                    "expiry": pos.get("expiry"),
                    "strike": pos.get("strike"),
                    "right": pos.get("right"),
                    "qty": pos.get("quantity", pos.get("position", 0)),
                    "avg_cost": pos.get("avg_cost", pos.get("avgCost", 0)),
                    "cur_price": pos.get("current_price", pos.get("marketPrice")),
                    "pnl": pos.get("unrealized_pnl", pos.get("unrealizedPNL")),
                    "account": "IB",
                },
            )
            count += 1
    print(f"  portfolio.json → {count} positions + 1 snapshot")
    return count


def migrate_nav_history(engine):
    path = DATA_DIR / "nav_history.jsonl"
    if not path.exists():
        print("  SKIP nav_history.jsonl (not found)")
        return 0
    count = 0
    with engine.begin() as conn:
        for line in open(path):
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            conn.execute(
                text("""
                INSERT INTO xenon.nav_history (date, nav, daily_pnl)
                VALUES (:date, :nav, :pnl)
                ON CONFLICT (date) DO UPDATE SET nav = :nav, daily_pnl = :pnl
            """),
                {
                    "date": entry["date"],
                    "nav": entry["nav"],
                    "pnl": entry.get("daily_pnl"),
                },
            )
            count += 1
    print(f"  nav_history.jsonl → {count} rows")
    return count


def migrate_trade_log(engine):
    path = DATA_DIR / "trade_log.json"
    if not path.exists():
        print("  SKIP trade_log.json (not found)")
        return 0
    with engine.connect() as conn:
        existing = conn.execute(text("SELECT count(*) FROM xenon.trades")).scalar()
        if existing > 0:
            print(f"  SKIP trade_log.json (trades already has {existing} rows)")
            return 0
    with open(path) as f:
        data = json.load(f)
    trades_list = data.get("trades", data) if isinstance(data, dict) else data
    if not isinstance(trades_list, list):
        trades_list = []
    count = 0
    with engine.begin() as conn:
        for t in trades_list:
            conn.execute(
                text("""
                INSERT INTO xenon.trades
                    (ticker, structure, action, quantity, entry_cost, exit_cost,
                     realized_pnl, edge, decision, metadata)
                VALUES (:ticker, :structure, :action, :quantity, :entry_cost,
                        :exit_cost, :pnl, :edge, :decision, CAST(:meta AS jsonb))
            """),
                {
                    "ticker": t.get("ticker", ""),
                    "structure": t.get("structure"),
                    "action": t.get("action", t.get("side", "")),
                    "quantity": t.get("quantity", 0),
                    "entry_cost": t.get("entry_cost"),
                    "exit_cost": t.get("exit_cost"),
                    "pnl": t.get("realized_pnl", t.get("pnl")),
                    "edge": t.get("edge"),
                    "decision": t.get("decision"),
                    "meta": json.dumps(t),
                },
            )
            count += 1
    print(f"  trade_log.json → {count} trades")
    return count


def migrate_orders_duckdb(engine):
    db_path = DATA_DIR / "orders.duckdb"
    if not db_path.exists():
        print("  SKIP orders.duckdb (not found)")
        return 0
    if duckdb is None:
        print("  SKIP orders.duckdb (duckdb not installed — pip install duckdb)")
        return 0

    duck = duckdb.connect(str(db_path), read_only=True)
    duck.execute("SET TimeZone='UTC'")

    count_sub = 0
    count_evt = 0
    count_ws = 0
    count_we = 0
    count_wca = 0
    count_wp = 0

    with engine.begin() as conn:
        # Order submissions
        rows = duck.execute("SELECT * FROM orders_submissions").fetchall()
        cols = [desc[0] for desc in duck.description]
        for row in rows:
            d = dict(zip(cols, row))
            # Stringify IDs that may be int but column is TEXT
            for key in ("ib_order_id", "perm_id", "submission_id", "user_id", "client_attempt_id"):
                if d.get(key) is not None:
                    d[key] = str(d[key])
            conn.execute(
                text("""
                INSERT INTO xenon.order_submissions
                    (submission_id, user_id, client_attempt_id, ticker,
                     security_type, action, quantity, expiry, strike, "right",
                     multiplier, con_id, placing_client_id, ib_order_id,
                     perm_id, limit_price, state, reason_code, filled_qty,
                     avg_fill_price, modify_sequence, submitted_at, updated_at)
                VALUES (:submission_id, :user_id, :client_attempt_id, :ticker,
                        :security_type, :action, :quantity, :expiry, :strike,
                        :right, :multiplier, :con_id, :placing_client_id,
                        :ib_order_id, :perm_id, :limit_price, :state,
                        :reason_code, :filled_qty, :avg_fill_price,
                        :modify_sequence, :submitted_at, :updated_at)
                ON CONFLICT (submission_id) DO NOTHING
            """),
                d,
            )
            count_sub += 1

        # Order events
        rows = duck.execute("SELECT * FROM orders_events").fetchall()
        cols = [desc[0] for desc in duck.description]
        for row in rows:
            d = dict(zip(cols, row))
            if isinstance(d.get("detail"), str):
                d["detail"] = json.loads(d["detail"]) if d["detail"] else None
            conn.execute(
                text("""
                INSERT INTO xenon.order_events (submission_id, kind, detail, "at")
                VALUES (:submission_id, :kind, CAST(:detail AS jsonb), :at)
            """),
                {
                    "submission_id": str(d["submission_id"]),
                    "kind": d["kind"],
                    "detail": json.dumps(d["detail"]) if d["detail"] else None,
                    "at": d["at"],
                },
            )
            count_evt += 1

        # Wizard sessions
        try:
            rows = duck.execute("SELECT * FROM wizard_sessions").fetchall()
            cols = [desc[0] for desc in duck.description]
            for row in rows:
                d = dict(zip(cols, row))
                if isinstance(d.get("payload"), str):
                    d["payload"] = json.loads(d["payload"]) if d["payload"] else None
                conn.execute(
                    text("""
                    INSERT INTO xenon.wizard_sessions
                        (session_id, ticker, state, structure_name, intent,
                         payload, current_attempt_id, created_at, updated_at)
                    VALUES (:session_id, :ticker, :state, :structure_name,
                            :intent, CAST(:payload AS jsonb), :current_attempt_id,
                            :created_at, :updated_at)
                    ON CONFLICT (session_id) DO NOTHING
                """),
                    {
                        "session_id": d["session_id"],
                        "ticker": d["ticker"],
                        "state": d["state"],
                        "structure_name": d.get("structure_name"),
                        "intent": d.get("intent"),
                        "payload": (json.dumps(d["payload"]) if d.get("payload") else None),
                        "current_attempt_id": d.get("current_attempt_id"),
                        "created_at": d.get("created_at"),
                        "updated_at": d.get("updated_at"),
                    },
                )
                count_ws += 1
        except Exception as _cat_exc:  # duckdb.CatalogException when table missing
            print("  SKIP wizard_sessions (table not found)")

        # Wizard events (DuckDB table is wizard_session_events)
        try:
            rows = duck.execute("SELECT * FROM wizard_session_events").fetchall()
            cols = [desc[0] for desc in duck.description]
            for row in rows:
                d = dict(zip(cols, row))
                if isinstance(d.get("detail"), str):
                    d["detail"] = json.loads(d["detail"]) if d["detail"] else None
                conn.execute(
                    text("""
                    INSERT INTO xenon.wizard_events (session_id, kind, detail, "at")
                    VALUES (:session_id, :kind, CAST(:detail AS jsonb), :at)
                """),
                    {
                        "session_id": d["session_id"],
                        "kind": d["kind"],
                        "detail": (json.dumps(d["detail"]) if d["detail"] else None),
                        "at": d["at"],
                    },
                )
                count_we += 1
        except Exception as _cat_exc:  # duckdb.CatalogException when table missing
            print("  SKIP wizard_session_events (table not found)")

        # Wizard combo attempts
        try:
            rows = duck.execute("SELECT * FROM wizard_combo_attempts").fetchall()
            cols = [desc[0] for desc in duck.description]
            for row in rows:
                d = dict(zip(cols, row))
                for json_col in ("legs", "combo_contract"):
                    if isinstance(d.get(json_col), str):
                        d[json_col] = json.loads(d[json_col]) if d[json_col] else None
                conn.execute(
                    text("""
                    INSERT INTO xenon.wizard_combo_attempts
                        (attempt_id, session_id, ticker, structure_name, legs,
                         combo_contract, ib_order_id, perm_id,
                         placing_client_id, limit_price, state, reason_code,
                         filled_qty, avg_fill_price, modify_sequence,
                         submitted_at, updated_at)
                    VALUES (:attempt_id, :session_id, :ticker, :structure_name,
                            CAST(:legs AS jsonb), CAST(:combo_contract AS jsonb), :ib_order_id,
                            :perm_id, :placing_client_id, :limit_price, :state,
                            :reason_code, :filled_qty, :avg_fill_price,
                            :modify_sequence, :submitted_at, :updated_at)
                    ON CONFLICT (attempt_id) DO NOTHING
                """),
                    {
                        "attempt_id": d["attempt_id"],
                        "session_id": d["session_id"],
                        "ticker": d.get("ticker", ""),
                        "structure_name": d.get("structure_name"),
                        "legs": (json.dumps(d.get("legs")) if d.get("legs") else None),
                        "combo_contract": (json.dumps(d.get("combo_contract")) if d.get("combo_contract") else None),
                        "ib_order_id": (str(d["ib_order_id"]) if d.get("ib_order_id") is not None else None),
                        "perm_id": (str(d["perm_id"]) if d.get("perm_id") is not None else None),
                        "placing_client_id": d.get("placing_client_id"),
                        "limit_price": d.get("limit_price"),
                        "state": d.get("state", ""),
                        "reason_code": d.get("reason_code"),
                        "filled_qty": d.get("filled_qty", 0),
                        "avg_fill_price": d.get("avg_fill_price"),
                        "modify_sequence": d.get("modify_sequence", 0),
                        "submitted_at": d.get("submitted_at"),
                        "updated_at": d.get("updated_at"),
                    },
                )
                count_wca += 1
        except Exception as _cat_exc:  # duckdb.CatalogException when table missing
            print("  SKIP wizard_combo_attempts (table not found)")

        # Wizard protection
        try:
            rows = duck.execute("SELECT * FROM wizard_protection").fetchall()
            cols = [desc[0] for desc in duck.description]
            for row in rows:
                d = dict(zip(cols, row))
                if isinstance(d.get("config"), str):
                    d["config"] = json.loads(d["config"]) if d["config"] else {}
                conn.execute(
                    text("""
                    INSERT INTO xenon.wizard_protection
                        (session_id, attempt_id, protection_type, config,
                         state, triggered_at, created_at)
                    VALUES (:session_id, :attempt_id, :protection_type,
                            CAST(:config AS jsonb), :state, :triggered_at, :created_at)
                """),
                    {
                        "session_id": d["session_id"],
                        "attempt_id": d.get("attempt_id"),
                        "protection_type": d.get("protection_type", ""),
                        "config": json.dumps(d.get("config", {})),
                        "state": d.get("state", "active"),
                        "triggered_at": d.get("triggered_at"),
                        "created_at": d.get("created_at"),
                    },
                )
                count_wp += 1
        except Exception as _cat_exc:  # duckdb.CatalogException when table missing
            print("  SKIP wizard_protection (table not found)")

    duck.close()
    print(
        f"  orders.duckdb → {count_sub} submissions, {count_evt} events, "
        f"{count_ws} wizard sessions, {count_we} wizard events, "
        f"{count_wca} combo attempts, {count_wp} protections"
    )
    return count_sub + count_evt


def migrate_scan_results(engine):
    with engine.connect() as conn:
        existing = conn.execute(text("SELECT count(*) FROM xenon.scan_results")).scalar()
        if existing > 0:
            print(f"  SKIP scan results (already has {existing} rows)")
            return 0
    count = 0
    with engine.begin() as conn:
        for scan_type, filename in [
            ("watchlist", "scanner.json"),
            ("discover", "discover.json"),
            ("gex", "gex.json"),
            ("vcg", "vcg.json"),
            ("cri", "cri.json"),
        ]:
            path = DATA_DIR / filename
            if not path.exists():
                continue
            with open(path) as f:
                data = json.load(f)
            conn.execute(
                text("""
                INSERT INTO xenon.scan_results (scan_type, payload)
                VALUES (:scan_type, CAST(:payload AS jsonb))
            """),
                {"scan_type": scan_type, "payload": json.dumps(data)},
            )
            count += 1

        # CRI series
        cri_dir = DATA_DIR / "cri_scheduled"
        if cri_dir.exists():
            for f in sorted(cri_dir.glob("*.json")):
                with open(f) as fh:
                    data = json.load(fh)
                conn.execute(
                    text("""
                    INSERT INTO xenon.cri_series (cri_level, alert, payload)
                    VALUES (:level, :alert, CAST(:payload AS jsonb))
                """),
                    {
                        "level": _extract_cri_level(data),
                        "alert": data.get("alert", False),
                        "payload": json.dumps(data),
                    },
                )
                count += 1

    print(f"  scan results → {count} rows")
    return count


def migrate_uw_data(engine):
    count = 0
    with engine.begin() as conn:
        # UW analyze cache
        path = DATA_DIR / "uw_analyze_cache.json"
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            data.pop("_checksum", None)
            # Handle both flat {ticker: {...}} and nested {entries: {ticker: {...}}}
            entries = data.get("entries", data) if isinstance(data, dict) else data
            if not isinstance(entries, dict):
                entries = {}
            for ticker, entry in entries.items():
                if ticker.startswith("_") or not isinstance(entry, dict):
                    continue
                conn.execute(
                    text("""
                    INSERT INTO xenon.uw_analyze_snapshots
                        (ticker, vrp_state, regime, flow_signals, portfolio_score)
                    VALUES (:ticker, CAST(:vrp AS jsonb), CAST(:regime AS jsonb),
                            CAST(:flow AS jsonb), :score)
                """),
                    {
                        "ticker": ticker,
                        "vrp": json.dumps(entry.get("vrp_state")),
                        "regime": json.dumps(entry.get("regime")),
                        "flow": json.dumps(entry.get("flow_signals")),
                        "score": entry.get("portfolio_score"),
                    },
                )
                count += 1

        # UW flow events — JSON format is {"events": {event_id: event_dict}}
        path = DATA_DIR / "uw_unusual_flow_log.json"
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            events_raw = data.get("events", data) if isinstance(data, dict) else data
            if isinstance(events_raw, dict):
                events_list = []
                for eid, evt in events_raw.items():
                    evt.setdefault("id", eid)
                    events_list.append(evt)
            elif isinstance(events_raw, list):
                events_list = events_raw
            else:
                events_list = []
            for evt in events_list:
                conn.execute(
                    text("""
                    INSERT INTO xenon.uw_flow_events
                        (flow_event_key, ticker, side, strike, expiry, detected_at, initial,
                         daily_track, status, anomaly_reason, closed_at)
                    VALUES (:key, :ticker, :side, :strike, :expiry, :detected_at,
                            CAST(:initial AS jsonb), CAST(:track AS jsonb), :status,
                            :reason, :closed_at)
                    ON CONFLICT (flow_event_key) DO NOTHING
                """),
                    {
                        "key": evt.get("id"),
                        "ticker": evt.get("ticker", ""),
                        "side": evt.get("side"),
                        "strike": evt.get("strike"),
                        "expiry": evt.get("expiry"),
                        "detected_at": evt.get("detected_at"),
                        "initial": json.dumps(evt.get("initial", {})),
                        "track": json.dumps(evt.get("daily_track")),
                        "status": evt.get("status", "open"),
                        "reason": evt.get("anomaly_reason"),
                        "closed_at": evt.get("closed_at"),
                    },
                )
                count += 1

        # UW API stats — JSON format is {"buckets": {hour_key: bucket_dict}}
        path = DATA_DIR / "uw_api_stats_history.json"
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            buckets = data.get("buckets", {}) if isinstance(data, dict) else {}
            if not isinstance(buckets, dict):
                buckets = {}
            for hour_key, bucket in buckets.items():
                if not isinstance(bucket, dict):
                    continue
                conn.execute(
                    text("""
                    INSERT INTO xenon.uw_api_stats
                        (bucket_hour, requests, cache_hits, latency_sum,
                         latency_count, status_2xx, status_4xx, status_5xx)
                    VALUES (:hour, :req, :cache, :lat_sum, :lat_count,
                            :s2xx, :s4xx, :s5xx)
                    ON CONFLICT (bucket_hour) DO NOTHING
                """),
                    {
                        "hour": hour_key,
                        "req": int(bucket.get("requests_2xx", 0))
                        + int(bucket.get("requests_4xx", 0))
                        + int(bucket.get("requests_5xx", 0)),
                        "cache": int(bucket.get("cached", 0)),
                        "lat_sum": float(bucket.get("sum_latency_ms", 0.0)),
                        "lat_count": int(bucket.get("latency_count", 0)),
                        "s2xx": int(bucket.get("requests_2xx", 0)),
                        "s4xx": int(bucket.get("requests_4xx", 0)),
                        "s5xx": int(bucket.get("requests_5xx", 0)),
                    },
                )
                count += 1

    print(f"  UW data → {count} rows")
    return count


def migrate_uw_history(engine):
    """Import uw_analyze_history/ archive into uw_analyze_snapshots."""
    history_dir = DATA_DIR / "uw_analyze_history"
    if not history_dir.exists():
        print("  SKIP uw_analyze_history/ (not found)")
        return 0

    ticker_dirs = sorted(d for d in history_dir.iterdir() if d.is_dir())
    total = 0
    batch = []
    BATCH_SIZE = 500

    def flush(conn, batch):
        if not batch:
            return
        conn.execute(
            text("""
            INSERT INTO xenon.uw_analyze_snapshots
                (ticker, vrp_state, regime, flow_signals, portfolio_score, snapshot_at)
            VALUES (:ticker, CAST(:vrp AS jsonb), CAST(:regime AS jsonb),
                    CAST(:flow AS jsonb), :score, :snapshot_at)
            """),
            batch,
        )
        batch.clear()

    with engine.begin() as conn:
        for ticker_dir in ticker_dirs:
            ticker = ticker_dir.name
            files = sorted(ticker_dir.glob("*.json"))
            for f in files:
                try:
                    with open(f) as fh:
                        data = json.load(fh)
                except (json.JSONDecodeError, OSError):
                    continue
                current = data.get("current", {})
                if not isinstance(current, dict):
                    continue
                archived_at = data.get("archived_at")
                batch.append(
                    {
                        "ticker": current.get("ticker", ticker),
                        "vrp": json.dumps(current.get("vrp_state")),
                        "regime": json.dumps(current.get("regime")),
                        "flow": json.dumps(current.get("flow_signals")),
                        "score": current.get("portfolio_score"),
                        "snapshot_at": archived_at,
                    }
                )
                total += 1
                if len(batch) >= BATCH_SIZE:
                    flush(conn, batch)
        flush(conn, batch)

    print(f"  uw_analyze_history/ → {total} snapshots ({len(ticker_dirs)} tickers)")
    return total


def migrate_caches(engine):
    count = 0
    with engine.begin() as conn:
        # Analyst ratings
        path = DATA_DIR / "analyst_ratings_cache.json"
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            for ticker, ratings in data.items():
                conn.execute(
                    text("""
                    INSERT INTO xenon.ticker_cache (ticker, cache_type, data, updated_at)
                    VALUES (:ticker, 'analyst_ratings', CAST(:data AS jsonb), now())
                    ON CONFLICT (ticker, cache_type)
                    DO UPDATE SET data = CAST(:data AS jsonb), updated_at = now()
                """),
                    {"ticker": ticker, "data": json.dumps(ratings)},
                )
                count += 1

        # Company info
        cache_dir = DATA_DIR / "company_info_cache"
        if cache_dir.exists():
            for f in cache_dir.glob("*.json"):
                ticker = f.stem
                with open(f) as fh:
                    data = json.load(fh)
                conn.execute(
                    text("""
                    INSERT INTO xenon.ticker_cache (ticker, cache_type, data, updated_at)
                    VALUES (:ticker, 'company_info', CAST(:data AS jsonb), now())
                    ON CONFLICT (ticker, cache_type)
                    DO UPDATE SET data = CAST(:data AS jsonb), updated_at = now()
                """),
                    {"ticker": ticker, "data": json.dumps(data)},
                )
                count += 1

        # Seasonality
        cache_dir = DATA_DIR / "seasonality_cache"
        if cache_dir.exists():
            for f in cache_dir.glob("*.json"):
                ticker = f.stem
                with open(f) as fh:
                    data = json.load(fh)
                conn.execute(
                    text("""
                    INSERT INTO xenon.ticker_cache (ticker, cache_type, data, updated_at)
                    VALUES (:ticker, 'seasonality', CAST(:data AS jsonb), now())
                    ON CONFLICT (ticker, cache_type)
                    DO UPDATE SET data = CAST(:data AS jsonb), updated_at = now()
                """),
                    {"ticker": ticker, "data": json.dumps(data)},
                )
                count += 1

    print(f"  caches → {count} rows")
    return count


def verify(engine):
    with engine.connect() as conn:
        tables = [
            "xenon.positions",
            "xenon.account_snapshots",
            "xenon.trades",
            "xenon.nav_history",
            "xenon.order_submissions",
            "xenon.order_events",
            "xenon.wizard_sessions",
            "xenon.wizard_events",
            "xenon.wizard_combo_attempts",
            "xenon.wizard_protection",
            "xenon.scan_results",
            "xenon.cri_series",
            "xenon.uw_analyze_snapshots",
            "xenon.uw_flow_events",
            "xenon.uw_api_stats",
            "xenon.ticker_cache",
        ]
        print("\n--- Verification ---")
        for table in tables:
            result = conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
            count = result.scalar()
            print(f"  {table}: {count} rows")


def main():
    print("=== Xenon Postgres Migration ===\n")

    if "DATABASE_URL" not in os.environ:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    engine = get_engine()

    print("Phase 1: Schema (handled by alembic upgrade head)\n")

    print("Phase 2: Critical data")
    migrate_portfolio(engine)
    migrate_nav_history(engine)
    migrate_trade_log(engine)
    migrate_orders_duckdb(engine)

    print("\nPhase 3: Scanner data")
    migrate_scan_results(engine)

    print("\nPhase 4: UW data")
    migrate_uw_data(engine)

    print("\nPhase 4b: UW history archive")
    migrate_uw_history(engine)

    print("\nPhase 5: Caches")
    migrate_caches(engine)

    print("\nPhase 6: Verify")
    verify(engine)

    print("\n=== Migration complete ===")


if __name__ == "__main__":
    main()
