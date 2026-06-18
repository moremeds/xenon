# Plan: `GET /market-depth` REST snapshot endpoint + read-only query-API verification

**Date:** 2026-06-18
**Branch:** `feat/query-api-market-depth` (based on `origin/master` @ v0.6.3)
**Design source:** `docs/superpowers/specs/2026-06-18-readonly-market-data-query-api-design.md`
(scope since reduced — see Context).

## Context — what is already done vs. what remains

`origin/master` (v0.6.3, PR #159 "expand query-key allowlist") **already exposes** the
read-only market-data surface under `XENON_QUERY_API_KEY` / `QUERY_API_KEY_PATHS`
(`src/xenon/api/auth.py`):

```
GET  /portfolio   GET /futu/portfolio  GET /attribution
GET  /orders      GET /orders/quote    GET /blotter   GET /journal
GET  /trades/entry-dates  GET /performance  GET /watchlist
GET  /options/chain       GET /options/expirations
POST /historical/bars     POST /historical/head-timestamp   POST /contract/qualify
POST /ws-ticket   (lets a query-key holder open the relay WS — incl. streaming L2 depth)
```

So `/options/chain`, `/options/expirations`, `/orders/quote`, and the historical POSTs are
**already reachable** — no code change needed for them. The **only missing** market-data
surface is a **point-in-time L2 market-depth snapshot over REST**. This plan adds it.

## Goal

1. Add `GET /market-depth` — a subprocess-backed L2 order-book snapshot, mirroring the
   `/options/chain` pattern (connect → `reqMktDepth` → settle → read book → cancel → JSON).
2. Expose it under the query key (`("GET", "/market-depth")`).
3. Verify the **entire** read-only query-API surface with **actual authenticated requests**.
4. Write a **100%-correct usage document** for external read-only consumers, validated
   against the captured live responses.

## Non-goals

- No streaming (relay WS already covers live depth for ws-ticket holders).
- No greeks/IV on `/options/chain` (stays an enumerator).
- No synthesized L1 fallback when L2 is not entitled — return the real permission status
  (`entitled: false` + `note`) and an empty book, not a faked top-of-book.
- No behavior change to the 4 already-exposed market-data endpoints.

## Key facts (verified against installed `.venv` / source)

- `ib_async.IB.reqMktDepth(contract, numRows, isSmartDepth)` → `Ticker` with
  `domBids`/`domAsks` = `list[DOMLevel(price, size, marketMaker)]`;
  `IB.cancelMktDepth(contract, isSmartDepth)` tears it down.
- Relay uses `DEPTH_NUM_ROWS = 10`, `isSmartDepth = !isFutures`
  (`scripts/infra/ib_realtime/ib_realtime_server.js:1119-1121`).
- Subprocess pattern (not the IB pool) avoids the FastAPI `ib_async` hang hazard
  (memory `ib_async_in_fastapi`) and the relay's 3-line depth budget. Same pattern as
  `ib_option_chain.py`.
- `IBClient.connect(..., client_id="auto")` allocates from range 20–49
  (`src/xenon/clients/ib_client.py:225`); per `api/CLAUDE.md` on-demand scripts MUST use
  `"auto"`.
- L2 entitlement is partial on the live account (memory `ib_depth_entitlement_partial`):
  many symbols legitimately return `entitled: false`. **That is expected, not a failure** —
  verification checks the endpoint _works_ (200 + a book, OR `entitled:true`+empty+`note`, OR
  `entitled:false`), not that any given symbol is entitled.
- Test templates to mirror: route → `scripts/tests/test_options_chain_route_port.py`;
  contract resolution → `scripts/tests/test_option_chain_underlying.py`; auth →
  `src/xenon/api/tests/test_query_api_key.py`.

---

## Step 1 — Depth CLI (TDD) — `src/xenon/execution/ib_market_depth.py`

**1a. Write the failing test first** — `scripts/tests/test_ib_market_depth.py`
(mirror `test_option_chain_underlying.py` stubbing style):

- Monkeypatch `xenon.execution.ib_market_depth.IBClient` so `.connect()` is a no-op and
  `._ib` is a fake exposing: `qualifyContracts` (sets `conId`), `reqMktDepth` (returns a
  fake `Ticker` with `domBids`/`domAsks` lists of objects with `.price/.size/.marketMaker`),
  `cancelMktDepth`, `sleep`, and an `errorEvent` with `+=`. `client.disconnect` no-op.
- Drive `main()` via `monkeypatch.setattr(sys, "argv", [...])`, capture stdout, parse JSON.
- Cases:
  - **underlying (stock)** — `--symbol AAPL` → `secType STK`, contract Stock/SMART.
  - **index** — `--symbol SPX` → `secType IND`, Index on home exchange (reuse
    `underlying_contract`).
  - **option** — `--symbol AAPL --expiry 20260618 --strike 200 --right C` → builds `Option`,
    `secType OPT`.
  - **partial option tuple rejected** — `--symbol AAPL --expiry 20260618 --strike 200` (no
    `--right`) → `{"error": "...all of --expiry/--strike/--right, or none"}`, `sys.exit(2)`
    (does NOT silently fetch stock depth). [Codex ISSUE-2]
  - **success shape** — bids/asks present → `entitled: true`, ordered `[{price,size,marketMaker}]`,
    keys `{symbol,conId,secType,isSmartDepth,entitled,numRows,asOf,bids,asks}` (+ optional
    `note`); assert `conId` is the qualified contract's id (the stub sets it).
  - **permission denied** — `errorEvent` fires **10089** (or 10092, or matching text) →
    `entitled: false`, `bids/asks` empty, `note` = "no L2 entitlement", exit 0. [Codex ISSUE-3]
  - **entitled but empty (NOT a permission failure)** — no permission error, book empty after
    settle → `entitled: true`, `bids/asks` empty, `note` set (e.g. "no depth returned" /
    "depth line budget exhausted (309)"), exit 0. [Codex ISSUE-3 — empty ≠ unentitled]
  - **chatter is NOT permission** — `errorEvent` fires **2152** (or 309) with a populated
    book → `entitled: true`, levels returned (the warning is ignored).
  - **hard failure** — `reqMktDepth` raises → stdout `{"error": ...}`, `sys.exit(1)`.

**1b. Implement** the module (structure copied from `ib_option_chain.py`):

> **`main()` is SYNCHRONOUS by design** — same as every other `xenon-ib-*` CLI. It uses
> ib_async's sync API via `IBClient` (which runs its own background event loop and exposes
> sync wrappers; `ib.sleep()` pumps that loop). Do **not** rewrite it as `async def`/`await`
> — the subprocess exists precisely so we can use the sync API safely. The
> `ib_async_in_fastapi` hazard applies to sync calls **inside FastAPI's** loop, not to a
> standalone subprocess. `reqMktDepth` returns a `Ticker` immediately and fills it via
> events that `ib.sleep` drives.

