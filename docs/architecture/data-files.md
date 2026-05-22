# Critical Data Files

| File                                | Purpose                                                                                                                                                                                  |
| ----------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `data/portfolio.json`               | Open positions, bankroll, exposure                                                                                                                                                       |
| `data/trade_log.json`               | **Append-only** trade journal                                                                                                                                                            |
| `docs/options-structures.json`      | Options structure catalog — 58 structures, guard decisions, bias, risk profile                                                                                                           |
| `data/watchlist.json`               | Surveillance tickers                                                                                                                                                                     |
| `data/ticker_cache.json`            | Ticker → company cache                                                                                                                                                                   |
| `data/reconciliation.json`          | IB reconciliation                                                                                                                                                                        |
| `data/seasonality_cache/`           | Per-ticker seasonality                                                                                                                                                                   |
| `data/menthorq_cache/`              | CTA + dashboard cache (daily)                                                                                                                                                            |
| `data/cri_scheduled/`               | Intraday CRI time-series                                                                                                                                                                 |
| `data/vcg.json`                     | VCG scan cache (signal, 20-session history)                                                                                                                                              |
| `data/price_history_cache/`         | Stock + option price histories (auto-pruned at 500)                                                                                                                                      |

## Apex R2 Mirror

- `data/apex_mirror/` — local mirror of Cloudflare R2 `apex-data` bucket.
  - `parquet/historical/{1d,1h}/{TICKER}.parquet` — OHLCV (tz=UTC)
  - `parquet/indicators/{1d,1h}/{TICKER}.parquet` — TA-Lib indicators + scanner-contract derived fields
  - `meta/universe.json`, `meta/last_updated.json` — R2-authoritative metadata (read-only in scanner)
  - `.last_sync.json` — local sync sentinel (not uploaded; consumed by `apex_sync.sync_if_stale`)

  Refreshed by `src/xenon/ta_lib/apex_sync.sync_if_stale()` at scanner startup. Nightly producer is `.github/workflows/apex-data-refresh.yml`.

- `data/apex_mirror_preview/` — `--dry-run` output of `xenon-fetch-apex-data` (`src/xenon/fetchers/fetch_apex_data.py`). Created on demand; safe to delete.
