# P1.4 — OP-10 live PAPER probe: external-fill visibility (EXPERIMENT, not a fix)

- **Date:** 2026-07-05
- **Proposed branch:** `experiment/op10-external-fill-probe`
- **Finding IDs:** OP-10 (`docs/fable/03-findings-table.md`)
- **Severity:** Medium (High if confirmed)
- **Goal (one line):** Determine, with captured evidence on the PAPER account, whether an
  IB fill originating **outside** Xenon (placed directly in TWS/mobile) reaches
  `xenon.order_fills` via the activity poller — then correct EITHER the memory OR the
  `ib_activity_mirror.py` docstring so the codebase stops asserting an unverified claim.

> This is an **experiment**, not a code change to production behavior. The only files this
> plan writes are: one new read-only research script, one docstring, one memory file, and
> one measurements artifact. **No production fills-path logic is modified.** Do NOT implement
> the Flex-overlay or master-clientId follow-up here — those are reference-only outcomes.

---

## Drift from review (READ THIS FIRST — the review docs are partly stale)

Three deltas found vs. the fable finding and the `project_ib_external_fills_invisible` memory.
Adapt to these; do not plan against the stale facts.

1. **The poller does NOT call `reqExecutions` — it calls `ib.fills()` (cached wrapper
   fills).** OP-10's row says "`reqExecutions`/`fills()` likely can't see TWS/mobile fills".
   Verified at HEAD: `ib_activity_mirror._safe_fills_tick` → `fetch_ib_executions(client)`
   (`src/xenon/execution/ib_reconcile.py:105`) → `client.get_fills()`
   (`src/xenon/clients/ib_client.py:833`) → `self._ib.fills()` →
   `list(self.wrapper.fills.values())` (`.venv/.../ib_async/ib.py:618`). `wrapper.fills` is
   populated by `execDetails` callbacks the connected client receives. So the real question
   is whether **clientId 3 receives `execDetails` for an externally-placed order**, which —
   like `reqExecutions` — is gated by IB master-client configuration. The probe script tests
   BOTH `fills()` and `reqExecutions` so the mechanism is nailed down regardless.

2. **The "resolution shipped (PR #158, `flex_fill_reconcile.py`)" claim in the memory is
   FALSE for master.** Verified: `src/xenon/api/services/flex_fill_reconcile.py` does **not
   exist** at HEAD (`git ls-files | grep flex_fill` → empty). Its commits (`8c8882a9`,
   `397ec99e`) live only on branch `fix/executed-orders-intraday-fills`
   (`git branch -a --contains 397ec99e`), and `git merge-base --is-ancestor 397ec99e HEAD`
   fails — local git evidence, independently corroborated by `gh pr view 158` →
   `"state":"OPEN"`, `"mergedAt":null` (checked 2026-07-05; `gh` may be network-blocked at
   execution time — the local git checks alone are sufficient proof).
   So on master today there is **no** external-fill backfill of any kind.
   The plan's memory-correction text (below) fixes this regardless of the probe outcome.

3. **The prod probe in the memory (clientId 0 and 6 both returned 0 execs) was on the LIVE
   macmini Gateway.** This plan is PAPER-only (port 4002). The paper Gateway is a separate
   process with its own config; we re-measure there for a clean, self-contained capture and
   do not assume the live result transfers.

---

## Context — what exists today (verified `file:function` citations)

- **Poller entry:** `src/xenon/api/server.py::_maybe_start_activity_poller` (line ~252)
  starts `activity_poller_loop` with `ib_client_factory=_ib_client_factory` (line ~288),
  where the factory returns `ib_pool.get_with_reconnect_sync("sync")`. The **"sync" pool
  role = clientId 3** (`POOL_ROLES = {"sync": 3, "orders": 4, "data": 5}`,
  `src/xenon/clients/ib_client.py:87`). Default interval 60 s
  (`DEFAULT_POLL_INTERVAL_S = 60`, override `XENON_IB_ACTIVITY_POLL_S`), poller enabled
  unless `XENON_IB_ACTIVITY_POLLER=0`.
- **Fills tick:** `src/xenon/api/services/ib_activity_mirror.py::_safe_fills_tick` →
  `fetch_ib_executions` → `record_external_fills` (writes `xenon.order_fills`).
- **`get_fills`:** `src/xenon/clients/ib_client.py::IBClient.get_fills` returns
  `self._ib.fills()`.
