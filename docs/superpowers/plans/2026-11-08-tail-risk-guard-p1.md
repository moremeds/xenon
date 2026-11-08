# Tail-Risk Guard P1 — detect + notify (implementation plan)

Spec: `docs/superpowers/specs/2026-11-08-tail-risk-guard-design.md` (v3).
Approved by the user 2026-11-08: P1 scope, table `risk_events`, Resend +
ntfy, second-host dead-man. **P1 places, cancels and modifies no order.**
Branch `feat/tail-risk-guard`; one PR.

## Gate before coding

P0 read-only results: `docs/research/2026-11-08-risk-guard-p0.md` (feed root cause + snapshot cadence done; rest open):

- the 11-05 frozen-feed root cause and its fix (relay and/or Python);
- IB live entitlement for SPX/VIX;
- the `account_snapshots` cadence;
- the Futu SPY source for Python (no Python consumer of the relay/futu-quotes
  exists; `futu_client` must not subscribe market data);
- the Resend quota;
- the ntfy topic and auth;
- the dead-man host;
- the early-close dates.

If the feed root cause needs a relay fix, that fix ships first, in this PR or
as a stated prerequisite.

## Tasks (red/green TDD each; pure logic first)

| #   | Task                                                                                                                                                                                                                                                                                             | Files                                                                                          | Tests                                                                                                |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| 1   | Ring buffer + window returns: 1 s grid, carry-forward, `now−W ±2 s`, `UNAVAILABLE`, warm-up, session/reconnect reset                                                                                                                                                                             | `api/services/risk_guard/windows.py`                                                           | unit: 11-05 5m path synthesised to 1 s, gaps, resets                                                 |
| 2   | Sources + freshness: monotonic receive clock, ±5 s source-ts gate, 20 s stale, `ts=receive` flag, 2-source agreement, `FEED_STALE` / `FEED_DIVERGENCE`; IB SPX/VIX streaming via `data` role (`get_quote(snapshot=False)` cache pattern, `combo_quote_source.py:134`), re-subscribe on reconnect | `risk_guard/sources.py`                                                                        | unit with fake sources; frozen / divergent / reconnect                                               |
| 3   | Session table: RTH / extended / overnight / closed per instrument; early-close list                                                                                                                                                                                                              | `utils/market_calendar.py` (+ `config/market_early_closes.json`)                               | unit incl. 2026-11-27 if an early close (verify)                                                     |
| 4   | Account ratios: leverage Σmax loss/NLV, drawdown vs cash-flow-adjusted `nav_history` close peak, cushion; strike distances; snapshot age                                                                                                                                                         | `risk_guard/account.py`, `db/queries/risk_guard.py`                                            | unit on fixture payloads; PG test for peak query                                                     |
| 5   | Tiers + incidents: market channel vs health channel, cause-keyed incidents, per-incident ack, worsening rules, clear after 5 min; leverage/drawdown crossing + step rules; daily 09:00 ET line                                                                                                   | `risk_guard/incidents.py`                                                                      | unit: ack FAST then DB down → new incident sends; ack BLIND then CRASH → sends; worsening thresholds |
| 6   | Notifier: ntfy POST + Resend POST (httpx, already a dep), timeouts, daily email cap → push only, sends before DB write, no DB dependency                                                                                                                                                         | `risk_guard/notify.py`                                                                         | unit with httpx mock; cap; DB-down path                                                              |
| 7   | `risk_events` table + migration + queries; scope columns; READ_ONLY → no write                                                                                                                                                                                                                   | `db/schema.py`, `db/migrations/versions/2026_11_xx_risk_events.py`, `db/queries/risk_guard.py` | PG test; READ_ONLY test                                                                              |
| 8   | Runtime settings: enable flag, thresholds (tuning), cadence                                                                                                                                                                                                                                      | `runtime_settings.py`                                                                          | unit per setting                                                                                     |
| 9   | Lifespan task + routes: start like `_maybe_start_wizard_risk_alert` (`server.py:279`); `GET /risk/state`; `POST /risk/incidents/{key}/ack` (internal token, READ_ONLY 403 top-level `reason_code`); `/health` block                                                                              | `api/server.py`, `api/routes/risk_guard.py`                                                    | route tests; TestClient lifespan seeding                                                             |
| 10  | Dead-man heartbeat: every 30 s, payload = last receive time per source + tier; external checker per P0                                                                                                                                                                                           | `risk_guard/heartbeat.py`, runbook                                                             | unit; manual kill test                                                                               |
| 11  | UI: top banner (tier, BLIND "stops offline", ack button) + risk panel (ratios, sources ages) via Next proxy `xenonFetch()`                                                                                                                                                                       | `web/app/api/risk/*`, `web/components/risk/*`                                                  | Vitest + Playwright/chrome-cdp screenshot `output/playwright/risk-guard-2026-11-xx.png`              |
| 12  | Replay harness: apex 5m/1m bars → tier first-crossings for 11-05, 2020-02-24…03-23, 2025-04; 12-month false-fire count; output persisted to `docs/research/`                                                                                                                                     | `scripts/research/replay_risk_guard.py`                                                        | runs offline on saved bars                                                                           |
| 13  | Docs: spec status, `src/xenon/api/CLAUDE.md` section, runbook `docs/runbooks/risk-guard.md`, `deploy/compose.prod.yml` env vars, CHANGELOG                                                                                                                                                       | docs                                                                                           | —                                                                                                    |

## Env / config (names proposed)

`XENON_RISK_GUARD` (0/1), `XENON_RISK_NTFY_URL`, `XENON_RISK_NTFY_TOKEN`,
`XENON_RISK_RESEND_TOKEN`, `XENON_RISK_EMAIL_TO`, `XENON_RISK_EMAIL_FROM`,
`XENON_RISK_EMAIL_MAX_PER_DAY`, `XENON_RISK_HEARTBEAT_URL`. Secrets live in
`/opt/xenon/.env` only.

## Acceptance

- Spec §8 P1 items: replay report saved; failure injection (frozen, divergent,
  reconnect, DB down, OpenD down, API kill, clock skew, early close) → the
  expected incident.
- Real notification received on the user's phone (ntfy) and inbox (Resend)
  from the dev stack, with `[TEST]` subject.
- `uv run python scripts/infra/dev/run_pytest_affected.py`, `cd web && npm test`,
  typecheck, lint, the six order-path guards: green. Browser screenshot of
  banner + panel.
- No import of order modules from `risk_guard/` (add to a guard or test).
- Deploy is a separate step: release tag + runbook cutover, after the user OKs.
