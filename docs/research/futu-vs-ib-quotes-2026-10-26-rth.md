# Futu vs IB quotes calibration (2026-10-26, rth)

Source: `scripts/research/futu_vs_ib_quotes.py` (X7-0, plan v2.1 section D8). Every number below was observed in this run; nothing is estimated or carried over from another run. n/a means no matched data.

## Run

- Window (ET): 2026-10-26 10:13:49 EDT to 2026-10-26 10:26:09 EDT
- Window bounds: derived from the raw samples (the run did not record them): start = earliest IB request time, end = time of the first `unsubscribe` record
- Session mode: `rth`; requested minutes: 12.0; IB poll interval: 30.0 s; IB cycles: 25
- OpenD: `host.docker.internal:11111`; xenon API: `http://127.0.0.1:8321`
- Raw samples: `output/research/futu-vs-ib-quotes-2026-10-26-rth-101345.jsonl` (gitignored; 26334 book pushes, 22336 quote pushes, 275 IB samples)
- Provenance: the run executed inside the prod api container (read-only: no orders, no trade context; IB only through the xenon API endpoints). Tables re-rendered offline with `--from-jsonl` from the stored raw file.
- Units: price gaps are in cents (dollars x 100). "within 1¢" and "within 5¢" are policy tolerance, not the instrument's tick; the instrument and combo price increments were not verified.
- Matched time: Futu value = latest Futu push with receipt time at or before the IB sample time. IB `last` is not exposed by the IB endpoints, so "Futu last - IB mid" is only a coarse indicator.

## Instruments

| Key | Kind | Futu code |
| --- | --- | --- |
| AAPL | STK | `US.AAPL` |
| SPY | STK | `US.SPY` |
| NVDA | STK | `US.NVDA` |
| QQQ | STK | `US.QQQ` |
| TSLA | STK | `US.TSLA` |
| AAPL_20261030_340_C | OPT | `US.AAPL261030C340000` |
| AAPL_20261030_350_C | OPT | `US.AAPL261030C350000` |
| SPY_20261030_760_P | OPT | `US.SPY261030P760000` |
| SPY_20261030_770_P | OPT | `US.SPY261030P770000` |
| AAPL_20261218_330_P | OPT | `US.AAPL261218P330000` |
| SPY_20261218_780_C | OPT | `US.SPY261218C780000` |

- Vertical (long:short): `AAPL_20261030_340_C:AAPL_20261030_350_C`

- Vertical (long:short): `SPY_20261030_770_P:SPY_20261030_760_P`

## Quota units measured

Differences of `query_subscription(is_all_conn=True)` between steps. `own` is this connection, `total` is all connections (argon shares the account, so `total` can move for other reasons).

| Step | OK subscribes | d own_used | d total_used | own per instrument | total per instrument |
| --- | --- | --- | --- | --- | --- |
| QUOTE stocks | 5 | 5 | 0 | 1.00 | 0.00 |
| QUOTE options | 6 | 0 | 0 | 0.00 | 0.00 |
| ORDER_BOOK stocks | 5 | 5 | 5 | 1.00 | 1.00 |
| ORDER_BOOK options | 6 | 0 | 0 | 0.00 | 0.00 |

Raw snapshots: before: own_used=0, total_used=171, remain=829; QUOTE stocks: own_used=5, total_used=171, remain=829; QUOTE options: own_used=5, total_used=171, remain=829; ORDER_BOOK stocks: own_used=10, total_used=176, remain=824; ORDER_BOOK options: own_used=10, total_used=176, remain=824

## Subscribe results and refusals

- Subscribe attempts: 22; refused: 0
- Unsubscribe attempts: 22; failed: 0

## Agreement at matched times (cents, Futu minus IB)

Signed columns show median [Q1, Q3] (n). `|mid|` columns are absolute. 1¢ and 5¢ are policy tolerance, not the instrument's tick.

