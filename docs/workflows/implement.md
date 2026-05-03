# Execution Runbook

## Source of Truth

- `docs/plans.md` defines the milestone sequence
- `docs/prompt.md` defines constraints and "done when"
- Execute milestones IN ORDER, do not skip

### ⚠️ Portfolio Source of Truth (CRITICAL)

**Interactive Brokers is the ONLY source of truth for current portfolio state.**

| Question                  | Source of Truth                     | NOT a Source of Truth                           |
| ------------------------- | ----------------------------------- | ----------------------------------------------- |
| What positions do I hold? | `xenon-ib-sync` (IB live)           | `docs/status.md`, `data/portfolio.json` (cache) |
| Is a position still open? | `xenon-ib-sync` (IB live)           | `docs/status.md` "Rule Violations" table        |
| Current P&L?              | `xenon-ib-sync` (IB live)           | `docs/status.md` "Portfolio State" section      |
| What trades happened?     | `data/trade_log.json` (append-only) | `docs/status.md` "Trade Log Summary"            |

**Rules:**

1. **NEVER claim a position exists or doesn't exist based on `docs/status.md` or `data/portfolio.json`.** These are caches that go stale.
2. **ALWAYS verify against IB** before making any statement about current holdings, open positions, or portfolio state.
3. `docs/status.md` is a **decision log and audit trail** — it records what happened and why. It is NOT a live portfolio dashboard.
4. `data/portfolio.json` is a **cache** updated by `xenon-ib-sync --sync`. It may be hours or days old.
5. When IB is unavailable (Gateway down), say so explicitly: _"Cannot verify — IB unavailable."_ Do NOT fall back to status.md.

## ⚠️ Evaluate Command → ALWAYS `evaluate.py`

**Any evaluation request routes to `xenon-evaluate [TICKER]`. No exceptions.**
Even if the user provides manual steps (e.g., "run fetch_flow.py, then fetch_options.py"),
ignore the manual steps and run the unified script. It handles M1–M3B (plus M1D news/catalysts) in parallel.

## API Client Architecture

All IB and UW access goes through centralized clients in `src/xenon/clients/`:

| Client     | File                             | Usage                                          |
| ---------- | -------------------------------- | ---------------------------------------------- |
| `IBClient` | `src/xenon/clients/ib_client.py` | `from xenon.clients.ib_client import IBClient` |
| `UWClient` | `src/xenon/clients/uw_client.py` | `from xenon.clients.uw_client import UWClient` |

**IBClient** wraps `ib_async.IB` with connection retries, context manager support, and methods for positions, orders, quotes, options chains, fills, flex queries, and historical data. Exception hierarchy: `IBError` → `IBConnectionError`, `IBOrderError`, `IBTimeoutError`, `IBContractError`. Raw access via `client.ib` property.

**UWClient** wraps all Unusual Whales REST endpoints with session pooling, automatic retry/backoff, and context manager support. Exception hierarchy: `UWAPIError` → `UWAuthError`, `UWRateLimitError`, `UWNotFoundError`, `UWValidationError`, `UWServerError`. 50+ methods covering dark pool, options flow, stock info, GEX, volatility, ratings, seasonality, and more.

**Legacy utils** (`src/xenon/utils/ib_connection.py`, `src/xenon/utils/uw_api.py`) are preserved but all scripts have been migrated to the new clients.

---

## Operating Rules

### 1. Validate Before Assuming

- NEVER identify a ticker from memory/training data
- ALWAYS run `fetch_ticker.py` first to get verified company info
- If script fails or returns no data, state "UNVERIFIED" and flag uncertainty

### 2. Always Fetch Fresh Data (CRITICAL)

- **Every evaluation milestone that calls a script or API MUST fetch live data at execution time**
- Scan results are LEADS — when evaluating, re-fetch everything (dark pool, options, OI, analyst ratings)
- If market is open, all data must include today. If a script's output doesn't include today's date, re-run or flag the gap
- Include a `📊 Data as of:` timestamp line at the start of every evaluation
- NEVER carry forward data from a prior scan session as if it were fresh evidence

### 3. Milestone Discipline

- Complete each milestone fully before proceeding
- Run validation command for each milestone
- If validation fails → repair immediately, do not continue
- If stop condition met → halt and report which gate failed