- **`reqExecutions`:** `src/xenon/clients/ib_client.py::IBClient.get_executions` returns
  `self._ib.reqExecutions(exec_filter)`. ib_async's `reqExecutionsAsync` defaults the filter
  to `ExecutionFilter()` whose `clientId=0` field (`.venv/.../ib_async/objects.py:88`) is a
  **result filter** ("no clientId filter"), NOT the same thing as being connected as the
  master client.
- **Target table:** `xenon.order_fills` (`src/xenon/db/schema.py:650`). Columns the queries
  below use: `exec_id` (PK), `perm_id`, `ticker`, `side`, `qty`, `price`, `filled_at`,
  `broker`, `account_env`, `broker_account`, `submission_id`, `metadata`.
- **Scope:** paper → `AccountScope(broker="IB", account_env="paper", broker_account="DU…")`
  (`src/xenon/execution/account_scope.py::resolve_from_env`, `_MODE_TO_PREFIX={"paper":"DU"}`).

**What the executor does NOT need to understand:** the naked-short guard, combo/BAG
semantics, the WS relay, NAV reconciliation, or how `record_external_fills` aggregates
trades. This experiment only reads DB rows and IB execution lists.

---

## Key facts (verified against the working tree)

| Fact                          | Value                                                                                                                                                                                                                                                     | Source                                                                                        |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Paper IB Gateway              | host `127.0.0.1`, port **4002**                                                                                                                                                                                                                           | `scripts/infra/dev.sh` header                                                                 |
| Live Gateway (FORBIDDEN here) | `100.66.147.98:4001`                                                                                                                                                                                                                                      | xenon CLAUDE.md                                                                               |
| Sync pool role clientId       | **3**                                                                                                                                                                                                                                                     | `ib_client.py:87` `POOL_ROLES`                                                                |
| Poller cadence env            | `XENON_IB_ACTIVITY_POLL_S` (default 60 s)                                                                                                                                                                                                                 | `ib_activity_mirror.py:40`                                                                    |
| Poller disable env            | `XENON_IB_ACTIVITY_POLLER=0`                                                                                                                                                                                                                              | `server.py:271`                                                                               |
| Poller uses                   | `ib.fills()` (cached), NOT `reqExecutions`                                                                                                                                                                                                                | see Drift #1                                                                                  |
| `ExecutionFilter().clientId`  | `0` = "no client filter" (result filter, not master)                                                                                                                                                                                                      | `ib_async/objects.py:88`                                                                      |
| IB doc note                   | "To receive commissions reports for all clients it is necessary to connect as the Master Client ID." + "By default, only … executions occurring since midnight … will be delivered."                                                                      | IBKR TWS API `executions_commissions.html` (fetched 2026-07-05)                               |
| Subprocess clientId band      | 20–49 (use `client_id="auto"` / raw `clientId` in-band)                                                                                                                                                                                                   | `ib_client.py:87` `SUBPROCESS_ID_RANGE`                                                       |
| Dev FastAPI port              | **8421**                                                                                                                                                                                                                                                  | xenon CLAUDE.md                                                                               |
| order_fills table             | `xenon.order_fills`                                                                                                                                                                                                                                       | `schema.py:650`                                                                               |
| Paper scope                   | `broker='IB'`, `account_env='paper'`, `broker_account` starts `DU`                                                                                                                                                                                        | `account_scope.py:33`                                                                         |
| Local paper DB                | `DATABASE_URL_PAPER` (LOCAL `127.0.0.1/core_test`) — NOT `DATABASE_URL_TEST`. It is a SQLAlchemy URL (`postgresql+asyncpg://…`); **strip `+asyncpg` before passing to psql**                                                                              | memory `project_two_core_test_dbs`; `.env`                                                    |
| `/orders/place` stock body    | `{"type":"stock","symbol":…,"action":"BUY","quantity":1,"limitPrice":…,"con_id":…,"quote_token":…,"client_attempt_id":…}` — `client_attempt_id` REQUIRED (400 `"client_attempt_id is required"` without it); there is **no** bare `orderType:"MKT"` shape | `scripts/tests/test_place_quote_gate.py` passing bodies; `server.py::_orders_place_from_body` |
| Quote endpoint                | `GET /orders/quote?ticker=<T>&con_id=<conId>` → `{token, bid, ask, bid_size, ask_size, ts_server_ms}`                                                                                                                                                     | `src/xenon/api/server.py:2085`                                                                |
| RTH window                    | Mon–Fri 9:30–16:00 ET (`TZ=America/New_York date +"%A %H:%M"`)                                                                                                                                                                                            | xenon CLAUDE.md § Market Hours                                                                |

