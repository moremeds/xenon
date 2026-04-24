# PR-34 Follow-up — Issues A/B/C/D

_Context dossier for a separate future PR. Nothing in here is fixed on `feat/position-order-quote-token`. This file is exhaustive so a fresh session (or human) can pick it up cold._

Date captured: 2026-04-24.

---

## Current branch state

**Branch:** `feat/position-order-quote-token` · **Base:** `master` · **PR:** #34

Local tip commits (oldest → newest):

```
ecf63909  fix(execution): band direction by sign(exec_net), not envelope_action
dea6b8cb  feat(api): emit QUOTE_CHECK_FAIL telemetry on combo check_combo failure
3359e57b  feat(web): expose reload() on useQuoteTokens + Retry button in modal
02b6986c  fix(web): accept camelCase conId in /api/orders/place, alias to con_id
a1dbf831  fix(execution): envelope-aware combo band math + credit-structure tests
577feba9  fix(orders): combo envelope is intent-only; hard-reject missing tokens
8c0c8806  fix(orders): revert combo-token hard-reject; other entry paths not wired
b69bf002  fix(orders): mint tokens at submit time; wire OrderTab combo; relax conId   ← HEAD at capture
```

Working-tree only (not committed, not tracked):

- `.env` has `XENON_QUOTE_TOKEN_SECRET=...` appended. Gitignored. Rotate when markets are closed — the value was printed in a session log.

Account status at capture: IB Gateway connected to **real money**, not paper. One duplicate SPX combo from the modify attempt had already been cancelled manually. No open orders at capture time.

---

## ISSUE A — `/orders/quote` throws `RuntimeError: no event loop in thread`

### Symptom

```
File "src/xenon/api/server.py", line 1481, in orders_quote
    snap = await asyncio.to_thread(_fetch_quote_snapshot, ticker, con_id)
File "src/xenon/clients/ib_client.py", line 768, in qualify_contract
    results = self._ib.qualifyContracts(contract)
...
File "ib_insync/util.py", line 464, in getLoop
    return asyncio.get_event_loop_policy().get_event_loop()
RuntimeError: There is no current event loop in thread 'ThreadPoolExecutor-0_1'.
```

Next.js surfaces as HTTP 500. PositionOrderModal shows "Quote unavailable: Error: quote 500 for <conId> Retry". Blocks all non-combo order placement because `/orders/place` hard-requires `quote_token` for stock/single-leg.

Intermittent — some worker threads inherit an event loop reference from a prior request, so ~50% of quote mints succeed.

### Root cause

`src/xenon/api/server.py:1481` wraps `_fetch_quote_snapshot` in `asyncio.to_thread()`, running it on a thread-pool worker.

Inside, `client.qualify_contract` / `client.get_quote` call `ib_insync`'s synchronous wrappers (`qualifyContracts`, `reqMktData`). Those wrappers do `self._run(awaitable)` → `util.run(awaitable)` → `asyncio.get_event_loop_policy().get_event_loop()`.

In Python 3.13 inside a worker thread with no event loop attached, `get_event_loop()` raises. `ib_insync` must be called from the main asyncio thread or from a dedicated thread that has `util.patchAsyncio()` / `util.startLoop()` applied.

### Files involved

- `src/xenon/api/server.py:1436-1501` — `_fetch_quote_snapshot` and `orders_quote` handler
- `src/xenon/clients/ib_client.py:761-773` — `qualify_contract` (uses sync `qualifyContracts`)
- `src/xenon/clients/ib_client.py:696-714` — `get_quote` (uses sync `reqMktData` + `ib.sleep(2)`)

### Reference: what other endpoints do

- `src/xenon/scanners/cri.py:135` — `await ib.qualifyContractsAsync(contract)` on the main loop
- `src/xenon/scanners/cri.py:345-346` — sync `ib.reqMktData(contract, "", True, False)` + `ib.sleep(2)` (but this is a standalone CLI scanner, NOT a FastAPI handler, so the sync path is fine in that context)

### Attempted fixes (both reverted)

