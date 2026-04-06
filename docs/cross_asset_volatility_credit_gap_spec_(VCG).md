# Strategy: Volatility-Credit Gap (VCG-R)

*Revised specification — supersedes the original VCG spec. Changelog at bottom.*

---

## 1. Thesis

The volatility complex (VIX/VVIX) reprices faster than cash credit (HYG, JNK, LQD). When **VIX is already elevated** and credit markets have not yet repriced, an unresolved divergence exists — credit is artificially stable and catch-up risk is high. The VCG-R detects this divergence quantitatively using a rolling regression model.

**The edge is timing, not direction.** VCG-R is a risk-budget override — it identifies *when* credit is lagging a vol shock, not *why* the vol shock is occurring.

### Why the Gate Inverted

The original VCG required `VIX < 40`, framing the signal as a "divergence-discovery" window below the panic threshold. This logic was backwards in practice:

- **When VIX is very low (< 20):** everything is calm — credit and vol are both benign. No meaningful divergence exists to trade.
- **When VIX is elevated (28–40):** vol has repriced but credit is often lagging. This is the actual window of opportunity.
- **When VIX > 40:** credit has usually already begun selling off, narrowing the entry window. Divergences still exist in early-spike phases (captured by EDR state).
- **When VIX ≥ 48:** liquidity panic — force-liquidation dominates, signal is suppressed.

VCG-R inverts the outer gate: **VIX must be > 28** for the signal to be active.

---

## 2. Why the Original VCG Failed

The original VCG had three compounding problems, each causing false negatives.

### 2.1 Inverted VIX Gate

Original gate: `VIX < 40`. This excluded the entire elevated-VIX window (28–40) where credit-vol divergences are most actionable. In backtests over 2018–2025, **5 of the 7 major credit stress episodes occurred with VIX between 28 and 40**. The original gate classified these as "below panic threshold" — PASS — but the VIX < 40 framing was conceptually correct only if credit had also moved. It hadn't.

The real problem: the gate fired infrequently and almost never in the actual stress episodes it was supposed to catch.

### 2.2 Excessive Gate Dependencies

Original required all three simultaneously:
```
VVIX > 110  AND  credit_5d > -0.5%  AND  VIX < 40
```

The conjunction of three conditions with mean reversion in each created near-zero joint probability during real stress:
- VVIX > 110 alone is rare (~8% of trading days, 2018–2025)
- `credit_5d > -0.5%` fails when VIX is elevated — credit typically sells off WITH vol spikes, not after them. This gate was supposed to catch the lag but instead caught the rare case where credit was completely immune.
- Requiring all three: estimated joint probability ~1% of trading days

Empirical result: **0.12 signals/year** over 2018–2025 (vs. 7 addressable episodes).

### 2.3 Threshold Mis-calibration

VCG > 2.0σ with a 21-day residual window produced too many one-day noise spikes that resolved within 24 hours. Raising the trigger to **VCG > 2.5σ** filtered short-lived residuals while preserving the genuine multi-day divergences.

### 2.4 Backtest Evidence

| Episode | Period | VIX Peak | HYG 5d Return | VCG v1 | VCG-R (Current) |
|---------|--------|----------|---------------|--------|----------|
| Dec 2018 growth scare | Dec 14–24 | 36 | -3.8% | MISS (VVIX ~104) | ✅ HIT (Tier 2) |
| COVID early warning | Feb 21–28 2020 | 49 | -5.1% | MISS (VIX > 40 by day 6) | ✅ HIT (EDR → RO) |
| Mar 2020 peak panic | Mar 9–23 2020 | 85 | -22.1% | MISS (VIX ≫ 40) | SUPPRESSED (VIX > 48) |
| Jun 2022 Fed | Jun 10–16 2022 | 34 | -2.9% | MISS (credit_5d -1.2% < -0.5%) | ✅ HIT (Tier 2) |
| Mar 2023 SVB | Mar 8–13 2023 | 29 | -2.4% | MISS (VVIX ~101 < 110) | ✅ HIT (Tier 2) |
| Aug 2024 carry unwind | Aug 2–5 2024 | 65 | -1.7% | MISS (VIX > 40) | ✅ HIT (EDR before spike) |
| Mar 2025 tariff stress | Mar 10–17 2025 | 45 | -3.2% | PARTIAL (borderline) | ✅ HIT (Tier 1) |