### 3. No Rationalization

- If a gate fails, stop evaluation
- Do not "find reasons" to proceed anyway
- State the failing gate clearly and move on

### 4. Diffs Stay Scoped

- When updating portfolio.json, only modify relevant fields
- When appending to trade_log.json, append only (never overwrite history)
- Keep watchlist.json updates minimal and targeted

### 5. Continuous Documentation

- Update `docs/status.md` after each evaluation
- Log EXECUTED trades only to trade_log.json (with full details)
- Log NO_TRADE decisions to docs/status.md (Recent Evaluations section)
- Include timestamp, ticker, decision, and rationale

### 5B. Strategy Registry Sync (MANDATORY)

- **`data/strategies.json` MUST stay in sync with `docs/strategies.md`**
- When a new strategy is added to `docs/strategies.md`, IMMEDIATELY add a corresponding entry to `data/strategies.json`
- When a strategy is modified (status, commands, instruments, etc.), update both files
- When a strategy is deprecated/removed, update both files
- **Required fields per strategy**: `id`, `name`, `status`, `description`, `edge`, `instruments`, `hold_period`, `win_rate`, `target_rr`, `risk_type`, `commands`, `doc`
- Optional fields: `manager_override` (only for undefined-risk strategies)
- After any change, validate: `python3.13 -m json.tool data/strategies.json`
- The `strategies` command reads `data/strategies.json` — if it's stale, users see outdated info

### 6. Verification Commands

After any trade decision:

```bash
# Validate JSON integrity
python3.13 -m json.tool data/portfolio.json
python3.13 -m json.tool data/trade_log.json
python3.13 -m json.tool data/watchlist.json
```

### 7. Error Recovery

If a script fails:

1. Check error message
2. Attempt repair if obvious (missing dependency, API issue)
3. If unrecoverable, log the failure and flag for manual review
4. Do not fabricate data

---

## Command Reference

### Evaluation Commands

| Action                            | Command                                      |
| --------------------------------- | -------------------------------------------- |
| **⭐ Full evaluation**            | `xenon-evaluate [TICKER]`                    |
| Full evaluation (JSON)            | `xenon-evaluate [TICKER] --json`             |
| Full evaluation (custom bankroll) | `xenon-evaluate [TICKER] --bankroll 1200000` |
| Validate ticker                   | `xenon-fetch-ticker [TICKER]`                |
| Fetch dark pool flow              | `xenon-fetch-flow [TICKER]`                  |
| Fetch options data                | `xenon-fetch-options [TICKER]`               |
| Fetch options (JSON)              | `xenon-fetch-options [TICKER] --json`        |
| Fetch analyst ratings             | `xenon-fetch-analyst [TICKER]`               |
| Fetch news & catalysts            | `xenon-fetch-news [TICKER]`                  |
| Calculate Kelly                   | `xenon-kelly --prob P --odds O --bankroll B` |

### Scanning Commands

| Action                                 | Command                                                                       |
| -------------------------------------- | ----------------------------------------------------------------------------- |
| **⭐ GARCH Convergence (all presets)** | `xenon-garch --preset all`                                                    |
| GARCH Convergence (one preset)         | `xenon-garch --preset semis`                                                  |
| GARCH Convergence (file preset)        | `xenon-garch --preset sp500-semiconductors`                                   |
| GARCH Convergence (ad-hoc)             | `xenon-garch NVDA AMD GOOGL META`                                             |
| GARCH Convergence (JSON)               | `xenon-garch --preset all --json`                                             |
| **⭐ Risk Reversal**                   | `xenon-risk-reversal IWM`                                                     |
| Risk Reversal (bearish)                | `xenon-risk-reversal SPY --bearish`                                           |
| Risk Reversal (custom)                 | `xenon-risk-reversal QQQ --bankroll 500000 --min-dte 21`                      |
| LEAP IV scan (UW)                      | `xenon-leap-uw --preset sectors`                                              |
| LEAP IV scan (IB)                      | `xenon-leap-iv AAPL --portfolio`                                              |
| Discovery (market-wide)                | `xenon-discover`                                                              |
| Discovery (preset)                     | `xenon-discover ndx100`                                                       |
| Discovery (tickers)                    | `xenon-discover AAPL MSFT NVDA`                                               |
| Watchlist scan                         | `xenon-scan`                                                                  |
| **⭐ Stress Test (model)**             | `xenon-scenario` (update params first, outputs `/tmp/scenario_analysis.json`) |
| **⭐ Stress Test (report)**            | `xenon-scenario-report` (reads JSON, generates HTML, opens browser)           |