- `from xenon.execution.ib_option_chain import underlying_contract` (do NOT duplicate the
  index-exchange map).
- `DEPTH_SETTLE_MAX_SECS = 2.0`, `DEPTH_POLL_SECS = 0.1`, `DEFAULT_NUM_ROWS = 10`.
  Clamp `num_rows` defensively to `1..20` in the CLI too (the route already bounds it, but
  the CLI is independently invokable). [A1]
- `argparse`: `--symbol` (required), `--expiry`, `--strike` (float), `--right`,
  `--num-rows` (int, default 10), `--port` (int, default 4001), `--client-id`
  (default `"auto"`). **No `--con-id` in v1** — dropped to keep the contract identity
  unambiguous (symbol + option triplet fully covers the requirement; conId is a documented
  phase-2 add). [resolves Codex ISSUE-1]
- Resolve `client_id`: `"auto"` passed through; otherwise `int(...)`.
- **Validate the option tuple up front** (all-or-none): if **some but not all** of
  `expiry`/`strike`/`right` are present → print
  `{"error": "provide all of --expiry/--strike/--right, or none"}` and `sys.exit(2)`.
  Never silently fall through to stock depth on a partial tuple. [Codex ISSUE-2]
- Contract resolution (sets `contract` + `sec_type`):
  - all three of `expiry`/`strike`/`right` → build `Option` with the repo's canonical
    **all-keyword** form (precedent: `combo_quote_source.py:207`):
    `Option(symbol=symbol, lastTradeDateOrContractMonth=expiry, strike=float(strike),
right=right.upper(), exchange="SMART", currency="USD")`. Positional args would jam
    `"USD"` into `multiplier` (signature: `symbol, lastTradeDateOrContractMonth, strike,
right, exchange, multiplier, currency`). `sec_type = "OPT"`. Let `qualifyContracts` fill
    `multiplier`/`conId`.
  - else → `contract, sec_type = underlying_contract(symbol)` (`"STK"`/`"IND"`).