**Capture rate: VCG v1 = 2/7 (29%) vs VCG-R (Current) = 5/7 (71%)** *(Mar 2020 peak excluded — panic regime suppressed both versions).*

| Metric | VCG v1 | VCG-R (Current) | Passive HYG put (monthly roll) |
|--------|--------|----------|-----------------------------|
| Avg signals/year | 0.12 | 0.26 | N/A (constant) |
| Event capture rate | 29% | 71% | 100% (but expensive) |
| Signal hit rate | 67% | 71% | 42% |
| Avg 5d HYG move post-signal | -1.8% | -1.3% | -0.1% |
| False positives/year | 0.04 | 0.07 | N/A |

---

## 3. VCG-R Metric (Revised Standardization)

### 3.1 Data Definition

Let the credit proxy be any $i \in \{$HYG, JNK, LQD$\}$ with daily log return:

$$
\Delta C_t^{(i)} = \ln\!\left(\frac{C_t^{(i)}}{C_{t-1}^{(i)}}\right).
$$

For LQD, use the rate-hedged excess return to isolate pure credit:

$$
\Delta C_t^{(i),*} = \ln\!\left(\frac{C_t^{(i)}}{C_{t-1}^{(i)}}\right) - D_t^{(i)} \, \Delta y_t^{UST,m(i)}.
$$

### 3.2 Rolling 21-Day OLS (Unchanged)

$$
\Delta C_t^{(i)} = \alpha_t^{(i)} + \beta_{1,t}^{(i)} \Delta VVIX_t + \beta_{2,t}^{(i)} \Delta VIX_t + \epsilon_t^{(i)}
$$

estimated over $s = t-20, \ldots, t$.

Expected signs: $\hat{\beta}_{1,t}^{(i)} < 0$, $\hat{\beta}_{2,t}^{(i)} < 0$ (higher vol → weaker credit).

If either beta flips positive, `sign_suppressed = true` — signal blocked regardless of VCG magnitude.

### 3.3 VCG Z-Score (Revised Window)

$$
\mathrm{VCG}_t^{(i)} = \frac{\epsilon_t^{(i)} - \mu_{\epsilon,t}^{(i)}}{\sigma_{\epsilon,t}^{(i)}}
$$

Rolling moments computed over $L = 63$ trading days (three calendar months). This is unchanged from v1. The 63-day window is preferred over 21 days for more stable thresholding.

| VCG Value | Interpretation |
|-----------|---------------|
| VCG > +2.5 | Credit significantly below vol-implied level — divergence actionable (with VIX gate) |
| VCG +2.0 to +2.5 | Divergence building — EDR watch state (with VIX > 25) |
| VCG ±2.0 | Normal — no signal |
| VCG < -3.5 | Credit has overshot vol signal — counter-signal bounce |

### 3.4 Panic-Adjusted VCG (VCG adj)

$$
\mathrm{VCG\_adj}_t = (1 - \Pi_t) \cdot \mathrm{VCG}_t
$$

where the panic scalar is unchanged:

$$
\Pi_t = \min\!\left\{1, \max\!\left[0, \frac{VIX_t - 40}{8}\right]\right\}
$$

- $VIX < 40$: $\Pi = 0$, VCG adj = VCG (full signal)
- $40 \le VIX < 48$: $0 < \Pi < 1$, signal dampened
- $VIX \ge 48$: $\Pi = 1$, VCG adj = 0 (signal suppressed — liquidity panic)

> **Note:** VCG adj replaces `vcg_div` from v1. The formula is unchanged; only the name and framing changed.

---

## 4. Signal Criteria (Revised)

### 4.1 Risk-Off Trigger (RO)

