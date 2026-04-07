"""CLI integration tests for scripts/flow_analysis.py account selection."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SCRIPTS_DIR.parent


def _run(account: str, env_overrides: dict | None = None) -> dict:
    env = {"PYTHONPATH": str(SCRIPTS_DIR)}
    if env_overrides:
        env.update(env_overrides)
    import os
    full_env = os.environ.copy()
    full_env.update(env)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "flow_analysis.py"), "--account", account],
        capture_output=True,
        text=True,
        env=full_env,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    return json.loads(proc.stdout)


def test_unknown_account_rejected():
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "flow_analysis.py"), "--account", "etrade"],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0


def test_missing_portfolio_returns_empty(monkeypatch, tmp_path, capsys):
    """When the portfolio file is missing, run_analysis emits a valid empty JSON."""
    from utils import portfolio_adapter
    monkeypatch.setattr(portfolio_adapter, "FUTU_PORTFOLIO", tmp_path / "nope.json")
    import flow_analysis
    flow_analysis.run_analysis("futu")
    out = json.loads(capsys.readouterr().out)
    assert out["positions_scanned"] == 0
    assert out["account"] == "futu"
    assert out["supports"] == []
    assert out["skipped_unsupported"] == 0


def test_per_ticker_dedup(monkeypatch, tmp_path, capsys):
    """Duplicate tickers should result in a single fetch_flow call per symbol."""
    from utils import portfolio_adapter

    futu = tmp_path / "futu_portfolio.json"
    futu.write_text(json.dumps({
        "positions": [
            {"futu_code": "US.TSLA", "normalized": {"kind": "STK", "symbol": "TSLA", "currency": "USD"},
             "quantity": 100, "position_side": "LONG"},
            {"futu_code": "US.TSLA240119C200000",
             "normalized": {"kind": "OPT", "symbol": "TSLA", "right": "C", "strike": 200, "currency": "USD"},
             "quantity": 1, "position_side": "LONG"},
            {"futu_code": "US.NVDA", "normalized": {"kind": "STK", "symbol": "NVDA", "currency": "USD"},
             "quantity": 50, "position_side": "LONG"},
        ]
    }))
    monkeypatch.setattr(portfolio_adapter, "FUTU_PORTFOLIO", futu)

    import flow_analysis
    calls: list[str] = []
    def fake_fetch(ticker):
        calls.append(ticker)
        return {}
    def fake_analyze(_):
        return {"signal": "NONE", "direction": "UNKNOWN", "strength": 0,
                "buy_ratio": None, "sustained_days": 0, "recent_direction": "UNKNOWN"}
    monkeypatch.setattr(flow_analysis, "fetch_flow_module", fake_fetch)
    monkeypatch.setattr(flow_analysis, "analyze_signal", fake_analyze)

    flow_analysis.run_analysis("futu")
    out = json.loads(capsys.readouterr().out)

    assert sorted(calls) == ["NVDA", "TSLA"]  # TSLA fetched once despite 2 rows
    assert out["positions_scanned"] == 3
    assert out["account"] == "futu"