### Portfolio Commands

| Action                           | Command                                                        |
| -------------------------------- | -------------------------------------------------------------- |
| **⭐ Generate portfolio report** | `xenon-portfolio-report` (self-contained: IB + DP flow + HTML) |
| Portfolio report (no browser)    | `xenon-portfolio-report --no-open`                             |
| Free trade analysis              | `xenon-free-trade-analyzer --table`                            |
| Sync IB portfolio                | `xenon-ib-sync --sync`                                         |
| Run reconciliation               | `xenon-ib-reconcile`                                           |
| View today's fills               | `xenon-blotter`                                                |
| Fetch historical trades          | `xenon-blotter-history --symbol [TICKER]`                      |
| Start realtime server            | `node scripts/infra/ib_realtime/ib_realtime_server.js`         |
| Validate JSON                    | `python3.13 -m json.tool data/[file].json`                     |

### Context Engineering Commands

| Action                     | Command                                                                                                             |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| **View persistent memory** | `python3.13 scripts/infra/dev/context_constructor.py`                                                               |
| View as JSON               | `python3.13 scripts/infra/dev/context_constructor.py --json`                                                        |
| View manifest only         | `python3.13 scripts/infra/dev/context_constructor.py --manifest-only`                                               |
| **Save a fact**            | `python3.13 scripts/infra/dev/context_constructor.py --save-fact "key" "value" --confidence 0.95 --source "source"` |
| **Save session episode**   | `python3.13 scripts/infra/dev/context_constructor.py --save-episode "summary" --session-id "id"`                    |

### Tweet-It Commands

| Action                    | Command                                                                               |
| ------------------------- | ------------------------------------------------------------------------------------- |
| **Generate tweet + card** | `tweet-it` (6-step workflow: text → card HTML → screenshot → base64 → preview → open) |

**⚠️ Card PNG must be base64-encoded into preview HTML as a data URI.** Chrome CORS blocks all `file://` image loads. See `.pi/skills/tweet-it/SKILL.md` for the full workflow.

### Order Execution Commands

**⚠️ ALWAYS use `ib_execute.py` — it monitors and logs automatically.**

| Action                          | Command                                                                                                                   |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| **Sell stock**                  | `xenon-ib-execute --type stock --symbol X --qty N --side SELL --limit N --yes`                                            |
| **Buy stock**                   | `xenon-ib-execute --type stock --symbol X --qty N --side BUY --limit N --yes`                                             |
| **Buy option**                  | `xenon-ib-execute --type option --symbol X --expiry YYYYMMDD --strike N --right C/P --qty N --side BUY --limit MID --yes` |
| **Sell option**                 | `xenon-ib-execute --type option --symbol X --expiry YYYYMMDD --strike N --right C/P --qty N --side SELL --limit N --yes`  |
| Monitor daemon status           | `xenon-monitor-daemon --status`                                                                                           |
| Run monitor daemon once         | `xenon-monitor-daemon --once`                                                                                             |
| Monitor daemon handlers         | `xenon-monitor-daemon --list-handlers`                                                                                    |
| Install monitor daemon          | `./scripts/services/setup_monitor_daemon.sh install`                                                                      |
| Monitor daemon status (launchd) | `./scripts/services/setup_monitor_daemon.sh status`                                                                       |
| **IBC Gateway status**          | `~/ibc/bin/status-secure-ibc-service.sh`                                                                                  |
| **IBC Gateway start**           | `~/ibc/bin/start-secure-ibc-service.sh`                                                                                   |
| **IBC Gateway stop**            | `~/ibc/bin/stop-secure-ibc-service.sh`                                                                                    |
| **IBC Gateway restart**         | `~/ibc/bin/restart-secure-ibc-service.sh`                                                                                 |
| **IBC remote helper**           | `./scripts/ibc_remote_control.sh check`                                                                                   |