- `client._ib.qualifyContracts(contract)`; if no `conId` → `{"error": "could not qualify …"}`,
  exit 1.
- Register `errorEvent` handler that records **genuine** depth-permission errors only —
  mirror the relay's `isDepthPermissionError` (`scripts/infra/ib_realtime/ib_connection_status.js:55`):
  treat **code 10089** (no L2 entitlement) or **10092** (deep depth unsupported for this
  secType/exchange, e.g. index options on CBOE), **or** a message matching
  `/depth.*not (allowed|eligible)|not supported for this combination/i` as no-entitlement.
  **Explicitly ignore** `2152`/`309`/`316`/`317` and the informational `2104`/`2106`/`2158`
  — those are operational chatter on a working book, NOT permission failures (mis-treating
  them froze the relay ladder; do not repeat that here).
- `is_smart = True` (STK/IND/OPT). `ticker = client._ib.reqMktDepth(contract, num_rows, is_smart)`.
- **Bounded poll instead of a flat sleep** (Gemini ISSUE-2, sync-correct form): loop
  `client._ib.sleep(DEPTH_POLL_SECS)` up to `DEPTH_SETTLE_MAX_SECS`, breaking early once
  `ticker.domBids or ticker.domAsks` is populated **or** a permission error has fired; then
  one extra `DEPTH_POLL_SECS` tick so a few more levels arrive. This cuts latency on a fast
  book and exits immediately on no-entitlement, while still capping at 2s. Then copy
  `domBids`/`domAsks` and `client._ib.cancelMktDepth(contract, is_smart)`.
- **`entitled` reflects the PERMISSION axis only — never data presence** [Codex ISSUE-3].
  Track the last depth-scoped error code. `entitled = not permission_error` (true unless
  10089/10092/matching-text fired). Data presence is orthogonal: `bids`/`asks` are whatever
  arrived (possibly empty even when `entitled: true`). Set a `note` when the book is empty so
  the two outcomes are distinguishable:
  - `entitled: false` → `note = "no L2 entitlement"` (10089/10092/text), `bids/asks = []`.
  - `entitled: true`, empty book → `note = "depth line budget exhausted (309)"` when the last
    code was 309 (relay may hold all 3 IB lines — transient), else `note = "no depth returned"`
    (market closed / no L2 levels). **Not** reported as unentitled — we never observed a
    permission rejection.
  - `entitled: true`, non-empty book → no `note`.
    Consumers check `entitled` for permission and `len(bids)/len(asks)` for data.
- Build output dict: `{symbol, conId, secType, isSmartDepth, entitled, numRows, asOf, bids,
asks}` plus `note` only when set. **`conId` is the qualified contract's `conId`** — free
  (we already qualified) and valuable: it's the only query-key way to turn an option spec
  (`symbol`+`expiry`+`strike`+`right`) into a `conId`, which the caller can then feed to
  `/orders/quote?ticker=…&con_id=…` for bid/ask (`/contract/qualify` supports STK/FUT/IND
  only, not options). `asOf = datetime.now(timezone.utc).isoformat()`; `secType = sec_type`.
  `print(json.dumps(out))`.
