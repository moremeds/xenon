# Unusual Whales API Scanner for 2-Week Options Swing Trades

## Objective

Build a scanner that uses **Unusual Whales API data + technical analysis filters** to identify strong stocks for **roughly 2-week options swing trades**.

The core principle is:

> Do not use unusual options flow alone as the signal.  
> Use it as a confirmation layer on top of trend, volatility state, dealer structure, and event context.

---

## Core Thesis

For a 2-week holding period, the most reliable setup is:

**strong chart + supportive gamma structure + acceptable IV + repeated confirming flow**

Not:

**big premium print = buy**

This matters because a 2-week swing is long enough that:
- chart quality matters,
- options pricing matters,
- positioning/pinning matters,
- one-off flow can easily fail without trend support.

---

## What Unusual Whales Data Is Useful For

The API surface is broad enough to support a serious scanner. The most relevant categories are:

### 1. Directional Trend / Price Context
Useful for determining whether the underlying stock is already acting well.

Relevant data:
- OHLC / price history
- technical indicators
- stock state data

Best use:
- trend qualification
- breakout detection
- relative strength filtering
- liquidity filtering

### 2. Volatility State
Useful for deciding whether the option itself is attractively priced for a 2-week hold.

Relevant data:
- IV rank
- implied volatility term structure
- volatility statistics
- realized volatility

Best use:
- identify whether options are cheap, fair, or expensive
- detect event premium in the front end
- choose between debit trades and premium-selling structures

### 3. Positioning / Dealer Structure
This is one of the most valuable parts of Unusual Whales for swing options.

Relevant data:
- greek exposure
- spot GEX exposure
- GEX by strike
- GEX by strike and expiry
- max pain
- option price levels
- open interest per strike
- open interest change

Best use:
- determine if the stock is in supportive or hostile structure
- locate walls, pin zones, support, resistance
- estimate whether the stock can trend or is likely to chop

### 4. Fresh Flow + Event Confirmation
Useful as a final confirmation layer.

Relevant data:
- flow alerts
- ticker-specific flow alerts
- greek flow
- greek flow by expiry
- news headlines
- dark pool data
- off/lit price levels

Best use:
- confirm institutional directional participation
- identify clustered expiry/strike activity
- avoid trading into unknown event risk
- separate repeated accumulation from one-off prints

---

## What Matters Most for a 2-Week Options Swing

For this holding period, the scanner should prioritize the following in order:

1. **TA trend quality**
2. **Dealer structure / gamma context**
3. **IV state**
4. **Flow confirmation**
5. **News / event sanity check**

This order is important. A good chart with supportive structure often works better than a bad chart with flashy flow.

---

## Interpreting the Screenshot Example

The screenshot appears to show a ticker with the following structure:

- Spot: **347.68**
- GEX flip: **346.25**
- Net GEX: **positive**
- Call wall: **400**
- Put wall: **340**
- Max pain: **355**
- IV 30D: **48.2%**
- IV rank: **19**
- Term structure: **inverted**
- Net call premium: positive
- Net put premium: negative

### Practical interpretation

#### Positive / supportive points
- Spot is **slightly above the gamma flip**, which is generally better than below it.
- IV rank is **relatively low/moderate**, so options are not obviously overpriced on a 1-year basis.
- Net call premium is supportive.
- Put wall near 340 may act as a reference support level.

#### Caution points
- Term structure is **inverted**, which suggests short-dated implied volatility may be elevated.
- Max pain is relatively close to spot, which can contribute to pinning/chop if momentum fades.
- A far call wall at 400 may be too distant to matter for a 2-week trade unless momentum accelerates significantly.

### Conclusion from the screenshot
This is **not automatically a buy**, but it is a potentially watchlist-worthy structure if:
- price holds above gamma flip,
- trend confirms,
- call flow continues,
- and near-term event premium is manageable.

In a proper scanner, this name would probably score reasonably on:
- structure,
- volatility,
- positioning,

but should only rank highly if **TA trend** and **fresh flow** also confirm.

---

## Recommended Scanner Architecture

A robust first version should use a **three-stage pipeline**.

## Stage A — Stock Quality / TA Prefilter

Start with the underlying stock, not options flow.

### Bullish prefilter
- price > 20DMA > 50DMA
- 20DMA slope positive
- RSI in a constructive range, e.g. 50–70
- price near a breakout or trend continuation setup
- positive relative strength vs SPY or QQQ
- sufficient liquidity / dollar volume
- acceptable ATR profile

### Bearish prefilter
- price < 20DMA < 50DMA
- 20DMA slope negative
- RSI in a weak range, e.g. 30–50
- failed bounce or clean breakdown
- negative relative strength
- sufficient liquidity

### Why this stage matters
This stage prevents the scanner from selecting bad charts simply because they have unusual flow.

---

## Stage B — Options Structure Filter

Only evaluate options positioning after the stock chart passes.

### Bullish structure signals
- spot above gamma flip
- supportive or non-hostile net gamma
- favorable call-side positioning above spot
- upside open interest build
- favorable OI change on higher strikes
- enough room to move before major overhead wall

### Bearish structure signals
- spot below gamma flip
- hostile / destabilizing gamma context
- downside put concentration
- supportive OI build below spot
- bearish OI change
- limited support nearby

### Reject conditions
- severe pinning around current spot
- very large nearby wall that caps the move
- heavy contradictory OI build
- structure suggests chop rather than expansion

### Why this stage matters
A good trend can still fail to translate into option profit if the stock is trapped in a pinned structure.

---

## Stage C — Flow Confirmation

