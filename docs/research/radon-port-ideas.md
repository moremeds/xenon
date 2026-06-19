# Radon → Xenon Port Backlog

**Date:** 2026-06-15
**Status:** Backlog / not yet scheduled. This is an idea inventory + sequencing
proposal, not an approved implementation plan. Each item still needs its own
red/green TDD + browser verification + PR per `CLAUDE.md`.

## Why this exists

`radon` (`~/projects/radon`) is xenon's architectural predecessor. The two share
a common ancestor — `web/app/[ticker]/page.tsx` is byte-identical, and
`web/lib/order/components/` is shared lineage — then diverged at the
reliability/UX layer, where radon went deeper. "Porting" here is mostly
**re-converging xenon onto patterns it already half-has**, so the risk is low.

Source: four read-only comparison audits (stock page · IB order module ·
operator console · dashboard), 2026-06-15.

## Hard constraints every port must respect

These are xenon invariants. A radon pattern that violates one is reworked, not
copied.

1. **DB-first** — no JSON writes on the order path. New persistence (watchlist,
   service-health, etc.) writes Postgres, never `data/*.json`. CI guards:
   `no_json_write_on_order_path.py`.
2. **Naked-short guard (3 layers)** — UI `nakedShortGuard.ts`, API Gate-4
   `preflight.py` (in-process), post-sync `naked_short_audit.py`. Any new order
   surface is an _additional_ signal, never a replacement for Gate-4.
3. **`XENON_READ_ONLY=1` live mode** — every write surface hard-refuses; any
   ported daemon must check the flag and no-op.
4. **In-process route bypass** — FastAPI `Depends` fire only on HTTP; in-process
   helpers skip auth/scope/read-only deps. New placement helpers run preflight
   at the function level + join the `order_path_caller_allowlist`.
5. **AccountScope** — every write/query carries `(broker, account_env,
broker_account)`. Radon has no scope concept; ported code must resolve it.
6. **Prod topology** — prod is the macmini **Docker compose** stack writing
   `core_dev`; dev is the MacBook writing `core_test`. Radon's `systemctl` /
   Turso / public-edge assumptions do not map.
7. **Identity** — "broker terminal, no signal generation, bring your own
   thesis." Skip every scanner/regime/opportunity/discover widget.

---

## Consolidated priority backlog (value-to-effort, highest first)

| #   | Area         | Item                                                     | Effort | New data plumbing?                   |
| --- | ------------ | -------------------------------------------------------- | ------ | ------------------------------------ |
| 1   | Order module | Back-port `_wait_for_perm_id` into single-leg place path | S      | none                                 |
| 2   | Operator     | User-facing IB connection banner w/ MFA guidance         | S      | none                                 |
| 3   | Dashboard    | Portfolio snapshot card on `/dashboard`                  | S      | none (props already hydrated)        |
| 4   | Dashboard    | Working + filled orders snapshot on `/dashboard`         | S      | none                                 |
| 5   | Stock page   | Instrument-type-aware Company tab (hide P/E on ETFs)     | S      | none                                 |
| 6   | Stock page   | Single-key tab navigation (`c/p/n/r/s/i` + Esc)          | S      | none                                 |
| 7   | Dashboard    | Make `/dashboard` a landing surface (chat → rail)        | S/M    | none                                 |
| 8   | Dashboard    | Click-through rows → `/orders` and `/[ticker]`           | S      | none                                 |
| 9   | Stock page   | Implied (Black-Scholes) column in chain                  | S      | none (IV already present)            |
| 10  | Operator     | `/admin` console rendering existing `/health` payload    | S/M    | none (health already rich)           |
| 11  | Order module | Order-risk chokepoint (brand-locked max-loss gate)       | M      | none                                 |
| 12  | Order module | Telemetry ring buffer (order-attempt traces)             | S      | none                                 |
| 13  | Operator     | `service_health` writer model + freshness table          | M      | new PG table                         |
| 14  | Operator     | Docker-native IB-liveness watchdog                       | M      | sidecar/probe                        |
| 15  | Stock page   | Watchlist / star toggle                                  | M      | new PG table                         |
| 16  | Stock page   | Time & Sales tape                                        | M + L  | relay `reqTickByTick`                |
| 17  | Stock page   | L2 depth-of-market ladder (the marquee Book)             | L      | relay `reqMktDepth` + IB entitlement |

