# VRP endpoint probe — 2026-04-07

**Result: UNAVAILABLE (HTTP 404)**

Probed: `GET https://api.unusualwhales.com/api/volatility/variance_risk_premium/SPY?timespan=1y`

Response:
```
HTTP/1.1 404 Not Found
{"error":"Route not found", ...}
```

Also confirmed: no `variance_risk_premium` reference in `docs/reference/unusual_whales_api_spec.yaml`.

## Implementation impact

- **Do NOT add** `get_variance_risk_premium` wrapper to `scripts/clients/uw_client.py` in Task 3.
- `TickerData.vrp_history` is always `None` in v1.
- `VRPState.vrp_zscore` is always `None` in v1.
- `vrp.classify_regime()` falls through its null-handling path: when `vrp_zscore is None` it biases toward R1 (cautious), never R0.
- The conditional `getattr(client, "get_variance_risk_premium", None)` check in `ticker_data.fetch_ticker_data` (Task 2) becomes a permanent no-op for v1, but the code stays in place so a future spec can add the wrapper without touching the fetcher.

## Future work

If UW exposes a VRP endpoint in a future API revision, add the wrapper and the `getattr` check in `fetch_ticker_data` will automatically pick it up. Alternatively, a follow-up spec can implement local rolling-history persistence (`data/vrp_history/{TICKER}.jsonl`, daily snapshots from a monitor-daemon handler) to compute z-scores from `iv - rv` ourselves.
