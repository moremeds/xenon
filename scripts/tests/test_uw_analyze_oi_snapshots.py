import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services.uw_analyze_oi_snapshots import RETENTION_DAYS, load_history, snapshot_oi


class FakeChain:
    def __init__(self, rows):
        self.rows = rows

    def get_option_chain(self, ticker, expiry=None, **_):
        return {"data": self.rows}


def test_snapshot_oi_writes_file(tmp_path):
    rows = [
        {"strike": 100, "call_oi": 500, "put_oi": 300, "expiration_date": "2026-05-15"},
        {"strike": 105, "call_oi": 800, "put_oi": 200, "expiration_date": "2026-05-15"},
    ]
    snap = snapshot_oi("AAPL", FakeChain(rows), data_dir=tmp_path)
    assert snap["data_date"] == date.today().isoformat()
    assert snap["strikes"]["100"] == {"call_oi": 500, "put_oi": 300}
    p = tmp_path / "AAPL.json"
    assert p.exists()
    payload = json.loads(p.read_text())
    assert len(payload["history"]) == 1


def test_snapshot_oi_rolling_retention(tmp_path):
    p = tmp_path / "AAPL.json"
    old = [{"data_date": (date.today() - timedelta(days=i + 1)).isoformat(), "strikes": {}} for i in range(10)]
    p.write_text(json.dumps({"history": old}))
    rows = [{"strike": 100, "call_oi": 1, "put_oi": 1, "expiration_date": "2026-05-15"}]
    snapshot_oi("AAPL", FakeChain(rows), data_dir=tmp_path)
    payload = json.loads(p.read_text())
    assert len(payload["history"]) == RETENTION_DAYS


def test_load_history_missing_returns_empty(tmp_path):
    assert load_history("AAPL", data_dir=tmp_path) == []


def test_snapshot_oi_overwrites_same_day(tmp_path):
    rows1 = [{"strike": 100, "call_oi": 1, "put_oi": 1, "expiration_date": "2026-05-15"}]
    rows2 = [{"strike": 100, "call_oi": 2, "put_oi": 2, "expiration_date": "2026-05-15"}]
    snapshot_oi("AAPL", FakeChain(rows1), data_dir=tmp_path)
    snapshot_oi("AAPL", FakeChain(rows2), data_dir=tmp_path)
    payload = json.loads((tmp_path / "AAPL.json").read_text())
    assert len(payload["history"]) == 1
    assert payload["history"][-1]["strikes"]["100"]["call_oi"] == 2
