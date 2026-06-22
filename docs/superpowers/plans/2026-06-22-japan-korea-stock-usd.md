# Japan & Korea Stock Support with Live USD Conversion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Support Japan (TSEJ/JPY) and Korea (KSE/KRW) cash-equity positions and orders in xenon the same way as US stocks, with every value converted to a live-USD headline (native price shown alongside).

**Architecture:** IB already returns foreign positions in their native currency (JPY/KRW) plus a per-currency `ExchangeRate`; today the code discards both and treats every number as USD. We (1) capture `currency`+`exchange` from the IB position contract, (2) normalize all FX to one `usd_per_unit` convention in a small `fx` module, (3) convert per-position values + portfolio totals to USD in the backend at sync time using IB's `ExchangeRate`, (4) stream live `USD.JPY`/`USD.KRW` forex ticks (and live foreign-stock quotes) through the existing realtime relay so the frontend refines the USD headline live, and (5) make the order path exchange/currency-aware so TSEJ/KSE limit orders can be placed. Conversion is isolated to two thin layers (backend totals + frontend display edge); the native-unit P&L math in `positionUtils.ts` is left untouched.

**Tech Stack:** Python 3.13 (`uv`, `ib_async` v2.1.0, SQLAlchemy Core, Alembic), Next.js/React (TypeScript, TypeBox schemas, Vitest), Node relay (`@stoqey/ib`), Postgres, Playwright + chrome-cdp for E2E.

## Global Constraints

- **All Python via `uv`** — never bare `python`/`pip`. Tests: `uv run pytest`. Migrations: `uv run alembic ...`.
- **DB-first, no JSON fallbacks** on order/portfolio paths (CI guards: `scripts/checks/no_json_*_on_order_path.py`). Postgres is the runtime source of truth.
- **Scope columns required on every write** — `broker`, `account_env`, `broker_account`. Use `AccountScope`; sync subprocesses read `XENON_TRADING_MODE` + `XENON_BROKER_ACCOUNT`.
- **`XENON_READ_ONLY=1` no-ops every write** — `_save_portfolio_to_postgres`, `_append_nav_snapshot`, order placement (403 `reason_code: READ_ONLY_MODE`). New persistence/order code MUST honor it.
- **Naked-short guard is mandatory** — short stock with no long shares is BLOCKED at all three layers. Foreign stocks are subject to the same rule; do not weaken it.
- **Brand:** 4px max border-radius, badges 999px capsule, colors via tokens (no raw hex), mono for machine / sans for product.
- **Red/green TDD** for every code change. **E2E browser verification** for every UI change (chrome-cdp primary, Playwright fallback). 95% coverage target.
- **Never commit without explicit user request.** Commit messages: no `Co-Authored-By`/AI-attribution trailers.
- **Leading-zero numeric tickers stay strings** end-to-end (`000660` must never be parsed to a number). Note `normalizeSymbolList` uppercases symbols — harmless for digit-only tickers.
- **Verified IB facts (ib_async v2.1.0, confirmed against installed source + live IDEALPRO snapshot 2026-06-22):**
  - `Position` = `NamedTuple(account, contract, position, avgCost)`. `avgCost` is **native currency**.
  - `PortfolioItem` = `NamedTuple(contract, position, marketPrice, marketValue, averageCost, unrealizedPNL, realizedPNL, account)` — `marketValue`/`unrealizedPNL` are **native currency, NOT base**.
  - `AccountValue` = `NamedTuple(account, tag, value:str, currency, modelCode)` — `value` is a **string** (cast it). Tag `ExchangeRate` = rate of `currency` to base (base = native × ExchangeRate).
  - `Forex('USDJPY')` (6-char pair, no dot) → `symbol="USD"`, `currency="JPY"`, `exchange="IDEALPRO"`, `secType="CASH"`.
  - IB exchange codes: Japan = `TSEJ`/`JPY`, Korea = `KSE`/`KRW`. `Position.contract.primaryExchange` may be empty pre-qualification — qualify foreign contracts.
  - `USD.JPY` (conId 15016059) and `USD.KRW` (conId 36363302) both return live IDEALPRO bid/ask from this account.

---

## File Structure

**New files:**

- `src/xenon/utils/fx.py` — FX normalization + `to_usd` (backend, ExchangeRate-based).
- `scripts/tests/test_fx.py` — unit tests for `fx.py`.
- `web/lib/fx.ts` — FX normalization + `toUsd` + `fmtNative` (frontend, forex-tick + payload fallback).
- `web/tests/fx.test.ts` — Vitest for `fx.ts`.
- `web/components/FxBadge.tsx` — live USD/JPY · USD/KRW badge with source+age.
- `web/tests/fx-badge.test.ts` — Vitest for FxBadge.
- `src/xenon/db/migrations/versions/2026_06_22_positions_currency_exchange.py` — add `currency` + `exchange` columns.
- `scripts/tests/test_ib_sync_currency.py` — tests for currency capture + USD totals.

**Modified files (with responsibility):**

- `src/xenon/execution/ib_sync.py` — capture currency/exchange; harvest `ExchangeRate`; fix SMART-forcing for foreign contracts; compute USD totals + per-position `*_usd`; stamp `fx_rates`/`base_currency` into payload; persist `currency`/`exchange` columns.
- `src/xenon/db/schema.py` — add `currency`, `exchange` columns to `positions`.
- `scripts/infra/ib_realtime/ib_contracts.js` — add `forexContract`; allow stock subscriptions to carry exchange/currency.
- `scripts/infra/ib_realtime/ib_realtime_server.js` — parse + subscribe forex pairs and exchange-qualified stocks; broadcast forex ticks.
- `web/lib/pricesProtocol.ts` — `ForexContract` type + stock-subscription-with-exchange descriptor + helpers.
- `web/lib/usePrices.ts` — accept + diff forex subscriptions and exchange-qualified stocks.
- `web/lib/useFx.ts` (new small hook) — derive `usd_per_unit` map from live forex ticks + payload fallback.
- `web/lib/portfolioDataSchema.ts` + `web/lib/types.ts` — add `currency`, `entry_cost_usd`, `market_value_usd` to leg/position; add `base_currency`, `fx_rates` to payload. (Canonical USD field names are `entry_cost_usd` + `market_value_usd` — never `value_usd`.)
- `web/components/PositionTable.tsx` — native price sub-line + USD headline (live) + FX badge; currency-aware row.
- `src/xenon/execution/ib_place_order.py` — `Stock(symbol, exchange, currency)` from body (default SMART/USD).
- `src/xenon/api/server.py` — `_contract_from_order_body` forex/foreign-stock branch; `security_type` unaffected (STK).
- `web/lib/placeOrderBodySchema.ts` + `web/lib/order/placeOrderContract.ts` + `web/app/api/orders/place/route.ts` — accept optional `exchange`/`currency` on stock bodies, forward them.
- `web/components/ticker-detail/BookTab.tsx` (`StockOrderForm`) + `web/components/ticker-detail/OrderTab.tsx` (`buildSingleLegOrderPayload`) — pass position's exchange/currency; market label.

---

## PHASE 1 — Backend: currency capture + USD conversion foundation

Pure backend. Testable via `uv run xenon-ib-sync` (display path) against the live 5016/000660 positions without writing (read-only safe).

### Task 1.1: `fx` module — normalize rates and convert to USD

**Files:**

- Create: `src/xenon/utils/fx.py`
- Test: `scripts/tests/test_fx.py`

**Interfaces:**

- Produces:
  - `to_usd(amount: float | None, currency: str | None, usd_per_unit: dict[str, float]) -> float | None`
  - `usd_per_unit_from_account_values(account_values: list, base_currency: str = "USD") -> dict[str, float]`
  - `SANE_USD_PER_UNIT_BAND: dict[str, tuple[float, float]]` (per-currency plausibility bands)

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_fx.py
from types import SimpleNamespace

from xenon.utils.fx import to_usd, usd_per_unit_from_account_values, SANE_USD_PER_UNIT_BAND


def test_to_usd_identity_for_usd():
    assert to_usd(1234.0, "USD", {"USD": 1.0}) == 1234.0


def test_to_usd_converts_native_with_rate():
    # 123_400 JPY * 0.0064 USD/JPY = 789.76 USD
    assert to_usd(123_400.0, "JPY", {"USD": 1.0, "JPY": 0.0064}) == 789.76


def test_to_usd_none_amount_returns_none():
    assert to_usd(None, "JPY", {"JPY": 0.0064}) is None


def test_to_usd_missing_rate_returns_none():
    assert to_usd(100.0, "KRW", {"USD": 1.0}) is None


def test_to_usd_defaults_blank_currency_to_usd():
    assert to_usd(50.0, "", {"USD": 1.0}) == 50.0


def test_usd_per_unit_from_account_values_reads_exchange_rate_tag():
    avs = [
        SimpleNamespace(tag="ExchangeRate", value="0.0064", currency="JPY"),
        SimpleNamespace(tag="ExchangeRate", value="0.00073", currency="KRW"),
        SimpleNamespace(tag="NetLiquidation", value="100000", currency="USD"),
    ]
    out = usd_per_unit_from_account_values(avs)
    assert out["USD"] == 1.0
    assert out["JPY"] == 0.0064
    assert out["KRW"] == 0.00073


def test_usd_per_unit_skips_unparseable_values():
    avs = [SimpleNamespace(tag="ExchangeRate", value="N/A", currency="JPY")]
    out = usd_per_unit_from_account_values(avs)
    assert "JPY" not in out  # bad value skipped, USD identity still present
    assert out["USD"] == 1.0


def test_sane_band_catches_inverted_jpy_rate():
    lo, hi = SANE_USD_PER_UNIT_BAND["JPY"]
    assert lo <= 0.0064 <= hi          # correct direction passes
    assert not (lo <= 161.5 <= hi)     # inverted (JPY-per-USD) fails
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest scripts/tests/test_fx.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'xenon.utils.fx'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/xenon/utils/fx.py
"""Native-currency → USD conversion for multi-currency IB portfolios.

IB returns position market values, avg cost, and unrealized P&L in the
contract's NATIVE currency (JPY for TSEJ, KRW for KSE) — verified against
ib_async v2.1.0 PortfolioItem semantics and IB's updatePortfolio callback.
This module normalizes rates into ONE convention: ``usd_per_unit[currency]``
is the USD value of 1 unit of that currency, so ``usd = native * rate``.

Two upstream rate sources exist and are reciprocals:
  * IB ``accountValues()`` ``ExchangeRate`` tag — already USD-per-unit for a
    USD-base account (base = native * ExchangeRate). Used here (backend).
  * IB forex tick ``USD.JPY`` = JPY-per-USD — inverted (1/tick) on the
    frontend. See web/lib/fx.ts.
"""
from __future__ import annotations

# Plausibility bands for usd_per_unit, used by tests + a runtime sanity log to
# catch an inverted-direction rate before it corrupts USD values. Wide on
# purpose — only meant to catch 1/x mistakes, not small drift.
SANE_USD_PER_UNIT_BAND: dict[str, tuple[float, float]] = {
    "JPY": (0.002, 0.02),    # ~ 1/500 .. 1/50
    "KRW": (0.0003, 0.003),  # ~ 1/3300 .. 1/330
    "USD": (1.0, 1.0),
}


def to_usd(amount: float | None, currency: str | None, usd_per_unit: dict[str, float]) -> float | None:
    """Convert a native amount to USD. Returns None when amount is None or no
    rate is available for the currency (caller decides how to surface that)."""
    if amount is None:
        return None
    cur = (currency or "USD").upper()
    if cur == "USD":
        return float(amount)
    rate = usd_per_unit.get(cur)
    if rate is None or rate <= 0:
        return None
    return round(float(amount) * rate, 2)


def usd_per_unit_from_account_values(account_values: list, base_currency: str = "USD") -> dict[str, float]:
    """Build usd_per_unit from IB accountValues() ExchangeRate rows.

    IB's ExchangeRate tag value is the rate of <currency> to the base currency,
    i.e. base_value = native_value * ExchangeRate, so for a USD-base account
    usd_per_unit[currency] = float(value)."""
    out: dict[str, float] = {base_currency.upper(): 1.0}
    for av in account_values:
        if getattr(av, "tag", None) != "ExchangeRate":
            continue
        cur = (getattr(av, "currency", "") or "").upper()
        if not cur:
            continue
        try:
            rate = float(getattr(av, "value", None))
        except (TypeError, ValueError):
            continue
        # Adversarial guard: IB emits sentinel values (DBL_MAX ≈ 1.79e308) for
        # unavailable fields. A sentinel rate would inflate every USD value
        # astronomically. Accept only plausible, finite rates. _PLAUSIBLE_RATE_MAX
        # is generous (covers IDR/VND-scale currencies) but kills sentinels.
        import math as _math

        if not _math.isfinite(rate) or not (0 < rate < _PLAUSIBLE_RATE_MAX):
            continue
        out[cur] = rate
    return out
