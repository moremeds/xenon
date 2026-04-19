# Apex R2 ETL — Cutover Runbook

Operator procedure for enabling the new R2-backed nightly ETL and verifying the scanner reads correctly from it. Spec: `docs/superpowers/specs/2026-04-16-apex-r2-etl-design.md` (on `trend-scan-cleanup` anchor branch). Plan with amendments: same directory, `-apex-r2-etl.md`.

**Pre-reqs:** R2 credentials in `.env` (`R2_ENDPOINT`, `R2_BUCKET=apex-data`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`). `MASSIVE_API_KEY` valid. Branch `feat/apex-r2-etl` green on CI.

## 1. Pre-flight

```bash
cd /Users/chenxi/projects/xenon
python3.13 -m pytest scripts/tests/test_r2_store.py \
  scripts/tests/test_parquet_store.py \
  scripts/tests/test_apex_sync.py \
  scripts/tests/test_apex_refresh.py \
  scripts/tests/test_ta_lib \
  scripts/tests/test_trend_scan.py \
  scripts/tests/test_trend_scan_lib -x
```

Expected: all pass.

## 2. Dry-run the producer locally (3-ticker smoke)

```bash
set -a && source .env && set +a
python3.13 scripts/fetchers/fetch_apex_data.py --mode full --dry-run --timeframes 1d --max-workers 3
```

Expected: writes to `data/apex_mirror_preview/`. Verify schemas:

```bash
python3.13 -c "
import pyarrow.parquet as pq
import glob
for p in sorted(glob.glob('data/apex_mirror_preview/parquet/historical/1d/*.parquet'))[:3]:
    print(p, pq.read_schema(p))
"
```

Timestamp type must be `timestamp[us, tz=UTC]`. Volume must be `int64`. No `__index_level_0__` column.

## 3. Coordinate with R2 bucket co-owners

Before the first live full refresh, confirm:

1. **External producer has stopped writing `parquet/historical/`.** Check `meta/last_updated.json` on R2 — the `historical` timestamp should not be moving from their runs.
2. **`signals/` pipeline** is either (a) not reading our historical parquets directly, or (b) can tolerate the tz-label flip from `Asia/Hong_Kong` to `UTC`. Daily dates are unchanged; hourly wall-clock values shift by 13 hours (the HKT label was wrong by 13h). If they can't adapt, consider a compat mirror under `parquet_historical_v1_compat/`.

Document contacts in the PR description.

## 4. First manual full refresh via GitHub Actions

```
GitHub UI → Actions → "Apex Data Refresh" → Run workflow
  branch: feat/apex-r2-etl
  mode: full
  timeframes: 1d,1h
```

Expected: ~30 min wall-clock. Watch for the `_FAILURE_RATIO_ABORT` log line — if more than 50% of tickers fail, the run returns non-zero and the manifest is NOT updated (per A4).

On success, verify:

```bash
python3.13 -c "
from scripts.ta_lib.r2_store import R2Store
r2 = R2Store()
print('Manifest:', r2.get_json('meta/last_updated.json'))
print('Data quality summary:', r2.get_json('meta/data_quality.json')['by_status'])
"
```

## 5. Shadow scan

```bash
# Sync the fresh mirror and run the scanner locally
python3.13 scripts/trend_scan.py --top 25 > /tmp/scan_new.json
jq '.candidates | length, .stage_a_survivors, .stage_b_survivors' /tmp/scan_new.json
```

Target: non-zero candidates; `stage_a_survivors` ≤ 200 (cfg cap is 100 per direction); `stage_b_survivors` ≤ 50.

Optional: baseline comparison against a scan from `trend-scan-cleanup` (checkout that branch, run, diff top-25 tickers). ≥60% overlap is the informal "looks right" bar.

## 6. Enable cron (merge PR)

Merging to `master` activates the scheduled triggers:

- Tue–Sat 01:00 UTC — incremental (with A18 session-completeness guard that defers if Massive hasn't published yesterday's SPY daily bar yet)
- Sat 05:00 UTC — full refresh (all ~2y rebuilt)

## 7. Week-long soak

Monitor:

- Action runs (green / defer / fail) — `Actions` tab in GitHub
- R2 storage cost — Cloudflare dashboard
- `meta/data_quality.json` `by_status` over the week
- Scanner output `data/trend_scan.json` — candidates, `warnings` array (populated by `apex_sync.SyncResult.errors`)

Keep `trend-scan-cleanup` branch parked as the rollback anchor.

## 8. Cleanup (post-soak)

After one clean week:

```bash
# Retire the anchor branch
git branch -D trend-scan-cleanup
git push origin --delete trend-scan-cleanup

# Prune any lingering local caches
rm -rf data/ta.duckdb
rm -rf data/apex_mirror_preview
```

Apply amendment A20 to the spec while it's still accessible: in `docs/superpowers/specs/2026-04-16-apex-r2-etl-design.md` §6, change `rsi > 50` to `rsi > 40 / rsi < 60` (matches `scripts/trend_scan_lib/stages/ta_prefilter.py`).

## Rollback

1. **Disable the workflow:** GitHub UI → Actions → "Apex Data Refresh" → three-dot menu → "Disable workflow". The scanner continues to read whatever state the mirror is in.
2. **If the scanner is also broken:** `git revert <merge-commit-sha>` and push. The pre-Apex code paths on `trend-scan-cleanup` are the safe fallback if you need to bisect.
3. **R2 contents are not destroyed** by rollback — we just stop writing. The external producer's `meta/universe.json` is read-only for us; it's unaffected.

## Troubleshooting

| Symptom                                                                    | Likely cause                                          | Action                                                                                                 |
| -------------------------------------------------------------------------- | ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| GH Action exits with "Massive has not published SPY 1d for YYYY-MM-DD yet" | A18 defer; Massive is slow for that day               | Re-run workflow manually 30+ min later                                                                 |
| Scanner logs `Apex sync errors: [...]`                                     | R2 partial download, tmp cleaned up, mirror untouched | Next run will retry; check R2 health                                                                   |
| Scanner raises `SchemaVersionError`                                        | Action wrote schema_version we don't know             | Update `_SUPPORTED_SCHEMA_VERSIONS` in `scripts/ta_lib/apex_sync.py` after reviewing the schema change |
| Manifest ETag race in Action logs                                          | Two overlapping runs (workflow_dispatch + cron)       | Harmless; A16 retry loop handles it                                                                    |
| `_compute_indicators_adapter` drops `high_52w` as NaN                      | Ticker has <252 daily bars                            | Expected; scanner coerces NaN → 0.0 at the boundary                                                    |
