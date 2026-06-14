# Handover — Futu daily statement ingestion (PR #128 merged)

> **For the next session:** read this end-to-end before touching anything. It is self-contained — you do not need the prior conversation. The work that shipped is durable; what's left is one well-scoped follow-up (Task #8: `performance.py` wiring).

## What shipped (merged commit `c52d8043` on master)

**PR #128** — `feat(futu): ingest official daily statements from Outlook → PG`

A complete pipeline from Outlook IMAP (XOAUTH2 device flow) → AES-encrypted PDF decrypt (pikepdf) → text extract (pdfplumber) → typed parser → Postgres. Six format buckets covered:

| Bucket           | Account        | Format                                     | Period                          |
| ---------------- | -------------- | ------------------------------------------ | ------------------------------- |
| Modern composite | 5668 (current) | Universal Account margin (English)         | 2024-07-12 → present            |
| Legacy English   | 6337           | US Stocks Margin                           | 2021-01-18 → 2024-08-12         |
| Legacy English   | 6415           | HK Stocks Margin                           | 2020-08-25 → 2024-07-15         |
| Legacy English   | 5270           | US Fund                                    | 2021-02-16 → 2023-08-31         |
| Transition cover | 5668           | Universal-account migration (Aug-Dec 2024) | embedded in modern              |
| Trad. Chinese    | 6415/6337      | 港股保證金 / 美股孖展                      | parsed via `_LEGACY_LABELS` map |

The 5668 composite account is the operator's current consolidated account; the 3 legacy accounts (6337 US, 6415 HK, 5270 Fund) were closed in Jul-Aug 2024 with small residual balances (HKD 11–10K).

### Database state (in `core_dev` on `100.66.147.98`)

```
xenon.futu_daily_statement: 667 rows total
  ├─ 5270:   4 rows  USD  (2021-02 → 2023-08)
  ├─ 5668: 483 rows  HKD  (2024-07 → 2026-06)
  ├─ 6337: 144 rows  USD  (2021-01 → 2024-08)
  └─ 6415:  36 rows  HKD  (2020-08 → 2024-07)

xenon.futu_statement_inbox: 667 rows  (parse_error IS NULL on all)
```

Each row carries: `(starting|ending)_(nav|cash|funds|portfolio)_base`, per-currency JSONB breakouts, exchange rates, full `page_text` for ad-hoc grep, `transaction_totals` JSONB (Cash Movement + Portfolio Movement extracted; 652 categorized cash entries), and the raw encrypted PDF bytes.

### Continuity validation

`validate_continuity()` runs after every sync. With 5 HKD tolerance per boundary, **475 / 482 daily pairs on the 5668 modern series match within tolerance (98.5%)**. The remaining 7:

