# Massive.com Historical Data — Cutover Runbook

Operator-run procedure for switching historical OHLCV from IB to Massive.com.
Execute in order. Each step is idempotent unless otherwise noted.

**Pre-reqs:** `MASSIVE_API_KEY` set in root `.env`. Weekend / off-hours preferred.

**Spec:** `docs/superpowers/specs/2026-04-15-massive-historical-data-migration-design.md`
**Plan:** `docs/superpowers/plans/2026-04-15-massive-historical-data-migration.md`

---

## Step 1 — Pre-flight: API key + smoke test

```bash
cd /Users/chenxi/projects/xenon
TZ=America/New_York date +"%A %H:%M"          # confirm weekend / off-hours
python3.13 -c "from scripts.clients.massive_client import MassiveClient; \
  print(MassiveClient().health_check())"
# Expect: True
```

**Pre-validated on agent run:** `health_check: True` at Wednesday 12:22 ET.

## Step 2 — Backup existing DuckDB

```bash
cp data/ta.duckdb data/ta.duckdb.pre-massive.bak
ls -lh data/ta.duckdb*
```

Keep the `.bak` file until rollback window (7 days) closes.

## Step 3 — Re-seed 2 years, both timeframes

```bash
python3.13 scripts/ta_reseed_massive.py --timeframes 1d,1h --lookback-days 730
```

Expected: runs for ~several minutes; summary `Done. 0 failures.` (<5 failures
acceptable for thin tickers). If >20 failures, STOP and investigate — likely
a rate-limit or schema mismatch, not a seed-size problem.

## Step 4 — Verify stats

```bash
python3.13 scripts/ta_cli.py --stats
```

Expected:

- ~500 1d bars/ticker (2 years × ~252 trading days)
- ~3500 1h bars/ticker (2 years × ~6.5h RTH per day)

Missing 1h data for a ticker is the most likely edge case — see §10 Risks.

## Step 5 — Spot-check against IB TWS

```bash
python3.13 scripts/ta_cli.py AAPL TSLA SPY --history | tail -20
```

Visually compare last 5 closes against IB TWS or a trusted chart. Record
ticker / date / IB close / Massive close / divergence in the PR description.
**Any divergence >0.5% warrants investigation before continuing.**

## Step 6 — Run E2E-3 (vendor divergence gate) — must pass

```bash
XENON_ENABLE_IB_E2E=1 python3.13 -m pytest -m e2e -xvs \
  scripts/tests/test_e2e_massive_pipeline.py::test_e2e_3_vendor_divergence_ib_vs_massive
```

Expected: all three tickers pass with `<0.5%` close-price divergence on last
5 bars. Requires IB Gateway running locally.

## Step 7 — Dry-run pre-market prep

```bash
python3.13 scripts/ta_premarket_prep.py --audit-only
# Expect exit 0, no network activity
unset MASSIVE_API_KEY
python3.13 scripts/ta_premarket_prep.py --audit-only
# Expect exit 0 still — offline audit must not require the key
export MASSIVE_API_KEY=<your-key>
```

## Step 8 — Run trend-scan off new cache

```bash
python3.13 scripts/trend_scan.py
```

Expected: output candidate count within ±20% of prior baseline. Check
`data/trend_scan.json` for:

- `"warnings": []` (no status-file staleness)
- Candidate counts sane

## Step 9 — FastAPI scheduler smoke (optional but recommended)

Start the server, tail logs, verify the 6 AM ET pre-market loop handles exit
code 3 without crashing:

```bash
MASSIVE_API_KEY=invalid python3.13 scripts/ta_premarket_prep.py; echo "exit=$?"
# Expect exit=3
cat data/ta_premarket_status.json
# Expect: {"status": "aborted", "failure_reason": "vendor_down", ...}
```

Confirm `scripts/api/server.py` `run_script("ta_premarket_prep.py", ...)` logs
a WARN on exit=3 but doesn't crash the scheduler loop. No code change required
— verification only.

Restore your real API key after this test.

## Step 10 — Record in PR

In the PR description, check off each runbook step and paste:

- `--stats` row counts (Step 4)
- Spot-check divergence table (Step 5)
- E2E-3 pytest output (Step 6)
- Cutover timestamp

---

## Rollback (if needed within 7 days)

```bash
mv data/ta.duckdb data/ta.duckdb.massive-failed
cp data/ta.duckdb.pre-massive.bak data/ta.duckdb
# Then revert the merge commit; or manually re-introduce IB in TAService if
# urgency is high. Spec §7.4.
```

## Known follow-ups (non-blocking)

1. **`pyproject.toml` `[project]` block has no `[build-system]`** — could break
   `pip install .` if anyone runs it. Mitigation: add `[build-system]` + empty
   `[tool.setuptools] packages = []` OR revert `[project]` addition in favor
   of `requirements-dev.txt`. Latent; repo uses `requirements.txt` today.
2. **Massive response body dropped on 4xx/5xx** in `massive_client.py` —
   `logger` is imported but unused at those raise sites. Add a
   `logger.warning("massive http %s for %s: %s", status, url, resp.text[:200])`
   before the raise to preserve debug info. Plan-neutral follow-up.
3. **Dividend-adjustment semantics** — Massive's `adjusted=true` is documented
   for splits; verify against a high-yield ticker (e.g., VZ or T) during Step 5
   spot-check if there's been an ex-dividend event in the last 5 bars.
4. **Yahoo residue** — `scripts/fetch_options.py` and `scripts/fetch_analyst_ratings.py`
   still import `yfinance` (for options chains / analyst ratings, not OHLCV).
   Out of scope for this migration; tracked separately if Yahoo needs to leave
   the codebase entirely.
