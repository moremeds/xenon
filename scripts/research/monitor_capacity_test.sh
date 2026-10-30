#!/usr/bin/env bash
# Live monitor for the option-chain capacity test.
#
# Run once for a snapshot, or:
#   watch -n 30 bash scripts/research/monitor_capacity_test.sh
set -euo pipefail
cd "$(dirname "$0")/../.."

DB="postgresql://localhost/optionchain_captest"
LOG=$(ls -t /tmp/oc-capacity-*.log 2>/dev/null | head -1)
DAEMON_PID=$(ps aux | awk '/python.*xenon\.option_chain_snapshotter/{print $2}' | head -1 || true)

now_et() {
  TZ=America/New_York date +"%H:%M:%S ET"
}

hr() { printf '%0.s─' {1..72}; echo; }

echo
printf "  ★  OPTION CHAIN CAPACITY MONITOR  ─  %s\n" "$(now_et)"
hr

# ── 1. Daemon health ───────────────────────────────────────────────────────
if [[ -n "$DAEMON_PID" ]]; then
  ELAPSED=$(ps -o etime= -p "$DAEMON_PID" 2>/dev/null | xargs || echo "?")
  printf "  Daemon  PID=%-7s  uptime=%-10s  ✓ alive\n" "$DAEMON_PID" "$ELAPSED"
else
  printf "  Daemon  ✗ NOT RUNNING\n"
fi

# ── 2. Log tail (last 8 lines) ─────────────────────────────────────────────
if [[ -n "$LOG" ]]; then
  printf "  Log: %s\n" "$LOG"
  hr
  tail -8 "$LOG" | sed 's/^/  /'
else
  printf "  Log: not found\n"
fi
hr

# ── 3. DB: snapshot_run summary ────────────────────────────────────────────
printf "  SNAPSHOT RUNS\n"
psql "$DB" -tAX 2>/dev/null <<'SQL' | column -t -s '|' | sed 's/^/  /' || echo "  (DB unreachable)"
SELECT
  ticker,
  to_char(started_at AT TIME ZONE 'America/New_York', 'HH24:MI:SS') AS started_et,
  status,
  coalesce(contracts_attempted::text, '?') AS attempted,
  coalesce(contracts_persisted::text, '?') AS persisted,
  coalesce(round(duration_ms/1000.0, 1)::text, '?') AS dur_s,
  coalesce(ib_lines_peak::text, '?') AS lines,
  coalesce(round(contracts_attempted / NULLIF(duration_ms/1000.0,0), 1)::text, '?') AS "c/s"
FROM archive.snapshot_run
ORDER BY started_at DESC
LIMIT 16;
SQL

hr

# ── 4. DB: option_chain row counts and latest ts ───────────────────────────
printf "  OPTION_CHAIN TABLE\n"
psql "$DB" -AX 2>/dev/null <<'SQL' | column -t -s '|' | sed 's/^/  /' || echo "  (DB unreachable)"
SELECT
  count(*) AS total_rows,
  count(DISTINCT snapshot_ts) AS distinct_cycles,
  coalesce(to_char(max(snapshot_ts) AT TIME ZONE 'America/New_York', 'HH24:MI:SS'), '—') AS latest_snapshot_et,
  count(DISTINCT ticker) AS tickers
FROM archive.option_chain;
SQL

hr

# ── 5. DB table sizes ──────────────────────────────────────────────────────
printf "  TABLE SIZES\n"
psql "$DB" -tAX 2>/dev/null <<'SQL' | column -t -s '|' | sed 's/^/  /' || echo "  (DB unreachable)"
SELECT
  relname AS table,
  pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size,
  pg_size_pretty(pg_relation_size(c.oid)) AS data_size,
  pg_size_pretty(pg_indexes_size(c.oid)) AS index_size,
  reltuples::bigint AS est_rows
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'archive' AND c.relkind = 'r'
ORDER BY pg_total_relation_size(c.oid) DESC;
SQL

hr

# ── 6. IB line usage (from latest completed run per ticker) ────────────────
printf "  IB LINE USAGE (latest completed run per ticker)\n"
psql "$DB" -tAX 2>/dev/null <<'SQL' | column -t -s '|' | sed 's/^/  /' || echo "  (DB unreachable)"
SELECT
  ticker,
  ib_lines_peak AS lines_peak,
  contracts_attempted AS attempted,
  round(duration_ms/1000.0, 1) AS dur_s,
  round(contracts_attempted / NULLIF(duration_ms/1000.0,0), 1) AS "c/s",
  CASE
    WHEN contracts_attempted > 0
    THEN round(19724.0 / NULLIF(contracts_attempted / NULLIF(duration_ms/1000.0,0),0) / 60, 1)::text || ' min'
    ELSE '?'
  END AS spx_full_chain_est
FROM (
  SELECT DISTINCT ON (ticker)
    ticker, ib_lines_peak, contracts_attempted, duration_ms
  FROM archive.snapshot_run
  WHERE status = 'ok'
  ORDER BY ticker, started_at DESC
) t;
SQL

hr

# ── 7. Error summary (last 50 log lines for IB errors) ────────────────────
if [[ -n "$LOG" ]]; then
  IB_ERRS=$(grep -c "Error\|10187\|CRITICAL\|error" "$LOG" 2>/dev/null || true)
  printf "  IB/error lines in log: %s\n" "$IB_ERRS"
  grep "Error\|10187\|CRITICAL" "$LOG" 2>/dev/null | tail -5 | sed 's/^/    /' || true
fi

echo
