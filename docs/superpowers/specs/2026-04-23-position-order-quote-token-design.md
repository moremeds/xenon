# Design: `quote_token` integration for position close/add

Date: 2026-04-23
Owner: xenon
Status: Design approved, awaiting implementation plan
Relates to: `docs/plans/2026-04-23-loose-ends.md` (P1 #1), prior F3 work
(`quote_guard`), PR #33 (position-order modal rework)

## Problem

`PositionOrderModal` (`web/components/PositionOrderModal.tsx:133`) submits
position close/add orders without a `quote_token`. Two concrete consequences:

1. **Combo close/add bypasses F3 entirely.** `src/xenon/api/server.py:1526`
   short-circuits `quote_guard.check` when `body.type == "combo"`, so combo
   closes currently have _no_ limit-band, tick-grid, or staleness protection.
2. **Single-leg close is currently inconsistent.** The non-combo branch at
   `server.py:1528` returns 400 when `quote_token` is missing. Either single-leg
   close from the modal is broken in practice today, or a payload shape
   mismatch is masking the gate. Needs verification during implementation.

Net: position close/add — the path most often used during adverse market
moves — silently bypasses the foundation safety F3 built for every other
submit path. This is the loose-ends doc's P1 #1.

## Scope

**In scope**

- Mint and send quote tokens from `PositionOrderModal` for:
  - stock close/add (single-leg path),
  - single-leg option close/add (single-leg path),
  - multi-leg combo close/add (new combo path).
- Server-side combo verification (`quote_guard.check_combo`).
- Observability for rollout.

**Out of scope (P3 backlog, per loose-ends doc)**

- Trailing SL/TP, Roll, Covered-Call/Collar/Synthetic close UX.
- Editable combo legs.
- `acknowledge_limit_override` button on close/add.
- BAG server-side preflight beyond the limit-band guard.

## Design

### Architecture

The existing F3 `quote_guard` operates on a single `(con_id, ticker,
limit_price)` tuple and enforces:

1. token freshness + HMAC validity,
2. payload ticker + `con_id` match,
3. non-crossed / non-zero-size quote,
4. limit on minTick grid,
5. limit within `min(ask × 1.05, ask + 2·minTick)` / `max(bid × 0.95, bid - 2·minTick)`.

For combos the limit is a **net** across legs. Steps 4–5 have no direct
per-leg analog. The design adds a parallel `check_combo` that keeps 1–3 per
leg and replaces 4–5 with a **net-band** check reconstructed from per-leg
token payloads.

### Frontend

- **New hook** `useQuoteTokens({ legs })` (expand
  `web/components/ticker-detail/useQuoteToken.ts`):
  - `legs` is `Array<{ ticker: string; conId: number; expiry: string | null }>`
    (length 1 for stock/single-leg, N for combo).
  - Issues N parallel `GET /api/orders/quote` requests.
  - Returns `{ tokens: Record<conId, string> | null, error: string | null }`.
  - Single-leg convenience: `tokens[conId]` on success.
- **`PositionOrderModal.handleSubmit`**:
  - Disable submit while any token is outstanding; surface `error` inline.
  - Payload attaches **one** of:
    - `quote_token: string` when `draft.payload.type ∈ {"stock", "option"}`,
    - `quote_tokens: { [con_id: string]: string }` when `type == "combo"`.

### Backend

- **`src/xenon/execution/quote_guard.py`** — new `check_combo(...)`:
  - Input: `legs: [{ token, con_id, ticker, action, right }]`, envelope
    `action`, combo `limit_price`, `now`, `token_secret`.
  - Per leg: token `verify`, ticker+con_id match, `bid_size>0`,
    `ask_size>0`, `bid ≤ ask`. Any failure → `QuoteVerdict(accept=False,
reason_code=STALE_QUOTE)`.
  - Net reconstruction (natural market, per `web/CLAUDE.md` "Combo Natural
    Market Bid/Ask"):
    - For each leg, compute effective side: `envelope_action XOR leg_action`.
      BUY envelope × BUY leg = pay ask; BUY envelope × SELL leg = receive bid;
      SELL envelope × BUY leg = receive bid; SELL envelope × SELL leg = pay ask.
    - `net_ask = Σ sign_leg × leg_side_price_for_cost_to_open`.
    - `net_bid = Σ sign_leg × leg_side_price_for_proceeds_to_close`.
  - Band: on envelope `action=BUY`, reject if `limit_price > net_ask × 1.05`.
    On `action=SELL`, reject if `limit_price < net_bid × 0.95`. **No tick
    grid** (combo nets are not on any leg's minTick).
  - Returns the same `QuoteVerdict` shape.

- **`src/xenon/api/server.py` `/orders/place` combo branch** (currently
  `server.py:1563`):
  - If `body.get("quote_tokens")` present → run `check_combo`. On failure,
    return the same 400 payload shape used on the non-combo path (`detail`,
    `reason_code`, `reason_detail`). On pass, emit `QUOTE_CHECK_PASS` to
    `orders_events`.
  - If absent → **soft-fail** (p2): append `QUOTE_TOKEN_MISSING_SOFT` to
    `orders_events` with `client_attempt_id`, continue. Exists for one
    rollout window only.
  - Non-combo branch (`server.py:1527`) is unchanged.

### Telemetry

Three new `orders_events` kinds on the combo path:

- `QUOTE_CHECK_PASS` — `{ net_bid, net_ask, limit_price, leg_count }`
- `QUOTE_CHECK_FAIL` — `{ reason_code, reason_detail, leg_count }`
- `QUOTE_TOKEN_MISSING_SOFT` — `{ leg_count }`

Rollout flip criterion: `QUOTE_TOKEN_MISSING_SOFT` count from the web UI ==
0 across one burn-in week → flip combo branch to hard-reject on missing
tokens in a follow-up one-line PR.

## Test plan

1. **`check_combo` unit — natural-market math across 6 canonical structures**
   (long single-leg, vertical, risk reversal, 1x2 ratio, straddle, jade lizard
   close). Verify reconstructed `net_bid`/`net_ask` against hand-computed
   values for both BUY and SELL envelopes.
2. **`check_combo` unit — one stale leg token → whole combo fails** with
   `STALE_QUOTE`.
3. **`check_combo` unit — net limit inside/outside band.**
4. **`/orders/place` route — combo with missing `quote_tokens` → 200**, one
   `QUOTE_TOKEN_MISSING_SOFT` row written.
5. **`/orders/place` route — combo with tampered token → 400** with
   `reason_code=STALE_QUOTE`.
6. **`/orders/place` route — combo with out-of-band net limit → 400** with
   `reason_code=LIMIT_OUT_OF_BAND`.
7. **Vitest `PositionOrderModal`** — mints N tokens in parallel; submit
   disabled until all present; payload carries `quote_tokens` map keyed by
   `con_id`.
8. **Playwright** — close a single-leg option and a vertical combo; verify
   network request payload contains token(s) and success toast.

## Risks and mitigations

| Risk                                                              | Mitigation                                                                             |
| ----------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| Natural-market sign math error → false-positive band rejects      | 6-structure unit set above; covers envelope×leg XOR matrix                             |
| Legitimate close rejected during volatile moments (±5% too tight) | Matches existing OrderTab behavior; acknowledge_limit_override is deliberately P3      |
| N parallel `/orders/quote` GETs add latency                       | Typical combos 2–4 legs; parallel, so ~1 RTT. Batch endpoint deferred under YAGNI      |
| Single-leg close already 400s today without surfacing             | Verify during implementation; if confirmed, this design also fixes it as a side effect |

## Open questions for implementation

- Does the modal currently trigger the 400 on single-leg option close, or is
  something else suppressing it? Confirm by instrumenting today's modal
  before/as part of the first commit.
- `acknowledge_limit_override` is out of scope, but if closes start hitting
  the band in the burn-in week, do we ship the override button in the same
  follow-up that flips missing→hard? Revisit at flip time.
