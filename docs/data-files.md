# Critical Data Files

| File | Purpose |
|------|---------|
| `data/portfolio.json` | Open positions, bankroll, exposure |
| `data/trade_log.json` | **Append-only** trade journal |
| `docs/options-structures.json` | Options structure catalog — 58 structures, guard decisions, bias, risk profile |
| `data/watchlist.json` | Surveillance tickers |
| `data/ticker_cache.json` | Ticker → company cache |
| `data/reconciliation.json` | IB reconciliation |
| `data/seasonality_cache/` | Per-ticker seasonality |
| `data/menthorq_cache/` | CTA + dashboard cache (daily) |
| `data/cri_scheduled/` | Intraday CRI time-series |
| `data/vcg.json` | VCG scan cache (signal, 20-session history) |
| `data/price_history_cache/` | Stock + option price histories (auto-pruned at 500) |