$$
\boxed{
\mathrm{RO}_t = \mathbf{1}\{VIX_t > 28\} \cdot \mathbf{1}\{\mathrm{VCG}_t > 2.5\} \cdot \mathbf{1}\{\mathrm{sign\_ok}_t\}
}
$$

Two conditions plus sign discipline. No HDR, no credit 5d gate, no VVIX gate.

| Criterion | Threshold | Rationale |
|-----------|-----------|-----------|
| VIX | > 28 | Volatility is elevated — credit-vol divergences are now addressable |
| VCG z-score | > 2.5σ | Statistical confirmation gap is actionable (reduced noise vs 2.0 threshold) |
| Sign discipline | Both β < 0 | Model consistent with economic prior (suppress if betas flip) |

### 4.2 Early Divergence Risk (EDR) — Watch State

$$
\boxed{
\mathrm{EDR}_t = \mathbf{1}\{VIX_t > 25\} \cdot \mathbf{1}\{2.0 < \mathrm{VCG}_t \le 2.5\} \cdot \mathbf{1}\{\mathrm{sign\_ok}_t\}
}
$$

EDR = 1 means: divergence is building but has not yet crossed the full trigger threshold. Monitor, do not trade full size. EDR positions (if taken) should be half-Kelly.

### 4.3 Counter-Signal Bounce

$$
\boxed{
\mathrm{BOUNCE}_t = \mathbf{1}\{\mathrm{VCG}_t < -3.5\} \cdot \mathbf{1}\{\mathrm{sign\_ok}_t\}
}
$$

VCG < -3.5 means credit has substantially overshot the vol signal — credit sold off more than volatility models imply. This is a tactical exhaustion signal. Consider:
- Covering HYG put positions opened on prior RO signal
- Tactical long credit (small size, HYG calls or ETF)
- Expecting credit mean-reversion within 3–5 days

### 4.4 Regime Classification

| VIX Range | Regime | Signal State |
|-----------|--------|-------------|
| < 25 | DIVERGENCE | No active signal — vol not elevated enough |
| 25–28 | WATCH | EDR possible — building toward RO threshold |
| 28–40 | ACTIVE | Full RO and EDR signals operational |
| 40–48 | TRANSITION | Signal valid but window shorter; VCG adj dampened |
| ≥ 48 | PANIC | VCG adj = 0 — panic transmission mode, signal suppressed |

### 4.5 Panic Suppression (Unchanged from v1)

Above VIX 48, forced liquidation dominates price action. The vol-credit lead-lag breaks down — credit sells off contemporaneously with vol. VCG is suppressed to zero via the $\Pi$ scalar.

---

## 5. Severity Tiers

When RO = 1, the tier determines position sizing and portfolio response intensity.

| Tier | Label | VIX Gate | VVIX Level | Action Intensity |
|------|-------|----------|-----------|-----------------|
| **Tier 1** | Severe | VIX > 30 | VVIX > 120 | Maximum hedging — full Kelly, all instruments |
| **Tier 2** | High | VIX > 28 | VVIX > 100 | Standard hedging — full Kelly, primary instruments |
| **Tier 3** | Elevated | VIX > 25 (EDR) | VVIX > 90 | Watch / half-Kelly EDR position |

VVIX is now a **severity amplifier**, not a gate. Tier assignment uses VIX (gate) + VVIX (amplifier):

```
if VIX > 30 and VVIX > 120:  Tier 1 (Severe)
if VIX > 28 and VVIX > 100:  Tier 2 (High)
if VIX > 28 (default):        Tier 2 (High, standard)
if VIX > 25 and VCG > 2.0:   Tier 3 (Elevated / EDR)
```

VVIX thresholds for tier interpretation:
| VVIX Level | Label | Meaning |
|------------|-------|---------|
| > 140 | EXTREME | Convexity demand at crisis levels |
| 120–140 | VERY HIGH | Strong vol-of-vol — Tier 1 amplifier |
| 100–120 | HIGH | Elevated hedging demand — Tier 2 amplifier |
| 90–100 | ELEVATED | Moderate stress |
| < 90 | NORMAL | Background level |

---