> **Quick-win cluster (all S, zero new data plumbing): #1–#9.** This is the
> highest-leverage first sprint — it captures most of radon's perceived polish
> and fixes the placement-robustness gap without any backend/relay project.

---

## Area 1 — Dashboard (user-flagged "quick win")

**Today:** xenon `/dashboard` renders **only** the AI chat panel
(`WorkspaceShell.tsx:446-448` → `<ChatPanel>`). A broker terminal lands on an
empty chat box; the portfolio is invisible at a glance.

**radon:** a real 2-col landing surface (`web/components/dashboard/
DashboardSurface.tsx`, `globals.css:1016-1095`): portfolio snapshot + working/
filled orders + news rail, all collapsible.

**Key enabler:** xenon already hydrates `portfolio` (`usePortfolio`) and `orders`
(`useOrders`) at the shell level (`WorkspaceShell.tsx:79,120`) and computes
`todayRealizedPnl` (`:259`) — these props are simply **unused on the dashboard
branch**. The top ports need _zero_ new data plumbing.

Quick wins:

- **Portfolio snapshot card** (S) — NAV / today P&L / open risk / cash. radon
  `PortfolioSnapshotCard.tsx:21-59`. xenon can drop in its _richer_ `MetricCards`
  ACCOUNT row rather than re-implement. Respect Day-Chg sign rules
  (`web/CLAUDE.md`); reuse xenon's formatters, not radon's `fmtMoneySigned`.
- **Working + filled orders snapshot** (S) — top-3 each, click→`/orders`. radon
  `OrdersSnapshotCard.tsx:51-110`. `orders` prop already hydrated.
- **Landing-surface IA** (S/M) — adopt radon's 2-col grid; move `ChatPanel` to a
  rail/launcher (radon keeps chat as `ChatLauncher`). Re-skin radon's
  `snapshot-card` styling to xenon brand tokens (4px radius, no gradients/glass).
- **Click-through rows** (S) — radon links every row onward; xenon has the
  `[ticker]` + `/orders` routes but **zero ticker-link wiring** in components.

Skip: `OpportunitiesCard` (scanner/discover/LEAP/GARCH — signal), regime panels,
`DashboardNewsFeed` (themarketear.com scrape into Turso — xenon runs neither).
Note: xenon's `MetricCards` already **exceeds** radon's metric depth; the gap is
pure _placement_ (hidden behind the portfolio tab), not capability.

## Area 2 — IB order module

**Diagnosis:** xenon's three placement paths have uneven robustness. Combo-wizard
(`combo_wizard/ib_adapter.py`) and cancel/modify (`ib_order_manage.py`) are
robust; the **single-leg/simple place path is the weak link**
(`ib_place_order.py:137-185`): blind `client.sleep(2)`, returns `status:"ok"`
even when `permId==0`, then disconnects — IB silently drops the unconfirmed
order. This is the most likely "order module not working" root.

Ports:

- **#1 Back-port `_wait_for_perm_id`** (S, critical) — the robust permId
  ack-confirm already exists at `combo_wizard/ib_adapter.py:91-134`; bring it into
  the single-leg subprocess. Highest value-to-effort in the entire backlog.
- **#11 Order-risk chokepoint** (M) — radon `web/lib/order/risk/useOrderRisk.ts`
  is the _only_ producer of an order summary, enforced by an unexported brand
  symbol + ESLint import-ban, so max-loss/unbounded detection can't be bypassed.
  Port as an _additional_ signal alongside xenon's Gate-4 preflight (never a
  replacement).
- **#12 Telemetry ring buffer** (S) — radon `risk/telemetry.ts:22-72`, a
  50-entry sessionStorage ring of order-risk evaluations, dumpable for bug
  reports. Cheap diagnostic value.

