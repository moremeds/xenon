# 2026-05-04 — Systematic SPXW short-options paper: review and reproducibility notes

Critical review of the paper _Systematic index option-writing strategies with Black-Scholes-Merton and Variance-Gamma Models_ (Economic Modelling, 2025; arxiv preprint 2407.13908v1, originally titled _Construction and Hedging of Equity Index Options Portfolios_). Sample period 2018-01-02 → 2023-12-29. Sells 7-DTE SPXW weeklies; tests 180 strategy variants across {short call, short put, strangle} × {0/2/5/10% OTM} × {BSM-delta, VG-delta, BSM-VIX, VG-VIX sizing} × {30-min, 130-min, daily, naked hedge frequency}, plus a buy-and-hold S&P 500 benchmark.

The user surfaced this paper for evaluation against Xenon's deployed-strategy bar. This note records what's verified, what's not, and what would need to change before any of it touches real capital.

---

## Bottom line

- The paper is methodologically sound and the headline result (5% OTM strangle + 130-min BSM-delta hedge: 7.96% aRC, 6.6% MD, 0.067 aSD) reproduces qualitatively and quantitatively from the arxiv HTML tables. It also lives inside the envelope of CBOE's long-running PUT/WPUT benchmarks, so it's not an overfit artifact of a 6-year window.
- **The paper's headline "risk-adjusted return" of 59.669 is NOT Sharpe.** It's a custom metric `IR*** = aRC³ / (aSD · MD · MLD) × 1000`. The cubic numerator inflates differences enormously. On honest Sharpe / Calmar the strategy still beats buy-and-hold but by a much smaller multiple (~2× on Sharpe, ~4× on Calmar, not "60×").
- Three implementation choices in the paper are too optimistic for live deployment: **mid-of-bid-ask fills** (real SPXW spreads warrant 25–50% haircut), **130-min hedging** (transaction costs eat the edge under realistic fills), and **roll into the gamma cliff** at expiry-day 3:30 PM ET.
- The 6-year sample contains COVID and the 2022 rate shock but **no overnight gap event** of 1987 / 2020-03-16 magnitude. The reported 18.9% MD on the naked 5% strangle during COVID is _not_ a worst-case bound under this strategy; it's the worst case in the sampled regime.
- Negative results in the paper (VG model worse than BSM for hedging; VIX-rank sizing worse than fixed delta sizing) are valuable and probably generalize. The VIX-rank failure has a **structural reason** the paper doesn't surface: rolling 1-year percentile is self-normalizing and can't track regime shifts in the VIX mean.

If we wanted to deploy this in Xenon, my recommendation is below in §6 — short answer: 5% OTM **iron condor** (not naked strangle), **daily** (not 130-min) hedge, fixed 1.5–2% sizing, hard gap kill switch. Accept ~5–6% aRC instead of 8% in exchange for not blowing up on a Black Monday.

---

## 1. Verified — what the paper actually does

| Aspect                  | Paper specifies                                                                                                                | Source        |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------ | ------------- | ------------------------------------------------ | ------------- |
| Underlying              | SPXW (S&P 500 weekly, European, cash-settled, PM)                                                                              | §3 + abstract |
| Maturity                | 7 DTE                                                                                                                          | §3            |
| Roll                    | "At each expiration date, before the market close and options settlement, we opened another position" — same session as expiry | §3            |
| Daily-hedge anchor      | 30 min before NYSE close (≈ 3:30 PM ET)                                                                                        | §4            |
| Position sizing — delta | `Q_t = ⌊PV_t / (Σ K_i ·                                                                                                        | Δ_i,t         | · M)⌋` (formula 24) — keeps risk-scaled exposure | §3 formula 24 |
| Position sizing — VIX   | `Q_t = ⌊(PV_t/SPX_t) · ρ · (1 - P_rank(VIX_t))⌋` with ρ = 1.4 (formula 25)                                                     | §3 formula 25 |
| Transaction costs       | IBKR option commissions; **fills at bid-ask midpoint** (half spread captured)                                                  | §3            |
| Sample                  | 2018-01-02 → 2023-12-29 (≈ 6 years)                                                                                            | §4            |
| Benchmark               | Buy-and-hold S&P 500 (aRC = 9.89%, MD = 34.0%)                                                                                 | Tables in §5  |

