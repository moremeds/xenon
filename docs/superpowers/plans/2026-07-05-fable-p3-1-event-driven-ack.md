# P3.1 — Event-driven ack: verify S2 delivered it, measure the latency win (OP-11)

- **Date:** 2026-07-05
- **Type:** mostly a VERIFICATION + MEASUREMENT plan. The implementation of the event-driven
  ack ships inside S2 (`2026-07-05-fable-s2-uncertain-orderref.md`, CLI ack loop replacing the
  blind 2s/5s sleep). P3.1 exists in the roadmap as "if not already done in S2" — it IS done
  in S2, so this plan verifies the outcome and closes the roadmap item.
- **Prereqs (hard):** S2 merged; P1.1 merged (stage-timing logs — the measurement instrument);
  a P1 baseline latency number recorded in `docs/fable/measurements-*.md` from BEFORE S2
  merged (if no pre-S2 baseline exists, record that honestly — the comparison is then against
  the code constants: old fixed sleep 2s/5s vs new event-driven exit).
- **Acceptance (roadmap):** p50 place latency drops ≥2s vs baseline for non-combo orders that
  IB accepts promptly (the old path always paid the full 2s sleep; the new path exits on
  `Submitted`/`PreSubmitted`).

## Re-verify preamble (MANDATORY — this plan is written ahead of its prereqs)

**At the HEAD this plan was authored against (post-`4d864294`), BOTH prereqs are ABSENT:**
`ib_place_order.py` still uses the fixed `client.sleep(wait_secs)` (2s/5s) and
`_orders_place_from_body` has no stage-timing calls. That is expected — this plan is
sequenced AFTER S2 and P1.1 in the canonical merge order and must not be executed before
they merge.

Before executing, confirm at HEAD: (1) `grep -n "ACK_WAIT_S\|stage.*ack" src/xenon/execution/ib_place_order.py`
shows S2's ack loop (the fixed `client.sleep(wait_secs)` is GONE); (2) `grep -n "_order_stage(" src/xenon/api/server.py`
shows P1.1's stage lines. If either grep fails → STOP, prereqs unmet — do not attempt any
step below, including Step 3's extraction (its stage names come from P1.1's implementation;
read P1.1's plan for the authoritative stage-name list and substitute if they differ from
the `subprocess_spawned`/`persisted` names assumed here).

## Steps

1. **Paper stack**, during RTH, with output captured to a file (the API logs to stdout —
   `server.py`'s logging config has no file handler, so `logs/*.log` will NOT contain the
   stage lines unless you capture them):
   ```bash
   scripts/infra/dev.sh paper 2>&1 | tee logs/p3-1-ack-measure-$(date +%Y%m%d).log
   ```
   IB port 4002 — never live.
2. Place 10 non-combo far-from-market limit orders (BUY 1 AAPL at ~2% below bid — will not
   fill) via the UI, ~30s apart; cancel each after placement. These get a prompt IB ack.
3. Extract per-order stage timings:
   ```bash
   grep 'order_stage ' logs/p3-1-ack-measure-*.log | sed 's/^.*order_stage //' \
     | jq -c 'select(.op=="place") | {cid: .client_attempt_id, stage, elapsed_ms}'
   ```
   For each order, `persisted.elapsed_ms - subprocess_spawned.elapsed_ms` ≈ subprocess wall
   time (dominated by connect + qualify + ack wait).
4. Record in `docs/fable/measurements-<date>.md`: p50/p95 of the subprocess wall time; the
   pre-S2 expectation for the same path was that number + ~2000ms fixed sleep. Verdict line:
   "P3.1 acceptance met/not met: p50 delta = <n> ms."
5. If NOT met (ack loop runs to its full deadline because `orderStatus` never reaches
   `Submitted` promptly on paper): investigate which status paper returns
   (`PreSubmitted` vs `PendingSubmit`) and, only if justified, extend S2's early-exit status
   set — that is a one-line change with its own mini-PR + the S2 test updates. Do NOT shrink
   `ACK_WAIT_S` (that re-opens the OP-1 reject-window regression S2's review closed).

## Verification matrix

| Check                                                    | Expected                                           |
| -------------------------------------------------------- | -------------------------------------------------- |
| Prereq greps (above)                                     | both non-empty                                     |
| 10 orders placed AND cancelled (psql, exact query below) | returns `0` after cleanup                          |
| measurements file updated                                | p50/p95 + verdict line present                     |
| no code changes (unless step 5 fires)                    | `git status` clean apart from the measurements doc |

Cleanup query (order rows are scoped — a bare count would match other sessions' rows).
Collect the 10 `client_attempt_id`s from the jq output in Step 3, then:

```bash
psql "$DATABASE_URL_PAPER" -tA -c "
  SELECT count(*) FROM xenon.order_submissions
  WHERE broker='IB' AND account_env='paper'
    AND state IN ('WORKING','PENDING','PARTIALLY_FILLED')
    AND client_attempt_id IN ('<cid1>','<cid2>', ... '<cid10>');"
```

Expected `0`. If nonzero → a test order is still resting: cancel it from the UI before
finishing (tripwire: never leave a resting paper order).

Paper-only; no schema; no web changes.
