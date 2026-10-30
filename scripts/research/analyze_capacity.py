"""Summarize a live capacity run of the option-chain snapshotter.

Reads archive.snapshot_run + archive.option_chain from the run DB and reports,
per ticker and per cycle: duration, contracts attempted/persisted, rows written,
and the implied live throughput (contracts/sec). From the measured throughput it
extrapolates how long the FULL chain (per the live probe sizes) would take, so we
can answer "what can this stack actually capture in 10 min" with real numbers.

    DATABASE_URL=postgresql://localhost/optionchain_captest uv run python scripts/research/analyze_capacity.py
"""

from __future__ import annotations

import os

import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get("DATABASE_URL", "postgresql://localhost/optionchain_captest").replace(
    "postgresql+psycopg://", "postgresql://", 1
)

# Full-chain sizes measured live 2026-06-21 (probe_full_chain_feasibility.py).
FULL_CHAIN = {"SPX": 19724, "NDX": 8906, "RUT": 8000, "VIX": 1000}  # RUT approx (probe undercounted)


def main() -> None:
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        runs = conn.execute(
            """
            SELECT ticker, started_at, finished_at, status,
                   contracts_attempted, contracts_persisted, duration_ms, ib_lines_peak,
                   EXTRACT(EPOCH FROM (finished_at - started_at)) AS wall_s
            FROM archive.snapshot_run
            ORDER BY started_at
            """
        ).fetchall()
        total_rows = conn.execute("SELECT count(*) AS n FROM archive.option_chain").fetchone()["n"]
        distinct_cycles = conn.execute("SELECT count(DISTINCT snapshot_ts) AS n FROM archive.option_chain").fetchone()[
            "n"
        ]

    if not runs:
        print("No snapshot_run rows yet — daemon hasn't completed a cycle.")
        return

    print(
        f"{'ticker':5s} {'started (UTC)':19s} {'status':8s} {'att':>6s} {'pers':>6s} "
        f"{'dur_s':>6s} {'lines':>5s} {'c/s':>6s}"
    )
    print("-" * 72)
    per_ticker: dict[str, list[float]] = {}
    for r in runs:
        dur = (r["duration_ms"] or 0) / 1000 or (r["wall_s"] or 0)
        att = r["contracts_attempted"] or 0
        cps = (att / dur) if dur else 0
        per_ticker.setdefault(r["ticker"], []).append(cps)
        started = r["started_at"].strftime("%Y-%m-%d %H:%M:%S") if r["started_at"] else "?"
        print(
            f"{r['ticker']:5s} {started:19s} {r['status']:8s} {att:6d} "
            f"{r['contracts_persisted'] or 0:6d} {dur:6.1f} {r['ib_lines_peak'] or 0:5d} {cps:6.1f}"
        )

    print("-" * 72)
    print(f"option_chain rows total: {total_rows}   distinct snapshot cycles: {distinct_cycles}\n")

    print("=== implied throughput + full-chain extrapolation ===")
    for t, cps_list in per_ticker.items():
        avg_cps = sum(cps_list) / len(cps_list) if cps_list else 0
        full = FULL_CHAIN.get(t)
        if avg_cps > 0 and full:
            full_min = full / avg_cps / 60
            print(f"  {t}: avg {avg_cps:5.1f} contracts/s  ->  full chain ({full}) ≈ {full_min:4.1f} min/cycle")
        else:
            print(f"  {t}: avg {avg_cps:5.1f} contracts/s  (no full-chain estimate)")
    print("\n  fits-in-10min check: a ticker is feasible at 10-min cadence iff full-chain est < ~9 min")


if __name__ == "__main__":
    main()