```

Add the module constant near the top of `fx.py` (after `SANE_USD_PER_UNIT_BAND`):

```python
# Upper bound for a believable USD-per-unit rate. Even the weakest currencies
# (IDR ~6e-5, VND ~4e-5) are far below 1.0; a USD-per-unit rate ≥ this is a
# sentinel or inverted value, never a real FX rate. Catches IB's DBL_MAX sentinel.
_PLAUSIBLE_RATE_MAX = 100.0
```

Add an adversarial test to `scripts/tests/test_fx.py`:

```python
def test_usd_per_unit_rejects_sentinel_rate():
    avs = [SimpleNamespace(tag="ExchangeRate", value="1.7976931348623157e308", currency="JPY")]
    out = usd_per_unit_from_account_values(avs)
    assert "JPY" not in out  # sentinel rejected; USD identity remains
    assert out["USD"] == 1.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest scripts/tests/test_fx.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add src/xenon/utils/fx.py scripts/tests/test_fx.py
git commit -m "feat(fx): add native→USD conversion module with ExchangeRate normalization"
```

### Task 1.2: Capture `currency` + `exchange` in `fetch_positions`

**Files:**

- Modify: `src/xenon/execution/ib_sync.py:603-637` (`fetch_positions`)
- Test: `scripts/tests/test_ib_sync_currency.py`

**Interfaces:**

- Consumes: `pos.contract.currency`, `pos.contract.primaryExchange`, `pos.contract.exchange` (ib_async `Contract`).
- Produces: each formatted position dict gains `"currency": str` (default `"USD"`) and `"exchange": str | None`.

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_ib_sync_currency.py
from types import SimpleNamespace

from xenon.execution.ib_sync import fetch_positions


def _pos(symbol, currency, primary, exch, sec="STK"):
    contract = SimpleNamespace(
        symbol=symbol, secType=sec, currency=currency,
        primaryExchange=primary, exchange=exch, conId=1, strike=0, right="",
        lastTradeDateOrContractMonth="",
    )
    return SimpleNamespace(contract=contract, position=100.0, avgCost=1234.0)


class _Client:
    def __init__(self, positions):
        self._positions = positions

    def get_positions(self):
        return self._positions


def test_fetch_positions_captures_currency_and_exchange():
    client = _Client([_pos("5016", "JPY", "TSEJ", "TSEJ")])
    out = fetch_positions(client)
    assert out[0]["currency"] == "JPY"
    assert out[0]["exchange"] == "TSEJ"


def test_fetch_positions_defaults_currency_usd_when_missing():
    client = _Client([_pos("AAPL", "", "", "SMART")])
    out = fetch_positions(client)
    assert out[0]["currency"] == "USD"


def test_fetch_positions_prefers_primary_exchange_falls_back_to_exchange():
    client = _Client([_pos("000660", "KRW", "", "KSE")])
    out = fetch_positions(client)
    assert out[0]["exchange"] == "KSE"  # primaryExchange empty → exchange
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest scripts/tests/test_ib_sync_currency.py -v`
Expected: FAIL — `KeyError: 'currency'`

- [ ] **Step 3: Write minimal implementation**

In `fetch_positions` (`src/xenon/execution/ib_sync.py`), add two keys to the appended dict (after the `"symbol"` line at :623):

```python
        formatted.append(
            {
                "symbol": contract.symbol,
                "currency": (getattr(contract, "currency", "") or "USD").upper(),
                "exchange": (getattr(contract, "primaryExchange", "") or getattr(contract, "exchange", "") or None),
                "secType": contract.secType,
                "position": position_size,
                "avgCost": avg_cost,
                # ... rest unchanged ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest scripts/tests/test_ib_sync_currency.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/ib_sync.py scripts/tests/test_ib_sync_currency.py
git commit -m "feat(ib-sync): capture native currency + exchange on positions"
```

### Task 1.3: Thread currency through `collapse_positions` → legs + position

**Files:**

- Modify: `src/xenon/execution/ib_sync.py:404-560` (`collapse_positions`)
- Test: `scripts/tests/test_ib_sync_currency.py` (append)

**Interfaces:**

- Consumes: `pos["currency"]`, `pos["exchange"]` from Task 1.2.
- Produces: each collapsed position dict gains `"currency"` + `"exchange"`; each formatted leg gains `"currency"`.

- [ ] **Step 1: Write the failing test (append)**

```python
def test_collapse_positions_propagates_currency_to_position_and_legs():
    from xenon.execution.ib_sync import collapse_positions

    legs = [{
        "symbol": "5016", "currency": "JPY", "exchange": "TSEJ", "secType": "STK",
        "position": 100.0, "avgCost": 1234.0, "entry_cost": 123400.0,
        "expiry": "N/A", "strike": 0, "right": "", "structure": "Stock (100 shares)",
        "conId": 1, "marketPrice": 1250.0, "marketValue": 125000.0,
        "marketPriceIsCalculated": False, "ibDailyPnl": None,
    }]
    out = collapse_positions(legs)
    assert out[0]["currency"] == "JPY"
    assert out[0]["exchange"] == "TSEJ"
    assert out[0]["legs"][0]["currency"] == "JPY"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest scripts/tests/test_ib_sync_currency.py::test_collapse_positions_propagates_currency_to_position_and_legs -v`
Expected: FAIL — `KeyError: 'currency'`

- [ ] **Step 3: Write minimal implementation**

**Grouping key (review fix — Codex ISSUE-7, conf 82):** `collapse_positions` groups by `key = (pos["symbol"], pos["expiry"])` (:415). Two same-symbol listings in different currencies/exchanges (a real risk with short numeric tickers like `5016`, and dual-listings) would merge into one row. Add currency to the key so distinct listings stay separate:

```python
    for pos in positions:
        # Stocks: include currency so a foreign listing never merges with a
        # same-symbol USD listing. Options keep (symbol, expiry) grouping.
        key = (pos["symbol"], pos["expiry"], (pos.get("currency") or "USD").upper())
        groups[key].append(pos)
```

Update the `for (symbol, expiry), legs in groups.items():` unpacking (:426) to `for (symbol, expiry, _cur), legs in groups.items():`. The covered-call merge pass (`_merge_covered_call_groups`) keys on `key[0]` (symbol) only — unaffected — but verify its `groups[key]` reads still work with the 3-tuple key (they index by the full key, so they do).

In `collapse_positions`, the per-leg dict built at :513-525 — add `"currency"`:

```python
            formatted_legs.append(
                {
                    "direction": "LONG" if leg["position"] > 0 else "SHORT",
                    "contracts": int(abs(leg["position"])),
                    "type": "Call" if leg.get("right") == "C" else ("Put" if leg.get("right") == "P" else "Stock"),
                    "currency": (leg.get("currency") or "USD").upper(),
                    # ... rest unchanged ...
```

And the collapsed position dict at :528-549 — add `"currency"` + `"exchange"` (derive from the first leg; all legs of a single-symbol group share currency):

```python
        _grp_currency = (legs[0].get("currency") or "USD").upper()
        _grp_exchange = legs[0].get("exchange")
        collapsed.append(
            {
                "id": position_id,
                "ticker": symbol,
                "currency": _grp_currency,
                "exchange": _grp_exchange,
                "structure": structure_desc,
                # ... rest unchanged ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest scripts/tests/test_ib_sync_currency.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/ib_sync.py scripts/tests/test_ib_sync_currency.py
git commit -m "feat(ib-sync): propagate currency/exchange through collapse_positions"
```

### Task 1.4: Stop forcing SMART on foreign position contracts (quote bug fix)

**Files:**

- Modify: `src/xenon/execution/ib_sync.py:1259-1262` (the `main()` SMART-forcing loop)
- Test: covered by E2E in Phase 5 (this touches the live IB market-data path; unit-asserting the loop is low value). Add an inline regression note.

> Scope note (verified): `fetch_market_prices` (:640) has **zero callers** — `main()` inlines its own price fetch (the only sync path). So this fix only needs `main()`'s loop. Do NOT modify or delete `fetch_market_prices` (out of scope).

**Interfaces:**

- Consumes: `pos["currency"]` (Task 1.2), `pos["contract"]`.
- Behavior: USD contracts keep `exchange="SMART"` (existing AMEX/BATS workaround). Non-USD contracts are qualified (cheap — only the few foreign ones) so IB fills in a routable exchange, instead of being force-set to SMART which fails for TSEJ/KSE.

- [ ] **Step 1: Implement (no isolated unit test — see note; verified live in Phase 5)**

Replace the loop at :1259-1262:

```python
            client.set_market_data_type(4)
            # USD contracts: force SMART (positions from get_positions() may carry
            # AMEX/BATS that fail with reqMktData type 4). Non-USD contracts (TSEJ/
            # KSE): SMART does not route foreign venues — qualify so IB supplies a
            # routable exchange. Verified: forcing SMART on a JPY contract breaks
            # the quote. See order-path-incident-history.md.
            foreign = [p for p in positions if (p.get("currency") or "USD").upper() != "USD"]
            for pos in positions:
                if (pos.get("currency") or "USD").upper() == "USD":
                    pos["contract"].exchange = "SMART"
            if foreign:
                try:
                    client.qualify_contracts(*[p["contract"] for p in foreign])
                except Exception as exc:
                    print(f"  Warning: could not qualify {len(foreign)} foreign contract(s): {exc}")
```

- [ ] **Step 2: Sanity-run the display path against live IB (read-only)**

Run (live gateway, read-only — does not write):
`XENON_TRADING_MODE=live XENON_READ_ONLY=1 uv run xenon-ib-sync --port 4001`
Expected: 5016 and 000660 appear in the POSITIONS list with a non-`None` market value (native JPY/KRW number). If they show market value `None`, the qualify path needs the exchange set explicitly (`pos["contract"].exchange = pos["exchange"]`) — adjust and re-run.

- [ ] **Step 3: Commit**

```bash
git add src/xenon/execution/ib_sync.py
git commit -m "fix(ib-sync): qualify foreign contracts instead of forcing SMART (TSEJ/KSE quotes)"
```

### Task 1.5: Harvest `ExchangeRate` + compute USD totals + per-position `*_usd` + stamp payload

**Files:**

- Modify: `src/xenon/execution/ib_sync.py` — ADD `get_fx_rates` next to `get_account_summary` (do NOT change `get_account_summary`'s `currency == "USD"` filter: for a USD-base account the USD-tagged NetLiquidation is IB's already-consolidated total, so NAV/bankroll stay correct); modify `convert_to_portfolio_format` (:920-1020); modify `main()` to capture + pass `fx_rates`.
- Test: `scripts/tests/test_ib_sync_currency.py` (append)

**Interfaces:**

- Consumes: `client.ib.accountValues()`, `fx.usd_per_unit_from_account_values`, `fx.to_usd`.
- Produces:
  - `get_account_summary` unchanged signature; **also returns** an `fx_rates` side-channel via a new `get_fx_rates(client) -> dict[str, float]`.
  - `convert_to_portfolio_format(account, collapsed, pnl_data=None, fill_dates=None, fx_rates=None)` — new optional `fx_rates` param.
  - Each position dict gains `entry_cost_usd`, `market_value_usd` (USD floats or None). Payload gains `base_currency: "USD"`, `fx_rates: dict`. `total_deployed_dollars`/`total_deployed_pct`/`remaining_capacity_pct` computed in USD.

- [ ] **Step 1: Write the failing test (append)**

```python
def test_get_fx_rates_reads_exchange_rate(monkeypatch):
    from xenon.execution.ib_sync import get_fx_rates
    avs = [
        SimpleNamespace(tag="ExchangeRate", value="0.0064", currency="JPY"),
        SimpleNamespace(tag="ExchangeRate", value="0.00073", currency="KRW"),
    ]
    client = SimpleNamespace(ib=SimpleNamespace(accountValues=lambda: avs))
    rates = get_fx_rates(client)
    assert rates["JPY"] == 0.0064 and rates["KRW"] == 0.00073 and rates["USD"] == 1.0


def test_convert_to_portfolio_format_adds_usd_fields_and_totals(monkeypatch):
    from xenon.execution import ib_sync

    # Avoid PG lookups. NOTE: convert_to_portfolio_format imports
    # load_entry_date_lookups_sync + EntryDateLookups LOCALLY from
    # xenon.utils.portfolio_loader, so patch THAT module, not ib_sync
    # (the names are not module attributes of ib_sync).
    import xenon.utils.portfolio_loader as _pl
    monkeypatch.setattr(_pl, "load_entry_date_lookups_sync", lambda scope: _pl.EntryDateLookups({}, {}, {}, {}))

    collapsed = [
        {"ticker": "5016", "currency": "JPY", "structure": "Stock (100 shares)",
         "expiry": "N/A", "entry_cost": 123400.0, "market_value": 125000.0,
         "risk_profile": "equity", "legs": [], "max_risk": None},
        {"ticker": "AAPL", "currency": "USD", "structure": "Stock (10 shares)",
         "expiry": "N/A", "entry_cost": 2000.0, "market_value": 2100.0,
         "risk_profile": "equity", "legs": [], "max_risk": None},
    ]
    account = {"NetLiquidation": 100000.0}
    fx_rates = {"USD": 1.0, "JPY": 0.0064}
    out = ib_sync.convert_to_portfolio_format(account, collapsed, fx_rates=fx_rates)

    assert out["base_currency"] == "USD"
    assert out["fx_rates"]["JPY"] == 0.0064
    # 123400 JPY * 0.0064 = 789.76 ; AAPL already USD
    assert out["positions"][0]["entry_cost_usd"] == 789.76
    assert out["positions"][0]["market_value_usd"] == 800.0
    assert out["positions"][1]["entry_cost_usd"] == 2000.0
    # total deployed USD = 789.76 + 2000 = 2789.76
    assert out["total_deployed_dollars"] == 2789.76
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest scripts/tests/test_ib_sync_currency.py::test_get_fx_rates_reads_exchange_rate scripts/tests/test_ib_sync_currency.py::test_convert_to_portfolio_format_adds_usd_fields_and_totals -v`
Expected: FAIL — `AttributeError: module 'xenon.execution.ib_sync' has no attribute 'get_fx_rates'`