**`[KNOWN, MED confidence]`** IB's documented behavior is that only the connection registered
as the Gateway **Master Client ID** receives execution/commission callbacks for orders placed
by _other_ clients (including the TWS UI's own client). A non-master client sees only its own
executions. The IB doc quote above confirms the master-client requirement for commissions;
the execution-visibility corollary is standard IB field knowledge and is exactly what this
probe measures directly rather than asserts.

---

## Goal / Non-goals

**Goal:** Produce a captured yes/no: _does an externally-placed PAPER fill land in
`xenon.order_fills` within 2 poller ticks?_ Correct the codebase (memory OR docstring) to
match reality. Land a reusable read-only probe script.

**Non-goals (explicitly NOT done in this PR):**

- Do **not** implement Flex-overlay backfill, master-clientId re-routing, or any fills-path
  fix. Those are the _follow-up_ an "invisible" outcome would motivate — reference only.
- Do **not** merge or cherry-pick PR #158 / branch `fix/executed-orders-intraday-fills`.
- Do **not** touch the health-probe false-positive (`ib_pool.connected: true`) — separate
  finding.
- Do **not** change poller cadence, pool roles, or `record_external_fills` logic.
- No live account, ever (tripwire below).

---

## Steps (strictly ordered)

### Step 0 — Preconditions (operator + agent)

0.1 Create the branch:

```bash
cd /Users/chenxi/projects/xenon
git checkout -b experiment/op10-external-fill-probe
```

0.2 **RTH precondition (HARD gate — tripwire T7):**

```bash
TZ=America/New_York date +"%A %H:%M"
```

Must be Mon–Fri between 09:30 and 15:45 ET (15:45 leaves headroom to fill + observe +
flatten before the close). If outside RTH, **STOP and defer the run** — a paper MKT order
after hours may sit unfilled and make the whole experiment uninterpretable. Do NOT
improvise with limit-at-close or overnight orders.

0.3 Confirm IB **paper** Gateway is running and logged in on `127.0.0.1:4002` (operator
approves 2FA if cold). If a probe/psql in this plan ever resolves to host `100.66.147.98`
or port `4001`, **STOP** (tripwire T1).

### Step 1 — Land the read-only probe script (no test-first; it is a measurement tool)

Create `scripts/research/probe_external_fills.py` with the EXACT content below. It mirrors
the convention of the existing `scripts/research/probe_tsej_marketdata.py` (raw `ib_async`,
`argparse`, JSON to stdout, error handler). It is **read-only**: it never places or cancels
an order. It connects on an auto subprocess clientId, then prints BOTH `ib.fills()` and
`ib.reqExecutions(ExecutionFilter())` results with each execution's `clientId`, so we learn
whether a fresh non-master client can see the externally-placed fill and via which path.

```python
#!/usr/bin/env python3
"""One-off: can a fresh (non-master) IB client see externally-placed fills?

OP-10 / P1.4 experiment. Read-only. Connects to the PAPER Gateway on an
auto-allocated subprocess clientId, then dumps BOTH execution surfaces:

  - ib.fills()                      -> cached execDetails this client received
  - ib.reqExecutions(EmptyFilter)   -> actively re-requested executions

For every execution it prints the originating execution.clientId. If an
externally-placed fill (e.g. a TWS-UI BUY) shows up here with a clientId that
is NOT this probe's own clientId, then executions ARE account-wide from any
client. If both lists are empty / contain only this client's execs, external
fills are client-scoped and invisible to xenon's non-master pool (clientId 3).

PAPER ONLY. Hard guard: host must be 127.0.0.1/localhost AND port must be
4002 — anything else exits 2 before connecting.

Usage:
    uv run python scripts/research/probe_external_fills.py            # 127.0.0.1:4002
    uv run python scripts/research/probe_external_fills.py --json-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from ib_async import IB, ExecutionFilter


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--client-id", type=int, default=29)  # subprocess band 20-49
    parser.add_argument("--json-only", action="store_true")
    args = parser.parse_args()

    # Hard PAPER-only guard: local host AND paper port, enforced in code —
    # not just in operator instructions.
    if args.host not in ("127.0.0.1", "localhost") or args.port != 4002:
        print(
            f"REFUSED: {args.host}:{args.port} — this probe only connects to the local "
            "PAPER gateway 127.0.0.1:4002 (never 4001/live, never remote hosts).",
            file=sys.stderr,
        )
        return 2

    ib = IB()
    errors: list[dict] = []
    ib.errorEvent += lambda reqId, code, msg, contract: errors.append(
        {"reqId": reqId, "code": code, "msg": msg}
    )

    await ib.connectAsync(args.host, args.port, clientId=args.client_id, timeout=10)
    try:
        # Give the wrapper a moment to receive any initial execDetails.
        await asyncio.sleep(2)

        def dump(fills):
            rows = []
            for f in fills:
                e = f.execution
                rows.append(
                    {
                        "execId": getattr(e, "execId", None),
                        "clientId": getattr(e, "clientId", None),
                        "permId": getattr(e, "permId", None),
                        "orderId": getattr(e, "orderId", None),
                        "symbol": getattr(f.contract, "symbol", None),
                        "side": getattr(e, "side", None),
                        "shares": getattr(e, "shares", None),
                        "price": getattr(e, "price", None),
                        "time": str(getattr(e, "time", None)),
                    }
                )
            return rows

        cached = dump(ib.fills())
        req = dump(await ib.reqExecutionsAsync(ExecutionFilter()))

        out = {
            "probe_client_id": args.client_id,
            "host": args.host,
            "port": args.port,
            "cached_fills_count": len(cached),
            "reqExecutions_count": len(req),
            "cached_fills": cached,
            "reqExecutions": req,
            "distinct_clientIds_in_reqExecutions": sorted(
                {r["clientId"] for r in req if r["clientId"] is not None}
            ),
            "errors": errors,
        }
        print(json.dumps(out, indent=2 if not args.json_only else None, default=str))
        return 0
    finally:
        ib.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

Smoke-check the script parses and the guard refuses non-paper targets:

```bash
uv run python scripts/research/probe_external_fills.py --port 4001; echo "exit=$?"
# EXPECT stderr: "REFUSED: 127.0.0.1:4001 ..." and exit=2
uv run python scripts/research/probe_external_fills.py --host 100.66.147.98; echo "exit=$?"
# EXPECT stderr: "REFUSED: 100.66.147.98:4002 ..." and exit=2
```

### Step 2 — Boot the PAPER stack with the poller running (fast cadence)

2.1 In a dedicated terminal:

```bash
cd /Users/chenxi/projects/xenon
XENON_IB_ACTIVITY_POLL_S=15 scripts/infra/dev.sh paper
```

(15 s cadence so "2 poller ticks" ≈ 30 s. `dev.sh paper` exports `XENON_TRADING_MODE=paper`,
derives port 4002, runs migrations, and starts FastAPI :8421 + Next :3200.)

2.2 Verify the stack is up and the sync pool client is connected:

```bash
curl -s http://localhost:8421/health | uv run python -m json.tool
# EXPECT: ib_gateway.port_listening: true ; ib_pool sync role present/connected
```

2.3 Capture the resolved paper scope for the psql queries (record the exact value):

```bash
grep -E '^DATABASE_URL_PAPER=' .env      # local core_test DSN used by dev.sh paper
```

Note the running `XENON_BROKER_ACCOUNT` (a `DU…` id). Call it `$DU` below.

2.4 Baseline: confirm no matching fills yet. Using the LOCAL paper DSN (`$PGP`):

```bash
# DATABASE_URL_PAPER is a SQLAlchemy URL (postgresql+asyncpg://...); psql rejects
# the +asyncpg driver suffix — strip it:
PGP="$(grep -E '^DATABASE_URL_PAPER=' .env | cut -d= -f2- | sed 's/+asyncpg//')"
psql "$PGP" -c "SELECT count(*) FROM xenon.order_fills WHERE account_env='paper' AND ticker='AAPL' AND filled_at::date = CURRENT_DATE;"
# EXPECT: count = 0 (or note the pre-existing baseline number)
```

### Step 3 — TREATMENT: place an external fill directly in TWS paper (operator action)

**Operator, in the TWS PAPER desktop app (NOT via Xenon, NOT via the API):**

1. Order Entry ticket → symbol **AAPL**, **BUY**, quantity **1**, order type **MKT**.
2. Transmit. Confirm it fills (Trade Log / Executions shows 1 share BOT).
3. Record the fill time (ET). This is the _externally-placed_ fill.

> AAPL @ 1 share market on paper is the smallest safe liquid probe. Do NOT use options
> (permId/execId correlation is identical but options add contract-qualification noise).

### Step 4 — Observe: TWO separate answers, recorded independently

This experiment answers **two distinct questions**. Do not conflate them; record each with
its own evidence.

- **Answer A (decides OP-10 for production):** does the CURRENT poller path — cached
  `ib.fills()` on pool clientId 3 — insert the external fill into `xenon.order_fills`?
- **Answer B (informs the remediation choice only):** does an ACTIVE
  `reqExecutions(ExecutionFilter())` on a fresh clientId return the external fill?

A probe-only (Answer-B) success **never** marks OP-10 VISIBLE. Only the `order_fills` row
(Answer A) does — production runs the poller, not the probe.

4.1 **Answer A** — poller path, query `order_fills` within 2 poller ticks (≥40 s to be safe
at the 15 s cadence):

```bash
psql "$PGP" -c "SELECT exec_id, perm_id, ticker, side, qty, price, filled_at, submission_id FROM xenon.order_fills WHERE account_env='paper' AND ticker='AAPL' AND filled_at::date = CURRENT_DATE ORDER BY filled_at DESC LIMIT 5;"
```

- **Row present** for your TWS fill (side `BOT`/`BUY`, qty 1) → **A = YES**.
- **No new row** after ≥ 2 ticks → **A = NO**.

  4.2 **Answer B** — independent probe (does not depend on the poller). While the stack still
  runs, in a second terminal:

```bash
uv run python scripts/research/probe_external_fills.py > /tmp/op10_external_probe.json
cat /tmp/op10_external_probe.json
```

- `reqExecutions` **containing the AAPL BOT** with a `clientId` that is NOT
  `probe_client_id` (29) → **B = YES** (active reqExecutions is account-wide here).
- No AAPL in `reqExecutions` → **B = NO** (executions are client-scoped).

  4.3 Poller log corroboration for Answer A (optional): the boot terminal's poller lines show
  `fills[ins=N]`. `ins=0` for the tick following the TWS fill corroborates **A = NO**.

### Step 5 — CONTROL: a Xenon-placed order must be visible

Place a small order **through Xenon** (paper) to prove the fills path itself works and the
`A = NO` result (if any) is specifically about _external_ origin, not a broken poller.

**Primary route — direct FastAPI curl** (body shape verified against
`scripts/tests/test_place_quote_gate.py` and `server.py::_orders_place_from_body`: fields
`type`/`symbol`/`action`/`quantity`/`limitPrice`/`con_id`/`quote_token`/`client_attempt_id`;
`client_attempt_id` is required, and there is no `orderType:"MKT"` shape — use a
**marketable limit** at the quoted ask):

```bash
# 1. Fetch a quote + single-use quote token. AAPL conId = 265598
#    ([KNOWN] IB conId for AAPL/NASDAQ; if this 404s/errors, read the conId from
#    the TWS contract description window and substitute).
Q=$(curl -s "http://localhost:8421/orders/quote?ticker=AAPL&con_id=265598")
echo "$Q" | uv run python -m json.tool          # note "ask" and "token"
ASK=$(echo "$Q" | uv run python -c "import sys,json;print(json.load(sys.stdin)['ask'])")
TOK=$(echo "$Q" | uv run python -c "import sys,json;print(json.load(sys.stdin)['token'])")

# 2. Place BUY 1 AAPL at the ask (marketable limit -> fills like a market order in RTH):
curl -s -X POST http://localhost:8421/orders/place \
  -H 'Content-Type: application/json' \
  -d "{\"type\":\"stock\",\"symbol\":\"AAPL\",\"action\":\"BUY\",\"quantity\":1,\"limitPrice\":$ASK,\"con_id\":265598,\"quote_token\":\"$TOK\",\"client_attempt_id\":\"op10-control-$(date +%s)\"}"
# EXPECT: HTTP 200 with an order/submission payload. A 400 with reason_code is a
# preflight/quote-gate rejection — read the reason_detail, do not retry blindly.
```

**Fallback route — Xenon web UI** (use only if the curl is rejected for a reason you cannot
resolve from `reason_detail`): open `http://localhost:3200`, open the AAPL ticket from the
portfolio/order-entry surface, set BUY, quantity 1, limit = current ask, submit, and confirm
the order appears in Xenon's open-orders panel. The point is Xenon-origin, any route.

Then within 2 ticks:

```bash
psql "$PGP" -c "SELECT exec_id, perm_id, ticker, side, qty, submission_id FROM xenon.order_fills WHERE account_env='paper' AND ticker='AAPL' AND filled_at::date = CURRENT_DATE ORDER BY filled_at DESC LIMIT 5;"
# EXPECT: a row WITH a non-null submission_id (Xenon-origin) appears -> control PASSES
```

If the control does NOT appear, **STOP** (tripwire T2): the poller is broken for an
unrelated reason and the external result is uninterpretable.

### Step 6 — Decide and record the outcome

The OP-10 verdict is decided by **Answer A alone** (the poller path is what production
runs). Answer B is recorded alongside it to steer the remediation choice:

- **A = YES** (external AAPL row in `order_fills` within 2 ticks) → OP-10 **VISIBLE** →
  Go to **Step 7a**. Record B too (expected YES as well).
- **A = NO** and control (Step 5) passed → OP-10 **INVISIBLE** → Go to **Step 7b**.
  - If **B = YES** (active `reqExecutions` DID return the external fill while the cached
    poller path missed it): note this explicitly in the measurements doc — it means the
    remediation could be as small as switching the fills tick from cached `ib.fills()` to
    an active `reqExecutions` call, no Master Client ID needed. Still reference-only here.
  - If **B = NO**: executions are client-scoped on this Gateway; remediation needs a
    Master Client ID or the Flex overlay.
- A probe-only success (**B = YES, A = NO**) is NOT "VISIBLE" — never take the 7a branch
  on Answer B.

Save the raw evidence:

```bash
cp /tmp/op10_external_probe.json output/op10-external-probe-2026-07-05.json
```

### Step 7a — OUTCOME = VISIBLE → correct the memory, keep the docstring

The docstring's claim (poller sees TWS-side activity) would be CORRECT, so leave
`ib_activity_mirror.py` untouched. Replace the body of the memory file
`/Users/chenxi/.claude/projects/-Users-chenxi-projects-xenon/memory/project_ib_external_fills_invisible.md`
(keep the YAML front-matter block lines 1–8 intact; replace everything from line 10 down)
with:

```markdown
**UPDATED 2026-07-05 (PAPER probe, P1.4/OP-10): external fills ARE visible on the paper
Gateway.** A market BUY 1 AAPL placed directly in TWS paper appeared in `xenon.order_fills`
within 2 poller ticks (15 s cadence), and `scripts/research/probe_external_fills.py` showed
the execution under a clientId other than the probe's own — i.e. `reqExecutions`/`fills()`
returned account-wide executions on this Gateway. The earlier prod finding (clientId 0 and 6
both returned 0 execs) reflects the LIVE macmini Gateway config, not the paper one; the two
Gateways differ. Do NOT assert "external fills invisible" as a universal fact — it is
Gateway-config-dependent. Capture: `output/op10-external-probe-2026-07-05.json`,
`docs/fable/measurements-2026-07-05.md`.

NOTE: PR #158 (`flex_fill_reconcile.py`) is still OPEN/unmerged on branch
`fix/executed-orders-intraday-fills` — there is no Flex external-fill backfill on master.

Validate IB changes against paper first ([[feedback_broker_bugs_paper_first]]).
```

### Step 7b — OUTCOME = INVISIBLE → fix the docstring + append measurements + note follow-up

**7b.1** Correct the misleading docstring. Edit `src/xenon/api/services/ib_activity_mirror.py`,
replacing the exact opening block:

```python
"""IB→Postgres activity mirror.

Symmetric counterpart to ``register_from_snapshot`` (open orders): pulls
fills/executions from IB and inserts them into ``xenon.order_fills`` so
the blotter sees TWS-side activity even when an order originated outside
Xenon (manually placed in TWS, modified in TWS, etc).

Phase 1 surfaces a single boot-time replay. The periodic poller (Phase 2)
will reuse the same internals.
```

with:

```python
"""IB→Postgres activity mirror.

Symmetric counterpart to ``register_from_snapshot`` (open orders): pulls
fills/executions from IB and inserts them into ``xenon.order_fills``.

SCOPE LIMIT (verified PAPER probe 2026-07-05, OP-10 / P1.4): the fills path
uses cached ``ib.fills()`` on the pool's ``sync`` role (clientId 3), a
NON-master client — and a fill on an order placed OUTSIDE Xenon (TWS UI,
IBKR mobile, claude.ai MCP) does NOT reach it, so external fills never land
in ``order_fills``. Only OPEN-ORDER state is all-client
(``reqAllOpenOrders``), so external cancels/modifies of still-open orders
ARE mirrored — external FILLS are the specific blind spot. Capture:
``docs/fable/measurements-2026-07-05.md``.

Phase 1 surfaces a single boot-time replay. The periodic poller (Phase 2)
reuses the same internals.
```

**7b.2** Create `docs/fable/measurements-2026-07-05.md` (append if it already exists) with:

```markdown
# Fable measurements — 2026-07-05

## OP-10 / P1.4 — external-fill visibility (PAPER probe)

**Result: INVISIBLE (Answer A = NO).** A market BUY 1 AAPL placed directly in TWS PAPER
(Gateway 127.0.0.1:4002) did NOT appear in `xenon.order_fills` within 2 poller ticks
(15 s cadence), while a Xenon-placed control BUY 1 AAPL DID appear (with a non-null
`submission_id`) within the same window.

**Answer B (active `reqExecutions` on fresh clientId 29): <YES|NO — fill in from
output/op10-external-probe-2026-07-05.json>.** If B = NO: the Gateway delivers executions
client-scoped (no Master Client ID configured) — remediation needs a Master Client ID or
the Flex overlay. If B = YES: the blind spot is specific to the CACHED `ib.fills()` path
the poller uses — an active `reqExecutions` per tick would already close it.

- Poller path: `ib_activity_mirror._safe_fills_tick` → `ib_client.get_fills()` → `ib.fills()`
  (cached `execDetails`, clientId 3). Non-master ⇒ external `execDetails` never arrive.
- Raw capture: `output/op10-external-probe-2026-07-05.json`.
- Confirms the pre-existing prod finding (`project_ib_external_fills_invisible`) also holds
  on paper.

**Follow-up (reference only — NOT implemented in this PR):** to surface externally-placed
fills, EITHER (a) run the fills mirror on a Master Client ID (a shared-Gateway change that
also affects radon — see `project_radon_xenon_shared_gateway_clientid`), OR (b) land an IB
Flex account-level overlay (the still-OPEN PR #158 `flex_fill_reconcile.py` on branch
`fix/executed-orders-intraday-fills`, deduped by `perm_id`). Track as a new roadmap item.
```

**7b.3** Update the memory file's `**Resolution shipped (PR #158 …)**` paragraph — it is
factually wrong (PR #158 is OPEN, not shipped, and absent from master). Edit
`/Users/chenxi/.claude/projects/-Users-chenxi-projects-xenon/memory/project_ib_external_fills_invisible.md`,
replacing the paragraph beginning `**Resolution shipped (PR #158,` with:

```markdown
**Status (re-verified 2026-07-05, P1.4):** NO fix on master. PR #158
(`flex_fill_reconcile.py`, branch `fix/executed-orders-intraday-fills`) is still OPEN and
absent from HEAD — there is no Flex external-fill backfill in production. The invisibility
was re-confirmed on the PAPER Gateway (BUY 1 AAPL in TWS never reached `order_fills` via
the poller's cached `ib.fills()` path; whether an ACTIVE `reqExecutions` sees it is
recorded as Answer B in `docs/fable/measurements-2026-07-05.md`). The
`ib_activity_mirror.py` docstring was corrected to state the scope limit. Follow-up (Master
Client ID route OR merge the Flex overlay) is deferred, dedup by `perm_id` NOT `exec_id`.
```

### Step 8 — Cleanup (MANDATORY, exact order)

8.1 **Flatten the paper position** created by the probes. In TWS paper (or via Xenon web UI
at :3200), SELL the AAPL shares you bought so paper net position returns to flat:

- Step 3 bought 1 (external) + Step 5 bought 1 (control) = SELL **2** AAPL MKT (or SELL the
  exact number you actually filled — check TWS positions first). Confirm position = 0.
  8.2 Stop the dev stack (Ctrl-C in the `dev.sh paper` terminal).
  8.3 Leave `output/op10-external-probe-2026-07-05.json` in place (gitignored artifact); the
  measurements doc and code/docstring/memory edits are the durable record.

---

## Verification matrix

| Check                                 | Exact command                                                                 | Expected outcome                                                                                                                                                                                                                                            |
| ------------------------------------- | ----------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Probe guard refuses live port         | `uv run python scripts/research/probe_external_fills.py --port 4001`          | stderr `REFUSED: 127.0.0.1:4001 …`; **exit code 2**                                                                                                                                                                                                         |
| Probe guard refuses remote host       | `uv run python scripts/research/probe_external_fills.py --host 100.66.147.98` | stderr `REFUSED: 100.66.147.98:4002 …`; **exit code 2**                                                                                                                                                                                                     |
| RTH gate                              | `TZ=America/New_York date +"%A %H:%M"` before Step 3                          | Mon–Fri, 09:30–15:45 ET; otherwise deferred (T7)                                                                                                                                                                                                            |
| Probe runs on paper                   | `uv run python scripts/research/probe_external_fills.py`                      | exit 0; valid JSON with keys `cached_fills_count`, `reqExecutions_count`, `distinct_clientIds_in_reqExecutions`, `errors`                                                                                                                                   |
| Stack health                          | `curl -s http://localhost:8421/health`                                        | JSON `ib_gateway.port_listening: true`                                                                                                                                                                                                                      |
| Baseline empty                        | Step 2.4 psql                                                                 | `count = 0` (or recorded baseline)                                                                                                                                                                                                                          |
| Control visible                       | Step 5 psql                                                                   | ≥1 AAPL row **with non-null `submission_id`**                                                                                                                                                                                                               |
| Answer A recorded (decides OP-10)     | Step 4.1 psql                                                                 | external row present ⇒ VISIBLE / absent after ≥2 ticks ⇒ INVISIBLE — recorded either way                                                                                                                                                                    |
| Answer B recorded (remediation input) | Step 4.2 probe JSON                                                           | AAPL exec with foreign `clientId` present/absent — recorded either way; B alone NEVER flips the OP-10 verdict                                                                                                                                               |
| Artifact saved                        | `ls output/op10-external-probe-2026-07-05.json`                               | file exists                                                                                                                                                                                                                                                 |
| Outcome recorded                      | Step 7a memory edit **or** Step 7b docstring+measurements+memory              | matching branch produced                                                                                                                                                                                                                                    |
| No live use                           | grep transcript                                                               | no `4001` / `100.66.147.98` in any executed probe/psql                                                                                                                                                                                                      |
| Position flat                         | TWS paper positions after Step 8.1                                            | AAPL net = 0                                                                                                                                                                                                                                                |
| No prod-logic change                  | `git diff --name-only master`                                                 | only: `scripts/research/probe_external_fills.py`, `docs/fable/measurements-2026-07-05.md` (7b), `src/xenon/api/services/ib_activity_mirror.py` (7b docstring only), memory file (outside repo), plan file. NOTHING in `src/xenon/execution/` or `server.py` |

**Not applicable** (this is a read-only experiment): web Vitest, tsc/lint, Playwright/E2E,
order-path CI guards (no order-path source changed), Alembic (no schema change). If Step 7b's
docstring edit is the only `src/` change, the affected-pytest sweep is a courtesy check:

```bash
uv run python scripts/infra/dev/run_pytest_affected.py   # EXPECT: no new failures
```

---

## Tripwires / abort criteria (STOP and report)

- **T1 — Live guard:** if ANY probe or psql resolves to host `100.66.147.98` or port `4001`,
  STOP immediately. This experiment is PAPER-only.
- **T2 — Broken control:** if the Step 5 Xenon-placed control fill does NOT appear in
  `order_fills` within 2 ticks, STOP — the poller is broken for an unrelated reason and the
  external result is uninterpretable. Report the poller failure instead of concluding OP-10.
- **T3 — Poller not running:** if `/health` shows the sync pool role disconnected or the boot
  log shows "ib activity poller skipped/disabled", fix that before Step 3; do not interpret a
  missing row as INVISIBLE.
- **T4 — Scope mismatch:** if the psql queries return rows under `account_env='live'`, STOP —
  you are querying the wrong DB/scope (must be LOCAL `DATABASE_URL_PAPER`, `account_env='paper'`).
- **T5 — Fills already present pre-treatment:** if Step 2.4 baseline is non-zero for today's
  AAPL, use a different liquid single-name (e.g. `MSFT`) so the treatment fill is unambiguous;
  update all `ticker='AAPL'` filters accordingly.
- **T6 — More than the listed files change:** if you find yourself editing anything under
  `src/xenon/execution/` or `server.py`, STOP — that is the follow-up fix, out of scope here.
- **T7 — Outside RTH:** if `TZ=America/New_York date +"%A %H:%M"` is not Mon–Fri
  09:30–15:45 ET at Step 3 time, STOP and defer the whole run. Do not place after-hours
  orders or improvise order types — an unfilled MKT/limit order makes every observation
  uninterpretable.

---

## Rollback

Pure experiment; nothing to migrate.

```bash
git checkout master
git branch -D experiment/op10-external-fill-probe   # discards probe script + doc/docstring edits
```

The memory-file edit lives outside the repo; if the outcome branch was wrong, re-run Steps
4–6 and re-apply the correct 7a/7b text. No DB writes to undo beyond the paper fills, which
Step 8.1 already flattens.

---

## Incident-history row

Not an order-path _fix_ — no row appended to `docs/reference/order-path-incident-history.md`.
(If Step 7b lands, the measurements doc + corrected docstring are the durable record; a future
order-path fix that acts on this finding should add the incident row at that time.)
