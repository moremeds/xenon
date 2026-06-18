# Read-only Query API (`XENON_QUERY_API_KEY`)

Headless, **read-only** access to xenon's data and market-data surfaces for
machine-to-machine consumers (e.g. a research notebook, a sibling service). Every
example below was captured against a live server; payloads are real, not invented.

## Authentication

Send the key in the `X-API-Key` header:

```bash
curl -H "X-API-Key: $XENON_QUERY_API_KEY" "https://<host>:8421/market-depth?symbol=QQQ"
```

Auth model (`src/xenon/api/auth.py`):

- The key grants **only** the methods+paths in `QUERY_API_KEY_PATHS` (listed below). A
  request to any other path/method with this key is **denied (401)**. It can never reach a
  write/sync path (`/orders/place|cancel|modify`, `/portfolio/sync`, `POST /futu/sync`, …).
- **Localhost bypass:** requests from `127.0.0.1`/`::1` are authorized _without_ a key
  (on-box / dev). So the key only matters for non-loopback callers — which is exactly who
  this doc is for.
- The key must differ from `XENON_INTERNAL_API_TOKEN`; the server refuses to boot otherwise.

Verified (real HTTP, non-localhost client):

| Request                                        | Result                                     |
| ---------------------------------------------- | ------------------------------------------ |
| `GET /market-depth?symbol=QQQ` **with** key    | `200` + book                               |
| `GET /market-depth?symbol=QQQ` **without** key | `401 {"detail":"Authentication required"}` |
| `POST /orders/place` **with** key              | `401` (write path, out of scope)           |

## Endpoints in scope

Full allowlist (`QUERY_API_KEY_PATHS`). Market-data endpoints are documented in detail below;
the portfolio/orders surfaces read from Postgres and are described in
`docs/architecture/api-infrastructure.md`.

| Method  | Path                         | Purpose                                                      |
| ------- | ---------------------------- | ------------------------------------------------------------ |
| GET     | `/portfolio`                 | Account snapshot (positions, NAV)                            |
| GET     | `/futu/portfolio`            | Futu account snapshot                                        |
| GET     | `/attribution`               | P&L attribution                                              |
| GET     | `/orders`                    | Order list                                                   |
| GET     | `/orders/quote`              | Single-contract bid/ask/mid — **params: `ticker`, `con_id`** |
| GET     | `/blotter`                   | Today's fills                                                |
| GET     | `/journal`                   | Journal entries                                              |
| GET     | `/trades/entry-dates`        | Closed-trade entry dates                                     |
| GET     | `/performance`               | Performance / NAV series                                     |
| GET     | `/watchlist`                 | Watchlist                                                    |
| GET     | `/options/chain`             | Option strikes — params: `symbol`, `expiry?`                 |
| GET     | `/options/expirations`       | Option expiries — param: `symbol`                            |
| **GET** | **`/options/greeks`**        | **Broker-computed option greeks (see below)**                |
| **GET** | **`/market-depth`**          | **L2 order-book snapshot (see below)**                       |
| POST    | `/historical/bars`           | Historical OHLCV — body below                                |
| POST    | `/historical/head-timestamp` | Earliest available bar — body below                          |
| POST    | `/contract/qualify`          | Qualify STK/FUT/IND specs → `conId` (no options)             |
| POST    | `/ws-ticket`                 | Mint a 30s ticket to open the realtime WS feed               |

---

## `GET /market-depth` — L2 order-book snapshot

Point-in-time top-N depth for a stock/index, or an option. **Snapshot, not a stream** (live
streaming L2 is the realtime WS feed via `/ws-ticket`). Backed by a short-lived subprocess
that subscribes `reqMktDepth`, lets the book settle (~2s), reads it, and cancels.

### Query parameters

| Param      | Type                 | Notes                                              |
| ---------- | -------------------- | -------------------------------------------------- |
| `symbol`   | string, **required** | Underlying ticker (stock/ETF/index). Upper-cased.  |
| `expiry`   | string `YYYYMMDD`    | Option leg. **All-or-none** with `strike`+`right`. |
| `strike`   | float                | Option strike.                                     |
| `right`    | `C`/`P`              | Option right.                                      |
| `num_rows` | int, default `10`    | Levels per side, **1–20** (out of range → `422`).  |