### Verified per-strategy result excerpts

| Strategy     | %OTM | Hedge   | Sizing    | aRC    | aSD              | MD    | CVaR    | IR\*\*\* |
| ------------ | ---- | ------- | --------- | ------ | ---------------- | ----- | ------- | -------- |
| Strangle     | 5%   | 130-min | BSM-delta | 7.96%  | 0.067            | 0.066 | -0.568% | 59.7     |
| Strangle     | 5%   | naked   | BSM-delta | 10.27% | 0.130            | 0.189 | —       | 64.2     |
| Put          | 0%   | naked   | VG-delta  | 13.92% | 0.320            | 0.548 | -5.560% | —        |
| Put          | 5%   | 30-min  | BSM-delta | 7.49%  | 0.117            | 0.063 | -0.395% | —        |
| Call         | 5%   | naked   | VIX       | 0.50%  | 0.006            | 0.008 | -0.064% | —        |
| Buy-and-hold | —    | —       | —         | 9.89%  | ~0.18 (inferred) | 34.0% | -1.935% | 6.97     |

(All numbers above pulled from arxiv 2407.13908v1 HTML tables.)

### What is NOT specified in the paper (reproducibility gaps)

These are real holes for anyone trying to clone the strategy:

1. **Day of week for entry/roll.** "7 DTE weekly" is ambiguous in 2018–2023: SPXW had Mon/Wed/Fri expiries for most of the window (Tue/Thu added 2022). Most likely Friday-to-Friday matching WPUT, but unstated.
2. **Anchor for 30/130-min intraday hedge.** 130-min anchored at 9:30 ET hits 11:40 / 13:50 / **15:30 ET**; anchored at 10:00 ET hits 12:10 / 14:20 / **16:00 (post-close)**. Different P&L distributions, identical "methodology."
3. **Holiday / shortened-session handling.** Not stated.
4. **Whether the final daily hedge at 3:30 PM ET on expiry day is the same order as the new entry, or two separate orders.** Affects fill costs by 1× spread.
5. **Expiry-day P&L vs non-expiry-day P&L breakdown.** For a 7-DTE strategy, expiry day is most of the theta and most of the gamma risk. Paper reports blended series only.

---

## 2. The IR\*\*\* metric — read carefully before quoting

The paper defines

```
IR*** = aRC³ / (aSD · MD · MLD) × 1000
```

with `MLD` = maximum loss duration in years. This is the paper's most stringent risk-adjusted metric. The cubic numerator means an 8% return contributes `512` before any risk normalization; a 4% return contributes `64` — already an 8× gap on returns alone before MD/MLD enter.

This means **two strategies with identical Sharpe can have IR\*\*\* differing by 5–10×** if their absolute returns differ. The ranking is not invariant under leverage rescaling. Don't read the IR\*\*\* column as a Sharpe-equivalent; it's a paper-specific risk-adjusted metric and its scale is not industry-comparable.

### Honest re-computation for the recommended strategy

5% OTM strangle, 130-min hedge, BSM-delta:

- aRC = 7.96%, aSD = 6.7%, MD = 6.6%
- **Sharpe (rf=0)** = 7.96 / 6.7 = **1.19**
- **Calmar** = 7.96 / 6.6 = **1.21**

Buy-and-hold S&P 2018–2023:

- aRC = 9.89%, aSD ≈ 18% (typical SPX), MD = 34%
- **Sharpe** ≈ 0.55, **Calmar** ≈ 0.29

So the strategy beats buy-and-hold on Sharpe by ~2.2× and on Calmar by ~4.2×. That's a meaningful win — just not the 8.6× the IR\*\*\* column suggests (59.7 / 6.97).

---

## 3. The mid-fill assumption is the load-bearing weakness

The paper assumes fills at the bid-ask midpoint. This is the single most consequential simplification.