### IB Gateway Management (IBC)

IB Gateway is managed by a **machine-global secure IBC service** (`local.ibc-gateway`). The active install lives at `~/ibc-install/`, with config and wrappers under `~/ibc/`. Credentials are stored in macOS Keychain, not on disk.

**Service commands:**

```bash
~/ibc/bin/start-secure-ibc-service.sh    # Start Gateway via launchd
~/ibc/bin/stop-secure-ibc-service.sh     # Stop Gateway
~/ibc/bin/restart-secure-ibc-service.sh  # Restart Gateway
~/ibc/bin/status-secure-ibc-service.sh   # Show launchd state
tail -f ~/ibc/logs/ibc-gateway-service.log
```

**Automated lifecycle:**

1. **00:00** — launchd starts Gateway via IBC
2. The secure runner reads credentials from Keychain, writes a temporary `0600` runtime config, and launches Gateway
3. You approve 2FA on IBKR Mobile once
4. **11:58 PM** — IBC restarts Gateway (reuses auth session, no 2FA)
5. **Sunday 07:05** — Cold restart with full re-auth (2FA required)

**Key config settings (`~/ibc/config.secure.ini`):**
| Setting | Value | Purpose |
|---------|-------|---------|
| `ExistingSessionDetectedAction` | `primary` | Gateway reconnects if bumped |
| `AcceptIncomingConnectionAction` | `accept` | No popup for API connections |
| `AutoRestartTime` | `11:58 PM` | Daily restart before IB's forced window |
| `ColdRestartTime` | `07:05` | Sunday re-auth |
| `CommandServerPort` | `7462` | IBC command server for stop/restart |
| `IbLoginId` / `IbPassword` | unset in file | Credentials come from Keychain only |

**Architecture:**

- LaunchAgent: `~/Library/LaunchAgents/local.ibc-gateway.plist`
- Runner: `~/ibc/bin/run-secure-ibc-gateway.sh`
- Logs: `~/ibc/logs/ibc-gateway-service.log` plus IBC diagnostics under `~/ibc/logs/`
- `KeepAlive=false` — IBC/Gateway manage their own lifecycle via `AutoRestartTime`

**Phase 1 remote access dependencies:**

- `Tailscale.app` on the Mac
- Tailscale on the iPhone, connected to the same tailnet
- macOS `Remote Login` enabled so SSH listens on port `22`
- iPhone SSH client such as Termius, Blink Shell, or Prompt
- Optional: dedicated SSH public key in `~/.ssh/authorized_keys`

**Phase 1 remote access usage:**

```bash
# Direct secure service commands over SSH
ssh joemccann@macbook-pro '~/ibc/bin/status-secure-ibc-service.sh'
ssh joemccann@macbook-pro '~/ibc/bin/restart-secure-ibc-service.sh'

# Optional repo helper
ssh joemccann@macbook-pro 'cd /Users/joemccann/dev/apps/finance/convex-scavenger && ./scripts/ibc_remote_control.sh ibc-status'
```

**Reference:** `docs/ibc-remote-access.md`

**Troubleshooting:**

- Gateway not running after a scheduled start → approve 2FA on IBKR Mobile, or run `~/ibc/bin/start-secure-ibc-service.sh`
- `ExistingSessionDetectedAction=primary` means this Gateway always wins session conflicts
- IBC command server (port 7462) allows `STOP`, `RESTART`, `RECONNECT` commands via `echo "STOP" | nc localhost 7462`
- Legacy `scripts/setup_ibc.sh` is retained for historical reference only and is not the active service path

### IB Connection Ports

| Port | Environment                               |
| ---- | ----------------------------------------- |
| 7496 | TWS Live                                  |
| 7497 | TWS Paper                                 |
| 4001 | IB Gateway Live                           |
| 4002 | IB Gateway Paper                          |
| 7462 | IBC Command Server (stop/restart Gateway) |

---

## Trade Specification Reports

**ALWAYS generate a Trade Specification HTML report when recommending a trade.**

```bash
# Template
.pi/skills/html-report/trade-specification-template.html

# Output
reports/{ticker}-evaluation-{date}.html
```

