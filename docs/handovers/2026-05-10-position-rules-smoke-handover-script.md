# Position Rules Paper Smoke Handover Script

Use this prompt with the next coding agent.

---

You are working in:

```bash
/Users/chenxi/projects/xenon/.worktrees/position-rules-implementation
```

Branch:

```bash
feature/position-rules-implementation
```

PR:

```bash
https://github.com/moremeds/xenon/pull/100
```

Current status as of 2026-05-10 HKT:

- Implementation is pushed.
- Worktree was clean and aligned with origin when this handover was written.
- PR CI was fully green after commit `cf873ab9`.
- Formal paper smoke is **not signed off**.
- Strict paper-smoke count is **8/11 complete**.

Completed smoke items:

- S1: stock SL+TP arming.
- S3: manual TWS cancel detection.
- S4: sweep CLI re-arm.
- S6: daemon kill + restart reconcile.
- S7: native + synthetic race, accepted integration-test fallback.
- S9: subprocess timeout after broker accept, accepted integration-test fallback.
- S10: out-of-band sweep.
- S11: UI.

Incomplete smoke items:

- S2: trailing TP MFE update. Needs real paper marks so `state_data.mfe` moves and no premature trigger occurs.
- S5: credit spread dual-trigger. Spread filled and rules armed, but underlying-breach and debit-to-close trigger checks need marks.
- S8: two rules same position. Long option filled and rules armed, but same-tick trigger race needs marks.

Current hard blocker:

```text
IB error 10197: No market data during competing live session
```

This means paper Gateway can place orders and read positions, but paper `reqMktData` / `reqTickers` returns `nan` because another live IBKR session is consuming the live market-data entitlement. Do not place more paper orders until this quote gate clears; more fills will not verify S2/S5/S8 without marks.

First thing to do:

1. Confirm the worktree and PR state.

```bash
git status --short --branch
gh pr checks 100 --repo moremeds/xenon
```

2. Confirm paper daemon health.

```bash
DATABASE_URL=postgresql+psycopg://xenon_app:xenon_dev@127.0.0.1:5432/core_dev \
XENON_TRADING_MODE=paper \
XENON_BROKER_ACCOUNT=DUQ378889 \
XENON_BROKER=IB \
uv run xenon-position-rules health --json
```

3. Before doing anything else, retry the IB quote probe. If this still returns error 10197 or all `nan`, stop and ask the operator to clear the competing live market-data session.

```bash
uv run python -c 'from ib_async import IB, Contract; ib=IB(); ib.connect("127.0.0.1",4002,clientId=9220,timeout=10); c=Contract(conId=418893644,secType="STK",symbol="USO",exchange="SMART",currency="USD"); ib.reqMarketDataType(1); ticks=ib.reqTickers(c); print([{"bid":t.bid,"ask":t.ask,"last":t.last,"close":t.close,"marketPrice":t.marketPrice(),"time":str(t.time)} for t in ticks]); ib.disconnect()'
```

Expected good result:

- No `10197` error.
- At least one finite positive mark field: bid/ask/last/close/marketPrice.

Expected blocked result:

- `Error 10197, reqId ... No market data during competing live session`
- bid/ask/last/close/marketPrice all `nan`

If quote gate is still blocked:

- Do not mark the goal complete.
- Do not mark paper smoke signed off.
- Do not place new orders just to create more evidence.
- Tell the operator to log out of live TWS/Gateway/Client Portal/mobile or use paper with independent market-data entitlement, then retry the probe.

If quote gate clears, continue in this order:

## S2 - trailing TP MFE update

Use existing S1 replacement row:

- `protection_id=34`
- `position_key=STK::USO`
- `rule_kind=trailing_tp`
- account scope: `IB / paper / DUQ378889`

Check current row:

```bash
psql postgresql://xenon_app:xenon_dev@127.0.0.1:5432/core_dev -x -c \
  "select protection_id, position_key, rule_kind, state, state_data, updated_at
   from xenon.position_protection
   where protection_id=34;"
```

Let the daemon tick after marks are available, then query again. To pass S2, evidence must show:

- `state_data.mfe` appears or increases when mark exceeds prior MFE.
- Rule remains `ARMED`; no premature trigger.

## S5 - credit spread dual-trigger

Existing spread:

- `position_key=CS::SPY::20260508::720::715::P`
- `protection_id=37` stop_loss
- `protection_id=38` take_profit_fixed

Check rows:

```bash
psql postgresql://xenon_app:xenon_dev@127.0.0.1:5432/core_dev -x -c \
  "select protection_id, position_key, asset_class, rule_kind, state, config, state_data, position_descriptor, updated_at
   from xenon.position_protection
   where protection_id in (37,38)
   order by protection_id;"
```

To pass S5, evidence must show either or both configured trigger paths working with real marks:

- stop-loss trigger when underlying breaches the short strike.
- fixed take-profit trigger when debit-to-close drops to at most 50% of credit.

If market conditions do not naturally hit the thresholds, do not fake DB state. Use the existing unit/integration tests as fallback only if the runbook explicitly permits it; otherwise record that market conditions did not exercise the trigger.

## S8 - two rules same position

Existing long option:

- `position_key=OPT::SPY::20260508::750::C`
- `protection_id=35` stop_loss
- `protection_id=36` trailing_tp

Check rows:

```bash
psql postgresql://xenon_app:xenon_dev@127.0.0.1:5432/core_dev -x -c \
  "select protection_id, position_key, asset_class, rule_kind, state, native_order_perm_id, config, state_data, position_descriptor, updated_at
   from xenon.position_protection
   where protection_id in (35,36)
   order by protection_id;"
```

To pass S8, evidence must show:

- One MKT close reaches IB.
- One rule reaches `CLOSED`.
- The other rule reaches `SUPERSEDED`.
- `position_close_claims` has one winning claim for the position.

Check close claims:

```bash
psql postgresql://xenon_app:xenon_dev@127.0.0.1:5432/core_dev -x -c \
  "select claim_id, position_key, claimed_by_protection_id, status, broker_order_ref, broker_perm_id, attempts, created_at, updated_at
   from xenon.position_close_claims
   where broker='IB' and account_env='paper' and broker_account='DUQ378889'
   order by claim_id desc
   limit 20;"
```

Important audit rule:

- Keep all DB trails. Do not delete order, fill, protection, claim, or outbox rows for cleanup.
- If cleanup is needed, use operator cancel paths and preserve audit rows.

Evidence files to update:

```text
docs/runbooks/position-rules-smoke-evidence-2026-05-07.md
docs/handovers/2026-05-07-position-rules-smoke-handover.md
docs/handovers/2026-05-10-position-rules-smoke-handover-script.md
```

Useful existing evidence:

- `docs/runbooks/position-rules-smoke-evidence-2026-05-07.md`
- `docs/handovers/2026-05-07-position-rules-smoke-handover.md`
- `docs/runbooks/position-rules-paper-smoke.md`
- `docs/runbooks/position-rules-acceptance-gate.md`

Do not call the active goal complete until:

- S2 is verified.
- S5 is verified or explicitly operator-deferred.
- S8 is verified or explicitly operator-deferred.
- Paper smoke evidence docs are updated.
- PR checks remain green.

