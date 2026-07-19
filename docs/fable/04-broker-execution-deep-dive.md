# 4. Broker Execution Deep Dive

## 4.1 IB — current state

**Model:** subprocess-per-write, pool-per-read (verified; see 02 §2.1). The write path is:
Next.js proxy → FastAPI gates → PG reservation → fresh subprocess (fresh TCP session,
auto clientId 20-49) → blind-sleep ack → PG `mark_submitted`/`mark_terminal`.

What is genuinely good (verify-before-change list):

- **Reservation-before-submit is a real transactional-outbox seed.** `reserve_attempt`
  commits a durable `PENDING` row before any broker interaction
  (`orders_store.py:106-160`), with a proper unique attempt key and proven one-winner
  semantics under concurrency (tests: `test_idempotency_route.py:95`,
  `test_orders_submissions_store.py:96`).
- **Fill idempotency is structural**: `order_fills.exec_id` PK + `ON CONFLICT DO NOTHING`
  (`orders_store.py:590-615`); commission lateness handled idempotently with
  `SELECT … FOR UPDATE` (`orders_store.py:643-710`); trade P&L self-heals via re-aggregation.
- **`order_events` is append-only** — a durable transition log (though not wired to
  literally every transition; `_mark_submission_cancelled` writes its event via a separate
  best-effort helper).
- **Ownership handling for cancel/modify is correct for IB semantics**: reconnect as the
  original placing clientId with 326 retries (`ib_order_manage.py:86-130`), classified
  errors → 503/409/404/400.
- **Account scope cannot be chosen by the browser on write routes** (`server.py:2140,2151`),
  scope is bound to the gateway login at boot and env-validated in subprocesses
  (`account_scope.py:52-102`).

The structural defects concentrate in one place: **the acknowledgement protocol**.
The subprocess prints exactly one JSON line at the end; every failure mode between
`placeOrder` and that line (timeout SIGKILL, crash, gateway drop) is collapsed into
terminal `FAILED` (OP-1/OP-2/OP-3). Nothing ties the broker-side order back to the
reservation — IB's `orderRef` field is unused (verified: no `orderRef` in
`ib_place_order.py`/`ib_client.py`), so post-hoc reconciliation cannot match an orphan
to its `submission_id` and instead invents a disconnected `snapshot-*` row.

## 4.2 Futu — current state

Verified capability matrix (evidence in the Futu audit; key cites inline):

| Capability                                                                   | Status                                                                                                                                  |
| ---------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| Positions + account summary                                                  | **Implemented** (`futu_client.py:280-674`; JSON cache path, deliberate DB-first exception, partial-failure guard `server.py:2683-2718`) |
| Orders query (open + 90-day history), deals, per-order fees                  | **Implemented, read mirror** → `xenon.futu_orders` / `futu_order_fees` / `futu_trades` (migration `2026_06_17_futu_orders`)             |
| Closed-trade FIFO reconstruction, NAV backfill, statements (PDF via Outlook) | **Implemented** (statement sync CLI is manual-only — no scheduler wiring found)                                                         |
| Quotes / market data                                                         | **Absent** (by policy)                                                                                                                  |
| Order placement / cancel / modify                                            | **Absent** — zero `place_order` / `unlock_trade` / `TrdSide` usages repo-wide                                                           |
| Partially implemented execution                                              | **None** — nothing half-built                                                                                                           |
| Dead execution code                                                          | **None** (closest: `FutuAuthError` defined for a not-yet-existing unlock flow)                                                          |

The read-side plumbing is a genuine head start for any future execution work
(`OpenSecTradeContext` already the connection object; `AccountScope.broker` already a
`Literal["IB","FUTU"]`; status/type vocabularies documented). The hard blockers are:

1. `CheckConstraint("broker = 'IB'")` on `order_submissions`, `trades`, wizard tables
   (`schema.py:606,115,707,757`) — the execution ledger structurally rejects Futu rows.
2. No `unlock_trade` flow, no Futu-side naked-short guard, no Futu equivalent of the
   place/manage CLIs, no caller-allowlist guard.
3. `futu_orders` is a read-mirror shape (no `client_attempt_id`, no lifecycle columns) —
   it cannot serve as a submission ledger.

**Do not claim Futu order placement exists. It does not.**

## 4.3 Broker abstraction assessment (Part 2 answers)