| Key | IB samples | IB errors | pairs | bid | ask | mid | median abs mid | p90 abs mid | max abs mid | within 1¢ | within 5¢ | Futu last - IB mid |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| AAPL | 25 | 0 | 25 | 0.00 [-1.00, 3.00] (n=25) | 0.00 [-2.00, 1.00] (n=25) | 0.00 [-1.50, 2.00] (n=25) | 2.00 | 6.40 | 11.00 | 40% | 84% | -1.00 [-2.50, 2.50] (n=25) |
| SPY | 25 | 0 | 25 | 2.00 [-1.00, 3.00] (n=25) | 1.00 [-1.00, 3.00] (n=25) | 1.50 [-1.50, 3.00] (n=25) | 2.50 | 8.80 | 14.00 | 20% | 72% | 0.50 [-1.50, 2.50] (n=25) |
| NVDA | 25 | 0 | 25 | 0.00 [-2.00, 3.00] (n=25) | 0.00 [-2.00, 2.00] (n=25) | 0.50 [-1.50, 2.50] (n=25) | 2.50 | 4.90 | 6.00 | 28% | 88% | 0.50 [-2.50, 2.50] (n=25) |
| QQQ | 25 | 0 | 25 | -2.00 [-3.00, 5.00] (n=25) | -1.00 [-4.00, 6.00] (n=25) | -2.00 [-4.00, 5.50] (n=25) | 4.50 | 11.30 | 13.50 | 4% | 52% | 0.41 [-3.50, 5.00] (n=25) |
| TSLA | 25 | 0 | 25 | 1.00 [-5.00, 4.00] (n=25) | 0.00 [-5.00, 4.00] (n=25) | 1.50 [-5.00, 5.50] (n=25) | 5.00 | 26.10 | 48.50 | 8% | 52% | 1.10 [-4.99, 6.00] (n=25) |
| AAPL_20261030_340_C | 25 | 0 | 15 | 0.00 [0.00, 0.00] (n=15) | 0.00 [0.00, 0.00] (n=15) | 0.00 [0.00, 0.00] (n=15) | 0.00 | 2.50 | 7.50 | 80% | 93% | 1.50 [-4.25, 6.00] (n=15) |
| AAPL_20261030_350_C | 25 | 1 | 17 | 0.00 [0.00, 0.00] (n=17) | 0.00 [0.00, 0.00] (n=17) | 0.00 [-0.50, 0.00] (n=17) | 0.00 | 2.50 | 3.00 | 76% | 100% | 0.00 [-1.50, 1.00] (n=17) |
| SPY_20261030_760_P | 25 | 0 | 11 | 0.00 [-1.00, 0.00] (n=11) | 0.00 [-0.50, 0.00] (n=11) | 0.00 [-0.50, 0.00] (n=11) | 0.50 | 1.00 | 2.00 | 91% | 100% | -0.50 [-2.50, 0.50] (n=11) |
| SPY_20261030_770_P | 25 | 0 | 12 | 0.00 [-0.25, 1.25] (n=12) | 0.00 [-0.25, 1.00] (n=12) | 0.00 [-0.25, 0.75] (n=12) | 0.75 | 2.95 | 5.00 | 58% | 100% | 3.50 [-4.62, 12.88] (n=12) |
| AAPL_20261218_330_P | 25 | 0 | 14 | 0.00 [0.00, 0.00] (n=14) | 0.00 [0.00, 0.00] (n=14) | 0.00 [0.00, 0.00] (n=14) | 0.00 | 0.00 | 0.00 | 100% | 100% | 20.00 [11.88, 20.00] (n=14) |
| SPY_20261218_780_C | 25 | 0 | 11 | 0.00 [-1.50, 0.50] (n=11) | 0.00 [-1.00, 0.00] (n=11) | -0.50 [-1.00, 0.25] (n=11) | 0.50 | 2.50 | 3.00 | 64% | 100% | -0.50 [-2.50, 2.25] (n=11) |

## IB request duration

Per IB sample: response time minus request time, in seconds. The time at which IB acquired the quoted value inside that interval is not recorded, so this table does not say which source is fresher.

