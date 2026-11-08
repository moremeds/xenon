# Tail-risk guard (P1) — runbook

Spec: `docs/superpowers/specs/2026-11-08-tail-risk-guard-design.md`. Plan:
`docs/superpowers/plans/2026-11-08-tail-risk-guard-p1.md`. Code:
`src/xenon/api/services/risk_guard/`.

P1 is **alerts only**. It never places, cancels or modifies an order
(`src/xenon/api/tests/test_risk_guard_no_order_path.py`). Close positions in
Xenon or TWS.

## What it does

A FastAPI lifespan task ticks once per second:

1. Reads streaming SPX (CBOE index) and SPY (SMART) tickers on the api `data`
   role (clientId base+5). A tick counts only when it is live
   (`marketDataType == 1`), carries a new IB time, and that time is within 5 s
   of the api clock.
2. Every 30 s reads the latest account snapshot (`account_snapshots`); every
   10 min the drawdown basis (`nav_history` close rows + IB Flex cash flows).
3. Raises market tiers from SPX/SPY speed and account state:

   | Tier  | Any of (defaults, `Thresholds` in `incidents.py`)                            | Repeats              |
   | ----- | ---------------------------------------------------------------------------- | -------------------- |
   | WATCH | 15 m ≤ −0.75 %, day ≤ −1 %, key level broken, cushion < 20 %                 | once                 |
   | FAST  | 30 m ≤ −1.5 %, day ≤ −2 %, nearest short strike ≤ 0.5 % away, cushion < 12 % | every 30 s until ack |
   | CRASH | 60 m ≤ −2.5 %, day ≤ −3 %, long leg ITM on a credit position, cushion < 8 %  | every 30 s until ack |

   A price metric counts only when both sources agree (the milder value
   wins). With one source fresh it counts only while a BLIND cause is raised.
   A fresh source still refilling its window after a tick gap does not veto
   the other. Freshness = any tick on the line: ib_async `Ticker.time` is a
   receive time, so a flat `last` with live size ticks still counts as fresh.

4. Raises BLIND causes (priority 5, every 30 s until ack):
   `FEED_STALE` / `SOURCE_STALE` (no tick for 20 s, RTH only),
   `FEED_DIVERGENCE` (5 m returns differ > 0.3 pp), `OWNER_HALTED`,
   `DATA_ROLE_DOWN`, `UPSTREAM_LOST` (1100/2110), `SCOPE_UNRESOLVED`,
   `DB_DOWN` (read failed or > 5 s), `SNAPSHOT_STALE` (> 120 s in RTH),
   `ACCOUNT_UNREADABLE`, `GUARD_ERROR`. Stale feeds count from 09:31 ET
   (the cash index first prints at the open). A down data role is retried
   every 30 s (the pool reconnects only inside `acquire`). System causes alarm only on a
   trading day from 09:00 ET to the close.
5. Sends ratio alerts on a level change or each further step: leverage
   (Σ max loss / NLV, levels 0.5 / 0.75 / 1.0, step 0.25) and drawdown (NLV vs
   the flow-adjusted close peak, levels −5 / −10 / −15 %, step 2 pp). A daily
   ratio line goes out at 09:00 ET on trading days.
6. After ack an incident sends again only when it rises or worsens (new low
   ≥ 0.25 % below the ack reference, cushion 2 pp lower, or more shorts
   crossed). It clears after 5 min without a trigger.

Every send goes to ntfy (push) and Resend (email) at once, then is recorded
in `xenon.risk_events` (skipped under `XENON_READ_ONLY=1`). A failed DB write
never blocks the alert. Paper / test scopes prefix titles with `[TEST]`.

## Enable

Set in `/opt/xenon/.env` (secrets live only there), then recreate the api
(`docs/runbooks/remote-deploy.md`, controlled `up -d --pull never api`):

| Variable                       | Meaning                                                    |
| ------------------------------ | ---------------------------------------------------------- |
| `XENON_RISK_GUARD`             | `1` starts the task (deploy switch)                        |
| `XENON_RISK_NTFY_URL`          | ntfy topic URL (e.g. `https://ntfy.sh/<private-topic>`)    |
| `XENON_RISK_NTFY_TOKEN`        | optional ntfy access token                                 |
| `XENON_RISK_RESEND_TOKEN`      | Resend API key                                             |
| `XENON_RISK_EMAIL_FROM`        | verified Resend sender                                     |
| `XENON_RISK_EMAIL_TO`          | comma-separated recipients                                 |
| `XENON_RISK_EMAIL_MAX_PER_DAY` | daily email cap (default 100; push continues past the cap) |
| `XENON_RISK_HEARTBEAT_URL`     | optional dead-man URL; POSTed every 30 s                   |

The task also needs the migration `2026_11_09_risk_events` (run the migrator
before the new api image, `deploy/README.md` § D).

With no channel configured the task still runs and logs an ERROR at start.

## Runtime settings (`/admin`)

- `XENON_RISK_GUARD` — turns a started guard off or on without a restart
  (typed confirm to turn off). It cannot start a task the env did not start.
- `XENON_RISK_THRESHOLDS` — JSON object of `Thresholds` fields to override,
  bounds-checked (`risk_guard/config.py::THRESHOLD_BOUNDS`).
- `XENON_RISK_KEY_LEVELS` — JSON list of SPX levels; breaking one from above
  inside the session raises WATCH.

## Ack

- Banner on every workspace page (`RiskGuardBanner`): one row per open
  incident with **ACK**.
- API: `POST /risk/incidents/{key}/ack` (internal token; keys `market` or
  `health:<CAUSE>`). 503 `RISK_ACK_UNAVAILABLE` = the ack could not be
  recorded, so alerts continue. 403 `READ_ONLY_MODE` under `XENON_READ_ONLY`.
- State: `GET /risk/state` (live state + last 20 `risk_events`), `/health`
  `risk_guard` block (no account values).

## Dead-man heartbeat

With `XENON_RISK_HEARTBEAT_URL` set, the guard POSTs
`{at, tier, blind, sources: {SPX: age_s, SPY: age_s}}` every 30 s. Point it at
an external checker that alerts when posts stop (P0 left the host choice
open). Manual test: stop the api; the checker must alert within its window.

## Failure checks

| Symptom                         | Check                                                                   |
| ------------------------------- | ----------------------------------------------------------------------- |
| No alerts at all                | `/health` `risk_guard.running`; api log `risk guard started: push=…`    |
| `BLIND DATA_ROLE_DOWN`          | `/health` `ib_pool.data.connected`; gateway port; IB connection runbook |
| `BLIND SOURCE_STALE` SPX in RTH | SPX index market-data entitlement (live account; unverified 2026-11-08) |
| `reached no channel` in the log | ntfy URL / Resend token; Resend quota                                   |
| `risk_events write failed`      | DB reachability; alerts still went out                                  |

## Replay

`uv run python scripts/research/replay_risk_guard.py --out docs/research/<date>-risk-guard-replay.md`
replays the market tiers on Unusual Whales SPY bars (cache in
`data/research/risk_guard_bars/`). Latest report:
`docs/research/2026-11-08-risk-guard-replay.md`.