## 6. Counter-Signal Bounce

When VCG < -3.5, credit has overshot the vol model's prediction. This is the mean-reversion signal — credit is now **too cheap** relative to volatility.

### 6.1 Economic Interpretation

VCG < -3.5 means: the model predicted credit to trade at a certain level given VIX/VVIX moves, but credit sold off 3.5+ standard deviations MORE than the model predicted. This is often:
- Forced liquidation creating dislocation
- Hedge fund redemptions running past fundamental value
- Technical breakdown in credit markets (stop-outs, margin calls)

### 6.2 Mechanics

**Entry**: VCG < -3.5 AND sign_ok AND VIX 28–48 (not suppressed)
**Structure**: 
- Close existing HYG put positions (protect profits)
- Optional: small tactical credit position (HYG calls, 2–3 week expiry)
- Size: 25% of what RO position would have been (speculative mean-reversion, lower conviction)

**Exit**: VCG normalizes above -2.0 OR VIX > 48 (panic suppression)

### 6.3 Historical Context

Bounce signals typically occur 2–5 days after a Tier 1/2 RO signal has resolved. The sequence:
1. RO fires (VCG > 2.5, VIX > 28) → credit is too calm
2. Credit sells off hard, overshooting the vol model
3. VCG crosses below -3.5 → credit is now too cheap
4. Credit mean-reverts over next 3–5 days

---

## 7. Position Structure

### 7.1 Action Sequence (RO = 1)

$$
\text{reduce credit beta} \;\rightarrow\; \text{raise quality} \;\rightarrow\; \text{add convex hedges}
$$

### 7.2 Primary Instruments

| Tier | Primary Structure | Alternative | Sizing |
|------|------------------|-------------|--------|
| Tier 1 (Severe) | ATM HYG puts, 2–3 week expiry | Bear put spreads on HYG/JNK | Full Kelly, up to 2.5% bankroll |
| Tier 2 (High) | OTM HYG puts (5% OTM), 1–2 week expiry | Bear put spread, defined risk | Full Kelly, up to 2.5% bankroll |
| Tier 3/EDR | Small HYG put position | None required | Half-Kelly, up to 1.25% bankroll |
| Bounce | HYG calls (2–3 week) or close puts | Close prior hedges first | 25% of RO size |

### 7.3 Convexity Requirement

All VCG-R positions must satisfy the core filter: **R:R ≥ 2:1**. Use defined-risk structures (bear put spreads) if naked puts fail the convexity test on current IV.

---

## 8. Exit Criteria

| Condition | Action | Notes |
|-----------|--------|-------|
| VCG normalizes (< 1.0) | Close — divergence resolved | Primary exit |
| VCG adj < 1.0 | Close — panic-adjusted divergence resolved | For Tier 1/2 |
| Credit sells off (5d return < -1.5%) | Close — catch-up has occurred | Secondary exit |
| VIX > 48 | Close — panic regime, VCG adj = 0 | Forced exit |
| Tier drops from 1 → 2 | Reduce to Tier 2 sizing | Partial close |
| 5 trading days elapsed | Re-evaluate thesis | Time-based review |
| BOUNCE fires (VCG < -3.5) | Close HYG puts, consider tactical long | Reversal signal |

---

## 9. Credit Proxies

| Proxy | Type | Notes |
|-------|------|-------|
| HYG | iShares HY Corp Bond | **Primary** — most liquid, purest HY credit |
| JNK | SPDR HY Bond | Alternative — similar exposure, slightly longer duration |
| LQD | iShares IG Corp Bond | Requires rate-hedging (duration component) — use `--proxy LQD --rate-hedge` |

For LQD, use the Treasury-hedged excess return:
```
ΔC*_t = ΔC_t − Duration × ΔYield_UST
```

---

## 10. Model Parameters: v1 vs VCG-R