- [ ] **Step 3: Write minimal implementation**

Add near `get_account_summary` (after :99):

```python
def get_fx_rates(client: IBClient) -> dict:
    """USD-per-unit FX rates from IB's cached accountValues ExchangeRate rows."""
    from xenon.utils.fx import usd_per_unit_from_account_values

    return usd_per_unit_from_account_values(client.ib.accountValues())
```

Make `convert_to_portfolio_format` accept `fx_rates` and compute USD fields. Change the signature (:920-922) and add conversion. After the `for pos in collapsed_positions:` entry-date loop (around :1003), add USD enrichment, and change the totals (:928-929, :1010-1012):

```python
def convert_to_portfolio_format(
    account: dict, collapsed_positions: list, pnl_data: Optional[dict] = None,
    fill_dates: Optional[dict] = None, fx_rates: Optional[dict] = None,
) -> dict:
    from xenon.utils.fx import to_usd

    fx_rates = fx_rates or {"USD": 1.0}
    bankroll = account.get("NetLiquidation", account.get("TotalCashValue", 0))
    # ... (entry-date enrichment loop unchanged) ...

    # USD enrichment — IB returns native values; convert per position currency.
    for pos in collapsed_positions:
        cur = (pos.get("currency") or "USD").upper()
        pos["entry_cost_usd"] = to_usd(pos.get("entry_cost"), cur, fx_rates)
        pos["market_value_usd"] = to_usd(pos.get("market_value"), cur, fx_rates)
        for leg in pos.get("legs", []):
            lcur = (leg.get("currency") or cur).upper()
            leg["market_value_usd"] = to_usd(leg.get("market_value"), lcur, fx_rates)

    # Deployed totals must be in a single unit (USD). NEVER fall back to the
    # native entry_cost for a missing rate — adding a ¥-sized number into a USD
    # total corrupts it. Skip unconverted positions and surface the count so a
    # missing rate is visible rather than silently wrong.
    usd_costs = [p.get("entry_cost_usd") for p in collapsed_positions if p.get("entry_cost_usd") is not None]
    total_deployed = sum(usd_costs)
    unconverted = sum(
        1 for p in collapsed_positions
        if (p.get("currency") or "USD").upper() != "USD" and p.get("entry_cost_usd") is None
    )
    deployed_pct = (total_deployed / bankroll * 100) if bankroll > 0 else 0

    result = {
        "bankroll": round(bankroll, 2),
        "peak_value": round(bankroll, 2),
        "base_currency": "USD",
        "fx_rates": fx_rates,
        "last_sync": datetime.now().isoformat(),
        "positions": collapsed_positions,
        "total_deployed_pct": round(deployed_pct, 2),
        "total_deployed_dollars": round(total_deployed, 2),
        "remaining_capacity_pct": round(100 - deployed_pct, 2),
        "fx_unconverted_count": unconverted,  # >0 → a non-USD row lacked an FX rate
        # ... rest unchanged ...
    }
    return result
```

In `main()`, capture fx rates after the account summary (after :1229) and pass to the converter (:1367):

```python
        account = get_account_summary(client)
        fx_rates = get_fx_rates(client)
```

```python
            portfolio = convert_to_portfolio_format(account, collapsed, pnl_data, fill_dates=fill_dates, fx_rates=fx_rates)
```

> Note: import `EntryDateLookups` at module top is NOT required for the test — the test monkeypatches `load_entry_date_lookups_sync` and references `ib_sync.EntryDateLookups`, which is imported lazily inside the function today. Add `from xenon.utils.portfolio_loader import EntryDateLookups, load_entry_date_lookups_sync` to the function's existing local import block so both names are attributes of the module for monkeypatching, OR adjust the test to patch the existing local-import path. Prefer keeping the lazy import and have the test patch `xenon.utils.portfolio_loader.load_entry_date_lookups_sync` instead.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest scripts/tests/test_ib_sync_currency.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/ib_sync.py scripts/tests/test_ib_sync_currency.py
git commit -m "feat(ib-sync): compute USD per-position values + deployed totals from IB ExchangeRate"
```

### Task 1.6: Persist `currency` + `exchange` columns (schema + migration)

**Files:**

- Modify: `src/xenon/db/schema.py:36-59` (`positions` table)
- Create: `src/xenon/db/migrations/versions/2026_06_22_positions_currency_exchange.py`
- Modify: `src/xenon/execution/ib_sync.py:1118-1134` (the `insert(positions)` call) — write `currency`/`exchange`.
- Test: `scripts/tests/test_ib_sync_currency.py` (append, `@pytest.mark.committed_db` if it forks; here a direct insert test under the normal txn fixture)

**Interfaces:**

- Consumes: leg `currency` (Task 1.3), position `exchange`.
- Produces: `positions.currency TEXT NOT NULL DEFAULT 'USD'`, `positions.exchange TEXT`.

- [ ] **Step 1: Add columns to `schema.py`** (after `Column("right", Text)` at :44, add):

```python
    Column("currency", Text, nullable=False, server_default=text("'USD'")),
    Column("exchange", Text),
```

- [ ] **Step 2: Get the current head, then write the migration**

Run: `uv run alembic heads` (expected current head: `2026_06_17_futu_orders` — use whatever it prints as `down_revision`).

```python
# src/xenon/db/migrations/versions/2026_06_22_positions_currency_exchange.py
"""add currency + exchange columns to positions

Revision ID: 2026_06_22_positions_currency_exchange
Revises: 2026_06_17_futu_orders
Create Date: 2026-06-22 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "2026_06_22_positions_currency_exchange"
down_revision: Union[str, Sequence[str], None] = "2026_06_17_futu_orders"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("currency", sa.Text(), nullable=False, server_default="USD"),
        schema="xenon",
    )
    op.add_column("positions", sa.Column("exchange", sa.Text(), nullable=True), schema="xenon")


def downgrade() -> None:
    op.drop_column("positions", "exchange", schema="xenon")
    op.drop_column("positions", "currency", schema="xenon")
```

- [ ] **Step 3: Apply against the dev DB**

Run: `uv run alembic upgrade head`
Expected: `Running upgrade 2026_06_17_futu_orders -> 2026_06_22_positions_currency_exchange`

- [ ] **Step 4: Write `currency`/`exchange` in `_save_portfolio_to_postgres`**

In the `insert(positions).values(...)` block (:1118-1134), add (currency derives from the leg, falling back to the position):

```python
                conn.execute(
                    insert(positions).values(
                        ticker=ticker,
                        security_type=sec_type,
                        # ... existing fields ...
                        currency=(leg.get("currency") or pos.get("currency") or "USD").upper(),
                        exchange=pos.get("exchange"),
                        account=broker_account,
                        broker=broker,
                        account_env=account_env,
                        broker_account=broker_account,
                    )
                )
```

- [ ] **Step 5: Write the persistence test (append)**

```python
def test_save_portfolio_persists_currency(pg_test_engine):
    import os
    from sqlalchemy import select
    from xenon.db.schema import positions
    from xenon.execution import ib_sync

    os.environ["XENON_BROKER_ACCOUNT"] = "TESTACCT"
    os.environ["XENON_TRADING_MODE"] = "paper"
    os.environ.pop("XENON_READ_ONLY", None)
    portfolio = {
        "positions": [{
            "ticker": "5016", "currency": "JPY", "exchange": "TSEJ", "expiry": "N/A",
            "legs": [{"type": "Stock", "direction": "LONG", "contracts": 100,
                      "avg_cost": 1234.0, "market_price": 1250.0, "strike": None,
                      "currency": "JPY"}],
        }],
        "account_summary": {"net_liquidation": 100000.0},
        "bankroll": 100000.0, "peak_value": 100000.0,
    }
    ib_sync._save_portfolio_to_postgres(portfolio)
    with pg_test_engine.begin() as conn:
        row = conn.execute(select(positions.c.currency, positions.c.exchange).where(positions.c.ticker == "5016")).first()
    assert row.currency == "JPY"
    assert row.exchange == "TSEJ"
```

**Mark this test `@pytest.mark.committed_db`** (add the decorator). `_save_portfolio_to_postgres` builds its own engine via `get_sync_engine()` from `DATABASE_URL` — a SECOND physical connection that cannot see the autouse `BEGIN/ROLLBACK` transaction, so its INSERT commits independently. Per CLAUDE.md § Pytest infrastructure, that requires the Phase-1 TRUNCATE-pre+post semantics the marker switches on; without it the row leaks across tests. `pg_test_engine` reads via yet another connection, which sees the committed row.

- [ ] **Step 6: Run tests**

Run: `uv run pytest scripts/tests/test_ib_sync_currency.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/xenon/db/schema.py src/xenon/db/migrations/versions/2026_06_22_positions_currency_exchange.py src/xenon/execution/ib_sync.py scripts/tests/test_ib_sync_currency.py
git commit -m "feat(db): persist position currency + exchange columns"
```

---

## PHASE 2 — Realtime relay: live forex ticks + exchange-qualified foreign stock quotes

Makes the USD headline (FX) and the foreign stock price stream live, the same way US stock quotes stream.

### Task 2.1: `forexContract` builder + exchange-carrying stock subscriptions

**Files:**

- Modify: `scripts/infra/ib_realtime/ib_contracts.js`
- Test: `scripts/infra/ib_realtime/__tests__/ib_contracts.test.js` if present, else add `web/tests/ibContracts.test.ts` is NOT applicable (JS module). Add a Node test file `scripts/infra/ib_realtime/__tests__/ib_contracts.test.mjs` run via `node --test`.

**Interfaces:**

- Produces: `forexContract(base, quote, exchange = "IDEALPRO")` → `{ symbol: base, secType: SecType.CASH, currency: quote, exchange }`. `stockContract` already accepts `(symbol, exchange, currency)` — no change needed, just used positionally with all three now.

- [ ] **Step 1: Write the failing test**

```js
// scripts/infra/ib_realtime/__tests__/ib_contracts.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { SecType } from "@stoqey/ib";
import { forexContract, stockContract } from "../ib_contracts.js";

test("forexContract builds an IDEALPRO CASH contract", () => {
  const c = forexContract("USD", "JPY");
  assert.equal(c.symbol, "USD");
  assert.equal(c.currency, "JPY");
  assert.equal(c.exchange, "IDEALPRO");
  assert.equal(c.secType, SecType.CASH);
});

test("stockContract carries non-default exchange + currency", () => {
  const c = stockContract("5016", "TSEJ", "JPY");
  assert.equal(c.exchange, "TSEJ");
  assert.equal(c.currency, "JPY");
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test scripts/infra/ib_realtime/__tests__/ib_contracts.test.mjs`
Expected: FAIL — `forexContract is not a function`

- [ ] **Step 3: Write minimal implementation** (append to `ib_contracts.js`):

```js
// Forex (CASH) for live FX conversion. IB CASH contracts use symbol=base,
// currency=quote, exchange=IDEALPRO. Verified live: USD.JPY, USD.KRW quote.
export function forexContract(base, quote, exchange = "IDEALPRO") {
  return { symbol: base, secType: SecType.CASH, currency: quote, exchange };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test scripts/infra/ib_realtime/__tests__/ib_contracts.test.mjs`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/infra/ib_realtime/ib_contracts.js scripts/infra/ib_realtime/__tests__/ib_contracts.test.mjs
git commit -m "feat(relay): add forexContract builder for IDEALPRO CASH pairs"
```

### Task 2.2: Relay — parse + subscribe forex pairs and exchange-qualified stocks

**Files:**

- Modify: `scripts/infra/ib_realtime/ib_realtime_server.js` — add `normalizeForex` (mirror `normalizeIndexes` :162-175), parse `payload.forexes` in `parseActionMessage` (:208-236), add a 4th subscribe loop (after the index loop at :1603) and the unsubscribe branch (:1607-1623). Add a `stocksMeta` map so a stock subscribed with `{symbol, exchange, currency}` builds the right contract.
- Test: extend the relay's existing handler test harness if present; otherwise a focused Node test of `normalizeForex`.

**Interfaces:**

- Consumes: client `subscribe` message may carry `forexes: [{base, quote}]` and `stocks: [{symbol, exchange, currency}]` (in addition to the existing `symbols: string[]` which stays SMART/USD).
- Produces: relay subscribes `forexContract(base, quote)` keyed `"<base>.<quote>"` (e.g. `"USD.JPY"`) and emits `type: "price"` for it like any symbol; foreign stocks subscribe with their real exchange/currency keyed by bare symbol.

- [ ] **Step 1: Write the failing test** (Node test of the normalizer; export it for testability):

```js
// scripts/infra/ib_realtime/__tests__/normalize_forex.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { normalizeForex } from "../ib_realtime_server.js";

test("normalizeForex keeps valid base/quote pairs and keys them", () => {
  const out = normalizeForex([
    { base: "usd", quote: "jpy" },
    { base: "USD", quote: "" },
    "bad",
  ]);
  assert.deepEqual(out, [{ base: "USD", quote: "JPY", key: "USD.JPY" }]);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test scripts/infra/ib_realtime/__tests__/normalize_forex.test.mjs`
Expected: FAIL — `normalizeForex is not a function` (or not exported)

- [ ] **Step 3: Write minimal implementation**

Add the normalizer near `normalizeIndexes` and `export` it:

```js
export function normalizeForex(raw) {
  if (!Array.isArray(raw)) return [];
  return raw
    .map((c) => {
      if (typeof c !== "object" || c === null) return null;
      const base =
        typeof c.base === "string" ? c.base.trim().toUpperCase() : null;
      const quote =
        typeof c.quote === "string" ? c.quote.trim().toUpperCase() : null;
      if (!base || !quote) return null;
      return { base, quote, key: `${base}.${quote}` };
    })
    .filter(Boolean);
}
```

Add a `normalizeStocksMeta` normalizer next to `normalizeForex` (and `export` it):

```js
export function normalizeStocksMeta(raw) {
  if (!Array.isArray(raw)) return [];
  return raw
    .map((c) => {
      if (typeof c !== "object" || c === null) return null;
      const symbol =
        typeof c.symbol === "string" ? c.symbol.trim().toUpperCase() : null;
      const exchange =
        typeof c.exchange === "string" ? c.exchange.trim().toUpperCase() : null;
      const currency =
        typeof c.currency === "string" ? c.currency.trim().toUpperCase() : null;
      if (!symbol || !exchange || !currency) return null;
      return { symbol, exchange, currency };
    })
    .filter(Boolean);
}
```

In `parseActionMessage`, after the `indexes` parse, add `const forexes = normalizeForex(payload.forexes);` and `const stocksMeta = normalizeStocksMeta(payload.stocks);`, and include both in the returned object.

In `handleClientMessage`'s `subscribe` case, the parsed message is destructured into local `symbols`/`contracts`/`indexes` — **also bind the two new fields** (review fix — Codex ISSUE-5, conf 86: the loops below reference `forexes`/`stocksMeta`, which are undefined until bound):

```js
const forexes = parsed.forexes ?? []; // from parseActionMessage
const stocksMeta = parsed.stocksMeta ?? []; // from parseActionMessage
```

(Match the existing destructure style — if `handleClientMessage` reads `message.symbols` directly, read `message.forexes`/`message.stockMeta` from the SAME parsed object the existing loops use. Confirm the variable name (`parsed`/`message`/`msg`) at the `subscribe` case head before writing.) Then, after the index loop (:1603), add the forex loop and a foreign-stock loop:

```js
// Forex pairs (live FX conversion, e.g. USD.JPY, USD.KRW on IDEALPRO)
for (const fx of forexes) {
  const key = fx.key;
  subscribeClientToSymbol(client, key);
  const ibContract = forexContract(fx.base, fx.quote);
  ensureSymbolState(key, ibContract);
  if (ibConnected) {
    startLiveSubscription(key, ibContract);
    const state = symbolStates.get(key);
    if (state)
      sendMessage(client, { type: "price", symbol: key, data: state.data });
    subscribed.push(key);
  }
}
// Foreign stocks with explicit exchange/currency (TSEJ/JPY, KSE/KRW).
// Keyed by bare symbol like SMART/USD stocks, but built with native venue.
for (const s of stocksMeta) {
  const key = s.symbol;
  subscribeClientToSymbol(client, key);
  const ibContract = stockContract(s.symbol, s.exchange, s.currency);
  ensureSymbolState(key, ibContract);
  if (ibConnected) {
    startLiveSubscription(key, ibContract);
    const state = symbolStates.get(key);
    if (state)
      sendMessage(client, { type: "price", symbol: key, data: state.data });
    subscribed.push(key);
  }
}
```

In the `unsubscribe` case (:1607-1623), also loop `forexes`/`stocksMeta` and call `unsubscribeClientFromSymbol(client, fx.key)` / `(client, s.symbol)`.

Import `forexContract` from `ib_contracts.js` (extend the existing import at :26-30).

> Reconnect/cancel/batch all key by string and need no change (`symbolStates`/`requestIdToSymbol`/`restoreSubscriptions` are opaque to contract type). Forex CASH ticks arrive as BID/ASK → `updateDerivedLast` (ib*tick_handler.js) sets `last = mid`; `"USD.JPY"` contains `.` not `*`, so the option-stale branch is skipped. Verified no tick-handler change needed.

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test scripts/infra/ib_realtime/__tests__/normalize_forex.test.mjs`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/infra/ib_realtime/ib_realtime_server.js scripts/infra/ib_realtime/__tests__/normalize_forex.test.mjs
git commit -m "feat(relay): subscribe forex pairs + exchange-qualified foreign stocks"
```

### Task 2.3: Frontend protocol + `usePrices` forex/foreign-stock subscriptions

**Files:**

- Modify: `web/lib/pricesProtocol.ts` — add `ForexContract` type + `StockMeta` type + `forexKey()` helper + hashes.
- Modify: `web/lib/usePrices.ts` — accept `forexes?: ForexContract[]` and `stocksMeta?: StockMeta[]`; include in the subscribe diff (mirror the `indexes` handling) and the outbound `subscribe`/`unsubscribe` payloads.
- Test: `web/tests/pricesProtocol.test.ts` (forexKey) + extend `web/tests/usePrices*.test.ts` diff tests if present.

**Interfaces:**

- Produces: `ForexContract = { base: string; quote: string }`, `forexKey(c) => "<BASE>.<QUOTE>"`, `StockMeta = { symbol: string; exchange: string; currency: string }`. `usePrices` sends `{ action: "subscribe", forexes: [...], stocks: [...] }` and surfaces forex prices in the same `prices` map keyed by `"USD.JPY"`.

- [ ] **Step 1: Write the failing test**

```ts
// web/tests/pricesProtocol.test.ts  (append or new)
import { describe, it, expect } from "vitest";
import { forexKey } from "@/lib/pricesProtocol";

describe("forexKey", () => {
  it("builds BASE.QUOTE uppercased", () => {
    expect(forexKey({ base: "usd", quote: "jpy" })).toBe("USD.JPY");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run tests/pricesProtocol.test.ts`
Expected: FAIL — `forexKey` not exported

- [ ] **Step 3: Write minimal implementation** (in `pricesProtocol.ts`):

```ts
export type ForexContract = { base: string; quote: string };
export type StockMeta = { symbol: string; exchange: string; currency: string };

export function forexKey(c: ForexContract): string {
  return `${c.base.trim().toUpperCase()}.${c.quote.trim().toUpperCase()}`;
}

export function forexesKey(list: ForexContract[]): string {
  return [...list.map(forexKey)].sort().join(",");
}
```

In `usePrices.ts`: add `forexes = []`, `stocksMeta = []` to options + destructure; add `forexHash`/`stockMetaHash` memos; thread into `desiredRef`, `buildHash`, and `syncSubscriptions` so adds emit `{ action: "subscribe", forexes: addedForexes.map(({base,quote})=>({base,quote})), stocks: addedStocks }` and removes emit `{ action: "unsubscribe", symbols: [...removedForexKeys, ...removedStockSymbols] }`. (Forex keys + foreign-stock symbols evict from `prices` like any removed symbol.) Keep the existing `symbols`/`contracts`/`indexes` behavior intact.

> **De-risk (Pass 6) — the hash-diff parser is the trap.** `syncSubscriptions` packs state into one `"|"`-joined string and splits it back with `const [lastSyms, lastCts, lastIdxs] = lastSentHashRef.current.split("|")` (usePrices.ts ~:356). Adding two subscription types means **`buildHash` must append two more `"|"` segments AND the destructure must take five** (`[lastSyms, lastCts, lastIdxs, lastFx, lastStocks]`), or the diff silently corrupts. Forex diffs on `forexKey` (`"USD.JPY"`); foreign stocks diff on bare symbol. The cleanest lower-risk alternative is to track forex + stocksMeta in a SEPARATE ref+diff (mirroring the dedicated `desiredDepthRef`/`syncDepth` path already in the file) rather than extending the 3-segment string — prefer that if the segment surgery feels fragile. Add a Vitest diff test covering add-forex / remove-forex / add-foreign-stock before wiring the socket.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx vitest run tests/pricesProtocol.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/lib/pricesProtocol.ts web/lib/usePrices.ts web/tests/pricesProtocol.test.ts
git commit -m "feat(prices): subscribe forex + foreign-stock quotes from the client"
```

### Task 2.4: `useFx` hook — derive `usd_per_unit` from live ticks + payload fallback

**Files:**

- Create: `web/lib/fx.ts`, `web/lib/useFx.ts`
- Test: `web/tests/fx.test.ts`

**Interfaces:**

- Produces (in `fx.ts`):
  - `toUsd(amount: number | null, currency: string | null, usdPerUnit: Record<string, number>): number | null`
  - `usdPerUnitFromForexTick(pairKey: string, last: number | null): { currency: string; rate: number } | null` — for `"USD.JPY"` last=161.5 → `{ currency: "JPY", rate: 1/161.5 }`.
  - `fmtNative(amount: number | null, currency: string): string` — `Intl.NumberFormat` per currency (JPY/KRW = 0 fraction digits).
- Produces (in `useFx.ts`):
  - `useFx(prices: Record<string, PriceData>, fallback: Record<string, number>, currencies: string[]): Record<string, number>` — merges live tick-derived rates over the payload fallback for the currencies present.

- [ ] **Step 1: Write the failing test**

```ts
// web/tests/fx.test.ts
import { describe, it, expect } from "vitest";
import { toUsd, usdPerUnitFromForexTick, fmtNative } from "@/lib/fx";

describe("toUsd", () => {
  it("is identity for USD", () =>
    expect(toUsd(1234, "USD", { USD: 1 })).toBe(1234));
  it("converts native with rate", () =>
    expect(toUsd(123400, "JPY", { USD: 1, JPY: 0.0064 })!).toBeCloseTo(
      789.76,
      2,
    ));
  it("returns null when rate missing", () =>
    expect(toUsd(100, "KRW", { USD: 1 })).toBeNull());
  it("returns null for null amount", () =>
    expect(toUsd(null, "JPY", { JPY: 0.0064 })).toBeNull());
});

describe("usdPerUnitFromForexTick", () => {
  it("inverts the JPY-per-USD tick into USD-per-JPY", () => {
    const r = usdPerUnitFromForexTick("USD.JPY", 161.5)!;
    expect(r.currency).toBe("JPY");
    expect(r.rate).toBeCloseTo(1 / 161.5, 8);
  });
  it("returns null for non-USD-base pairs or bad ticks", () => {
    expect(usdPerUnitFromForexTick("USD.JPY", 0)).toBeNull();
    expect(usdPerUnitFromForexTick("EUR.JPY", 1.2)).toBeNull();
  });
});

describe("fmtNative", () => {
  it("formats JPY with no decimals + symbol", () => {
    expect(fmtNative(123400, "JPY")).toContain("123,400");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run tests/fx.test.ts`
Expected: FAIL — module not found

- [ ] **Step 3: Write minimal implementation**

```ts
// web/lib/fx.ts
/** Native-currency → USD conversion (mirror of src/xenon/utils/fx.py).
 *  usdPerUnit[cur] = USD value of 1 unit of `cur`, so usd = native * rate. */
export function toUsd(
  amount: number | null,
  currency: string | null,
  usdPerUnit: Record<string, number>,
): number | null {
  if (amount == null || !Number.isFinite(amount)) return null;
  const cur = (currency || "USD").toUpperCase();
  if (cur === "USD") return amount;
  const rate = usdPerUnit[cur];
  if (rate == null || !(rate > 0)) return null;
  return amount * rate;
}

/** Forex tick "USD.JPY"=161.5 is JPY-per-USD → invert to USD-per-JPY. Only
 *  USD-base pairs are usable for converting a native amount to USD here. */
export function usdPerUnitFromForexTick(
  pairKey: string,
  last: number | null,
): { currency: string; rate: number } | null {
  const [base, quote] = pairKey.split(".");
  if (base !== "USD" || !quote) return null;
  if (last == null || !(last > 0)) return null;
  return { currency: quote, rate: 1 / last };
}

const ZERO_DECIMAL = new Set(["JPY", "KRW"]);

export function fmtNative(amount: number | null, currency: string): string {
  if (amount == null || !Number.isFinite(amount)) return "---";
  const cur = (currency || "USD").toUpperCase();
  try {
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: cur,
      maximumFractionDigits: ZERO_DECIMAL.has(cur) ? 0 : 2,
    }).format(amount);
  } catch {
    return `${amount.toLocaleString("en-US")} ${cur}`;
  }
}
```

```ts
// web/lib/useFx.ts
import { useMemo } from "react";
import type { PriceData } from "@/lib/pricesProtocol";
import { usdPerUnitFromForexTick } from "@/lib/fx";

/** Merge live forex-tick-derived USD-per-unit rates over a payload fallback,
 *  for the set of currencies present in the portfolio. USD is always 1. */
export function useFx(
  prices: Record<string, PriceData>,
  fallback: Record<string, number>,
  currencies: string[],
): Record<string, number> {
  return useMemo(() => {
    const out: Record<string, number> = { USD: 1, ...(fallback || {}) };
    for (const cur of currencies) {
      if (cur === "USD") continue;
      const live = usdPerUnitFromForexTick(
        `USD.${cur}`,
        prices[`USD.${cur}`]?.last ?? null,
      );
      if (live) out[live.currency] = live.rate;
    }
    return out;
  }, [prices, fallback, currencies.join(",")]);
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx vitest run tests/fx.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/lib/fx.ts web/lib/useFx.ts web/tests/fx.test.ts
git commit -m "feat(fx): frontend USD conversion + live-tick rate hook"
```

---

## PHASE 3 — Frontend display: native price + live-USD headline + FX badge

### Task 3.1: Extend portfolio schema + types with currency/USD fields

**Files:**

- Modify: `web/lib/portfolioDataSchema.ts` — add `currency` + `entry_cost_usd`/`market_value_usd` to leg + position, `base_currency` + `fx_rates` to payload.
- Modify: `web/lib/types.ts` — mirror in `PortfolioLeg`, `PortfolioPosition`, `PortfolioData`.
- Test: `web/tests/portfolioDataSchema.test.ts` (parse a JPY payload).

**Interfaces:**

- Produces: `PortfolioLeg.currency?: string`, `PortfolioLeg.market_value_usd?: number | null`; `PortfolioPosition.currency?: string`, `.exchange?: string | null`, `.entry_cost_usd?: number | null`, `.market_value_usd?: number | null`; `PortfolioData.base_currency?: string`, `.fx_rates?: Record<string, number>`. All OPTIONAL so existing USD payloads validate unchanged.

- [ ] **Step 1: Write the failing test**

```ts
// web/tests/portfolioDataSchema.test.ts (append)
import { Value } from "@sinclair/typebox/value";
import { PortfolioDataSchema } from "@/lib/portfolioDataSchema";

it("accepts a JPY position with USD fields + fx_rates", () => {
  const payload = {
    bankroll: 100000,
    peak_value: 100000,
    last_sync: "2026-06-22T00:00:00Z",
    base_currency: "USD",
    fx_rates: { USD: 1, JPY: 0.0064 },
    positions: [
      {
        id: 1,
        ticker: "5016",
        currency: "JPY",
        exchange: "TSEJ",
        structure: "Stock (100 shares)",
        structure_type: "Stock",
        risk_profile: "equity",
        expiry: "N/A",
        contracts: 100,
        direction: "LONG",
        entry_cost: 123400,
        entry_cost_usd: 789.76,
        max_risk: null,
        market_value: 125000,
        market_value_usd: 800,
        legs: [
          {
            direction: "LONG",
            contracts: 100,
            type: "Stock",
            currency: "JPY",
            strike: null,
            entry_cost: 123400,
            avg_cost: 1234,
            market_price: 1250,
            market_value: 125000,
            market_value_usd: 800,
          },
        ],
        kelly_optimal: null,
        target: null,
        stop: null,
        entry_date: "unknown",
      },
    ],
    total_deployed_pct: 0.79,
    total_deployed_dollars: 789.76,
    remaining_capacity_pct: 99.21,
    position_count: 1,
    defined_risk_count: 1,
    undefined_risk_count: 0,
    avg_kelly_optimal: null,
  };
  expect(Value.Check(PortfolioDataSchema, payload)).toBe(true);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run tests/portfolioDataSchema.test.ts`
Expected: FAIL — unknown property `currency`/`fx_rates`

- [ ] **Step 3: Write minimal implementation**

In `portfolioDataSchema.ts`, add to `PortfolioLegSchema`: `currency: Type.Optional(Type.String())`, `market_value_usd: Type.Optional(Type.Union([Type.Number(), Type.Null()]))`. To `PortfolioPositionSchema`: `currency: Type.Optional(Type.String())`, `exchange: Type.Optional(Type.Union([Type.String(), Type.Null()]))`, `entry_cost_usd: Type.Optional(Type.Union([Type.Number(), Type.Null()]))`, `market_value_usd: Type.Optional(Type.Union([Type.Number(), Type.Null()]))`. To `PortfolioDataSchema`: `base_currency: Type.Optional(Type.String())`, `fx_rates: Type.Optional(Type.Record(Type.String(), Type.Number()))`.

Mirror the same optional fields in `web/lib/types.ts` (`PortfolioLeg`, `PortfolioPosition`, `PortfolioData`).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx vitest run tests/portfolioDataSchema.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/lib/portfolioDataSchema.ts web/lib/types.ts web/tests/portfolioDataSchema.test.ts
git commit -m "feat(portfolio): schema + types for currency, USD values, fx_rates"
```

### Task 3.2: FxBadge component

**Files:**

- Create: `web/components/FxBadge.tsx`, `web/tests/fx-badge.test.ts`

**Interfaces:**

- Consumes: `rates: Record<string, number>` (usd_per_unit), `liveCurrencies?: string[]` (currencies with a fresh `USD.<cur>` tick).
- Produces: `<FxBadge rates={...} liveCurrencies={...} />` — one capsule per non-USD currency showing the human pair (USD/JPY = 1/rate) + a per-pair live/snapshot dot. Brand: 999px capsule, tokenized colors.

- [ ] **Step 1: Write the failing test**

```ts
// web/tests/fx-badge.test.ts
import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import FxBadge from "@/components/FxBadge";

describe("FxBadge", () => {
  it("shows USD/JPY as JPY-per-USD (inverted from usd_per_unit)", () => {
    render(<FxBadge rates={{ USD: 1, JPY: 0.0064 }} liveCurrencies={["JPY"]} />);
    // 1/0.0064 ≈ 156
    expect(screen.getByText(/USD\/JPY/)).toBeTruthy();
    expect(screen.getByText(/156/)).toBeTruthy();
  });
  it("marks a pair as snapshot (not live) when not in liveCurrencies", () => {
    const { container } = render(<FxBadge rates={{ USD: 1, KRW: 0.00073 }} liveCurrencies={[]} />);
    expect(container.querySelector(".fx-dot-live")).toBeNull(); // hollow dot
    expect(screen.getByText(/USD\/KRW/)).toBeTruthy();
  });
  it("renders nothing when only USD present", () => {
    const { container } = render(<FxBadge rates={{ USD: 1 }} liveCurrencies={[]} />);
    expect(container.textContent).toBe("");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run tests/fx-badge.test.ts`
Expected: FAIL — module not found

- [ ] **Step 3: Write minimal implementation**

```tsx
// web/components/FxBadge.tsx
"use client";

/** Live FX badge: one capsule per non-USD currency, shown as the human pair
 *  USD/XXX (= 1 / usd_per_unit). Filled dot = live IDEALPRO tick for THAT pair,
 *  hollow = snapshot/fallback rate. Liveness is per-currency, not global. */
export default function FxBadge({
  rates,
  liveCurrencies = [],
}: {
  rates: Record<string, number>;
  liveCurrencies?: string[];
}) {
  const liveSet = new Set(liveCurrencies.map((c) => c.toUpperCase()));
  const pairs = Object.entries(rates)
    .filter(([cur, rate]) => cur !== "USD" && rate > 0)
    .map(([cur, rate]) => ({
      cur,
      perUsd: 1 / rate,
      live: liveSet.has(cur.toUpperCase()),
    }));
  if (pairs.length === 0) return null;
  return (
    <span className="fx-badge-group">
      {pairs.map(({ cur, perUsd, live }) => (
        <span
          key={cur}
          className="fx-badge"
          title={live ? "Live IDEALPRO" : "Snapshot rate"}
        >
          <span
            className={live ? "fx-dot fx-dot-live" : "fx-dot"}
            aria-hidden
          />
          {`USD/${cur} ${perUsd.toLocaleString("en-US", { maximumFractionDigits: cur === "KRW" ? 1 : 2 })}`}
        </span>
      ))}
    </span>
  );
}
```

Add minimal styles to the position panel CSS module / globals using brand tokens (999px radius capsule, mono font). Reuse existing badge classes if present (search `PositionTable` for an existing capsule class and prefer it).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx vitest run tests/fx-badge.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/components/FxBadge.tsx web/tests/fx-badge.test.ts
git commit -m "feat(ui): FxBadge live USD/JPY · USD/KRW capsule"
```

### Task 3.3: PositionTable — native sub-line + live USD headline + badge

**Files:**

- Modify: `web/components/PositionTable.tsx` — `PositionTable` already receives `prices?: Record<string, PriceData>` and `positions: PortfolioPosition[]` as props (signature at :604-630) and does NOT call `usePrices` itself (that lives in `WorkspaceShell`, see Task 3.4). For non-USD rows, render the native price/MV (via `fmtNative`) and a USD headline derived live (`toUsd(nativeMV, currency, usdPerUnit)`, falling back to `market_value_usd` from payload); render `<FxBadge>` in the table header.
- Test: `web/tests/position-table-currency.test.tsx`

**Interfaces:**

- Consumes: `PortfolioPosition.currency`/`.exchange`/`.market_value_usd`, the existing `prices` prop (now also carrying `"USD.JPY"`/`"USD.KRW"` ticks once Task 3.4 subscribes them), a new `fxRates?: Record<string, number>` prop, plus `useFx`, `toUsd`, `fmtNative`, `FxBadge`.
- Produces: a non-USD row shows e.g. `¥125,000 ≈ $800` for MV and the USD figure as the sortable/headline number; USD rows are visually unchanged. `useFx` is a `useMemo` hook — fine to call inside `PositionTable` against the `prices` prop.

- [ ] **Step 1: Write the failing test**

```tsx
// web/tests/position-table-currency.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import PositionTable from "@/components/PositionTable";

// PositionTable does NOT call usePrices — it receives `prices` as a prop
// (WorkspaceShell owns the WS hook). So we pass the forex tick directly.

const jpyPos = {
  id: 1,
  ticker: "5016",
  currency: "JPY",
  exchange: "TSEJ",
  structure: "Stock (100 shares)",
  structure_type: "Stock",
  risk_profile: "equity",
  expiry: "N/A",
  contracts: 100,
  direction: "LONG",
  entry_cost: 123400,
  entry_cost_usd: 789.76,
  max_risk: null,
  market_value: 125000,
  market_value_usd: 800,
  legs: [
    {
      direction: "LONG",
      contracts: 100,
      type: "Stock",
      currency: "JPY",
      strike: null,
      entry_cost: 123400,
      avg_cost: 1234,
      market_price: 1250,
      market_value: 125000,
      market_value_usd: 800,
    },
  ],
  kelly_optimal: null,
  target: null,
  stop: null,
  entry_date: "unknown",
};

describe("PositionTable currency", () => {
  it("renders native JPY value and a USD headline for a TSEJ row", () => {
    render(
      <PositionTable
        positions={[jpyPos as any]}
        prices={{ "USD.JPY": { symbol: "USD.JPY", last: 156.25 } as any }}
        fxRates={{ USD: 1, JPY: 0.0064 }}
      />,
    );
    expect(screen.getByText(/¥125,000|125,000/)).toBeTruthy(); // native
    // live USD = 125000 / 156.25 ≈ 800 (live tick; payload fallback 0.0064 agrees)
    expect(screen.getByText(/\$800/)).toBeTruthy();
  });
});
```

(Confirm `PositionTable`'s prop names against :604-630 before writing — `positions`, `prices`, plus the new optional `fxRates` sourced from `portfolio.fx_rates`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run tests/position-table-currency.test.tsx`
Expected: FAIL — USD headline not rendered / `fxRates` prop unknown.

- [ ] **Step 3: Write minimal implementation**

- Add an optional `fxRates?: Record<string, number>` prop (default `{ USD: 1 }`), threaded from `WorkspaceShell` (Task 3.4). PositionTable does NOT subscribe anything — it reads the `prices` prop it already receives.
- `const currencies = useMemo(() => [...new Set(positions.map(p => (p.currency || "USD").toUpperCase()))], [positions]);`
- `const usdPerUnit = useFx(prices ?? {}, fxRates ?? { USD: 1 }, currencies);`
- In the row/leg renderer, when `pos.currency && pos.currency !== "USD"`: compute `nativeMV = getDisplayMarketValue(pos, prices)` (native units — for a foreign stock this reads `prices[ticker].last`, which is now the live native JPY/KRW quote from Task 3.4) and `usdMV = toUsd(nativeMV, pos.currency, usdPerUnit) ?? pos.market_value_usd ?? null`; render headline `fmtUsd(usdMV)` with a `fmtNative(nativeMV, pos.currency)` sub-line. USD rows keep the current `fmtUsd(...)` path unchanged.
- **Sort key:** `makePositionExtract`/`useSort` (:174, :631) currently sorts by native market value — a foreign row would sort by its raw ¥/₩ magnitude and float to the top wrongly. In `makePositionExtract`, convert the market-value sort key to USD for non-USD rows: `toUsd(getDisplayMarketValue(pos, prices), pos.currency, usdPerUnit) ?? pos.market_value_usd ?? 0`. (Pass `usdPerUnit` into `makePositionExtract`.)
- Render the badge with **per-currency** liveness (review fix — Codex ISSUE-12, conf 82: a single `live` boolean lit up whenever any price existed, even with `USD.JPY` absent/stale). Compute `const liveCurrencies = currencies.filter((c) => c !== "USD" && (prices?.[`USD.${c}`]?.last ?? null) != null);` and pass `<FxBadge rates={usdPerUnit} liveCurrencies={liveCurrencies} />` (skip when `hideHeader`). FxBadge marks a filled dot only for currencies in `liveCurrencies`, hollow (snapshot/fallback) otherwise. Per-pair freshness (stale-after-N-seconds) is a follow-up — `live = tick present` is the v1 bar.

> Keep `positionUtils.ts` untouched — it computes native-unit values; conversion happens here at the display edge. Header aggregate totals that must be USD should sum `toUsd(getDisplayMarketValue(pos, prices), pos.currency, usdPerUnit) ?? pos.market_value_usd`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx vitest run tests/position-table-currency.test.tsx`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/components/PositionTable.tsx web/tests/position-table-currency.test.tsx
git commit -m "feat(ui): native price + live USD headline + FX badge for foreign positions"
```

### Task 3.4: WorkspaceShell — subscribe forex + foreign stocks, thread fxRates down

**Files:**

- Modify: `web/components/WorkspaceShell.tsx` — this is where `usePrices(...)` is called (import :18) and where `portfolioSymbols` (:100-102), `portfolioContracts` (:105-115) are built. Add forex + foreign-stock subscriptions and pass `fxRates` to `PositionTable`.
- Modify: `web/components/PositionTable.tsx` — already takes the new `fxRates` prop (Task 3.3).
- Test: `web/tests/workspace-shell-fx.test.tsx` (assert the derived forexes/stocksMeta from a JPY+KRW+USD portfolio) — or, if WorkspaceShell is too heavy to render in jsdom, extract the derivation into a pure helper `deriveFxSubscriptions(positions)` in `web/lib/fx.ts` and unit-test that.

**Interfaces:**

- Produces (pure helper, testable): `deriveFxSubscriptions(positions: {ticker:string; currency?:string; exchange?:string|null}[]) => { usdSymbols: string[]; forexes: ForexContract[]; stocksMeta: StockMeta[] }`.
  - `usdSymbols` = tickers of USD positions (these keep going through the existing SMART/USD `symbols` path).
  - `forexes` = one `{base:"USD", quote:<cur>}` per distinct non-USD currency.
  - `stocksMeta` = `{symbol, exchange, currency}` per non-USD position (so its quote streams from the native venue, not a failing SMART/USD subscription).

- [ ] **Step 1: Write the failing test**

```ts
// web/tests/fx.test.ts (append) — pure helper, no React render needed
import { deriveFxSubscriptions } from "@/lib/fx";

it("splits USD vs foreign positions into the right subscription buckets", () => {
  const out = deriveFxSubscriptions([
    { ticker: "AAPL", currency: "USD", exchange: "SMART" },
    { ticker: "5016", currency: "JPY", exchange: "TSEJ" },
    { ticker: "000660", currency: "KRW", exchange: "KSE" },
  ]);
  expect(out.usdSymbols).toEqual(["AAPL"]);
  expect(out.forexes).toEqual([
    { base: "USD", quote: "JPY" },
    { base: "USD", quote: "KRW" },
  ]);
  expect(out.stocksMeta).toEqual([
    { symbol: "5016", exchange: "TSEJ", currency: "JPY" },
    { symbol: "000660", exchange: "KSE", currency: "KRW" },
  ]);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run tests/fx.test.ts`
Expected: FAIL — `deriveFxSubscriptions` not exported

- [ ] **Step 3: Implement the helper + wire WorkspaceShell**

Add to `web/lib/fx.ts`:

```ts
import type { ForexContract, StockMeta } from "@/lib/pricesProtocol";

export function deriveFxSubscriptions(
  positions: { ticker: string; currency?: string; exchange?: string | null }[],
): { usdSymbols: string[]; forexes: ForexContract[]; stocksMeta: StockMeta[] } {
  const usdSymbols: string[] = [];
  const stocksMeta: StockMeta[] = [];
  const quotes = new Set<string>();
  for (const p of positions) {
    const cur = (p.currency || "USD").toUpperCase();
    if (cur === "USD") {
      usdSymbols.push(p.ticker);
    } else if (p.exchange) {
      stocksMeta.push({
        symbol: p.ticker,
        exchange: p.exchange,
        currency: cur,
      });
      quotes.add(cur);
    }
    // A non-USD position WITHOUT an exchange can't stream live (no venue) —
    // it still converts via the payload fx_rates fallback; we just don't
    // subscribe a foreign quote for it.
    else {
      quotes.add(cur);
    }
  }
  const forexes = [...quotes].map((quote) => ({ base: "USD", quote }));
  return { usdSymbols, forexes, stocksMeta };
}
```

In `WorkspaceShell.tsx`: derive `const { usdSymbols, forexes, stocksMeta } = useMemo(() => deriveFxSubscriptions(portfolio?.positions ?? []), [portfolio?.positions]);`. Change `portfolioSymbols` (:100-102) to use `usdSymbols` instead of mapping ALL tickers (so foreign tickers no longer double-subscribe as SMART/USD and fail to quote). Pass `forexes={forexes}` and `stocksMeta={stocksMeta}` into the `usePrices(...)` call (the options added in Task 2.3). Pass `fxRates={portfolio?.fx_rates}` to every `<PositionTable .../>` (and `PortfolioByStructure`, which forwards to PositionTable). NAV/deployed cards already read USD values from Phase 1 — no math change.

> **Watch (WorkspaceShell:222):** `portfolioSymbols` is merged into the WS symbol set via `[...new Set([...portfolioSymbols, ...orderSymbols, ...tickerSymbols])]`. Excluding foreign tickers from `portfolioSymbols` is correct, but if `orderSymbols`/`tickerSymbols` ever re-introduce a foreign ticker (e.g. a foreign symbol open in the order panel or a ticker page), it would re-subscribe as bare SMART/USD and fail to quote. For this feature's portfolio-display scope that path isn't exercised; if foreign ticker pages are added later, route those through `stocksMeta` too. Add a regression note rather than over-building now.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx vitest run tests/fx.test.ts`
Expected: PASS

- [ ] **Step 5: Run the full web suite**

Run: `cd web && npm test && npm run typecheck`
Expected: PASS / clean

- [ ] **Step 6: Commit**

```bash
git add web/lib/fx.ts web/components/WorkspaceShell.tsx web/components/PositionTable.tsx web/components/PortfolioByStructure.tsx web/tests/fx.test.ts
git commit -m "feat(ui): subscribe forex + foreign-stock quotes in WorkspaceShell, thread fx_rates"
```

### Task 3.5: Convert the OTHER USD-displaying surfaces + surface missing-FX

> **Found in review (Codex ISSUE-3 conf 90 + ISSUE-9 conf 86):** PositionTable is not the only consumer that formats native values as USD. `PortfolioByStructure` (per-structure aggregate MV/P&L), the ticker-detail position/book panels (`BookTab.tsx`, `OrderTab.tsx` position summary), and any structure-aggregate helper still sum/format native JPY/KRW as `$`. And a foreign position with NO FX rate is silently dropped from `total_deployed_dollars` (only `fx_unconverted_count` flags it) — the UI must SHOW that, not hide it.

**Files:**

- Modify: `web/components/PortfolioByStructure.tsx` — pass `fxRates` through (already in Task 3.4) AND convert per-structure aggregate MV/P&L to USD via `toUsd(nativeAgg, pos.currency, usdPerUnit)` before `fmtUsd`. For a foreign Stock card this is a 1-row aggregate, so reuse the PositionTable conversion path.
- Modify: ticker-detail position summary in `web/components/ticker-detail/BookTab.tsx` / `OrderTab.tsx` where a held foreign position's MV/P&L is shown — convert to USD (native sub-line + USD headline) using the same `toUsd`/`fmtNative` helpers.
- Modify: `web/components/MetricCards.tsx` — add a small warning chip when `portfolio.fx_unconverted_count > 0` ("N position(s) missing FX rate — excluded from USD totals"). Account-level NAV/cash stay as-is (IB already consolidated them).
- Test: `web/tests/portfolio-by-structure-currency.test.tsx` (foreign stock card shows USD) + extend `web/tests/metric-cards.test.tsx` (warning chip when `fx_unconverted_count>0`).

**Interfaces:**

- Consumes: `toUsd`, `fmtNative`, `useFx` outputs, `portfolio.fx_unconverted_count`.
- Produces: every surface that displays a foreign position's money value shows USD (native sub-line where space allows); a visible warning when any foreign row lacks an FX rate. Order entry itself does NOT require FX (orders are in native price) — do not block placement on missing FX; only the USD _display_ is affected.

- [ ] **Step 1: Write the failing test** — render `PortfolioByStructure` with the JPY fixture from Task 3.3 + a `USD.JPY` price; assert the structure card shows the USD headline (`$800`), not `¥125,000` formatted with `$`. Add a MetricCards test asserting the warning chip appears for `fx_unconverted_count: 1`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the conversions + warning chip. Prefer a shared helper (e.g. extend `positionUtils` with a thin `displayUsd(pos, prices, usdPerUnit)` that wraps `getDisplayMarketValue` + `toUsd`) so PositionTable, PortfolioByStructure, and the detail panels share ONE conversion path (DRY).
- [ ] **Step 4: Run** → PASS; `cd web && npm test && npm run typecheck`.
- [ ] **Step 5: Commit**

```bash
git add web/components/PortfolioByStructure.tsx web/components/MetricCards.tsx web/components/ticker-detail/BookTab.tsx web/components/ticker-detail/OrderTab.tsx web/lib/positionUtils.ts web/tests/portfolio-by-structure-currency.test.tsx web/tests/metric-cards.test.tsx
git commit -m "feat(ui): convert all foreign-position surfaces to USD + missing-FX warning"
```

---

## PHASE 4 — Order placement: exchange/currency-aware stock orders

Enables placing TSEJ/KSE limit orders the same way as US stocks.

### Task 4.0: Preflight — allow foreign cash equities through the universe gate (BLOCKER)

> **Found in review (Codex, conf 95; verified):** `preflight.evaluate()` (`src/xenon/execution/preflight.py:359`) rejects any ticker failing `is_known()` with `UNIVERSE_UNKNOWN` **before** the BUY-accept branch (:374). The V1 universe (`src/xenon/execution/universe.py`) is 9 US symbols, so `5016`/`000660` are rejected at the API gate (`_run_preflight` in `server.py`, called from `_orders_place_from_body`) and never reach IB. **Without this task, no foreign order can be placed — Phase 4 is non-functional.** This task must land before 4.1–4.3 are meaningful.

**Files:**

- Modify: `src/xenon/execution/preflight.py` — `PreflightRequest` (:60-70) gains `currency: str = "USD"` + `exchange: str | None = None`; `evaluate()` (:342) bypasses the universe gate for a **foreign cash equity** (`security_type == "STK"` and `currency != "USD"`), keeping BUY-accept (②) and SELL-coverage (③) intact.
- Modify: `src/xenon/api/server.py:1606` (`_body_to_preflight_request`) — populate `currency`/`exchange` from the order body.
- Test: `scripts/tests/test_preflight_foreign.py`

**Interfaces:**

- Consumes: order body `currency`/`exchange` (Task 4.2).
- Produces: foreign-STK BUY → `accept=True`; foreign-STK SELL with no shares → still BLOCKED (`INSUFFICIENT_SHARES`); US tickers unchanged (universe gate still applies); index/option logic untouched.

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_preflight_foreign.py
from xenon.execution.preflight import PreflightRequest, evaluate
from xenon.execution.preflight import PortfolioView  # adjust import to actual location


def _view(positions=None):
    return PortfolioView(positions=positions or [])


def test_foreign_stock_buy_bypasses_universe():
    req = PreflightRequest(ticker="5016", security_type="STK", action="BUY", quantity=100, currency="JPY", exchange="TSEJ")
    v = evaluate(req, _view())
    assert v.accept is True


def test_foreign_stock_sell_without_shares_still_blocked():
    req = PreflightRequest(ticker="000660", security_type="STK", action="SELL", quantity=10, currency="KRW", exchange="KSE")
    v = evaluate(req, _view())
    assert v.accept is False
    assert v.reason_code.value == "INSUFFICIENT_SHARES" or v.reason_code == __import__("xenon.execution.preflight", fromlist=["ReasonCode"]).ReasonCode.INSUFFICIENT_SHARES


def test_unknown_us_ticker_still_rejected():
    req = PreflightRequest(ticker="ZZZZ", security_type="STK", action="BUY", quantity=1, currency="USD")
    v = evaluate(req, _view())
    assert v.accept is False  # universe gate still applies to USD tickers
```

(Confirm the exact `PreflightRequest`/`PortfolioView`/`ReasonCode` constructors at :60-110 before finalizing the test.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest scripts/tests/test_preflight_foreign.py -v`
Expected: FAIL — `PreflightRequest` has no `currency` field / foreign BUY rejected as `UNIVERSE_UNKNOWN`

- [ ] **Step 3: Write minimal implementation**

Add fields to `PreflightRequest` (dataclass at :60-70): `currency: str = "USD"` and `exchange: str | None = None`. In `evaluate()`, change the universe gate (:358-364) to skip foreign cash equities:

```python
    # ① Universe — V1 universe is US-only. Foreign cash equities (non-USD STK)
    # are out of that universe by definition; gate them on currency instead.
    is_foreign_equity = req.security_type == "STK" and (req.currency or "USD").upper() != "USD"
    if not is_foreign_equity and not is_known(req.ticker):
        return Verdict(
            accept=False,
            reason_code=ReasonCode.UNIVERSE_UNKNOWN,
            reason_detail=f"{req.ticker} not in V1 universe",
        )
```

(The `is_index` check at :366 is unreachable for foreign equities since indices are USD; BUY-accept ② and SELL-coverage ③ run unchanged.)

In `server.py::_body_to_preflight_request` (:1606), pass `currency=str(body.get("currency") or "USD").upper()` and `exchange=body.get("exchange")` into the `PreflightRequest(...)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest scripts/tests/test_preflight_foreign.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/preflight.py src/xenon/api/server.py scripts/tests/test_preflight_foreign.py
git commit -m "feat(preflight): allow foreign cash equities past the US-only universe gate"
```

### Task 4.1: `ib_place_order` builds `Stock(symbol, exchange, currency)` from body

**Files:**

- Modify: `src/xenon/execution/ib_place_order.py:100-105` (the stock branch)
- Test: `scripts/tests/test_ib_place_order_contract.py`

**Interfaces:**

- Consumes: order body keys `exchange` (default `"SMART"`), `currency` (default `"USD"`).
- Produces: stock branch builds `Stock(symbol, exchange, currency)` then qualifies.

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_ib_place_order_contract.py
from xenon.execution.ib_place_order import _build_stock_contract  # to be extracted


def test_build_stock_contract_defaults_to_smart_usd():
    c = _build_stock_contract({"symbol": "AAPL"})
    assert c.symbol == "AAPL" and c.exchange == "SMART" and c.currency == "USD"


def test_build_stock_contract_uses_body_exchange_currency():
    c = _build_stock_contract({"symbol": "5016", "exchange": "TSEJ", "currency": "JPY"})
    assert c.symbol == "5016" and c.exchange == "TSEJ" and c.currency == "JPY"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest scripts/tests/test_ib_place_order_contract.py -v`
Expected: FAIL — `_build_stock_contract` not defined

- [ ] **Step 3: Write minimal implementation**

Extract a small helper and use it in `place_order` (replaces the bare `Stock(symbol, "SMART", "USD")` at :101). Note `symbol` in `place_order` is `.upper()`d at :32 — pass the raw body symbol uppercased; digit-only tickers (5016/000660) are unaffected by `.upper()`.

```python
def _build_stock_contract(params: dict) -> "Stock":
    symbol = str(params["symbol"]).upper()
    exchange = str(params.get("exchange") or "SMART").upper()
    currency = str(params.get("currency") or "USD").upper()
    return Stock(symbol, exchange, currency)
```

In `place_order`, the `else` (stock) branch:

```python
        else:
            contract = _build_stock_contract(params)
            qualified = client.qualify_contracts(contract)
            if not qualified:
                return {"status": "error", "message": f"Could not qualify contract: {symbol}"}
            contract = qualified[0]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest scripts/tests/test_ib_place_order_contract.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/ib_place_order.py scripts/tests/test_ib_place_order_contract.py
git commit -m "feat(orders): build foreign stock contract with body exchange/currency"
```

### Task 4.1b: Quote path — `_contract_from_order_body` honors exchange/currency

> **Found in review (Claude, codebase-aware):** `_contract_from_order_body` (`server.py:1854-1865`) builds `Stock(symbol, "SMART", "USD")` and feeds BOTH `_fetch_order_quote_snapshot_with_client` (`:1872`, the `GET /orders/quote` path) and `_qualify_order_con_id_with_client` (`:1885`). When the order ticket opens on a foreign position it fetches a quote/qualifies via this helper, so without this fix the 5016/000660 order ticket price prefill fails (wrong currency/venue). The PLACE path is unaffected (it forwards the body verbatim to the subprocess — Task 4.1), but the QUOTE path is not.

**Files:**

- Modify: `src/xenon/api/server.py:1854-1865` (`_contract_from_order_body`).
- Test: `src/xenon/api/tests/test_order_quote_contract.py`

**Interfaces:**

- Consumes: body `exchange` (default `"SMART"`), `currency` (default `"USD"`).
- Produces: stock branch builds `Stock(symbol, exchange, currency)`; option branch unchanged (US/index options only — out of scope for foreign).

- [ ] **Step 1: Write the failing test**

```python
# src/xenon/api/tests/test_order_quote_contract.py
from xenon.api.server import _contract_from_order_body


def test_contract_from_body_stock_defaults_smart_usd():
    c = _contract_from_order_body({"type": "stock", "symbol": "aapl"})
    assert c.symbol == "AAPL" and c.exchange == "SMART" and c.currency == "USD"


def test_contract_from_body_foreign_stock_uses_exchange_currency():
    c = _contract_from_order_body({"type": "stock", "symbol": "5016", "exchange": "TSEJ", "currency": "JPY"})
    assert c.symbol == "5016" and c.exchange == "TSEJ" and c.currency == "JPY"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest src/xenon/api/tests/test_order_quote_contract.py -v`
Expected: FAIL — foreign stock currency is `USD` (hardcoded)

- [ ] **Step 3: Write minimal implementation** (replace the stock fallback at :1865):

```python
    return Stock(
        symbol,
        str(body.get("exchange") or "SMART").upper(),
        str(body.get("currency") or "USD").upper(),
    )
```

> Note: the conId-based quote path `_fetch_quote_snapshot_with_client` (:1848) builds `Contract(conId, exchange="SMART")`. With a valid `conId`, IB qualifies by conId regardless of the exchange hint, so position-row quotes (which pass `con_id`) already work for foreign stocks — confirm in Task 5.2. This task fixes the symbol-based body path only.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest src/xenon/api/tests/test_order_quote_contract.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/xenon/api/server.py src/xenon/api/tests/test_order_quote_contract.py
git commit -m "feat(orders): quote-path contract honors exchange/currency for foreign stocks"
```

### Task 4.2: Accept + forward `exchange`/`currency` through the web order body

**Files:**

- Modify: `web/lib/placeOrderBodySchema.ts` — add optional `exchange`, `currency` to the schema.
- Modify: `web/lib/order/placeOrderContract.ts:6-42` (`buildFastApiPlaceOrderPayload`) — forward `exchange`/`currency` when present.
- Modify: `web/app/api/orders/place/route.ts` — include them in `PlaceBody` + pass through (the builder already centralizes forwarding).
- Test: `web/tests/placeOrderContract.test.ts`

**Interfaces:**

- Produces: a `type:"stock"` body may carry `exchange?: string`, `currency?: string`; the FastAPI payload includes them; `server.py` forwards the raw body verbatim to the subprocess (no server change needed for the place path).

- [ ] **Step 1: Write the failing test**

```ts
// web/tests/placeOrderContract.test.ts (append)
import { buildFastApiPlaceOrderPayload } from "@/lib/order/placeOrderContract";

it("forwards exchange + currency for a foreign stock order", () => {
  const out = buildFastApiPlaceOrderPayload({
    type: "stock",
    symbol: "5016",
    action: "BUY",
    quantity: 100,
    limitPrice: 1,
    tif: "DAY",
    exchange: "TSEJ",
    currency: "JPY",
    client_attempt_id: "x",
  } as any);
  expect(out.exchange).toBe("TSEJ");
  expect(out.currency).toBe("JPY");
});

it("omits exchange/currency for a normal US stock order", () => {
  const out = buildFastApiPlaceOrderPayload({
    type: "stock",
    symbol: "AAPL",
    action: "BUY",
    quantity: 10,
    limitPrice: 200,
    tif: "DAY",
    client_attempt_id: "x",
  } as any);
  expect("exchange" in out).toBe(false);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run tests/placeOrderContract.test.ts`
Expected: FAIL — `out.exchange` undefined

- [ ] **Step 3: Write minimal implementation**

- `placeOrderBodySchema.ts`: add `exchange: Type.Optional(Type.String())`, `currency: Type.Optional(Type.String())`.
- `placeOrderContract.ts`: in the returned payload, append `...(body.exchange ? { exchange: body.exchange } : {})` and `...(body.currency ? { currency: body.currency } : {})`.
- `route.ts`: add `exchange?`, `currency?` to `PlaceBody`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx vitest run tests/placeOrderContract.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/lib/placeOrderBodySchema.ts web/lib/order/placeOrderContract.ts web/app/api/orders/place/route.ts web/tests/placeOrderContract.test.ts
git commit -m "feat(orders): accept + forward exchange/currency on stock orders"
```

### Task 4.3: Order forms pass the position's exchange/currency

**Files:**

- Modify: `web/components/ticker-detail/BookTab.tsx` (`StockOrderForm`, body at :430-438) — include `exchange`/`currency` from the position when non-USD.
- Modify: `web/components/ticker-detail/OrderTab.tsx` (`buildSingleLegOrderPayload`, stock fallback at :333-341) — same.
- Test: extend `web/tests/order-*.test.ts` where the stock body is asserted, or add `web/tests/foreign-stock-order.test.ts`.

**Interfaces:**

- Consumes: the `PortfolioPosition.currency`/`.exchange` now present on foreign rows.
- Produces: foreign-stock order bodies carry `exchange`/`currency`; US bodies unchanged.

- [ ] **Step 1: Write the failing test** (component-level, asserting the POSTed body includes exchange/currency when the position is JPY). Mirror the existing StockOrderForm test pattern; stub `fetch` and assert the JSON body.

- [ ] **Step 2: Run test to verify it fails** — `cd web && npx vitest run tests/foreign-stock-order.test.ts` → FAIL.

- [ ] **Step 3: Implement** — in both builders, add `...(position.currency && position.currency !== "USD" ? { exchange: position.exchange ?? undefined, currency: position.currency } : {})` to the body. (Read the exact `position` prop names in each component first.)

- [ ] **Step 4: Run test** → PASS.

- [ ] **Step 5: Commit**

```bash
git add web/components/ticker-detail/BookTab.tsx web/components/ticker-detail/OrderTab.tsx web/tests/foreign-stock-order.test.ts
git commit -m "feat(orders): foreign-stock order forms send exchange/currency"
```

### Task 4.4: Naked-short guard / preflight currency-safety check

**Files:**

- Inspect: `src/xenon/execution/naked_short_audit.py`, `web/lib/nakedShortGuard.ts`, `src/xenon/api/server.py` preflight (`_run_preflight`).
- Test: `scripts/tests/test_naked_short_audit.py` (append a JPY case).

**Interfaces:**

- Behavior: the guard is share-count based (SELL stock with no long shares = BLOCK). Confirm it operates on `ticker`+`quantity` and is currency-agnostic, so a SELL of 5016 with no long shares is still blocked and a BUY is allowed. Add a regression test proving a JPY symbol is handled identically to a USD one. Do NOT special-case currency.

- [ ] **Step 1: Write the failing/"""passing-by-design""" test** — feed `find_naked_short_violations` a JPY ticker SELL with no covering position; assert it's flagged the same as USD. If it already passes (currency-agnostic), keep it as a locked-in regression.

- [ ] **Step 2: Run** — `uv run pytest scripts/tests/test_naked_short_audit.py -v` → PASS (confirms no currency regression).

- [ ] **Step 3: Commit**

```bash
git add scripts/tests/test_naked_short_audit.py
git commit -m "test(guard): lock in currency-agnostic naked-short handling for foreign stocks"
```

### Task 4.5: Order lifecycle carries venue/currency (modify/cancel/render)

> **Found in review (Codex ISSUE-10, conf 78):** placing with `exchange/currency` (Tasks 4.1–4.2) is not enough. The full lifecycle — `order_submissions` persistence, the `/orders` list render, and modify/cancel — must distinguish `5016 TSEJ JPY` from a default `SMART/USD` stock, or a later modify/cancel rebuilds the wrong contract and the orders panel mislabels the row. Cancel/modify run as a subprocess (`ib_order_manage.py`) keyed by the original clientId and operate on the IB open-order snapshot, which DOES carry the real contract from IB — so cancel likely works via the IB-side contract — but this must be **verified, not assumed** (per project rule: no asserting IB behavior from memory; paper-first for order-path bugs).

**Files (verify-first; modify only where a gap is proven):**

- Inspect: `src/xenon/execution/ib_order_manage.py` (how it rebuilds the contract for cancel/modify), `src/xenon/execution/orders_store.py` (does `order_submissions` store enough to reconstruct venue?), `web/components` order list render, `src/xenon/api/services/ib_activity_mirror.py` (does the IB→PG mirror carry currency/exchange?).
- Possibly modify: `orders_store` schema/writer to persist `currency`/`exchange` on `order_submissions` IF cancel/modify cannot otherwise rebuild the foreign contract.
- Test: `scripts/tests/test_order_lifecycle_foreign.py` (or extend existing cancel/modify tests) — a foreign-stock order round-trips place → render → cancel with correct venue.

- [ ] **Step 1:** Trace the cancel/modify path for a foreign order. Determine whether `ib_order_manage` reconstructs the contract from the IB open-order `Trade` (which carries currency/exchange) or from stored body fields. Record the finding.
- [ ] **Step 2:** If the path rebuilds from stored/SMART-USD fields → write the failing test, then persist `currency`/`exchange` in `order_submissions` (migration mirrors Task 1.6 pattern) and use them on rebuild. If it uses the IB Trade contract → write a regression test that locks in correct foreign cancel and skip the schema change (YAGNI).
- [ ] **Step 3:** Verify the `/orders` list renders venue/currency (not SMART/USD) for a foreign row.
- [ ] **Step 4:** Run targeted tests → PASS. (Live cancel is exercised in Task 5.3 step 3.)
- [ ] **Step 5:** Commit with a message describing what was found + changed.

> **Scope boundary (review fix — Codex ISSUE-4):** v1 covers portfolio DISPLAY of foreign positions and order entry FROM the position row. Standalone foreign ticker-detail PAGES (searching/charting a foreign symbol that isn't held) are OUT of v1 scope — those would re-subscribe the symbol as bare SMART/USD via `tickerSymbols`/`orderSymbols` (WorkspaceShell:222) and need their own exchange/currency routing. Add a regression note; do not build foreign ticker pages now.

---

## PHASE 5 — Verification: suites, local boot, E2E, gated order test

### Task 5.1: Full test suites green

- [ ] **Step 1:** `uv run pytest` → all pass (note any pre-existing unrelated failures).
- [ ] **Step 2:** `cd web && npm test` → all pass.
- [ ] **Step 3:** `cd web && npm run typecheck` → clean.
- [ ] **Step 4:** `cd web && npm run lint` → clean.
- [ ] **Step 5:** `uv run python scripts/checks/no_json_fallback_on_order_path.py` and `no_json_write_on_order_path.py` and `order_path_caller_allowlist.py` → all pass (no new violations).

### Task 5.2: Boot local + verify display against live positions

> Use the `boot-local` skill or `scripts/infra/dev.sh live` (read-only against live IB — needed because 5016/000660 live in the LIVE account, not paper). Dev ports: Next 3200, FastAPI 8421, relay 8866.

> **Read-only verification gap (review fix — Codex ISSUE-2, conf 92):** under `XENON_READ_ONLY=1`, `ib_sync._save_portfolio_to_postgres` is a no-op, so `GET /portfolio` keeps returning the LAST committed snapshot — which predates this feature and lacks `currency`/`market_value_usd`/`fx_rates`. Booting read-only and curling `/portfolio` therefore CANNOT prove the new fields. Use the two independent proofs below: a read-only in-process payload dump (proves the compute path) and a supervised one-shot snapshot write (proves the PG/API/UI path end-to-end).

- [ ] **Step 1:** Boot: `scripts/infra/dev.sh live` (exports `XENON_READ_ONLY=1`). Confirm `curl http://localhost:8421/health` → `ib_gateway.port_listening: true`.
- [ ] **Step 2 (read-only compute proof):** Run a one-off script that connects read-only and prints the computed payload WITHOUT writing — proves `fetch_positions`→`collapse_positions`→`convert_to_portfolio_format` produce currency/exchange/`*_usd`/`fx_rates` for 5016 + 000660:

```bash
XENON_TRADING_MODE=live XENON_READ_ONLY=1 uv run python - <<'PY'
import json
from xenon.execution import ib_sync
from xenon.clients.ib_client import DEFAULT_HOST
c = ib_sync.connect_ib(DEFAULT_HOST, 4001, "auto")
try:
    acct = ib_sync.get_account_summary(c); fx = ib_sync.get_fx_rates(c)
    pos = ib_sync.fetch_positions(c)
    # (run the same market-data + collapse path main() uses, or call the
    #  extracted helper; minimally:)
    collapsed = ib_sync.collapse_positions(pos)
    payload = ib_sync.convert_to_portfolio_format(acct, collapsed, fx_rates=fx)
    rows = [p for p in payload["positions"] if p["ticker"] in ("5016", "000660")]
    print(json.dumps({"fx_rates": payload["fx_rates"], "rows": rows}, indent=2, default=str))
finally:
    c.disconnect()
PY
```

Expect each foreign row to carry `currency`, `exchange`, `market_value_usd`, `entry_cost_usd`; `fx_rates` to contain `JPY`/`KRW`; rates in the sane band (USD/JPY ~150–165 ⇒ usd_per_unit ~0.006, USD/KRW ~1300–1600 ⇒ ~0.00065). If a rate is inverted, fix Task 1.5 (ExchangeRate direction) before proceeding.

- [ ] **Step 3 (supervised PG/API/UI proof):** With operator sign-off, run ONE non-read-only sync against live IB (`XENON_TRADING_MODE=live uv run xenon-ib-sync --sync --port 4001`, `XENON_READ_ONLY` unset) so a fresh snapshot with the new fields lands in `core_test`. (Writes a portfolio snapshot only — no orders.) Then `curl http://localhost:8421/portfolio | jq '.positions[] | select(.ticker=="5016" or .ticker=="000660") | {ticker,currency,exchange,market_value,market_value_usd}'` and `... | jq .fx_rates` — fields populated.
- [ ] **Step 4 (Playwright/chrome-cdp):** Open `http://localhost:3200`, portfolio view. Verify 5016 (TSEJ/JPY) and 000660 (KSE/KRW) rows show a native price (¥/₩) sub-line AND a `$`-USD headline; FX badge shows `USD/JPY` + `USD/KRW` with live dots. Screenshot → `output/playwright/japan-korea-usd-<date>.png`.

### Task 5.3: GATED — foreign-stock order placement with an unfillable limit

> **STOP — this transmits a real order to a real exchange.** The dev `live` stack is `XENON_READ_ONLY=1`, which 403s order placement by design. Two ways to actually exercise placement, each requires an explicit operator decision (see the plan's "Open Decision" below). DO NOT proceed autonomously.
>
> **Two gates apply to `POST /orders/place`, not one:** (1) `is_read_only()` → 403 `READ_ONLY_MODE`, and (2) `require_mode_verified(request)` for IB broker (`server.py:2078`, `guards.py:69`). Even with read-only lifted, an unverified session 403s on mode. The browser flow sets mode-verified; a raw `curl`/test harness must replicate it (check `curl /health | jq .mode_verified` first). Drive this test through the **UI order ticket** (which verifies mode) rather than a bare API call, or explicitly establish mode-verified state in the harness.

Safe test design (once an environment is chosen):

> **Adversarial caution — the non-read-only window leaks live state into `core_test`:** lifting `XENON_READ_ONLY` against live IB means the FastAPI activity poller (`_maybe_start_activity_poller`) and boot reconcilers will mirror ANY live fills/orders into `core_test`, not just this test's order. Keep the window minimal: set `XENON_IB_ACTIVITY_POLLER=0` for the test session, place → assert → cancel quickly, then restore `XENON_READ_ONLY=1`. The order itself is unfillable, but other concurrent live activity is the leak vector.

> **Board-lot + tick warning (review fix — Codex ISSUE-6, conf 84):** TSEJ and KSE enforce board-lot sizes (TSE trading unit is commonly 100 shares) and price ticks that differ from the US 0.01 stub. A 1-share or off-tick order may be IB-REJECTED — and the executor must NOT mistake an expected lot/tick rejection for a working implementation, nor for a bug. Before the test, read the contract's board lot + min tick (IB `reqContractDetails` → `marketRuleIds`/`minSize`, or just match the existing held lot size of 5016/000660) and size the order to a VALID lot at a valid (absurdly low) tick.

- [ ] **Step 1:** Place a **BUY** limit on 5016 (TSEJ/JPY) using a VALID board lot (e.g. 100 shares — confirm via contract details) at a price far BELOW market (e.g. ¥1 when market is ~¥1000+) → cannot fill. Body: `type:"stock", symbol:"5016", action:"BUY", quantity:<board_lot>, limitPrice:<valid low tick>, tif:"DAY", exchange:"TSEJ", currency:"JPY"`. A code-110 (tick) or lot-size rejection here means the order body needs a valid tick/lot — it is NOT a feature failure; fix the test inputs and retry.
- [ ] **Step 2:** Assert the API returns `status:"ok"` with `initialStatus` in {`Submitted`,`PreSubmitted`,`Working`} and NOT `Filled`.
- [ ] **Step 3:** Immediately CANCEL the order via the cancel path; assert it cancels. **Verify the cancel rebuilds/uses the foreign contract correctly** (see Task 4.5 / Codex ISSUE-10) — a cancel that silently fails or targets a SMART/USD contract is a real bug, not a pass.
- [ ] **Step 4:** Repeat for 000660 (KSE/KRW) — BUY a valid board lot at an absurdly low won price (e.g. ₩1, on a valid tick).
- [ ] **Step 5:** Confirm via IB/`/orders` that no fill occurred and the working order is gone, AND the `/orders` row renders the correct venue/currency (not SMART/USD). Screenshot the order lifecycle.

---

## Open Decision (must resolve before Task 5.3)

The dev `live` stack forces `XENON_READ_ONLY=1` (orders 403). The paper stack (`dev.sh paper`, port 4002) does NOT hold 5016/000660 and may lack TSE/KRX market-data + trading permissions. So testing real foreign-order placement requires one of:

1. **Paper account** — verify TSE/KRX trading permission exists on paper first; place the unfillable order there. Safest (no live money), but may be blocked by missing paper entitlements.
2. **Live account, read-only lifted for the test** — run the live IB gateway with `XENON_READ_ONLY` unset for a single supervised, unfillable order, then restore read-only. Exercises the real path but transmits a genuine (unfillable) order to TSEJ/KSE.

This is the one step that cannot be auto-decided; it needs operator sign-off on which account + acceptance that a real unfillable order is transmitted.

---

## Self-Review (post review-cycle)

- **Spec coverage:** Full trading (orders) ✓ Phase 4 (4.0 preflight gate, 4.1 place, 4.1b quote, 4.2 web body, 4.3 forms, 4.4 guard, 4.5 lifecycle); native+USD headline ✓ Phase 3 (3.3 table, 3.5 all other surfaces); live FX tick + account-rate fallback ✓ Phases 1+2; stock-only (options untouched) ✓; capture currency/exchange ✓ Phase 1; USD totals ✓ Task 1.5; SMART-forcing quote bug ✓ Task 1.4; persistence ✓ Task 1.6.
- **Review-cycle additions:** Pass 1 retargeted Phase 3 to `WorkspaceShell` (the real `usePrices` owner) + fixed the `total_deployed` raw-native bug + `committed_db` marker. Pass 2 (Codex) added the **preflight universe blocker (Task 4.0 — was a hard show-stopper)**, the quote-path contract fix (4.1b), order-lifecycle venue (4.5), all-surfaces USD conversion (3.5), grouping-key currency (1.3), relay handler bindings (2.2), monkeypatch target (1.5), per-pair FX liveness (3.2/3.3), board-lot order test (5.3), and the read-only verification gap (5.2). Pass 3 added the sentinel-rate guard (1.1) and the non-read-only leak caution (5.3).
- **Placeholders:** Tasks 4.3/4.4/4.5/3.5 say "read the exact prop names / trace the path first" — these are deliberate verify-before-edit steps with concrete TDD cycles, not skipped work. All code-bearing steps include real code.
- **Type consistency:** `usd_per_unit` (py) / `usdPerUnit` (ts) consistent; `to_usd`/`toUsd` match; `forexKey`/`normalizeForex` both key `"BASE.QUOTE"`; canonical USD fields are `entry_cost_usd` + `market_value_usd` (no `value_usd`) across `ib_sync.py`, `portfolioDataSchema.ts`, `types.ts`; `FxBadge` uses `liveCurrencies` (not `live`) at both the component and call sites.
- **Standing-rule check:** Yahoo Finance — not used (IB + IB forex only) ✓. Naked-short guard — preserved, currency-agnostic, Task 4.4 locks it in; foreign SELL without shares still BLOCKED (Task 4.0 test) ✓. Secrets to codex — N/A (plan review used a read-only prompt, no secrets) ✓. DB-first / no JSON fallback — no new JSON read/write on order/portfolio path; Task 5.1 runs the three CI guards ✓. `XENON_READ_ONLY` honored — all new writers (positions columns, no new write surface) go through existing guarded paths ✓.
- **Risk flags:** Task 1.4 has no isolated unit test (live IB path) — covered by Task 5.2. Task 5.3 (live order) gated on the Open Decision + dual gate (read-only + mode-verified) + board lot. FX direction double-guarded (SANE band test + 5.2 visual). The plan grew from 4 to 6 effective order-path tasks after review — Phase 4/5 confidence is correspondingly lower (see Pass 6).