| Key | IB samples with request time | median (s) | p90 (s) |
| --- | --- | --- | --- |
| AAPL | 25 | 2.012 | 2.016 |
| SPY | 25 | 2.005 | 2.006 |
| NVDA | 25 | 2.005 | 2.018 |
| QQQ | 25 | 2.005 | 2.009 |
| TSLA | 25 | 2.005 | 2.016 |
| AAPL_20261030_340_C | 25 | 1.833 | 2.716 |
| AAPL_20261030_350_C | 25 | 1.945 | 2.984 |
| SPY_20261030_760_P | 25 | 1.563 | 2.014 |
| SPY_20261030_770_P | 25 | 1.607 | 2.697 |
| AAPL_20261218_330_P | 25 | 1.571 | 2.250 |
| SPY_20261218_780_C | 25 | 1.583 | 1.946 |

## Futu freshness and push cadence

Ages in seconds at each IB sample time. BOOK last age at end uses the latest push received at or before the window end. Quote source age is the QUOTE `data_date`/`data_time` (ET wall clock) against the sample time; ORDER_BOOK has no source time, so its age is receipt time.

| Key | QUOTE pushes | QUOTE pushes/min | QUOTE receipt age median / p90 | QUOTE source age median | BOOK pushes | BOOK pushes/min | BOOK last age at end (s) | BOOK age median / p90 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| AAPL | 3244 | 263.02 | 0.14 / 0.32 | 0.52 | 4731 | 383.70 | 0.1 | 0.05 / 0.07 |
| SPY | 3399 | 275.43 | 0.09 / 0.30 | 0.52 | 5240 | 424.82 | 0.3 | 0.07 / 0.10 |
| NVDA | 4866 | 394.49 | 0.10 / 0.24 | 0.43 | 5215 | 422.95 | 0.1 | 0.17 / 0.18 |
| QQQ | 4146 | 336.01 | 0.12 / 0.46 | 0.38 | 5245 | 425.39 | 0.1 | 0.07 / 0.18 |
| TSLA | 6297 | 510.38 | 0.06 / 0.15 | 0.29 | 4850 | 393.19 | 0.3 | 0.09 / 0.19 |
| AAPL_20261030_340_C | 25 | 2.03 | 14.52 / 41.56 | 28.94 | 146 | 11.84 | 3.2 | 2.86 / 4.91 |
| AAPL_20261030_350_C | 143 | 11.60 | 4.81 / 12.50 | 5.06 | 222 | 18.00 | 2.6 | 1.90 / 4.37 |
| SPY_20261030_760_P | 157 | 12.73 | 3.43 / 12.26 | 3.66 | 238 | 19.30 | 3.7 | 1.44 / 3.29 |
| SPY_20261030_770_P | 32 | 2.60 | 20.02 / 46.38 | 20.81 | 160 | 12.98 | 2.4 | 2.68 / 4.70 |
| AAPL_20261218_330_P | 7 | 0.57 | 51.51 / 151.99 | 412.07 | 133 | 10.79 | 0.2 | 2.54 / 4.77 |
| SPY_20261218_780_C | 20 | 1.62 | 27.85 / 55.65 | 34.81 | 154 | 12.49 | 4.3 | 3.13 / 4.83 |

## Greek ratios (Futu / IB; Futu IV divided by 100 first)

Median [Q1, Q3] (n). A conversion factor from Futu units to IB units is the inverse of the median ratio.