This should be the final step, not the first.

### Bullish flow confirmation
- repeated ask-side call flow
- multiple trades or clustered activity
- flow concentrated in 1–4 week expiries
- strikes not absurdly far OTM
- positive delta/vega flow
- no contradictory negative news

### Bearish flow confirmation
- repeated ask-side put flow
- clustering in reasonable expiries
- negative directional greek flow
- downside strike selection makes sense relative to price structure
- no contradictory bullish catalyst

### Why this stage matters
The goal is not to chase prints.  
The goal is to confirm that real positioning aligns with the stock setup.

---

## Suggested Scoring Model

A practical first-pass weighting for a bullish scanner:

### 1. Trend Score — 35%
Components:
- MA alignment
- breakout/base quality
- slope of short and medium trend
- relative strength vs benchmark
- RSI regime

### 2. Volatility Score — 20%
Components:
- IV rank
- term structure shape
- implied vs realized volatility sanity
- whether the premium is reasonable for a 2-week trade

### 3. Structure Score — 25%
Components:
- spot relative to gamma flip
- distance to call wall / put wall
- max pain proximity
- GEX profile by strike / expiry
- OI change and concentration

### 4. Flow Score — 20%
Components:
- ask-side premium concentration
- repeated flow instead of single print
- opening-style activity
- expiry clustering
- consistency with price action

### Use minimum thresholds
Do not rank purely by total score.  
Require:
- minimum Trend Score
- minimum Structure Score
- minimum liquidity threshold

This avoids garbage names reaching the top because of one strong category.

---

## Trade-Type Logic

The scanner should not treat every candidate as a straight long-call or long-put trade.

### When debit calls / puts are better
Use when:
- directional conviction is high,
- IV is not too elevated,
- and the structure supports expansion.

### When call / put spreads are better
Use when:
- direction is correct,
- but IV is not cheap,
- or the move is likely capped by nearby structure.

### When premium selling is better
Use when:
- IV is rich,
- structure supports containment,
- and you are intentionally trading a range / pin scenario.

This means the scanner should ideally output not only a ticker and direction, but also a **preferred trade expression**.

---

## Biggest Mistakes to Avoid

### 1. Using premium size as the main signal
A huge premium print by itself is not enough.  
You need:
- side,
- strike,
- expiry,
- likely opening context,
- and price confirmation.

### 2. Ignoring volatility regime
A correct directional view can still lose money if the option was overpriced.

### 3. Ignoring gamma flip / wall structure
The stock can stay pinned even when the chart looks good.

### 4. Trading against the chart because of flow
For a 2-week hold, price structure matters more than a one-off alert.

### 5. Mixing trade styles
A scanner for debit trades should not use the same ranking logic as one for premium-selling or spreads.

---

## Recommended Version 1 Build

### Inputs
Use:
- daily OHLC
- 20DMA / 50DMA
- RSI
- ATR
- dollar volume / liquidity
- IV rank
- implied volatility term structure
- gamma flip / spot GEX
- call wall / put wall / max pain
- ticker flow alerts
- greek flow
- OI change
- news headlines

### Outputs
For each ranked name, return:
- ticker
- direction: bullish or bearish
- trend summary
- structure summary
- flow summary
- volatility summary
- suggested trade style:
  - debit call
  - call spread
  - debit put
  - put spread
- invalidation level
- expected holding window: 5–15 trading days

---

## Suggested Bullish Scanner Logic (Conceptual)

A bullish candidate should ideally satisfy most of the following:

### Stock / TA
- close > 20DMA > 50DMA
- positive short-term slope
- RSI constructive
- relative strength positive
- price near breakout or continuation zone

### Volatility
- IV rank not extreme
- front-end term structure not absurdly inflated
- option pricing still reasonable

### Structure
- spot above gamma flip
- supportive GEX near/above spot
- no nearby wall that clearly caps the move
- OI change supports upside

### Flow
- repeated ask-side call flow
- reasonable expiry cluster
- strikes aligned with expected move
- positive delta/vega flow

### News
- no unexpected risk event that invalidates the setup

---

## Suggested Bearish Scanner Logic (Conceptual)

A bearish candidate should ideally satisfy most of the following:

### Stock / TA
- close < 20DMA < 50DMA
- negative slope
- weak RSI
- relative weakness
- failed bounce or breakdown

### Volatility
- option premium not absurd unless using spreads
- event risk understood

### Structure
- spot below gamma flip
- downside GEX / OI structure supportive
- nearby support not too strong

### Flow
- repeated put flow
- strikes and expiries realistic
- negative greek flow
- downside participation is persistent

### News
- no offsetting bullish catalyst

---

## Best Unusual Whales Features for This Project

### Highest-value data
These are the most useful for the scanner:
- ticker flow alerts
- greek flow
- greek exposure
- spot GEX by strike / expiry
- open interest change
- IV rank
- implied volatility term structure
- price / OHLC / technical indicators
- news headlines

### Secondary / contextual data
Useful, but usually not primary ranking inputs:
- dark pool data
- off/lit price levels
- max pain

---

## Bottom Line

Unusual Whales can support a strong scanner for **2-week options swing trading**, but only when used correctly.

The most robust framework is:

**TA trend + dealer structure + IV state + repeated flow + event check**

That is much stronger than:
- chasing large premium prints,
- following one sweep,
- or buying options without considering IV and pinning.

If implemented well, the scanner should help identify:
- strong trending stocks,
- supportive options structure,
- reasonable premium conditions,
- and institutional participation that aligns with the chart.

That is the right base for a disciplined options swing workflow.
