"""Print a sanity report after running normalize_payloads + backfills.

Usage:
  uv run python scripts/migrations/2026_04_26_verify_normalize_payloads.py
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine, text

SUMMARY_QUERIES = {
    "uw_analyze_snapshots": """
        SELECT count(*) AS rows,
               count(report) AS report_non_null,
               count(display) AS display_non_null,
               count(derived) AS derived_non_null,
               count(dark_pool_summary) AS dp_non_null,
               count(options_flow_summary) AS of_non_null,
               count(price) AS price_extracted,
               count(grade) AS grade_extracted,
               count(regime_label) AS regime_extracted
        FROM xenon.uw_analyze_snapshots;
    """,
    "uw_analyze_flow_alerts": "SELECT count(*) AS rows, count(DISTINCT snapshot_id) AS snapshots FROM xenon.uw_analyze_flow_alerts;",
    "uw_analyze_gex_strikes": "SELECT count(*) AS rows, count(DISTINCT snapshot_id) AS snapshots FROM xenon.uw_analyze_gex_strikes;",
    "uw_analyze_short_volume_trend": "SELECT count(*) AS rows, count(DISTINCT snapshot_id) AS snapshots FROM xenon.uw_analyze_short_volume_trend;",
    "cri_series": """
        SELECT count(*) AS rows,
               count(vix) AS vix_extracted,
               count(cri_score) AS cri_score_extracted,
               count(*) FILTER (WHERE crash_trigger_fired) AS crash_triggered
        FROM xenon.cri_series;
    """,
    "vcg_series": """
        SELECT count(*) AS rows,
               count(vcg) AS vcg_extracted,
               count(*) FILTER (WHERE regime IS NOT NULL) AS with_regime
        FROM xenon.vcg_series;
    """,
    "gex_snapshots": "SELECT count(*) AS rows, count(DISTINCT ticker) AS tickers FROM xenon.gex_snapshots;",
    "scan_results": "SELECT count(*) AS rows, count(DISTINCT scan_type) AS scan_types FROM xenon.scan_results;",
    "uw_flow_events": """
        SELECT count(*) AS rows,
               count(*) FILTER (WHERE daily_track IS NOT NULL) AS with_daily_track,
               count(initial_premium_usd) AS premium_extracted
        FROM xenon.uw_flow_events;
    """,
    "uw_flow_event_ticks": "SELECT count(*) AS rows, count(DISTINCT event_id) AS events FROM xenon.uw_flow_event_ticks;",
    "uw_api_stats": "SELECT count(*) AS buckets, min(bucket_hour) AS earliest, max(bucket_hour) AS latest FROM xenon.uw_api_stats;",
}


def main() -> None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)
    sync_url = url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    engine = create_engine(sync_url, pool_pre_ping=True)
    try:
        with engine.begin() as conn:
            for table, query in SUMMARY_QUERIES.items():
                print(f"\n=== {table} ===")
                row = conn.execute(text(query)).first()
                if row is None:
                    print("  (no rows)")
                    continue
                for key, value in row._mapping.items():
                    print(f"  {key}: {value}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