**Do NOT port `exit_order_service.py`** — it's weaker than it looks (single GTC
limit, no OCA/stop, hardcoded shape, JSON I/O, author's absolute paths). Radon's
own `place_bracket_order` is dead code. If auto-exit is wanted, build fresh on
xenon's combo-wizard `place_combo_tp`/`attach_protection`, DB-first + scoped.

## Area 3 — Operator console ("operator session")

"Operator session" = radon's `/admin` "Operator" console
(`AdminWorkspace.tsx:438`, literally `<h1>Operator</h1>`). xenon's `/health` is
**richer than radon's** but nothing renders it.

Ports:

- **#2 IB connection banner + MFA guidance** (S, very high value) — radon
  `ConnectionBanner.tsx` + `getConnectionBannerState`. xenon tracks the state
  (`IBStatusContext.tsx`) but has no top-of-app banner that says "approve the push
  on your phone." Turns a silent dead session into a one-line instruction.
- **#10 `/admin` console** (S/M) — render xenon's existing `/health` payload
  (`server.py:964`: `ib_gateway.port_listening`, per-role `ib_pool`,
  `snapshotter.stale_seconds`, `order_submissions.alarm`, `realtime_subscribers`).
  Pure additive Next route.
- **#13 `service_health` writer model + freshness table** (M) — radon migration
  0011 + `WriterFreshnessTable.tsx`. Makes a hung activity-poller visible. One PG
  table in `core_dev` + a `record_service_health()` call per writer loop.
- **#14 Docker-native IB watchdog** (M) — radon `ib_watchdog.py` catches the
  API-thread hang that Docker's TCP healthcheck misses (xenon shares the risk).
  Rework to `docker restart`, honor `XENON_READ_ONLY` + 2FA-cost-per-restart.

Skip: systemd `ServiceControlPanel` (xenon prod is Docker, not systemd), the
Turso data plane (use Postgres), the public-edge SLO prober (xenon prod is
private behind Tailscale).

## Area 4 — Individual stock page

Tab _set_ is already at parity; the praised "feel" is ~60% the cockpit shell +
keyboard decks (cheap, frontend-only) and ~40% a real depth-of-market Book
(expensive — needs relay + protocol + IB entitlement).

Quick wins (all on data xenon already has):

- **#5 Instrument-type-aware Company tab** (S) — radon `CompanyTab.tsx:131-142`
  hides P/E, EPS, Next Earnings on ETF/index/fund; xenon shows `---` clutter on
  QQQ/SPY today (`CompanyTab.tsx:122-136`). Highest value/effort on this page.
- **#6 Single-key tab navigation** (S) — radon `AssetDeck.tsx:73-93` maps
  `c/p/n/r/s/i` + Esc, guarded against typing targets. xenon tabs are click-only.
- **#9 Implied (Black-Scholes) column** (S) — radon computes `computeLegImplied
Value()` per chain row; xenon omits it. IV already in the payload.

Bigger bets (gated on extending the xenon realtime relay, which is **L1-only**
today — `scripts/infra/ib_realtime/ib_realtime_server.js` has no `reqMktDepth`/
`reqTickByTick`):

- **#16 Time & Sales tape** (M frontend + L backend).
- **#17 L2 depth ladder** (L) — needs `reqMktDepth` + IB depth entitlement;
  radon gates it behind `RADON_DEPTH_ENABLED`. Scope as its own project.
- **#15 Watchlist / star toggle** (M) — radon `StarToggle` + `useWatchlist` +
  `/api/watchlist`. xenon must persist to Postgres (DB-first), not JSON.

Skip: futures ladder DOM + `FuturesOrderForm` (xenon trades no futures), the
per-instrument order-form split (xenon's unified `OrderTab` + regime gate + combo
wizard + naked-short guard is strictly better for the options-only niche),
radon's inline `PositionTradeTicket` (would bypass xenon's order-path guards —
keep the `PositionOrderModal`).

---

## Suggested first sprint

Items **#1–#9** (the all-S, zero-data-plumbing cluster): fixes the order-placement
robustness gap, gives the dashboard a real landing surface, and lifts the
stock-page polish — all without a single backend/relay project. Everything from
#10 down is a follow-on once the quick wins land.