**Workflow:**

1. Complete evaluation milestones 1-6
2. Generate HTML report using template
3. Present to user for confirmation
4. On "execute" → use `ib_execute.py` (auto-monitors and logs)
5. Place exit orders (stop loss + target)

**Reference:** `reports/goog-evaluation-2026-03-04.html`

---

## Scenario Stress Test

**Interactive two-step command (`stress-test`):**

1. Agent asks: _"What is the change in the overall market?"_
2. User describes scenario → Agent parses, models, generates report

```bash
# Template
.pi/skills/html-report/stress-test-template.html

# Output
reports/stress-test-{date}.html

# Pricing engine (update parameters per scenario, then run)
xenon-scenario

# Reference report generator (reads /tmp/scenario_analysis.json)
xenon-scenario-report
```

**Model pipeline:**

1. Parse user scenario into: SPX move, VIX level, sector shocks (oil, crypto, etc.)
2. Update `scenario_analysis.py` parameters: `SCENARIO_SPX_MOVE`, `SCENARIO_VIX`, `SCENARIO_OIL_MOVE`, etc.
3. Run `scenario_analysis.py` → outputs `/tmp/scenario_analysis.json`
4. Write per-position narratives (oil, SPX beta, VIX stress, options structure)
5. Generate HTML from template with all 10 sections + expandable ▶ detail rows
6. Open in browser

**Key modeling constraints:**

- Single per-ticker IV (never per-leg)
- Defined risk P&L clamped: `[-debit, +max_width]`
- LEAP IV dampening: >180 DTE 50%, 60-180 DTE 75%, <60 DTE 100%
- VIX crash-beta only when scenario VIX > 30

**Reference:** `reports/scenario-stress-test-2026-03-08.html`

---

## Order Execution Workflow

**⚠️ ALWAYS use `ib_execute.py` for all orders. It automatically:**

- Places the order
- Monitors for fills (real-time updates)
- Logs filled trades to `trade_log.json`

### Single-Leg Orders

**Stock:**

```bash
# Sell stock at bid
xenon-ib-execute --type stock --symbol NFLX --qty 4500 --side SELL --limit BID --yes

# Buy stock at limit
xenon-ib-execute --type stock --symbol AAPL --qty 100 --side BUY --limit 175.50 --yes
```

**Option:**

```bash
# Buy call at mid
xenon-ib-execute --type option --symbol GOOG --expiry 20260417 --strike 315 --right C --qty 44 --side BUY --limit MID --yes

# Sell put at limit
xenon-ib-execute --type option --symbol GOOG --expiry 20260417 --strike 290 --right P --qty 10 --side SELL --limit 3.50 --yes
```

**Multi-leg spread:** Use inline Python with `ib_async` (see `ib-order-execution` skill)

### Exit Orders

After entry fill, place exit orders:

1. **Stop Loss** — Stop-limit order at stop price
2. **Target Profit** — Limit sell order at target

**Note:** IB rejects limit orders >40% from current price. Use the monitor daemon's `exit_orders` handler for automated placement once the order becomes valid.

---

## Monitor Daemon

The monitor daemon is the active background service for post-entry workflows.

Installed behavior:

- launchd runs `python -m monitor_daemon.run --once` every 60 seconds
- `fill_monitor` and `exit_orders` enforce market hours
- `preset_rebalance` and `flex_token_check` are allowed to run off-hours

**Status:**

```bash
python3.13 -m monitor_daemon.run --status
./scripts/setup_monitor_daemon.sh status
```

**Run once manually:**

```bash
python3.13 -m monitor_daemon.run --once
```

**List handlers:**

```bash
python3.13 -m monitor_daemon.run --list-handlers
```

**Install / logs:**

```bash
./scripts/setup_monitor_daemon.sh install
./scripts/setup_monitor_daemon.sh logs
```

**Legacy note:** `scripts/exit_order_service.py` and `scripts/setup_exit_order_service.sh` are older standalone paths and should not be the primary scheduled service anymore.

---

## Options Flow Analysis

The `fetch_options.py` script provides comprehensive options analysis:

```bash
# Full analysis with formatted report
xenon-fetch-options AAPL

# JSON output for programmatic use
xenon-fetch-options AAPL --json

# Force specific data source
xenon-fetch-options AAPL --source uw   # Unusual Whales
xenon-fetch-options AAPL --source ib   # Interactive Brokers
xenon-fetch-options AAPL --source yahoo # LAST RESORT ONLY
```

**Output includes:**

- Chain: Premium, volume, OI, bid/ask volume, P/C ratio, bias
- Flow: Institutional alerts, sweeps, bid/ask side premium, flow strength
- Combined: Synthesized bias with conflict detection and confidence rating

---

## Trade Blotter & P&L

### Today's Fills

```bash
xenon-blotter
```

Shows:

- All executions grouped by contract
- Spread detection (put spreads, call spreads, risk reversals)
- Combined P&L for multi-leg positions
- Commission totals

### Historical Trades (Flex Query)

```bash
# All trades
xenon-blotter-history

# Filter by symbol
xenon-blotter-history --symbol EWY
```

Requires `IB_FLEX_TOKEN` and `IB_FLEX_QUERY_ID` environment variables.

---

## P&L Reports

When generating P&L reports, use the template:

```
.pi/skills/html-report/pnl-template.html
```

**Required sections:**

1. Header with CLOSED/OPEN status pill
2. 4 metrics: Realized P&L, Commissions, Hold Period, Return on Risk
3. Trade Summary callout
4. Execution table(s) with cash flows
5. Combined P&L panel (for spreads)
6. Trade timeline
7. Footer with data source

**Return on Risk formula:**

```
Return on Risk = Realized P&L / Capital at Risk

Capital at Risk:
  - Debit spread: Net debit paid
  - Credit spread: Max loss (width - credit)
  - Long option: Premium paid
  - Stock: Cost basis
```

---

## Startup Reconciliation (Auto-Log)

The startup extension automatically runs `ib_reconcile.py` when Pi starts:

- **Async**: Does not block Pi startup
- **Detects**: New trades, new positions, closed positions
- **Output**: `data/reconciliation.json`
- **Notification**: Shows if action needed
- **⚠️ Auto-Log**: If new trades detected, IMMEDIATELY log them

### Auto-Log Workflow (MANDATORY)

When startup shows `⚠️ IB: N new trades`, **immediately** run this workflow:

```bash
# 1. Check reconciliation data
cat data/reconciliation.json | python3.13 -m json.tool

# 2. For each trade in new_trades, add to trade_log.json:
#    - id: next sequential ID
#    - date, time from reconciliation
#    - ticker, action, realized_pnl, commission
#    - validation_method: "ib_reconciliation"
#    - notes: "Detected via IB reconciliation"

# 3. Update docs/status.md:
#    - Trade Log Summary table
#    - Today's Trades section
#    - Portfolio State metrics (positions, deployed %, etc.)

# 4. Clear reconciliation flag
# Write to reconciliation.json:
#    needs_attention: false
#    processed_trades: [list of processed trade IDs]

# 5. Validate JSON
python3.13 -m json.tool data/trade_log.json
```

**Do NOT prompt user** — this auto-log runs automatically whenever `needs_attention: true`.

### Manual Reconciliation

```bash
# Trigger reconciliation manually
xenon-ib-reconcile

# Check results
cat data/reconciliation.json | python3.13 -m json.tool
```

---

## Data File Locations

| File                                  | Purpose                                                         |
| ------------------------------------- | --------------------------------------------------------------- |
| `data/trade_log.json`                 | Executed trades (append-only)                                   |
| `data/portfolio.json`                 | Current positions from IB                                       |
| `data/reconciliation.json`            | IB sync discrepancies                                           |
| `data/watchlist.json`                 | Tickers under surveillance                                      |
| `data/ticker_cache.json`              | Ticker → company name cache                                     |
| `data/analyst_ratings_cache.json`     | Cached analyst data                                             |
| `context/memory/fact/`                | Persistent facts (trading lessons, API quirks, portfolio state) |
| `context/memory/episodic/`            | Session summaries                                               |
| `context/human/`                      | Human annotations (overrides model output)                      |
| `context/history/_transactions.jsonl` | All context read/write operations                               |
| `context/metadata.json`               | Governance policies + token budget                              |