- ORATS' published backtesting standard for single-leg options: **75% of bid-ask** (i.e., the trader pays 25% past mid). For multi-leg spreads ORATS uses 56%.
- Real SPXW OTM-wing spreads in 2018–2023 routinely 0.20–0.50 wide. A 25% spread haircut on each leg is real money.
- A 130-min hedging schedule across one weekly cycle generates ~4 entry/roll fills + ~13 hedge adjustments. If the hedge instrument is SPX futures or SPY (tight spreads), this is fine. If the hedge instrument involves the option legs themselves (the paper isn't fully explicit about what gets hedged), the cost goes up nonlinearly.

### Predicted impact under realistic fills

Switch from mid to _bid + 0.33 × (ask - bid)_ on sells, _ask - 0.33 × (ask - bid)_ on buys, and re-rank:

- 130-min hedge: aRC probably loses 1.5–3% to friction → IR\*\*\* drops from ~60 to ~30 range. Still beats buy-and-hold but the margin compresses.
- 30-min hedge: paper already flags this as "lowest aRC due to transaction costs" under mid-fill. Under realistic fills it likely turns negative or near-zero.
- **Naked variants gain relative ranking** — the 10.27% naked 5% strangle becomes more attractive vs the hedged version once hedging gets expensive.

This means the paper's claim that "130-min is the sweet spot" is **conditional on optimistic fill assumptions** and likely shifts to "daily or even less frequent" under realistic costs.

---

## 4. The sample is too short for a tail strategy

6 years (2018–2023) covers:

- COVID crash (March 2020) — _intraday_ drawdown, hedger can keep up
- 2022 rate-driven bear (-25% peak-to-trough, slow grind)
- 2021 melt-up (favorable for short premium)

It does **not** cover:

- 1987-style overnight gap (-22%)
- 2008 credit crunch with regime-shifting volatility
- 2020-03-16 type weekend gap (-12% Sunday → Monday open)
- 2024-08-05 Japanese carry unwind (VIX +180% open)

Why this matters for short strangle specifically:

- 5% OTM put with 7 DTE has delta ~0.1 at entry. Under a -22% gap, that delta jumps to ~1.0; gamma between current spot and strike makes the realized loss far worse than linear delta would suggest.
- **130-min hedging cannot help with overnight gaps** by construction. The "Maximum Drawdown" column for the recommended strategy in the paper is therefore an _in-sample realized_ value, not a worst-case bound.

The paper's reported 18.9% MD on the naked 5% strangle in COVID is the worst the strategy actually saw. A 1987-replay would likely produce 60–100% MD on the naked variant and 30–50% on the 130-min hedged variant.

---

## 5. The VIX-rank sizing failure is structural, not bad luck

The paper reports VG-VIX and BSM-VIX sizing produced 0–4% aRC vs 7–8% for delta sizing, and attributes it to "VIX being elevated for most of the sample." That's the symptom, not the cause.

**Cause:** Rolling 1-year VIX percentile is self-normalizing — by construction roughly half the days will be in the "low percentile, add size" zone regardless of absolute VIX level. The sizing failure was specifically that the **VIX mean shifted upward over the sample window** (~12 in 2017 → ~20 in 2022), and the rolling percentile lagged the regime change.

**Implications:**

1. The "VIX sizing doesn't work" conclusion is window-dependent. In a sample where VIX mean was _stable_, VIX-rank sizing might do fine.
2. A better volatility-conditioning rule would use **absolute VIX level** (e.g., reduce size linearly as VIX > 20) or **VIX z-score over a longer window** (5y), not 1-year percentile.
3. None of this rescues the strategy in the 2018–2023 sample, but it changes the lesson from "VIX sizing is bad" to "1-year-rolling-percentile-of-VIX sizing is bad." Worth retesting before discarding the family.

---

## 6. If we deploy this in Xenon — concrete deviations from the paper

Constraints from `CLAUDE.md`:

- **Gate 1 (convexity, defined risk):** rules out naked strangle. We need long protective wings.
- **Gate 3 (Kelly, ≤2.5% per position):** rules out scaling sizing to "1× leverage" notional.
- **Gate 4 (no naked shorts):** rules out short call without a long-stock or long-call cover.

### Recommended deviations

| Paper                            | Xenon deployment                                                                            | Reason                                                                                                                                    |
| -------------------------------- | ------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| 5% OTM short strangle            | 5% / 10% **iron condor** (long protective wings 5% beyond shorts)                           | Gate 1 + 4. Caps gap-risk to wing distance. Costs ~30% of premium.                                                                        |
| 1× leverage delta sizing         | **Fixed 1.5–2% account per condor**, never sized to delta                                   | Gate 3 cap of 2.5%. Predictable risk per position.                                                                                        |
| 130-min intraday hedge           | **Daily hedge at 11:00 ET** (avoids 3:30 close imbalance and 9:30 open noise)               | Realistic fills make 130-min costly; once-daily is operationally simpler and our IB connection isn't built for 13×/week precision orders. |
| Roll at 3:30 PM ET on expiry day | **Roll Thursday 11:00 ET** for Friday-expiring weeklies                                     | Avoids 0DTE gamma cliff on Friday. Pays slightly less theta, much cheaper risk profile.                                                   |
| No gap protection                | **Hard kill switch**: SPX gap > 3% at open → close all short legs immediately, eat the loss | The 1987 / 2020-03-16 protection the paper doesn't have.                                                                                  |
| VIX-rank sizing                  | **Skip**. Use fixed sizing.                                                                 | Paper's negative result + structural reason it's broken.                                                                                  |
| VG model                         | **Skip**. Use BSM only.                                                                     | Paper's negative result. Don't pay complexity for no gain.                                                                                |

Expected aRC after these changes: **~5–6%** instead of 7.96%. We're paying ~2% per year for tail protection and operational simplicity. That's a good trade for live capital.

### Existing Xenon infrastructure that fits this

- `src/xenon/scanners/_shared/` — universe + executor
- IB primary connection — already handles SPX chains, weekly expiries
- `data/options-structures.json` — iron condor is structure #X (verify ID before deploying)
- `xenon.account_snapshots` — Postgres-backed P&L tracking
- `events.outbox` regime transitions — could feed a `gap_kill_switch` consumer

What's missing:

- No backtest harness for option strategies (only signal scanners)
- No SPX historical 1-min option quote data — would need to source ORATS or CBOE LiveVol
- No automated weekly roll scheduler in `server.py` (one would need to be written)

---

## 7. Open questions / next research

In rough priority order:

1. **Reproduce the 5% strangle + daily hedge variant on real SPXW data with 33% spread haircut.** Do we still beat buy-and-hold on Sharpe, or does friction kill it? This is the binary go/no-go.
2. **Extend the backtest to 2008–2024.** WPUT data is publicly available; reconstruct 5% OTM strangle proxy from SPX option history.
3. **Stress-test against synthetic 1987-style overnight gap.** Monte Carlo with -10%, -15%, -22% open gaps; what's the actual worst case under the proposed iron-condor variant?
4. **Compare day-of-week entry choices** (Mon/Wed/Fri weeklies) for variance risk premium capture. WPUT uses Friday-to-Friday — is there a documented edge to other weekdays?
5. **Test absolute VIX-level sizing** (not 1-year rank) to salvage the volatility-conditioning idea on the longer 2008–2024 window.

---

## 8. Authors and context

The paper's first preprint title (_Construction and Hedging of Equity Index Options Portfolios_) framed it as a new model contribution. The published title (_Systematic index option-writing strategies with BSM and VG Models_) reframes as a comparison study. This suggests the VG model originally was the headline contribution but didn't survive review — the published version positions VG as a baseline against which BSM wins.

That's relevant for our use: **the paper's most actionable result is a negative result** (VG hedging doesn't help; VIX-rank sizing doesn't help), not a positive finding of new alpha. The positive finding (130-min hedge cadence is optimal) is conditional on mid-fill assumptions.

---

## 9. Synthetic BSM backtest results (2007–2024)

Separate research session extended the analysis beyond the paper by testing the related 45-DTE
short put / credit spread strategy (popularised by tastylive / 天哥复利之道) using a synthetic
BSM backtest. Code: `scripts/research/spx_short_put_backtest.py`.

**Method**: SPX daily close (yfinance ^GSPC) + VIX daily close (CBOE public CSV) fed into BSM
to compute theoretical put prices. One-at-a-time entry — open new position immediately when
previous closes. 2.5% max capital at risk per trade (Xenon Gate 3). No vol skew, no bid/ask
friction → results ~1–2% CAGR optimistic vs live execution.

**Scenarios tested (2007–2024, 18-year sample covering 2008, 2020, 2022):**

| Scenario                            | CAGR      | Sharpe   | Max DD    | Calmar   | Win rate | Avg loss |
| ----------------------------------- | --------- | -------- | --------- | -------- | -------- | -------- |
| SPX buy-and-hold                    | 8.25%     | 0.50     | 56.8%     | 0.15     | —        | —        |
| A Naked 0.16Δ · 50%/21DTE           | 4.30%     | 0.45     | 32.6%     | 0.13     | 88.4%    | −345% cr |
| B Spread 0.16/0.05Δ · 50%/21DTE     | 4.89%     | 1.10     | **9.8%**  | 0.50     | 87.0%    | −219% cr |
| C Naked 0.16Δ · hold to expiry      | −0.09%    | 0.11     | 61.6%     | 0.00     | 90.5%    | −887% cr |
| D Naked 0.30Δ · 50%/21DTE           | 7.57%     | 0.77     | 24.4%     | 0.31     | 84.5%    | −194% cr |
| E Naked 0.16Δ · VIX>16 filter       | 3.56%     | 0.34     | 51.6%     | 0.07     | 88.9%    | −389% cr |
| F Spread 0.16/0.05Δ · VIX>16        | 4.35%     | 1.00     | 9.8%      | 0.44     | 87.8%    | −227% cr |
| **G Spread 0.30/0.10Δ · 50%/21DTE** | **7.24%** | **1.30** | **13.3%** | **0.54** | 82.2%    | −123% cr |

**Key conclusions from the backtest:**

1. **Scenario C proves the core tastylive claim**: holding naked puts to expiry → −0.09% CAGR
   over 18 years despite 90.5% win rate. 50%/21DTE exit adds ~4.3% CAGR and cuts drawdown in
   half. The exit rule is real, not marketing.

2. **Converting naked → credit spread (A→B) is the single most impactful change**: Max DD
   32.6% → 9.8% with _higher_ CAGR (4.89% vs 4.30%). No CAGR sacrifice for a 3.3× drawdown
   reduction. This is why the naked structure fails Xenon's Gates regardless of the strategy's
   positive EV.

3. **Best overall: scenario G (0.30/0.10Δ credit spread)**: CAGR 7.24% (1% below SPX),
   Sharpe 1.30 (2.6× SPX), Max DD 13.3% (4.3× shallower than SPX). Best risk-adjusted result
   in the tested space.

4. **VIX filter actively harms naked puts (E worse than A on every metric including Max DD)**.
   For credit spreads the filter is neutral, not helpful (F ≈ B with fewer trades). Drop it.
   Separately computed: VIX rank > 40% allows trading on only 1–5% of days in calm years
   (2012, 2017, 2019, 2021), essentially a trading ban. Absolute-VIX or minimum-credit-floor
   filters are better conditioners if any filter is wanted.

5. **VIX rank distribution (CBOE data, rolling 252-day window)**:

   | Year | VIX avg | Days rank > 40% |
   | ---- | ------- | --------------- |
   | 2017 | 11.1    | 2%              |
   | 2019 | 15.4    | 3%              |
   | 2021 | 19.7    | 3%              |
   | 2022 | 25.6    | 63%             |
   | 2023 | 16.8    | 5%              |

   A tastylive-style "IV rank > 50%" rule misses almost entire years in low-vol regimes.

**Recommended filter if any**: require credit collected ≥ 0.5% of spread width at entry.
This is absolute (regime-invariant) and directly tests whether compensation justifies the risk.

---

## Sources

- arxiv preprint (HTML, full tables): https://arxiv.org/html/2407.13908v1
- Published version (Economic Modelling 2025): https://www.sciencedirect.com/science/article/abs/pii/S0264999325002299
- CBOE PUT/WPUT methodology: https://cdn.cboe.com/api/global/us_indices/governance/Cboe_PutWrite_Indices_Methodology.pdf
- CBOE WPUT dashboard: https://www.cboe.com/us/indices/dashboard/wput/
- Bakshi & Kapadia, _Delta-Hedged Gains and the Negative Market Volatility Risk Premium_ (RFS 2003): https://academic.oup.com/rfs/article-abstract/16/2/527/1579962
- Bondarenko, _Historical Performance of Put-Writing Strategies_ (CBOE 2019): https://cdn.cboe.com/resources/education/research_publications/PutWriteCBOE19_v14_by_Prof_Oleg_Bondarenko_as_of_June_14.pdf
- ORATS backtesting methodology (slippage standards): https://orats.com/university/backtesting-methodology
- Backtest script: `scripts/research/spx_short_put_backtest.py`
- VIX historical data: https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv
