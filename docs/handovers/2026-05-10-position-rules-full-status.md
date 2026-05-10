# Position Rules Full Status

Date: 2026-05-10 HKT  
Branch: `feature/position-rules-implementation`  
Worktree: `/Users/chenxi/projects/xenon/.worktrees/position-rules-implementation`  
PR: https://github.com/moremeds/xenon/pull/100  
Latest pushed commit checked locally: `c712c22f`

## Executive Status

The implementation is built, pushed, and documented. The formal paper smoke is not complete.

Current strict paper-smoke count: **8/11 complete**.

The remaining blocker is not code execution or order placement. It is IBKR paper market data:

```text
IB error 10197: No market data during competing live session
```

This blocks mark-driven smoke checks because paper `reqMktData` / `reqTickers` returns `nan` for quote fields.

Do not mark the active goal complete until S2/S5/S8 are verified or explicitly operator-deferred in the smoke sign-off.

## Git / PR State

Local check at handover time:

```text
## feature/position-rules-implementation...origin/feature/position-rules-implementation
c712c22f
```

The branch was clean and aligned with origin when this status file was written.

Latest committed handover artifact:

```text
c712c22f docs(position-rules): add smoke handover script
```

## Current PR Checks

After the docs-only handover commit, CI reran. Current observed state:

```text
version-sync      pass     7s
web-lint          pass     50s
web-tests         pass     2m31s
web-typecheck     pass     47s
order-path-guards pass     11s
python-tests      pending
```

Earlier, before the docs-only handover push, all PR checks were green, including `python-tests` at 26m42s. The current pending Python job is from the docs-only rerun.

## Smoke Checklist Status

Completed:

- **S1** stock SL+TP arming.
- **S3** manual TWS cancel detection.
- **S4** sweep CLI re-arm.
- **S6** daemon kill + restart reconcile.
- **S7** native + synthetic race, accepted integration-test fallback.
- **S9** subprocess timeout after broker accept, accepted integration-test fallback.
- **S10** out-of-band sweep.
- **S11** UI.

Incomplete:

- **S2** trailing TP MFE update.
- **S5** credit spread dual-trigger.
- **S8** two rules same position.

## Smoke Blocker Details

The evidence file records:

- `docs/runbooks/position-rules-smoke-evidence-2026-05-07.md` says strict count is **8/11 complete**.
- S2 is not complete.
- S5 and S8 are partial.
- Latest recorded blocker: IB 10197 `No market data during competing live session`.

Fresh retry captured in the evidence:

```text
Direct IB probes for USO, GM, SPY 750C, SPY 720P, and SPY 715P returned nan
for bid/ask/last/close/marketPrice across market data types 1, 2, 3, and 4.
A second reqTickers probe returned IB error 10197.
```

Interpretation:

- Paper Gateway can place orders.
- Paper Gateway can read positions.
- Paper Gateway cannot receive quote marks while a competing live IBKR market-data session is active.
- Further paper order placement will not complete S2/S5/S8 until the quote gate clears.

## Remaining Smoke Items

### S2 - trailing TP MFE update

Existing row:

```text
protection_id=34
position_key=STK::USO
rule_kind=trailing_tp
scope=IB / paper / DUQ378889
```

Pass criteria:

- Real paper mark is available.
- `state_data.mfe` appears or increases when the mark exceeds prior MFE.
- Rule remains `ARMED`; no premature trigger.

### S5 - credit spread dual-trigger

Existing rows:

```text
position_key=CS::SPY::20260508::720::715::P
protection_id=37 stop_loss
protection_id=38 take_profit_fixed
```

Pass criteria:

- Stop-loss path triggers when underlying breaches short strike, or equivalent allowed evidence is captured.
- Fixed take-profit path triggers when debit-to-close drops to at most 50% of credit, or equivalent allowed evidence is captured.
- Requires real marks for spread legs and underlying.

### S8 - two rules same position

Existing rows:

```text
position_key=OPT::SPY::20260508::750::C
protection_id=35 stop_loss
protection_id=36 trailing_tp
```

Pass criteria:

- One MKT close reaches IB.
- One rule reaches `CLOSED`.
- The other rule reaches `SUPERSEDED`.
- `position_close_claims` records one winning claim.
- Requires real marks or a permitted deterministic fallback.

## First Commands For Next Agent

Check branch:

```bash
cd /Users/chenxi/projects/xenon/.worktrees/position-rules-implementation
git status --short --branch
```

Check PR:

```bash
gh pr checks 100 --repo moremeds/xenon
```

Check paper health:

```bash
DATABASE_URL=postgresql+psycopg://xenon_app:xenon_dev@127.0.0.1:5432/core_dev \
XENON_TRADING_MODE=paper \
XENON_BROKER_ACCOUNT=DUQ378889 \
XENON_BROKER=IB \
uv run xenon-position-rules health --json
```

Check whether the quote blocker cleared:

```bash
uv run python -c 'from ib_async import IB, Contract; ib=IB(); ib.connect("127.0.0.1",4002,clientId=9220,timeout=10); c=Contract(conId=418893644,secType="STK",symbol="USO",exchange="SMART",currency="USD"); ib.reqMarketDataType(1); ticks=ib.reqTickers(c); print([{"bid":t.bid,"ask":t.ask,"last":t.last,"close":t.close,"marketPrice":t.marketPrice(),"time":str(t.time)} for t in ticks]); ib.disconnect()'
```

If this returns `10197` or all quote fields are `nan`, stop and ask the operator to clear the competing live market-data session.

## Important Audit Rule

Keep the database trail of orders for audit.

Do not delete or manually scrub:

- `xenon.order_submissions`
- `xenon.order_fills`
- `xenon.order_events`
- `xenon.position_protection`
- `xenon.position_close_claims`
- `events.outbox`

If cleanup is needed, use operator cancel paths and preserve audit rows.

## Key Evidence Files

- `docs/runbooks/position-rules-smoke-evidence-2026-05-07.md`
- `docs/handovers/2026-05-07-position-rules-smoke-handover.md`
- `docs/handovers/2026-05-10-position-rules-smoke-handover-script.md`
- `docs/runbooks/position-rules-paper-smoke.md`
- `docs/runbooks/position-rules-acceptance-gate.md`

## Current Verdict

Implementation/PR side: good, with current docs-only rerun waiting on `python-tests`.

Paper smoke side: **not signed off**.

Goal status: **not complete** until S2/S5/S8 are completed or explicitly operator-deferred.

