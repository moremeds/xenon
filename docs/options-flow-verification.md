# Options Open Interest Analysis Guide

## Overview

**OI change analysis is a REQUIRED part of every evaluation, not just for external signals.**

UW has two separate data sources:
1. **Flow Alerts** (`/api/option-trades/flow-alerts`) — Filtered for "unusual" activity
2. **OI Change** (`/api/stock/{ticker}/oi-change`) — Raw positioning changes

Flow alerts may miss large institutional trades that don't trigger their filters. **OI changes show ALL significant positioning regardless of whether it's "unusual".**

## The Data Gap

| Source | Shows | Misses |
|--------|-------|--------|
| UW Flow Alerts | "Unusual" daily activity | Large trades that don't trigger alerts |
| UW OI Change | **All significant positioning** | Nothing — this is the source of truth |

**Example:** The $95M MSFT LEAP call purchase did NOT appear in flow alerts but showed up clearly in OI change data:
- $625 Call: +100,458 OI change, $51M premium
- $575 Call: +50,443 OI change, $45M premium
- $675 Call: +50,148 OI change, $15M premium

**Key Insight:** Flow alerts filter for "unusual" activity. OI changes show **all** significant positioning. Always check both.

## Standard Workflow (Every Evaluation)

### Step 1: Fetch OI Changes for Ticker

```bash
# Standard OI change analysis (ALWAYS run this)
python3.13 scripts/fetch_oi_changes.py MSFT

# Or with the options script
python3.13 scripts/fetch_options.py MSFT --oi-changes
```

This uses UW's `/api/stock/{ticker}/oi-change` endpoint which shows ALL significant OI changes.

### Step 2: Identify Large OI Changes

Look for:
- OI change > 10,000 contracts (significant size)
- Premium > $1M (institutional size)
- LEAPs with large OI change (long-term positioning)
- OI change NOT accompanied by flow alert (hidden signal)

**Example Output:**
```
MSFT OI Changes:
Symbol                    OI Change    Premium
MSFT270115C00625000      +100,458   $50,974,889  ← MASSIVE
MSFT270115C00575000       +50,443   $44,800,215  ← MASSIVE
MSFT270115C00675000       +50,148   $15,068,081  ← MASSIVE
```

### Step 3: Cross-Reference with Flow Alerts

| Scenario | Interpretation |
|----------|----------------|
| Large OI change + Flow alert | ✅ Confirmed — UW flagged it |
| Large OI change + NO flow alert | ⚠️ Hidden signal — investigate |
| Flow alert + Small OI change | Day trade, not positioning |

### Step 4: Verify External Claims (If Applicable)

When verifying screenshots/tweets:
```bash
python3.13 scripts/verify_options_oi.py MSFT --expiry 2027-01-15 --verify "575:50000,625:100000"
```

**Verification Criteria:**
- OI within 10% of claimed size → **VERIFIED**
- OI significantly lower → **SUSPECT** (claim may be false)
- OI near zero → **FALSE** (no position exists)

## Integration with Evaluation Workflow

### When to Use This Verification

1. **External signals** — Any flow claim from Twitter, Discord, screenshots
2. **Large block trades** — Claims of $10M+ single trades
3. **Unusual strikes** — Far OTM positions (>30% from spot)
4. **LEAP positions** — Long-dated options where OI is more meaningful

### Updated Evaluation Milestone

**Milestone 3B: Options Flow Verification** (NEW)

If evaluating based on external flow signal:
```bash
python3.13 scripts/verify_options_oi.py [TICKER] --expiry [DATE] --strikes [S1,S2,S3]
```

**Acceptance Criteria:**
- OI matches claimed position size (within 10%)
- Position is still open (OI > recent daily volume)
- Multiple strikes corroborate the structure (spreads, etc.)

**Stop Condition:** If OI doesn't support the claim → signal is UNVERIFIED, reduce confidence or pass.

## Example: MSFT $95M LEAP Call Verification

**Claim:** @SubuTrade screenshot showing $95M in MSFT Jan 2027 calls

**Verification:**
```
Strike   Claimed    Actual OI   Premium/Contract   Total Premium
$575     50,000     51,005      $8.80              $44.9M
$625     100,000    100,788     $5.05              $50.9M  
$675     -50,000    50,862      $3.00              -$15.3M (sold)
```

**Result:** ✅ VERIFIED — OI matches claims within 2%

**Conclusion:** Institutional positioning is real. Net bullish call spread structure confirmed.

## Script Reference

### verify_options_oi.py

```bash
# Basic usage
python3.13 scripts/verify_options_oi.py MSFT --expiry 2027-01-15

# Filter specific strikes
python3.13 scripts/verify_options_oi.py MSFT --expiry 2027-01-15 --strikes 575,625,675

# High strikes only (for far OTM verification)
python3.13 scripts/verify_options_oi.py MSFT --expiry 2027-01-15 --min-strike 500

# JSON output for programmatic use
python3.13 scripts/verify_options_oi.py MSFT --expiry 2027-01-15 --json
```

### Output Includes:
- Strike, Volume, Open Interest, Total Premium
- OI change from previous day (if available)
- Position age estimate (OI / avg daily volume)

## Key Takeaways

1. **Flow alerts are filtered** — they don't show everything
2. **Open Interest is truth** — positions cannot hide from OI
3. **Always verify external claims** — before trading on screenshots/tweets
4. **Check position age** — OI vs volume reveals if position is held
5. **Multiple strikes corroborate** — verify the full structure, not just one leg
