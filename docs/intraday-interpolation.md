# Intraday Dark Pool Interpolation

When evaluating during market hours, today's partial data is volume-weighted interpolated to estimate full-day values. **Always output BOTH actual and interpolated values.**

## Why Interpolation is Required

Comparing today's partial data (e.g., 45% of day) to yesterday's full-day data is misleading. A "55% buy ratio" at noon could become 75% by close, or could be masking active distribution.

## Calculation Method

**Step 1: Trading Day Progress**
```
Progress = Minutes Since 9:30 AM ET / 390 minutes
```

**Step 2: Project Today's Volume**
```
Projected Volume = Actual Volume / Progress
Projected Buy = Actual Buy Volume / Progress
Projected Sell = Actual Sell Volume / Progress
```

**Step 3: Blend with Prior Pattern**
```
Actual Weight = Progress (e.g., 0.45 at noon)
Prior Weight = 1 - Progress (e.g., 0.55)

Prior Avg Buy Ratio = Mean of prior 5 days' buy ratios
Blended Ratio = (Today's Projected Ratio × Actual Weight) + (Prior Avg × Prior Weight)
```

**Step 4: Recalculate Aggregate**
Use interpolated today + actual prior days for aggregate strength.

## Confidence Levels

| Progress | Confidence | Blending |
|----------|------------|----------|
| 0-25% | VERY_LOW | 75%+ prior weight |
| 25-50% | LOW | 50-75% prior weight |
| 50-75% | MEDIUM | 25-50% prior weight |
| 75-100% | HIGH | <25% prior weight |

## Volume Pace

```
Expected Volume = Avg Prior Volume × Progress
Volume Pace = Actual Volume / Expected Volume
```

Pace >1.1x = above average (signal more reliable). Pace <0.9x = below average (signal less reliable).

## Output Format (MANDATORY)

Always show both when `is_interpolated: true`:

```
TODAY'S FLOW (45% of trading day)
                      ACTUAL          INTERPOLATED
  Buy Ratio:           25.4%           53.3%
  Direction:          DISTRIBUTION   NEUTRAL
  Strength:            49.3             0.0

AGGREGATE (5-Day)
                      ACTUAL          INTERPOLATED
  Buy Ratio:           70.4%           65.3%
  Strength:            40.7            30.6
```

## Edge Assessment with Interpolation

Use **interpolated values** for edge determination, but flag confidence level:
- LOW/VERY_LOW confidence → recommend re-evaluation after 2 PM ET
- Volume pace >1.2x → signal is real despite partial data
- Today's actual direction opposite to prior pattern → likely reversal, not noise