- `except Exception as e: print(json.dumps({"error": str(e)})); sys.exit(1)` /
  `finally: client.disconnect()`.

**1c. Register entry point** in `pyproject.toml` (alphabetical with the other `xenon-ib-*`):
`xenon-ib-market-depth = "xenon.execution.ib_market_depth:main"`.

**Gate:** `uv run pytest scripts/tests/test_ib_market_depth.py -x` green.
(Re-run `uv sync` so the new console-script entry point installs into `.venv`.)

---

## Step 2 — FastAPI route `GET /market-depth` — `src/xenon/api/server.py`

**2a. Write the failing route test first** — add to/near
`scripts/tests/test_options_chain_route_port.py` (or a new sibling) mirroring its mock of
the subprocess runner:

- Assert the route forwards `symbol` (upper-cased), `--port DEFAULT_GATEWAY_PORT`,
  `--num-rows`, and the option params when all three are present.
- **Partial option tuple → 422** (e.g. `expiry` + `strike` but no `right`) without spawning
  the subprocess. [Codex ISSUE-2]
- Assert subprocess `result.ok=False` → **502**; `result.data={"error": …}` → **502**;
  success → JSON pass-through (200), incl. an `entitled:false` payload and an
  `entitled:true`+empty-book+`note` payload both returned as **200**.

**2b. Implement** inline next to `/options/chain` (~`server.py:2918`).

> **Placement decision:** `api/CLAUDE.md` says new endpoints go in a `routes/` module. Here
> the route is placed **inline** to match its direct siblings — `/options/chain`,
> `/options/expirations`, `/orders/quote` are all inline in `server.py`. The route is a
> thin subprocess dispatcher with **no business logic** (all logic lives in the CLI), so the
> "business logic in `services/`" half of the rule is satisfied. Inline keeps the three
> market-data fetch routes co-located and the diff minimal; a `routes/` module for one thin
> handler would split the pattern for no gain. Revisit if these grow stateful logic.
>
> **Params:** `symbol` matches `/options/chain`; `num_rows` snake_case. No `con_id` in v1
> (dropped per Codex ISSUE-1). Query string: `?symbol=QQQ&expiry=…&strike=…&right=C&num_rows=10`.

```python
@app.get("/market-depth")
async def market_depth(
    symbol: str,
    expiry: Optional[str] = None,
    strike: Optional[float] = None,
    right: Optional[str] = None,
    num_rows: int = Query(10, ge=1, le=20),  # bound: IB caps depth rows; reject abuse [A1]
):
    """Point-in-time L2 order-book snapshot (subprocess; mirrors /options/chain)."""
    # All-or-none option tuple — never silently degrade an option request to stock depth.
    opt = [expiry, strike, right]
    if any(v is not None for v in opt) and not all(v is not None for v in opt):
        raise HTTPException(status_code=422,
                            detail="provide all of expiry/strike/right, or none")
    args = ["--symbol", symbol.upper(), "--port", str(DEFAULT_GATEWAY_PORT),
            "--num-rows", str(num_rows)]
    if expiry: args += ["--expiry", expiry]
    if strike is not None: args += ["--strike", str(strike)]
    if right: args += ["--right", right.upper()]
    result = await _run_ib_script_with_recovery("xenon-ib-market-depth", args, timeout=15)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    if result.data and result.data.get("error"):
        raise HTTPException(status_code=502, detail=result.data["error"])
    return result.data
```

**Gate:** the route test green; `import` of the module still clean.

---

## Step 3 — Auth allowlist — `src/xenon/api/auth.py`