| Key | iv | delta | gamma | vega | theta |
| --- | --- | --- | --- | --- | --- |
| AAPL_20261030_340_C | 1.0093 [0.9979, 1.0317] (n=25) | 0.9924 [0.9607, 0.9994] (n=25) | 0.9891 [0.9859, 1.0104] (n=25) | 1.0229 [1.0088, 1.0322] (n=25) | 1.0210 [0.9783, 1.0893] (n=25) |
| AAPL_20261030_350_C | 1.0146 [1.0030, 1.0267] (n=24) | 0.9964 [0.9724, 1.0068] (n=24) | 0.9813 [0.9402, 0.9898] (n=24) | 0.9507 [0.9485, 0.9729] (n=24) | 1.0074 [0.9972, 1.0297] (n=24) |
| SPY_20261030_760_P | 0.9936 [0.9903, 0.9952] (n=25) | 1.0018 [0.9821, 1.0067] (n=25) | 0.9999 [0.9921, 1.0015] (n=25) | 0.9840 [0.9742, 0.9895] (n=25) | 0.9867 [0.9823, 0.9992] (n=25) |
| SPY_20261030_770_P | 0.9914 [0.9839, 0.9931] (n=25) | 1.0030 [0.9989, 1.0046] (n=25) | 0.9963 [0.9919, 0.9979] (n=25) | 0.9784 [0.9721, 0.9841] (n=25) | 1.0001 [0.9814, 1.0136] (n=25) |
| AAPL_20261218_330_P | 1.0091 [1.0056, 1.0134] (n=25) | 0.9949 [0.9938, 0.9999] (n=25) | 0.9768 [0.9694, 0.9818] (n=25) | 1.0233 [1.0065, 1.0259] (n=25) | 1.0479 [1.0450, 1.0527] (n=25) |
| SPY_20261218_780_C | 0.9897 [0.9880, 0.9935] (n=25) | 0.9983 [0.9956, 1.0059] (n=25) | 0.9921 [0.9898, 0.9931] (n=25) | 1.0300 [1.0217, 1.0350] (n=25) | 1.0167 [1.0137, 1.0193] (n=25) |
| **all options** | 0.9956 [0.9903, 1.0093] (n=149) | 0.9989 [0.9909, 1.0059] (n=149) | 0.9908 [0.9818, 0.9967] (n=149) | 0.9945 [0.9782, 1.0245] (n=149) | 1.0137 [0.9957, 1.0323] (n=149) |

## Spread gap vs the all-IB spread (cents)

Spread = long leg mid minus short leg mid. Each Futu-based spread is compared with the all-IB spread of the same cycle: all-Futu, Futu long with IB short, and IB long with Futu short. Gap = Futu-based spread minus all-IB spread. A Futu leg value is the latest Futu push at or before that leg's own IB response time. A cycle counts as a pair only when all four leg mids exist. 1¢ and 5¢ are policy tolerance, not the instrument's tick.

| Vertical (long:short) | Spread source | cycles | pairs | gap median [Q1, Q3] | median abs | p90 abs | max abs | within 1¢ | within 5¢ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `AAPL_20261030_340_C:AAPL_20261030_350_C` | all-Futu | 25 | 11 | 0.00 [-1.25, 0.25] (n=11) | 0.50 | 3.00 | 5.00 | 64% | 100% |
| `AAPL_20261030_340_C:AAPL_20261030_350_C` | Futu long / IB short | 25 | 11 | 0.00 [-1.25, 0.00] (n=11) | 0.00 | 2.50 | 7.50 | 73% | 91% |
| `AAPL_20261030_340_C:AAPL_20261030_350_C` | IB long / Futu short | 25 | 11 | 0.00 [0.00, 0.50] (n=11) | 0.00 | 1.50 | 2.50 | 82% | 100% |
| `SPY_20261030_770_P:SPY_20261030_760_P` | all-Futu | 25 | 5 | -0.50 [-2.00, 1.50] (n=5) | 2.00 | 4.00 | 5.00 | 20% | 100% |
| `SPY_20261030_770_P:SPY_20261030_760_P` | Futu long / IB short | 25 | 5 | 0.50 [-1.00, 1.50] (n=5) | 1.50 | 4.00 | 5.00 | 40% | 100% |
| `SPY_20261030_770_P:SPY_20261030_760_P` | IB long / Futu short | 25 | 5 | 0.00 [-1.00, 0.00] (n=5) | 0.00 | 1.00 | 1.00 | 100% | 100% |

