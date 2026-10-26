# Futu vs IB quotes calibration (2026-10-26, rth)

Source: `scripts/research/futu_vs_ib_quotes.py` (X7-0, plan v2.1 section D8). Every number below was observed in this run; nothing is estimated or carried over from another run. n/a means no matched data.

## Run

- Window (ET): 2026-10-26 10:13:49 EDT to 2026-10-26 10:26:09 EDT
- Session mode: `rth`; requested minutes: 12.0; IB poll interval: 30.0 s; IB cycles: 25
- OpenD: `host.docker.internal:11111`; xenon API: `http://127.0.0.1:8321`
- Raw samples: `output/research/futu-vs-ib-quotes-2026-10-26-rth-101345.jsonl` (15.4 MB, gitignored; kept on the lead's machine: 26,334 book pushes, 22,336 quote pushes, 275 IB samples). Run inside the prod api container (read-only: no orders, no trade context); IB side only through the xenon API.
- Tick rule: stocks 0.01; options below 3.00 use 0.01, 3.00 and above 0.05 (chosen from the IB mid). Combo gaps use the same rule on the IB combo mid.
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

## Agreement at matched times (ticks, Futu minus IB)

Signed columns show median [Q1, Q3] (n). `|mid|` columns are absolute.

| Key | IB samples | IB errors | pairs | bid | ask | mid | median abs mid | p90 abs mid | max abs mid | within 1 tick | Futu last - IB mid |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| AAPL | 25 | 0 | 25 | 0.00 [-1.00, 3.00] (n=25) | 0.00 [-2.00, 1.00] (n=25) | 0.00 [-1.50, 2.00] (n=25) | 2.00 | 6.40 | 11.00 | 40% | -1.00 [-2.50, 2.50] (n=25) |
| SPY | 25 | 0 | 25 | 2.00 [-1.00, 3.00] (n=25) | 1.00 [-1.00, 3.00] (n=25) | 1.50 [-1.50, 3.00] (n=25) | 2.50 | 8.80 | 14.00 | 20% | 0.50 [-1.50, 2.50] (n=25) |
| NVDA | 25 | 0 | 25 | 0.00 [-2.00, 3.00] (n=25) | 0.00 [-2.00, 2.00] (n=25) | 0.50 [-1.50, 2.50] (n=25) | 2.50 | 4.90 | 6.00 | 28% | 0.50 [-2.50, 2.50] (n=25) |
| QQQ | 25 | 0 | 25 | -2.00 [-3.00, 5.00] (n=25) | -1.00 [-4.00, 6.00] (n=25) | -2.00 [-4.00, 5.50] (n=25) | 4.50 | 11.30 | 13.50 | 4% | 0.41 [-3.50, 5.00] (n=25) |
| TSLA | 25 | 0 | 25 | 1.00 [-5.00, 4.00] (n=25) | 0.00 [-5.00, 4.00] (n=25) | 1.50 [-5.00, 5.50] (n=25) | 5.00 | 26.10 | 48.50 | 8% | 1.10 [-4.99, 6.00] (n=25) |
| AAPL_20261030_340_C | 25 | 0 | 15 | 0.00 [0.00, 0.00] (n=15) | 0.00 [0.00, 0.00] (n=15) | 0.00 [0.00, 0.00] (n=15) | 0.00 | 0.50 | 1.50 | 93% | 0.30 [-0.85, 1.20] (n=15) |
| AAPL_20261030_350_C | 25 | 1 | 17 | 0.00 [0.00, 0.00] (n=17) | 0.00 [0.00, 0.00] (n=17) | 0.00 [-0.50, 0.00] (n=17) | 0.00 | 2.50 | 3.00 | 76% | 0.00 [-1.50, 1.00] (n=17) |
| SPY_20261030_760_P | 25 | 0 | 11 | 0.00 [-0.20, 0.00] (n=11) | 0.00 [-0.10, 0.00] (n=11) | 0.00 [-0.10, 0.00] (n=11) | 0.10 | 0.20 | 0.40 | 100% | -0.10 [-0.50, 0.10] (n=11) |
| SPY_20261030_770_P | 25 | 0 | 12 | 0.00 [-0.05, 0.25] (n=12) | 0.00 [-0.05, 0.20] (n=12) | 0.00 [-0.05, 0.15] (n=12) | 0.15 | 0.59 | 1.00 | 100% | 0.70 [-0.93, 2.57] (n=12) |
| AAPL_20261218_330_P | 25 | 0 | 14 | 0.00 [0.00, 0.00] (n=14) | 0.00 [0.00, 0.00] (n=14) | 0.00 [0.00, 0.00] (n=14) | 0.00 | 0.00 | 0.00 | 100% | 4.00 [2.38, 4.00] (n=14) |
| SPY_20261218_780_C | 25 | 0 | 11 | 0.00 [-0.30, 0.10] (n=11) | 0.00 [-0.20, 0.00] (n=11) | -0.10 [-0.20, 0.05] (n=11) | 0.10 | 0.50 | 0.60 | 100% | -0.10 [-0.50, 0.45] (n=11) |

## Futu freshness and push cadence

Ages in seconds at each IB sample time. Quote source age is the QUOTE `data_date`/`data_time` (ET wall clock) against the sample time; ORDER_BOOK has no source time, so its age is receipt time.

| Key | QUOTE pushes | QUOTE pushes/min | QUOTE receipt age median / p90 | QUOTE source age median | BOOK pushes | BOOK pushes/min | BOOK last age at end (s) | BOOK age median / p90 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| AAPL | 3244 | 263.02 | 0.14 / 0.32 | 0.52 | 4731 | 383.70 | 0.1 | 0.05 / 0.07 |
| SPY | 3399 | 275.43 | 0.09 / 0.30 | 0.52 | 5240 | 424.82 | -0.0 | 0.07 / 0.10 |
| NVDA | 4866 | 394.49 | 0.10 / 0.24 | 0.43 | 5215 | 422.95 | 0.1 | 0.17 / 0.18 |
| QQQ | 4146 | 336.01 | 0.12 / 0.46 | 0.38 | 5245 | 425.39 | 0.1 | 0.07 / 0.18 |
| TSLA | 6297 | 510.38 | 0.06 / 0.15 | 0.29 | 4850 | 393.19 | -0.0 | 0.09 / 0.19 |
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

## Combo mid gap (Futu minus IB, ticks)

Combo mid = long leg mid minus short leg mid, per source. D12: a gap above 1 tick at matched times means route all legs of one underlying to one source.

| Vertical (long:short) | cycles | pairs | gap median [Q1, Q3] | median abs | p90 abs | max abs | within 1 tick |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `AAPL_20261030_340_C:AAPL_20261030_350_C` | 25 | 11 | 0.00 [-0.25, 0.05] (n=11) | 0.10 | 0.60 | 1.00 | 100% |
| `SPY_20261030_770_P:SPY_20261030_760_P` | 25 | 5 | -0.10 [-0.40, 0.30] (n=5) | 0.40 | 0.80 | 1.00 | 100% |

## Decisions (lead, 2026-10-26, from this run only)

All statements are observations from the run above unless marked *inference*.

- **Greek unit factors.** Futu / IB median ratios over 149 matched option samples: IV (after /100) 0.996, delta 0.999, gamma 0.991, vega 0.995, theta 1.014; every per-contract median is within 0.95–1.05. So Futu greeks are already in IB units (theta per day, vega per vol point); only IV needs /100. `FUTU_GREEK_FACTORS={"iv":0.01,"delta":1,"gamma":1,"vega":1,"theta":1}`. Per-contract spreads of up to ~5 % (AAPL 12/18 330P theta 1.048, SPY 12/18 780C vega 1.030) are model differences, not units (*inference*).
- **Quota units per subtype.** Stocks: QUOTE 1 + ORDER_BOOK 1 = **2 units per stock** (own_used 0→5→10 for 5 stocks). US options: QUOTE and ORDER_BOOK both moved own_used by **0** for 6 options. `FUTU_UNITS_PER_SYMBOL=2` is correct for stocks and conservative for options. Whether option subscriptions draw on a separate quota that `query_subscription` does not report is unverified.
- **ORDER_BOOK for options.** Available under the current OpenD level: 6/6 option ORDER_BOOK subscribes succeeded, 0 refusals, 10.8–19.3 book pushes/min per contract, book age p90 3.3–4.9 s. D3 holds for options.
- **Book staleness (D11 / contract A1).** RTH book pushes never went silent: book age p90 ≤ 0.19 s for stocks and ≤ 4.9 s for options; book age at the end ≤ 4.3 s. Keep A1 (age rule off by default). Option QUOTE (last-trade) pushes are sparse (0.6–12.7/min; AAPL 12/18 330P source age median 412 s), so freshness of option rows must come from the book, as X7a does (`timestamp = max(quote ts, book receipt)`).
- **Overnight ORDER_BOOK cadence.** Not measured (RTH run). Overnight rows keep last only (G3), so no decision depends on it now; measure with `--session overnight` before any overnight bid/ask work.
- **Combo mid gap (D12).** Both verticals within 1 tick at 100 % of matched pairs (AAPL 340/350C: median abs 0.10, max 1.00 tick, n=11; SPY 770/760P: median abs 0.40, max 1.00 tick, n=5). No gap above 1 tick → **no one-source-per-underlying rule**; mixed-source combos stay allowed. n is small (IB returned no option bid/ask in 8–14 of 25 samples per contract, so pairs are 11–17 per leg); re-check after the options flip.
- **IB side gaps observed.** The xenon IB endpoints returned no option bid/ask in 32–56 % of samples and one `/options/greeks` 502 (20 s timeout); Futu had a live book for every option the whole run.
- **Stocks flip (`XENON_QUOTES_DEFAULT=futu-first`): pass.** Signed mid differences have medians of −2 to +1.5 ticks with IQRs straddling 0 (no bias). Absolute differences (median 2–5 ticks, TSLA max 48.5) are dominated by sampling time: Futu receipt age median ≤ 0.14 s, while an IB sample is a request/response snapshot whose own latency is not recorded (*inference*). Futu freshness: QUOTE source age median ≤ 0.52 s, book age median ≤ 0.17 s.
- **Options flip (`XENON_FUTU_OPTIONS=1`): pass, after X7a ships greeks with the factors above.** Option mids agree within 1 tick in 76–100 % of pairs (median 0 for every contract).
- **IB vs Futu "which is better" (user question 2026-10-26).** This run shows no systematic price bias either way. Futu was fresher and more complete on options (live book every sample vs IB gaps above). A per-field "best source" rule needs a timestamped IB stream sample (relay, not snapshots) — not measured here.
