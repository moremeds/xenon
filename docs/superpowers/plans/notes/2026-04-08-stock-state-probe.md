# `UWClient.get_stock_state` payload probe — 2026-04-08

Run:
```
python3 -c "from scripts.clients.uw_client import UWClient; import json; \
  print(json.dumps(UWClient().get_stock_state('SPY'), indent=2, default=str))"
```

Result (SPY, post-close 2026-04-07):
```json
{
  "data": {
    "close": "656.635",
    "high": "657.58",
    "low": "651.06",
    "open": "656.64",
    "volume": 28298515,
    "total_volume": 31507136,
    "tape_time": "2026-04-07T17:10:50Z",
    "market_time": "regular",
    "prev_close": "656.64"
  }
}
```

## Findings
- Top-level shape: `{"data": {...}}` (same nesting pattern as `get_stock_info`).
- Authoritative live price field: **`data.close`** (string — needs `float()` cast).
- All OHLC fields are strings; volume fields are ints.
- `tape_time` (UTC) gives freshness; `market_time` ∈ {regular, pre, post} for session label.
- `prev_close` available — useful for the "% change today" display in the formatter.

## Normalization rule for `fetch_ticker_data`
```python
state = client.get_stock_state(ticker).get("data") or {}
price = float(state["close"]) if state.get("close") is not None else None
```
No fallback to `get_stock_ohlc` needed — `close` is always present on this endpoint.