- **2 explained** by captured Cash Movement (deposits/withdrawals crossing the boundary).
- **5 explained as missing-from-source** — Futu did not send a statement on those days. Verified by querying the inbox table for the missing intermediate date: zero matches each time.
  - 2025-11-27 (US Thanksgiving, market closed)
  - 2025-12-25 (Christmas, market closed)
  - 2024-10-24 / 2025-12-16 / 2025-12-30 (no obvious market reason — likely email delivery hiccups on operator's mailbox)

**Do not treat these 5 as parser bugs.** The parser is correct on every PDF it received. If the operator finds the missing emails (junk folder, archive), re-running the bulk fetcher will pick them up — the inbox table is idempotent on `source_uid`.

## What is NOT shipped — Task #8 (deferred)

The Tasks list carries one in-progress item:

> #8. Update `performance.py` to consume daily-statement NAV from `futu_daily_statement` for FUTU broker.

Currently, `src/xenon/api/services/performance.py` reads NAV from `xenon.nav_snapshots` (the IB-side ledger). For the Futu broker tab, it should:

1. Detect `broker == 'FUTU'` in the scope.
2. Read NAV series from `xenon.futu_daily_statement.ending_nav_base` (or `starting_nav_base` for opening points), ordered by `statement_date`.
3. Use the official Futu time-weighted formula (see "Orphan test" below for what was scaffolded).

Cash flows for the TWR denominator come from `transaction_totals['cash_movements']` — Cash Movement rows are already categorized (Deposit / Withdrawal / Interest / Dividend / IPO / MMF Subscription / etc.) in the `cash_movements` JSONB array.

### Orphan test lesson (do not repeat)

The first push of PR #128 had a CI failure on `scripts/tests/test_futu_official_performance_formula.py`. It tried to load `scripts/research/validate_futu_official_performance.py` via `importlib`, but that validator script was never `git add`'d — it only existed in the original author's working copy. The test was deleted in the second commit of #128 to unblock the merge.

When you resume Task #8:

1. **Recreate the validator script** at `scripts/research/validate_futu_official_performance.py` with `NavPoint`, `CashFlow`, and `official_performance()` types — the test file (before deletion) defined the shape: `mod.NavPoint(date, Decimal)`, `mod.CashFlow(date, type_str, source_str, Decimal)`, `mod.official_performance(nav_points, cashflows, mode=...)`.
2. **Re-add the test** at the same path. Both must be committed together — verify with `git ls-files scripts/research/validate_futu_official_performance.py` before pushing.
3. Use Futu's published formula from their help center as the reference for the test cases (the deleted test had three: time-weighted, simple, and trade-income variants).
4. Wire `performance.py` last, with `pg_test_engine` fixtures that seed `futu_daily_statement` rows and assert the API returns the expected TWR.

## Operational commands

### Re-run the full sync (idempotent)

```bash
# from primary repo, after sourcing .env:
uv run xenon-futu-statement-sync                  # fetch all available
uv run xenon-futu-statement-sync --since 2026-01-01

# the bulk-fetch script writes raw bytes to futu_statement_inbox first;
# the regular sync drains parsed rows into futu_daily_statement.
uv run python scripts/research/bulk_fetch_futu_statements.py --limit 50 --dry-run
```

Env required: `OUTLOOK_USER`, `OUTLOOK_OAUTH_CLIENT_ID`, `FUTU_STATEMENT_PASSWORD`, `XENON_TRADING_MODE=live`, `XENON_BROKER_ACCOUNT=5668`, `DATABASE_URL`.

### Audit current state

```bash
# row counts per account
psql -h 100.66.147.98 -U xenon_dev core_dev -c "
  SELECT broker_account, COUNT(*), MIN(statement_date), MAX(statement_date), base_currency
  FROM xenon.futu_daily_statement WHERE broker='FUTU'
  GROUP BY 1, base_currency ORDER BY 1;
"

# inbox drain health (should be 0 still_failing)
psql -h 100.66.147.98 -U xenon_dev core_dev -c "
  SELECT COUNT(*) FILTER (WHERE parse_error IS NULL) parsed_ok,
         COUNT(*) FILTER (WHERE parse_error IS NOT NULL) still_failing
  FROM xenon.futu_statement_inbox WHERE broker='FUTU';
"

# inspect one statement's page_text + transaction_totals
psql -h 100.66.147.98 -U xenon_dev core_dev -c "
  SELECT statement_date, ending_nav_base, jsonb_pretty(transaction_totals)
  FROM xenon.futu_daily_statement
  WHERE broker='FUTU' AND broker_account='5668' AND statement_date='2026-06-01';
"
```

## Files of interest

| File                                               | Purpose                                                                                                              |
| -------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `src/xenon/clients/futu_statement_pdf.py`          | Typed parser; has both `parse()` (modern) and `parse_legacy()`. `parse_any()` dispatches by `_detect_legacy_format`. |
| `src/xenon/clients/outlook_imap.py`                | IMAP fetcher. **Token acquired BEFORE opening socket** to avoid Broken Pipe on device-code wait.                     |
| `src/xenon/clients/outlook_oauth.py`               | MSAL device-flow + cache. Clear AT but keep RT to force silent refresh after probes.                                 |
| `src/xenon/api/services/futu_statement_sync.py`    | Orchestrator. On parse failure, saves raw bytes to inbox table for offline re-parse.                                 |
| `src/xenon/cli/futu_statement_sync.py`             | `xenon-futu-statement-sync` entry point.                                                                             |
| `scripts/research/bulk_fetch_futu_statements.py`   | One-off historical bulk fetcher. Decodes RFC 2047 filenames, accepts `application/octet-stream` with `%PDF-` magic.  |
| `scripts/research/inspect_futu_statement.py`       | Single-PDF inspector — handy when adding a new format.                                                               |
| `scripts/research/validate_statement_nav.py`       | Ad-hoc continuity / NAV audit.                                                                                       |
| `src/xenon/db/queries/futu_history.py`             | All UPSERTs + lists, scoped by `AccountScope`.                                                                       |
| `src/xenon/db/migrations/versions/2026_06_03_*.py` | 3 migrations: `futu_daily_statement`, `futu_statement_inbox`, extra fields.                                          |
| `scripts/tests/test_futu_statement_pdf.py`         | Parser tests.                                                                                                        |
| `scripts/tests/test_futu_history_queries.py`       | Query-layer tests.                                                                                                   |

## Subtle gotchas captured in code (DO NOT regress)

These were each a multi-hour debug — there are tests that pin them, but the patterns are easy to break in a refactor:

1. **`Base Currency` regex must be same-line.** Use `[^\S\n]+` not `\s+` between the label and the 3-letter ISO code — otherwise it spans newlines and grabs `LLI` from `LLII CHEN` (cumulative across all rows: 252 wrong rows fixed via SQL UPDATE).
2. **Doubled-letter PDF rendering.** Some 2024-2025 PDFs render `Account` as `AAccccoouunntt` and `11,,880044..8877`. `_dedupe_doubled_letters` handles both alphabetic runs ≥4 chars and numeric runs ≥6 chars with a pair-equality check. 4-char uppercase tickers are protected.
3. **`re.MULTILINE` + `[\s\S]*?` is a footgun.** The non-greedy capture can collapse to empty when MULTILINE is on. For section regexes, use `\Z` anchor and drop MULTILINE.
4. **Inter-field whitespace.** Inside a row regex, use `[^\S\n]+` for spaces. `\s+` will span newlines and pull the next row's data into the current row's optional remarks field.
5. **`preparation_date` may be missing.** On legacy formats with no cover page. The dataclass has `Optional[date]`; the inserter substitutes `statement_date` when None to satisfy the NOT NULL column.
6. **Decimal → JSONB.** `_jsonable()` in `futu_statement_sync.py` recursively converts `Decimal` and `date` to strings. Adding a new JSONB field? Run it through `_jsonable`.
7. **Tailscale flakiness on long-running drain.** The bulk-fetch script uses `pool_pre_ping=True` + `pool_recycle=300`. Long sessions still drop occasionally — wrap the per-row insert in a 3-retry loop with a fresh engine on each retry.

## Where to start (Task #8 concretely)

1. Read `src/xenon/api/services/performance.py` — find where it builds the NAV series for the response.
2. Branch off master: `git switch -c feat/futu-performance-from-statements`.
3. Recreate `scripts/research/validate_futu_official_performance.py` (see "Orphan test lesson" above).
4. Re-add `scripts/tests/test_futu_official_performance_formula.py` — `git log c52d8043^ -- scripts/tests/test_futu_official_performance_formula.py` to recover the original.
5. Wire `performance.py` to read from `futu_daily_statement` when `scope.broker == 'FUTU'`.
6. Add a test in `scripts/tests/test_performance_service.py` using `pg_test_engine` fixture + seeded rows.
7. Run `uv run python scripts/infra/dev/run_pytest_affected.py` locally, then `cd web && npm test` for the frontend hook.

Reminders that apply: `uv` for all Python; never `git push origin master` directly; CI green via `gh pr checks --json` (not `--watch`); worktrees live in `.worktrees/<slug>/`.