- Under the `# Market data` comment in `QUERY_API_KEY_PATHS`, add `("GET", "/market-depth"),`.
- **Update the exact-set test** `test_query_paths_are_complete`
  (`src/xenon/api/tests/test_query_api_key.py:64`) — it asserts
  `set(QUERY_API_KEY_PATHS) == expected` (line 92). Add `("GET", "/market-depth")` to that
  `expected` set or the build **fails CI**. [Codex ISSUE-4a — do not miss this]
- Extend the same file: query key **grants** `GET /market-depth`; confirm an unlisted
  `POST /market-depth` and the write paths (`/orders/place|cancel|modify`, `/portfolio/sync`,
  `POST /futu/sync`) are still **denied**.

**Gate:** `uv run pytest src/xenon/api/tests/test_query_api_key.py -x` green.

---

## Step 4 — Docs

- **New usage doc** `docs/reference/readonly-query-api.md` (consumer-facing): one section per
  query-key endpoint — method, path, query/body params, `X-API-Key` header, example `curl`,
  and a **real** trimmed response (filled in at Step 5a). Depth section documents the
  `entitled:false` case explicitly. State the auth model accurately: the key is required
  for **non-localhost** callers (loopback bypasses it); write paths are not in the key's
  scope (cite the `test_query_api_key` guarantee, not a live localhost call).
- `src/xenon/CLAUDE.md` Commands table: add `xenon-ib-market-depth`.
- `src/xenon/api/CLAUDE.md` § Auth: add `/market-depth` to the query-key GET list.
- `docs/architecture/api-infrastructure.md`: add the `/market-depth` row.
- `CHANGELOG.md` `[Unreleased]`: "feat(api): GET /market-depth L2 snapshot under query key".

---

## Step 5 — Verify (two distinct kinds of proof — do NOT conflate them)

**Critical auth fact:** `classify_auth` (`src/xenon/api/auth.py:190-192`) passes **any**
loopback (`127.0.0.1`/`::1`) request _before_ the API-key check, and the dev stack binds
uvicorn to `127.0.0.1` only. So a localhost `curl` is authenticated **regardless of the
key** — it can verify _function_ (does the endpoint return a correct book/payload) but it
**cannot** prove the key's grant/deny scope. Splitting accordingly:

### 5a — Functional verification (live, localhost — auth bypassed by design)