- **Broker-neutral domain order model?** Partially in column names only. `order_submissions`
  carries `broker/account_env/broker_account`, but the CHECK pins it to IB, and the row
  stores IB-native identity (`ib_order_id`, `perm_id`, `placing_client_id`). Not neutral in
  practice.
- **Broker-neutral order state machine?** No explicit machine at all — states are string
  literals scattered across writers (see 4.4); semantics are IB-shaped (WORKING, permId
  resurrection).
- **Broker request objects leaking upward?** Yes: the browser payload carries IB concepts
  end-to-end (`permId`, `orderId` in cancel/modify bodies; `initialStatus` = raw IB status
  in the place response; `con_id`, `exchange` in the UI payload builder).
- **clientId/permId/BAG/status isolation?** Not isolated — they are the lingua franca of
  the whole stack, including the UI polling logic (`OrderActionsContext` compares
  `permId`/`orderId`).
- **Can Futu execution be added without duplicating the IB workflow?** No. Today it would
  require a parallel CLI + parallel gates + schema surgery. That is a fact, not necessarily
  a defect — see verdict below.
- **Is AccountScope sufficient?** For _identity_ (broker, env, account) yes, and it is
  enforced well. For _capabilities_ no — capability is expressed as one hardcoded branch
  (403 `READ_ONLY_BROKER` when `broker != "IB"`, `server.py:2140-2149`).
- **Capability differences modelled explicitly?** No table/enum of capabilities exists; but
  unsupported operations do **fail early and clearly** (the 403 above, tested in
  `test_place_quote_gate.py:234`).
- **Paper/live separation?** Strong and multi-layered (dev.sh guard `dev.sh:133-138`,
  PG role grants, `XENON_READ_ONLY`, scope columns + env validation). Verified.
- **Persistence broker-neutral in practice?** No — see CHECK constraints; Futu data lives in
  its own mirror tables. Honest, if not neutral.

**Verdict on the proposed `BrokerExecutionAdapter` Protocol:** do **not** introduce it now.
There is exactly one execution implementation; an adapter would be an interface with one
implementor, removing zero duplication while adding an indirection that the review's own
evidence says isn't the problem (the problem is ack semantics and validation duplication).
What _is_ worth doing now, cheaply:

1. A `BROKER_CAPABILITIES` dict (place/cancel/modify/stream flags) replacing the scattered
   `broker != "IB"` branches — one lookup, one 403 helper.
2. Typed `PlaceOrderCommand` / `OrderAck` dataclasses at the FastAPI↔subprocess boundary
   (the JSON contract already exists informally; typing it costs little and is the seam an
   adapter would later slot into).
3. Defer the Protocol to the moment a second execution backend is approved (Phase 5).

## 4.4 Order state machine (Part 3 answers)

Reconstructed from writers (full inventory and transition map in the state-machine audit;
verified spot checks this review):