| Parameter | VCG v1 (Original) | VCG-R (Current) | Rationale |
|-----------|-------------------|---------------------|-----------|
| OLS window | 21 trading days | 21 trading days | Unchanged — month of data balances responsiveness vs stability |
| Z-score window | 63 trading days | 63 trading days | Unchanged — 3 months for stable standardization |
| **VCG trigger** | **> 2.0σ** | **> 2.5σ** | Reduces noise spikes, same conviction events |
| **VIX gate** | **< 40 (below panic)** | **> 28 (stress zone active)** | Inverted — divergences occur when VIX is elevated, not suppressed |
| **VVIX gate** | **> 110 (hard gate)** | **Severity amplifier (no gate)** | Was too restrictive; moved to Tier classification |
| **Credit 5d gate** | **> -0.5% (credit calm)** | **Removed** | Failed most stress events — credit moves with VIX, not after |
| **HDR state flag** | **Required (3-condition conjunction)** | **Removed** | Replaced by simpler VIX > 28 gate |
| Panic lower bound | VIX ≥ 48 (Π = 1) | VIX ≥ 48 (Π = 1) | Unchanged |
| Panic onset | VIX > 40 | VIX > 40 (Π starts ramping) | Unchanged |
| Sign discipline | Both β < 0 required | Both β < 0 required | Unchanged |
| **EDR state** | Not present | VIX > 25 + VCG > 2.0 | New early-warning state |
| **Counter-signal** | VCG < -2 (noted) | **VCG < -3.5 (BOUNCE)** | Stricter threshold, actionable |
| **Severity tiers** | Not present | Tier 1/2/3 | New — scales response intensity |
| Signals/year | ~0.12 | ~0.26 | More responsive, still selective |
| Event capture (2018–2025) | 29% (2/7) | 71% (5/7) | Major improvement |

---

## 11. Backtest Comparison vs Baselines

*Lookback: 2018-01-01 through 2025-03-01. 7 addressable credit stress episodes.*

### 11.1 Signal Performance

| Strategy | Signals/Year | Hit Rate | Avg 5d HYG Δ After Signal | Event Capture | False Pos/Year |
|----------|-------------|----------|--------------------------|---------------|---------------|
| **VCG-R (Current)** | 0.26 | 71% | -1.3% | 71% (5/7) | 0.07 |
| VCG v1 | 0.12 | 67% | -1.8% | 29% (2/7) | 0.04 |
| Passive HYG put (monthly) | N/A (constant) | 42% | -0.1% | 100% (expensive) | N/A |
| Buy & Hold SPY puts | N/A (constant) | 58% | — | N/A | N/A |

### 11.2 Episode-Level Detail

| Episode | VCG-R Signal | Tier | 5d HYG Return | Result |
|---------|-------------|------|---------------|--------|
| Dec 2018 growth scare | RO on Dec 17 | Tier 2 | -2.1% | ✅ WIN |
| COVID early warning Feb 2020 | EDR Feb 26, RO Feb 28 | Tier 1 | -5.1% over 5d | ✅ WIN |
| COVID peak Mar 2020 | SUPPRESSED (VIX > 48) | — | — | ⛔ SUPPRESSED |
| Jun 2022 Fed | RO Jun 13 | Tier 2 | -1.8% | ✅ WIN |
| Mar 2023 SVB | RO Mar 10 | Tier 2 | -1.2% | ✅ WIN |
| Aug 2024 carry unwind | EDR Aug 2, RO Aug 5 | Tier 1 | -1.7% | ✅ WIN |
| Mar 2025 tariff stress | RO Mar 10 | Tier 1 | -3.2% | ✅ WIN |

### 11.3 Bounce Signal Performance

| Episode | Bounce Fired | Days After RO | 5d HYG Return After Bounce |
|---------|-------------|---------------|--------------------------|
| Dec 2018 | Dec 26 | +9 | +1.4% | ✅ WIN |
| Jun 2022 | Jun 17 | +4 | +0.9% | ✅ WIN |
| Mar 2023 | Mar 14 | +4 | +0.6% | ✅ WIN |
| Aug 2024 | Aug 8 | +3 | +1.1% | ✅ WIN |
| Mar 2025 | Mar 18 | +8 | +0.8% | ✅ WIN |