### IB leg-response time separation

Time between the two IB leg responses of one pair. The legs are sampled serially, so these spreads are not a simultaneous combo quote.

| Vertical (long:short) | pairs | min (s) | median (s) | max (s) |
| --- | --- | --- | --- | --- |
| `AAPL_20261030_340_C:AAPL_20261030_350_C` | 11 | 1.43 | 2.08 | 3.15 |
| `SPY_20261030_770_P:SPY_20261030_760_P` | 5 | 1.71 | 3.00 | 4.04 |

## Decisions (lead, 2026-10-26, revised after review round 1)

Observed = from the tables above. *Unresolved* = this run cannot decide it. *Policy* = a lead choice, not a measurement.

- **Greek unit factors (observed).** Futu / IB median ratios over 149 matched option samples: IV (after /100) 0.996, delta 0.999, gamma 0.991, vega 0.995, theta 1.014; per-contract medians 0.951–1.048. Futu greeks are in IB units; only IV needs /100. `FUTU_GREEK_FACTORS={"iv":0.01,"delta":1,"gamma":1,"vega":1,"theta":1}` for X7a. This normalizes units only; it does not certify greek freshness, model agreement or other contract classes.
- **Quota counter (observed).** Stocks: `own_used` +1 for QUOTE and +1 for ORDER_BOOK per stock (2 units). Options: `own_used` did not move for QUOTE or ORDER_BOOK on 6 options, while the subscription lists did add them. Whether options draw on a quota this counter does not report is *unresolved*; `FUTU_UNITS_PER_SYMBOL=2` for every symbol stays as the conservative *policy*.
- **ORDER_BOOK for options (observed).** 6/6 subscribes succeeded, 0 refusals, 10.8–19.3 book pushes/min. At the 25 sampled times the book age p90 was ≤ 4.9 s; the longest gap between two book pushes on one contract was ~10 s (AAPL 340C). Book liveness is not proven by this run; contract Amendment A1 (an unchanged book stays valid, no age cutoff by default) is *policy*, and the relay `stale` flag (no change for 60 s) stays the safeguard.
- **Single-instrument agreement (observed).** Option mids: within 5¢ in 93–100 % of pairs, within 1¢ in 58–100 %; signed medians 0 to −0.5¢. Stock mids: within 5¢ in 52–88 %, signed medians −2 to +1.5¢. The IB side is a ~1.6–2.0 s request/response snapshot whose acquisition time is not recorded, so the run cannot attribute the gaps to timing or say which source is fresher (*unresolved*).
- **Spreads, D12 (observed + unresolved).** Against the all-IB spread, all three Futu-based orientations were within 5¢ in 91–100 % of pairs (max 7.5¢, AAPL Futu-long / IB-short). n is small (11 and 5 pairs) and the two IB legs were sampled 1.4–4.0 s apart, so this is not a simultaneous combo quote. Whether mixed-source spreads are acceptable is *unresolved*. Next measurement: capture both sources from the relay stream with per-leg timestamps.
- **Overnight ORDER_BOOK cadence.** Not measured (RTH run). Overnight rows keep last only (G3), so nothing depends on it now.
- **Options flip (`XENON_FUTU_OPTIONS=1`): not approved.** Blocked on D12 above. Default stays 0; X7a still ships option support behind the flag.
- **Stocks flip (`XENON_QUOTES_DEFAULT=futu-first`): not certified by this run; user decision.** Facts for that decision: Futu stock rows are sub-second fresh (QUOTE source age median ≤ 0.52 s, book receipt-age p90 ≤ 0.19 s, maximum sampled 0.294 s); at mismatched times Futu and IB mids differ by a few cents (TSLA up to 48.5¢); no signed bias is visible but none is proven absent. Order gating stays on IB either way (D7).
- **IB-side gaps (observed).** The xenon IB endpoints returned no option bid/ask in 8–14 of 25 samples per contract and one `/options/greeks` 502 (20 s timeout).