- **States written:** `PENDING`, `WORKING`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`,
  `REJECTED`, `FAILED`, `UNKNOWN` — free-text column, no CHECK (OP-9). `trades.state` is
  separately constrained (OPEN/PARTIALLY_FILLED/CLOSED). Combos run a **second** state
  machine on `wizard_sessions.state`.
- **Implicit states:** `snapshot-<permId>` rows (externally-discovered orders), permId=0
  identity window (defended only in the sweep), `orderId=0` BAG-by-foreign-client fallback,
  and "FAILED-but-live" (OP-1) which has no representation at all.
- **Non-atomic transitions:** every broker-side effect is a subprocess; the paired DB write
  is a separate later transaction (`place → mark_submitted`, `cancel → mark_terminal`).
  Rehydrate decides in memory then writes per-row in separate transactions.
- **Races found:** terminal-clobber (expected_states unused outside the sweep, OP-8);
  audit-cancel vs poller (OP-5); permId=0 fill-resolution fallthrough; snapshot-import
  SELECT-then-INSERT race (explicitly no-op-tolerated in code, `orders_store.py:421-427`).
- **Stuck states:** hung-subprocess `PENDING` until next restart (OP-3); WORKING rows
  frozen by the empty-snapshot sweep guard until a non-empty snapshot (documented trade-off).
- **Timeout treated as failure though broker may have accepted:** yes — the central defect
  (OP-1). **Retry can duplicate:** yes, under a new attempt id (OP-16).
- **Idempotency verdict:** submission is idempotent **per attempt id** (genuinely well
  built and well tested); it is **not** idempotent across the timeout/ambiguity boundary,
  and reconciliation cannot re-associate orphans (no orderRef).

**Recommendation:** an explicit state machine is justified here — not as a framework, but
as (a) one enum + CHECK constraint, (b) one `transition(submission_id, from_states, to,
event)` function that every writer must use (it already almost exists as
`mark_terminal(expected_states=…)`), and (c) a new `UNCERTAIN` state for ambiguous broker
outcomes. This solves demonstrated problems: OP-1, OP-5, OP-8, OP-9.

## 4.5 Subprocess execution model — design comparison (Part 4)

Facts first (all confirmed): fresh process + fresh IB session per write; connect timeout
10 s (place) / 3 s (manage); blind 2/5 s ack sleep; 15 s outer SIGKILL; no concurrency
bound; no orphan reaping beyond the kill; clientId auto-probe; full env inheritance.
Latency ceilings are code constants; typical values are **unmeasured** (see 08).

| Criterion              | D1: subprocess-per-op (current)                                                    | D2: persistent in-process session                                                                          | D3: dedicated execution service + durable queue | D4: hybrid (persistent worker + scheduled recon) |
| ---------------------- | ---------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- | ----------------------------------------------- | ------------------------------------------------ |
| Expected latency       | Worst: interpreter+connect+qualify+2-5 s sleep (est 3.5–6 s typical; 15 s cap)     | Best: no spawn/connect; event-driven ack could be ~0.3–1 s (est)                                           | Same as D2 + queue hop                          | Same as D2                                       |
| Failure isolation      | **Excellent** — a wedged IB call can't poison FastAPI's loop; SIGKILL always works | Poor-medium: ib_async loop pinning; a hung session degrades the API process                                | Excellent (separate process)                    | Good (worker process separate from FastAPI)      |
| Ordering guarantees    | None across concurrent ops                                                         | Natural serialization per session                                                                          | Strong (queue)                                  | Strong per worker                                |
| Horizontal scaling     | N/A (clientId range is the limit)                                                  | No                                                                                                         | Yes (but nothing here needs it)                 | No                                               |
| Broker API fit         | Fine; owner-clientId dance needed for cancel/modify                                | Master clientId 0 or fixed owner id simplifies cancel/modify (the dormant `pool_order_manage` proves this) | Fine                                            | Fine                                             |
| Operational complexity | **Lowest** — no new daemon, restart-safe by construction                           | Low (code risk instead)                                                                                    | Highest: new deployable, queue, monitoring      | Medium: one worker task/process                  |
| Restart behavior       | Trivially safe (state in PG)                                                       | API restart drops session mid-op                                                                           | Queue survives; best                            | Worker restart = reconnect + resume from PG      |
| Idempotency            | Reservation good; ack boundary broken (OP-1)                                       | Same reservation; event-driven ack closes most of the OP-1 window                                          | Outbox pattern native                           | Same as D2                                       |
| Observability          | Poor (one JSON line; no in-flight visibility)                                      | Good (in-proc metrics/events)                                                                              | Good                                            | Good                                             |
| Migration risk         | —                                                                                  | Medium (loop pinning, ownership semantics, well-known ib_async pitfalls in this repo's memory)             | High                                            | Medium                                           |

**Assessment.** D1's fault isolation and operational simplicity are real strengths for a
single-operator terminal, and the repo's own history (ib_async event-loop hangs, clientId
kicks) justifies the paranoia. Its two genuine costs are the fixed multi-second latency
and the ambiguous-ack window. D3 is overkill at this scale. The pragmatic path:

- **Now:** keep D1, fix the ack protocol inside it (orderRef + early ack line +
  UNCERTAIN + reconciliation sweep + semaphore). This removes the Critical risk without
  architectural change.
- **Later, if measured latency matters:** move to **D4** by promoting the already-written,
  already-tested `pool_order_manage` path to primary for cancel/modify, then place — a
  persistent "orders"-role session (clientId 4) owned by a single worker task, with the
  subprocess path retained as a degraded-mode fallback. Reconciliation (poller/rehydrate)
  stays exactly as scheduled today.

## 4.6 Target architecture for execution

See 09 for the full options and diagrams. Summary: Option A (minimal, ack-protocol fix)
immediately; Option B (D4 hybrid) as a later phase gated on measurements; broker adapter
Protocol only if/when Futu execution is approved (Phase 5).