`symbol` alone → stock/index depth. The full option triplet (`expiry`+`strike`+`right`) →
option depth. A **partial** option tuple → `422` (never silently degrades to stock depth).

### Response

| Field                        | Meaning                                                                                                                                                                                                        |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `symbol`, `conId`, `secType` | Qualified contract (`conId` is the resolved IB id).                                                                                                                                                            |
| `isSmartDepth`               | Always `true` (SMART aggregated depth).                                                                                                                                                                        |
| `entitled`                   | **Permission axis only.** `false` only on a genuine L2-permission rejection (IB code 10089/10092 or matching text). An empty book with no rejection stays `true`.                                              |
| `numRows`                    | Requested levels (post-clamp).                                                                                                                                                                                 |
| `asOf`                       | UTC ISO timestamp of the snapshot.                                                                                                                                                                             |
| `bids` / `asks`              | `[{price, size, marketMaker}]`, best-first. May be empty.                                                                                                                                                      |
| `note`                       | Present only when the book is empty: `"no L2 entitlement"` (not entitled), `"depth line budget exhausted (309)"` (transient — IB's ~3 depth lines busy), or `"no depth returned"` (market closed / no levels). |

> **`conId` bonus:** `/market-depth` returns the qualified `conId` — including for **options**,
> which `/contract/qualify` cannot resolve (it supports STK/FUT/IND only). Feed that `conId`
> to `GET /orders/quote?ticker=<sym>&con_id=<conId>` for the option's bid/ask.

> **`entitled` vs data:** check `entitled` for _permission_ and `len(bids)/len(asks)` for
> _data_. They are independent — an entitled symbol can return an empty book (e.g. outside
> regular trading hours).

### Examples (real captures, 2026-06-18, live IB)

Populated book (overnight session):

```bash
curl -H "X-API-Key: $XENON_QUERY_API_KEY" "http://<host>:8421/market-depth?symbol=QQQ&num_rows=5"
```

```json
{
  "symbol": "QQQ",
  "conId": 320227571,
  "secType": "STK",
  "isSmartDepth": true,
  "entitled": true,
  "numRows": 5,
  "asOf": "2026-06-18T07:24:28.493228+00:00",
  "bids": [{ "price": 732.89, "size": 823.0, "marketMaker": "OVERNIGHT" }],
  "asks": [{ "price": 733.01, "size": 200.0, "marketMaker": "OVERNIGHT" }]
}
```

Entitled but empty (market closed — a passing result, **not** an error):

```json
{
  "symbol": "AAPL",
  "conId": 265598,
  "secType": "STK",
  "isSmartDepth": true,
  "entitled": true,
  "numRows": 5,
  "asOf": "2026-06-18T07:17:53.992283+00:00",
  "bids": [],
  "asks": [],
  "note": "no depth returned"
}
```

Not entitled (genuine L2-permission rejection):

```json
{
  "symbol": "<sym>", "conId": <id>, "secType": "STK",
  "isSmartDepth": true, "entitled": false, "numRows": 10,
  "asOf": "...", "bids": [], "asks": [], "note": "no L2 entitlement"
}
```

Option depth (full triplet):

```bash
curl -H "X-API-Key: $XENON_QUERY_API_KEY" \
  "http://<host>:8421/market-depth?symbol=QQQ&expiry=20260717&strike=600&right=C"
```

Errors: `422` (partial option tuple, or `num_rows` out of 1–20); `502`
(`{"detail":"could not qualify <symbol>"}` for an unlistable contract, or IB gateway
unreachable). A no-entitlement / empty book is a **200**, not an error.

---

## `GET /options/greeks` — broker-computed option greeks

Point-in-time IB `modelGreeks` (the broker's own option-model output) for a single option
contract. **Snapshot, not a stream.** Backed by a short-lived subprocess that subscribes
`reqMktData(snapshot=True)`, polls until IB computes greeks (delta present), reads them, and
cancels. Greeks are **option-only** — there is no stock/index form.

### Query parameters

| Param    | Type                            | Notes                                              |
| -------- | ------------------------------- | -------------------------------------------------- |
| `symbol` | string, **required**            | Underlying ticker. Upper-cased.                    |
| `expiry` | string `YYYYMMDD`, **required** | Option expiry.                                     |
| `strike` | float, **required**             | Option strike.                                     |
| `right`  | `C`/`P`, **required**           | Option right (upper-cased). Anything else → `422`. |

All four are required (greeks are option-only). A missing/blank leg or a `right` outside
`{C,P}` → `422` before any subprocess spawns.

### Response

| Field                        | Meaning                                                                                                                                                         |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `symbol`, `conId`, `secType` | Qualified option (`conId` is the resolved IB id; `secType` is always `OPT`).                                                                                    |
| `expiry`, `strike`, `right`  | Echo of the requested contract.                                                                                                                                 |
| `asOf`                       | UTC ISO timestamp of the snapshot **request** (not the data vintage — see freshness note).                                                                      |
| `bid` / `ask`                | Option NBBO, or `null` when no quote is active (IB's `-1` "no quote" sentinel is normalized to `null`).                                                         |
| `greeks`                     | `{impliedVol, delta, gamma, vega, theta, undPrice}`, or `null` when IB computes none. Greeks may be negative (theta, put delta). NaN fields collapse to `null`. |
| `note`                       | Present only when `greeks` is `null`: `"no greeks returned"`.                                                                                                   |

> **Freshness / 24-7 behavior:** the subprocess sets IB **frozen** market-data type (`2`), so
> during regular trading hours you get live greeks and after hours you get the **last recorded
> session** values — the endpoint returns greeks around the clock rather than `null` every
> evening/weekend. `asOf` is the request time, so it does **not** distinguish live from
> last-session greeks; treat off-hours values as last-close.

> **`conId` for options:** like `/market-depth`, this returns the qualified option `conId`
> (which `/contract/qualify` cannot resolve). Feed it to
> `GET /orders/quote?ticker=<sym>&con_id=<conId>` for the live bid/ask/mid.

### Examples (real capture, 2026-06-18, live IB)

```bash
curl -H "X-API-Key: $XENON_QUERY_API_KEY" \
  "http://<host>:8421/options/greeks?symbol=QQQ&expiry=20260717&strike=600&right=C"
```

```json
{
  "symbol": "QQQ",
  "conId": 870718877,
  "secType": "OPT",
  "expiry": "20260717",
  "strike": 600.0,
  "right": "C",
  "asOf": "2026-06-18T09:07:46.149849+00:00",
  "bid": null,
  "ask": null,
  "greeks": {
    "impliedVol": 0.4071,
    "delta": 0.9548,
    "gamma": 0.00116,
    "vega": 0.2306,
    "theta": -0.2295,
    "undPrice": 722.39
  }
}
```

(Captured pre-market: `bid`/`ask` are `null` — no active quote — while the frozen fallback
still returns the last-session greeks. A deep-ITM 600C on QQQ@722 → delta ≈ 0.95.)

When IB computes no greeks at all (illiquid contract), `greeks` is `null` with
`"note": "no greeks returned"` — still a **200**.

Errors: `422` (missing leg or `right` ∉ `{C,P}`); `502`
(`{"detail":"could not qualify <symbol> ..."}` for an unlistable contract, or IB gateway
unreachable).

---

## Other market-data endpoints

`GET /options/chain?symbol=AAPL[&expiry=YYYYMMDD]` — strikes (and expiry detail when given);
enumerator only, no quotes/greeks.
`GET /options/expirations?symbol=AAPL` — list of expiries.
`GET /orders/quote?ticker=AAPL&con_id=265598` — single-contract bid/ask/mid (needs a `conId`).

`POST /historical/bars` — body:

```json
{
  "contract": {
    "sec_type": "STK",
    "symbol": "AAPL",
    "exchange": "SMART",
    "currency": "USD"
  },
  "end_date_time": "",
  "duration": "1 D",
  "bar_size": "1 day",
  "what_to_show": "TRADES",
  "use_rth": true
}
```

`POST /historical/head-timestamp` — `{"contract": {...}, "what_to_show": "TRADES", "use_rth": true}`.

`POST /contract/qualify` — `{"contracts": [{"sec_type": "STK", "symbol": "AAPL"}]}` → each with
its resolved `conId`. **STK/FUT/IND only** — for options, use `/market-depth`'s `conId`.