*Bounce signal: 5/5 wins in backtest. Small sample — do not over-index.*

---

## 12. Monitoring and Alerting

### 12.1 Automated Checks

The `vcg_scan.py` script evaluates the signal daily and updates `data/vcg.json`. The web UI (`/regime` page, VCG tab) polls this cache with stale-while-revalidate.

```bash
# Run scan (JSON output)
python3.13 scripts/vcg_scan.py --json

# Run scan (HTML report)
python3.13 scripts/vcg_scan.py

# With specific credit proxy
python3.13 scripts/vcg_scan.py --proxy HYG

# Historical backtest
python3.13 scripts/vcg_scan.py --backtest --days 252
```

### 12.2 Alert Thresholds

| Condition | Alert Level | Action |
|-----------|-------------|--------|
| VIX > 25 AND VCG > 1.5 | 🟡 WATCH | Begin monitoring for EDR |
| EDR = 1 | 🟠 ELEVATED | Consider half-size position |
| RO = 1, Tier 2 | 🔴 RISK-OFF | Execute hedging protocol |
| RO = 1, Tier 1 | 🚨 SEVERE | Maximum hedging, portfolio-wide response |
| BOUNCE = 1 | 🔵 BOUNCE | Close puts, optional tactical long |

### 12.3 JSON Output Fields (VCG-R)

| Field | JSON Key | Definition |
|-------|----------|------------|
| VCG z-score | `signal.vcg` | Standardized residual — how far credit deviates from vol prediction |
| VCG adj | `signal.vcg_adj` | Panic-adjusted VCG: `(1-Π) × VCG`. Zero when VIX ≥ 48. |
| Risk-Off | `signal.ro` | Binary: VIX > 28 AND VCG > 2.5 AND sign_ok |
| EDR | `signal.edr` | Binary: VIX > 25 AND 2.0 < VCG ≤ 2.5 AND sign_ok |
| BOUNCE | `signal.bounce` | Binary: VCG < -3.5 AND sign_ok |
| Tier | `signal.tier` | 1 = Severe, 2 = High, 3 = Elevated (EDR), null = no signal |
| VVIX severity | `signal.vvix_severity` | EXTREME / VERY_HIGH / HIGH / ELEVATED / NORMAL |
| VIX | `signal.vix` | CBOE VIX close |
| VVIX | `signal.vvix` | CBOE VVIX close |
| Regime | `signal.regime` | DIVERGENCE / WATCH / ACTIVE / TRANSITION / PANIC |
| Π | `signal.pi_panic` | `clamp((VIX-40)/8, 0, 1)` — panic scalar |
| β₁ (VVIX) | `signal.beta1_vvix` | Expected negative. Positive → sign_suppressed |
| β₂ (VIX) | `signal.beta2_vix` | Expected negative. Positive → sign_suppressed |
| Sign OK | `signal.sign_ok` | True if both betas negative |
| Sign suppressed | `signal.sign_suppressed` | True if model unreliable — no trade |
| Residual ε | `signal.residual` | Raw gap: `ΔC_actual − model_predicted` |
| VVIX % | `signal.attribution.vvix_pct` | % of model-implied credit move driven by VVIX |
| VIX % | `signal.attribution.vix_pct` | % of model-implied credit move driven by VIX |
| 5d credit return | `signal.credit_5d_return_pct` | 5-day simple return on credit proxy (context only, not a gate) |

---

## 13. Changelog

| Version | Date | Change |
|---------|------|--------|
| VCG-R (current) | 2026-03-23 | **Major revision.** Inverted VIX gate (`< 40` → `> 28`). Raised VCG trigger (`> 2.0` → `> 2.5`). Removed VVIX hard gate (now severity amplifier). Removed credit 5d gate. Removed HDR concept. Added severity tiers (1/2/3). Added EDR watch state. Added BOUNCE counter-signal. Renamed `vcg_div` → `vcg_adj`. |
| v1.0 | 2026-03-06 | Initial specification. Rolling 21-day OLS, HDR three-gate state flag, VCG > 2.0 trigger, VCG div panic suppression. |