**Attempt 1 (reverted commit `07ec88df`):** rewrote `_fetch_quote_snapshot` to `async def` + called `ib.qualifyContractsAsync` + `ib.reqTickersAsync`. **Hung for 30 s on SPX.** `reqTickersAsync` waits for a snapshot-completion marker IB does not emit for index options (completion depends on tick types like `volume` / `historical vol` that SPX options don't stream).

**Attempt 2 (reverted commit `104948ae`):** kept `async def` but replaced `reqTickersAsync` with fire-and-forget `reqMktData` + `asyncio.sleep(1.5)` + `cancelMktData` in a `finally`. Not empirically tested on user's machine before the revert.

### Correct fix candidates (untried, ordered by risk)

1. **Stay async, use `reqMktData` fire-and-forget + cooperative wait + `cancelMktData`.** This is attempt #2. Mirrors scanners/cri.py pattern but cooperative. Needs paper-account verification against an SPX index option specifically.
2. **Keep `asyncio.to_thread` but attach an event loop to the worker.** Call `ib_insync.util.patchAsyncio()` at subprocess startup; at the top of `_fetch_quote_snapshot` do `asyncio.set_event_loop(asyncio.new_event_loop())` if there is no running loop. Keeps the blocking semantics but requires per-worker loop setup.
3. **Add `get_quote_async` to `IBClient`** that uses `qualifyContractsAsync` + `reqMktData` + `asyncio.sleep`. Keeps `_fetch_quote_snapshot` simple and pushes complexity into the client.

Whichever path is chosen: **must be tested with an SPX index-option conId** (user's actual failure), not SPY/AAPL equity options. The 30-s hang only manifested for index options.

### Verification command

```bash
curl -sS "http://localhost:8321/orders/quote?ticker=SPX&con_id=872609959" | head -c 300
```

Expected: `{"token":"...","bid":"...","ask":"...","bid_size":N,"ask_size":N,"ts_server_ms":...}` in < 2 s. No 500, no 30-s hang.

### Test to add

`scripts/tests/test_quote_route.py` already has `test_quote_route_returns_signed_token` with a sync `fake_fetch`. Add a second test that asserts `/orders/quote` succeeds in under 2 seconds. Mock IB with a realistic SPX-option contract that does NOT emit snapshot-complete; verify the handler returns within `asyncio.sleep(1.5) + epsilon`.

---

## ISSUE B — Modify a combo order produces a duplicate instead of modifying in place

### Symptom

After clicking Modify on a single SPX combo (qty 12 → 16, price $2 → $3):

```
| SPX Combo | SELL | LMT | 12 | $2.00 | ↓$1.40 | Submitted    | GTC | [MODIFY] [CANCEL] |  ← original, untouched
| SPX Combo | SELL | LMT | 16 | $3.00 | ↓$1.40 | PreSubmitted | GTC | [MODIFY] [CANCEL] |  ← duplicate with new orderId
```

API response: **400** `IB_REJECT` `"Modify not confirmed by refreshed IB open orders"`.

### DB evidence

`web/data/orders.duckdb` `orders_events`:

```
2026-04-22 15:07:03  MODIFY  sub=snapshot-153  IB_REJECT "Modify not confirmed by refreshed IB open orders"  http_status=400
2026-04-24 00:26:00  MODIFY  sub=snapshot-777  IB_REJECT "Modify not confirmed by refreshed IB open orders"  http_status=400
2026-04-24 00:44:18  CANCEL  sub=snapshot-777  "Order cancelled (orderId=-4)"                                 http_status=200  # manual cancel of duplicate
```

### Code path traced

1. **Client** `web/components/ModifyOrderModal.tsx:358-386` — `submitModify()`. For `isComboOrder` with legs unchanged takes the atomic `{newPrice, newQuantity}` path.
2. **Next route** `web/app/api/orders/modify/route.ts:220-265` — forwards to FastAPI `/orders/modify`.
3. **FastAPI** `src/xenon/api/server.py:1938-2095` — `orders_modify` runs preflight + subprocess via `_run_ib_script_with_recovery("xenon-ib-order-manage", ["modify", ...])`.
4. **Subprocess** `src/xenon/execution/ib_order_manage.py:280-447` — `modify_order`:
   - `find_trade` via `client.get_open_orders()` → `reqAllOpenOrders()`
   - `_reconnect_as_owner` if current clientId ≠ original placer
   - `find_trade` again after reconnect
   - Mutates `trade.order.lmtPrice` / `totalQuantity` / `smartComboRoutingParams`
   - `client.place_order(trade.contract, trade.order)` → `self._ib.placeOrder(contract, order)`
   - Polls `modify_is_confirmed` on the original orderId; times out 5 s → "not confirmed" error

### Leading hypothesis (unverified)

`reqAllOpenOrders()` returns a read-only snapshot that is NOT bound to the current client session. Per IBKR docs (user-cited):

> `reqOpenOrders()` can bind eligible orders for the session, while `reqAllOpenOrders()` does not bind them.

Without binding, `ib.placeOrder(contract, order)` with a previously-placed orderId is interpreted by TWS as a new order — IB allocates a fresh orderId; original stays alive; polling never sees the attempted modify reflected on the original. Matches every observed symptom.

One prior hypothesis was wrong ("IB doesn't support BAG in-place modify" — IBKR docs are unambiguous that placeOrder-with-same-orderId modifies in place for price/size/TIF on any type including BAG). So the binding theory is the next best theory, not proven.

### Alternative theories to test

1. **Binding issue** (above) — fix = `client.ib.reqOpenOrders()` + `sleep(0.5)` after `_reconnect_as_owner`, before `place_order`. Was attempted in reverted commit `56af6995`.
2. **`smartComboRoutingParams` rebinding** — `ib_order_manage.py:374-377` always resets `NonGuaranteed=1` on BAG. If IB interprets this as a material change, it may allocate a new order. Test by skipping that block on BAG modify.
3. **`volatility` sentinel reset** — `ib_order_manage.py:367-369` resets `volatility` and `volatilityType` to IB sentinels on every modify. Possibly redundant for a BAG that was never VOL to begin with.
4. **`client.place_order` wrapper behavior** — `src/xenon/clients/ib_client.py:548-567`. Passes through to `self._ib.placeOrder(contract, order)`. Prior audit said `ib_insync` honors `order.orderId` if set, but worth re-confirming against the installed version.
5. **NonGuaranteed + reqOpenOrders interaction** — IBKR has subtle BAG modify edge cases around `NonGuaranteed` and smart routing. May need a BAG-specific modify path.

### Reference code (pasted by user in session)

> **repricing: use placeOrder() again with the same orderId**
> **best way in ib_async: reuse trade.order and trade.contract**
> **do not create a fresh order unless you intentionally want a new live order**
> **cancel + recreate when changing combo structure, not just price.**

```python
def reprice_trade_in_place(trade, new_limit: float):
    if trade.orderStatus.status in {'Filled','Cancelled','Inactive','ApiCancelled'}:
        raise RuntimeError(...)
    trade.order.lmtPrice = new_limit                # reuse same order obj with live orderId
    modified_trade = ib.placeOrder(trade.contract, trade.order)  # reuse same combo contract
    ib.sleep(0)
    return modified_trade
```

This is essentially what `modify_order` does. So either the pattern is right and the bug is a subtle wrapper interaction (binding, NonGuaranteed, volatility sentinel, clientId reconnect edge case) — or the pattern is version-sensitive and our `ib_insync` version has a regression. Check `requirements.txt` for the pinned version.

### How to confirm root cause

**Paper account only, NOT real money.**

1. Switch IB Gateway to paper.
2. Place a bull call spread (SPY, cheap, 2-leg, 1 contract) directly via the UI. Record `orderId` and `permId`.
3. Click Modify, change **price only** (no qty, no legs). Submit.
4. Observe in TWS + in `/open-orders`:
   - If `orderId` stays the same and price updates → atomic modify works; today's bug was environmental.
   - If a new orderId appears alongside the original → binding or BAG modify limitation is real. Then:
     a. Temporarily add `client.ib.reqOpenOrders(); client.ib.sleep(0.5)` before `place_order` in `modify_order`, repeat. If this fixes it → root cause is binding.
     b. Temporarily skip the `smartComboRoutingParams` + `volatility` reset block for BAG modifies, repeat. If this fixes it → root cause is param-mutation-triggers-new-order.
5. Repeat for qty-only change and price+qty change.
6. Whatever works, write a test in `scripts/tests/test_ib_order_manage.py` that mocks the successful sequence, asserting strict call ordering.

### Salvage-worthy from reverted commits

`scripts/tests/test_ib_order_manage.py` in reverted commit `56af6995` had two new tests with strict call-order assertions:

- `test_modify_binds_live_order_before_place_order` — asserts `reqOpenOrders` is called exactly once before `placeOrder`
- `test_modify_binds_after_reconnect_as_owner` — asserts sequence `connect:<originalClientId>` → `reqOpenOrders` → `placeOrder`

If the binding theory is confirmed on paper, salvage these via `git show 56af6995`.

### Architectural consideration

Regardless of root cause, the UX around combo modify on a live-money account needs redesign:

- "Modify" button on a combo hides a two-step sequence from the user when the in-place atomic path fails.
- Cancel+place data-loss window (between cancel-ack and place-ack) has no safety net.
- On failure (today's 502 scenario), the UI doesn't attempt to re-place the original with its exact legs/price/qty; user has to reconstruct manually.

None of this blocks fixing Issue B. Mention it in the post-fix PR description for follow-up.

### Safety mode for next attempt

- Paper account only for diagnosis. Refuse to test on real.
- Resting limit far from market (e.g. $50 debit close on a $2.70 spread) so even if IB accepts the replacement, it parks in Pending and cancels on demand.
- Inspect in TWS + in `orders_events` after every test action. Don't rely only on the modal UI.

---

## ISSUE C — Naked-short guard likely triggers on combo replacement after cancel

Not confirmed; worth checking before closing out Issue B.

When the replace-path cancels a SHORT credit vertical and attempts to re-place it, the naked-short guard (`web/lib/nakedShortGuard.ts`) re-runs on the fresh payload. If the guard interprets a structurally-identical short-credit-spread as a "new naked short exposure" (because no position cache reflects the just-cancelled order yet), it returns 403. Route surfaces as part of the CRITICAL 502.

To verify: check `web/app/api/orders/place/route.ts:211-214` — if the guard runs for combo replacements of SHORT credit structures, fix is either:

- Skip the guard when called from the `/api/orders/modify` replace path (already-cancelled context known); or
- Pass a flag from the modify route that tells place to trust the preceding cancel.

---

## ISSUE D — Pre-existing issues from earlier tribunal review

From `/codex-review` runs earlier in the session (Codex P1 findings, branch-wide scope):

1. **500 ms quote-token TTL vs cached modal-open tokens** — `web/components/ticker-detail/useQuoteToken.ts:52-74`. Addressed for PositionOrderModal and OrderTab via `mintNow()` in commit `b69bf002`. Still unaddressed in OptionsChainTab and InstrumentDetailModal.
2. **Chain-based combo entry lacks `quote_tokens` and `conId`** — `OptionsChainTab.tsx:332` and `InstrumentDetailModal.tsx:159` submit `type: "combo"` without tokens or per-leg conId. Server soft-fails via `QUOTE_TOKEN_MISSING_SOFT`; no band check runs. Fix = plumb conId through the chain builder, mint tokens on submit, then flip server soft-fail to hard-reject.

Known gaps, already noted in the PR description.

---

## Useful commands / paths

```bash
# Current tip
git log --oneline master..HEAD | head -10

# Salvage the reverted binding fix if paper smoke supports the theory
git show 56af6995   # binding fix
git show 07ec88df   # quote-async (sync→async) attempt — starting point for Issue A fix #1
git show 104948ae   # reqMktData fire-and-forget variant for Issue A fix #1

# Orders DB
PYTHONPATH=src XENON_ORDERS_DB_PATH=./web/data/orders.duckdb python3.13 -c "
from xenon.execution import orders_store
con = orders_store._connect_utc(orders_store._resolve_path(None))
print([r for r in con.execute('SELECT \"at\", kind, submission_id, detail FROM orders_events ORDER BY \"at\" DESC LIMIT 15').fetchall()])
"

# Health
curl -sS http://localhost:8321/health | python3 -m json.tool
curl -sS http://localhost:8321/open-orders | python3 -m json.tool
curl -sS "http://localhost:8321/orders/quote?ticker=SPX&con_id=<real-con-id>" | head -c 300
```

## Key lessons for next session

- **Don't guess IB API semantics.** A prior hypothesis "IB doesn't support BAG in-place modify" was shipped without verifying against IBKR docs; the docs said the opposite. Verify every IB claim against IBKR docs + ib_insync source before shipping.
- **Don't test fixes on real money.** Every diagnostic step on this class of bug must be paper-first.
- **`reqTickersAsync` is SPX-hostile.** It expects a snapshot-complete marker that index options don't emit. Don't use it on generic option contracts without testing against an index option first.
- **The pytest `eventkit` DeprecationWarning about `asyncio.get_event_loop_policy()`** is the same class of bug live in `/orders/quote`. Ecosystem-wide pattern: ib_insync sync calls only work from a thread with an event loop. Either set one up or use async API end-to-end.