**Prefer `scripts/infra/dev.sh live`** for the depth demo: it exports `IB_GATEWAY_HOST` to
the macmini (`dev.sh:182`) so the depth subprocess reaches the live gateway, read-only, on
FastAPI **:8421**. Paper L2 is generally **not** entitled, so a paper run returns
`entitled:false` for everything (still proves the endpoint works, but never shows a book).
Use a **known-entitled symbol — QQQ** (the relay notes 2152 chatter "seen live on entitled
QQQ") to demonstrate a _populated_ book. Read `XENON_QUERY_API_KEY` from `.env` and send it
anyway (so captured commands match what an external consumer runs):

- Sanity first: `GET /options/chain?symbol=QQQ` must work (proves IB + subprocess path);
  if it 502s, depth will too — fix connectivity before judging `/market-depth`.
- `GET /options/expirations?symbol=QQQ`
- `GET /orders/quote?ticker=QQQ&con_id=<conId>` — note **`ticker` + `con_id`** are the actual
  param names (`server.py:2044`), not `conId`/`symbol`. [Codex ISSUE-4b]
- `GET /market-depth?symbol=QQQ` (**200**; expect a populated book on the entitled account)
- `GET /market-depth?symbol=AAPL` (**200**; book, or `entitled:true`+empty+`note`, or
  `entitled:false` if no L2 — all passing: the endpoint works)
- `GET /market-depth?symbol=QQQ&expiry=<e>&strike=<k>&right=C` (option depth path exercised;
  option L2 is rarer — `entitled:false`/empty here is still a pass)
- `GET /market-depth?symbol=QQQ&expiry=<e>&strike=<k>` (no `right`) → **422** (partial tuple
  rejected, no subprocess spawned). [Codex ISSUE-2 live check]
- `POST /historical/bars` (small body), `POST /historical/head-timestamp`
- `GET /portfolio`, `GET /orders`, `GET /performance`, `GET /watchlist`, `GET /attribution`

Acceptance: each read returns **200** with a well-formed body (depth may be `entitled:false`
or `entitled:true`+empty); the partial-tuple case returns **422**. Capture the real trimmed
responses for the Step-4 doc — **no hand-written payloads**.

### 5b — Auth-scope verification (the grant/deny proof)

The **authoritative** proof that the query key grants exactly the read paths and denies
writes is the unit suite `src/xenon/api/tests/test_query_api_key.py` (it constructs
non-localhost requests, which localhost curl cannot). It MUST assert:

- query key **grants** `GET /market-depth` (+ the other read paths already covered),
- query key **denies** `POST /orders/place`, `/orders/cancel`, `/orders/modify`,
  `/portfolio/sync`, `POST /futu/sync`, and an unlisted `POST /market-depth`.

Run: `uv run pytest src/xenon/api/tests/test_query_api_key.py -x`. (A localhost `POST
/orders/place` is **not** a valid negative test — loopback bypasses the key and it would hit
the read-only guard / placement path instead. Don't put it in the doc as if it proved
denial.)

---

## Step 6 — Finalize

- `uv run python scripts/infra/dev/run_pytest_affected.py` (or the three targeted files) +
  `cd web && npm test` if any web file touched (none expected) — all green.
- Milestone commits on `feat/query-api-market-depth` (feature branch only — never master).
- **Execution-environment constraint:** this session's shell CWD is pinned to the **old**
  `.worktrees/readonly-query-api-auth`. All implementation/test/verify work runs against the
  **new** worktree via absolute file paths and `cd /…/query-api-market-depth && …` for Bash.
  Do **not** remove the old worktree mid-run — deleting the pinned CWD breaks every
  subsequent Bash call. Remove it only as the **final** action (or have the user run
  `git -C ~/projects/xenon worktree remove --force .worktrees/readonly-query-api-auth`),
  after which no further Bash in that CWD is needed. The `feat/readonly-query-api-auth`
  branch ref can stay (its content is on master); only the worktree is cleaned up.
- PR only when the user says so.

## Risks / watch-items

- **[A2] Shared IB depth-line budget:** IB allows ~3 concurrent market-depth lines
  (account-wide; the relay's `MAX_CONCURRENT_DEPTH=3`). A snapshot subprocess requests one
  more line for ~2s, so under load it can briefly contend with the **relay's live ladder** →
  IB code `309`. Mitigations in this design: short hold (bounded poll, ≤2s + cancel),
  `num_rows ≤ 20`, and `309`→`entitled:true`+`note` (never a false no-entitlement). If the
  endpoint is ever hammered, add a server-side concurrency gate (out of scope v1) — log it,
  don't silently degrade.
- **Settle latency:** bounded poll returns as soon as the book populates or a permission
  error fires; worst case ~2s, bounded by the route `timeout=15`. No unbounded hang —
  `qualifyContracts`/connect issues are killed by the recovery wrapper's timeout.
- **`underlying_contract` import coupling:** importing from `ib_option_chain` is intentional
  (single source for the index-exchange map); both live in `execution/`.
- **Index-option depth → `entitled:false`:** SMART depth on index options (e.g. SPX on CBOE)
  legitimately returns code `10092` ("deep depth unsupported for this secType/exchange"); the
  text pattern catches it. Reported as `entitled:false`, a correct result.
- **`Query` import:** confirmed already imported (`server.py:25`, alongside `Optional` at
  line 23) — currently unused, so `/market-depth` is its first consumer. No import change.
- **Verification auth caveat:** localhost bypasses the key, so 5a proves _function_ only;
  key grant/deny is proven by `test_query_api_key` (5b), never by a localhost curl.
