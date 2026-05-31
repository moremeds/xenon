from __future__ import annotations

import json
import os
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text as sa_text

from xenon.execution.combo_wizard.combo_quotes import compute_combo_quote
from xenon.execution.combo_wizard.models import ComboLegQuote, ComboLegSpec

_CENT = Decimal("0.01")

# --------------------------------------------------------------------------
# Postgres helpers
# --------------------------------------------------------------------------

_TEST_DB_URL = os.environ.get(
    "DATABASE_URL_TEST",
    "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
)
_SYNC_URL = _TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql+psycopg://")


def _pg_engine():
    return create_engine(_SYNC_URL, pool_pre_ping=True)


def _cleanup(engine):
    with engine.begin() as conn:
        conn.execute(sa_text("TRUNCATE xenon.position_protection CASCADE"))
        conn.execute(sa_text("TRUNCATE xenon.wizard_events CASCADE"))
        conn.execute(sa_text("TRUNCATE xenon.wizard_combo_attempts CASCADE"))
        conn.execute(sa_text("TRUNCATE xenon.wizard_sessions CASCADE"))


@pytest.fixture(autouse=True)
def _setup_pg(monkeypatch):
    """Point get_sync_engine() at the test database and clean tables."""
    monkeypatch.setenv("DATABASE_URL", _SYNC_URL)
    import xenon.db.engine as eng_mod

    monkeypatch.setattr(eng_mod, "_sync_engine", None)

    engine = _pg_engine()
    _cleanup(engine)
    engine.dispose()
    yield
    engine = _pg_engine()
    _cleanup(engine)
    engine.dispose()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _fixtures() -> list[dict]:
    return [
        {
            "name": "bull_call_spread",
            "ticker": "AAPL",
            "legs": [
                {
                    "contract_id": "AAPL_20260417_200_C",
                    "action": "BUY",
                    "right": "C",
                    "strike": "200",
                    "expiry": "20260417",
                    "quantity": 1,
                },
                {
                    "contract_id": "AAPL_20260417_210_C",
                    "action": "SELL",
                    "right": "C",
                    "strike": "210",
                    "expiry": "20260417",
                    "quantity": 1,
                },
            ],
            "prices": {
                "AAPL_20260417_200_C": {"bid": 4.50, "ask": 4.70},
                "AAPL_20260417_210_C": {"bid": 2.00, "ask": 2.20},
            },
        },
        {
            "name": "bear_put_spread",
            "ticker": "AAPL",
            "legs": [
                {
                    "contract_id": "AAPL_20260417_210_P",
                    "action": "BUY",
                    "right": "P",
                    "strike": "210",
                    "expiry": "20260417",
                    "quantity": 1,
                },
                {
                    "contract_id": "AAPL_20260417_200_P",
                    "action": "SELL",
                    "right": "P",
                    "strike": "200",
                    "expiry": "20260417",
                    "quantity": 1,
                },
            ],
            "prices": {
                "AAPL_20260417_210_P": {"bid": 5.10, "ask": 5.30},
                "AAPL_20260417_200_P": {"bid": 2.20, "ask": 2.40},
            },
        },
        {
            "name": "iron_condor",
            "ticker": "AAPL",
            "legs": [
                {
                    "contract_id": "AAPL_20260417_180_P",
                    "action": "BUY",
                    "right": "P",
                    "strike": "180",
                    "expiry": "20260417",
                    "quantity": 1,
                },
                {
                    "contract_id": "AAPL_20260417_190_P",
                    "action": "SELL",
                    "right": "P",
                    "strike": "190",
                    "expiry": "20260417",
                    "quantity": 1,
                },
                {
                    "contract_id": "AAPL_20260417_210_C",
                    "action": "SELL",
                    "right": "C",
                    "strike": "210",
                    "expiry": "20260417",
                    "quantity": 1,
                },
                {
                    "contract_id": "AAPL_20260417_220_C",
                    "action": "BUY",
                    "right": "C",
                    "strike": "220",
                    "expiry": "20260417",
                    "quantity": 1,
                },
            ],
            "prices": {
                "AAPL_20260417_180_P": {"bid": 1.00, "ask": 1.10},
                "AAPL_20260417_190_P": {"bid": 2.50, "ask": 2.60},
                "AAPL_20260417_210_C": {"bid": 2.20, "ask": 2.40},
                "AAPL_20260417_220_C": {"bid": 0.90, "ask": 1.00},
            },
        },
        {
            "name": "long_call_butterfly",
            "ticker": "AAPL",
            "legs": [
                {
                    "contract_id": "AAPL_20260417_100_C",
                    "action": "BUY",
                    "right": "C",
                    "strike": "100",
                    "expiry": "20260417",
                    "quantity": 1,
                },
                {
                    "contract_id": "AAPL_20260417_110_C",
                    "action": "SELL",
                    "right": "C",
                    "strike": "110",
                    "expiry": "20260417",
                    "quantity": 2,
                },
                {
                    "contract_id": "AAPL_20260417_120_C",
                    "action": "BUY",
                    "right": "C",
                    "strike": "120",
                    "expiry": "20260417",
                    "quantity": 1,
                },
            ],
            "prices": {
                "AAPL_20260417_100_C": {"bid": 8.00, "ask": 8.20},
                "AAPL_20260417_110_C": {"bid": 3.80, "ask": 4.00},
                "AAPL_20260417_120_C": {"bid": 1.40, "ask": 1.50},
            },
        },
    ]


def _load_ts_reference(fixtures: list[dict]) -> dict[str, dict[str, str | None]]:
    web_dir = _repo_root() / "web"
    script = f"""
import {{ computeNetOptionQuote }} from "./lib/optionsChainUtils.ts";

const fixtures = {json.dumps(fixtures)};
const output = Object.fromEntries(
  fixtures.map((fixture) => [
    fixture.name,
    computeNetOptionQuote(fixture.legs, fixture.prices, fixture.ticker),
  ]),
);

console.log(JSON.stringify(output));
"""
    result = subprocess.run(
        [
            "node",
            "--import",
            "./node_modules/tsx/dist/loader.mjs",
            "--input-type=module",
            "-e",
            script,
        ],
        cwd=web_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _to_python_legs(fixture: dict) -> list[ComboLegSpec]:
    return [
        ComboLegSpec(
            contract_id=leg["contract_id"],
            action=leg["action"],
            right=leg["right"],
            strike=Decimal(leg["strike"]),
            expiry=leg["expiry"],
            quantity=int(leg["quantity"]),
        )
        for leg in fixture["legs"]
    ]


def _to_python_quotes(fixture: dict) -> dict[str, ComboLegQuote]:
    return {
        contract_id: ComboLegQuote(
            bid=Decimal(str(values["bid"])),
            ask=Decimal(str(values["ask"])),
        )
        for contract_id, values in fixture["prices"].items()
    }


def _money(value: float | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(_CENT)


def test_compute_combo_quote_matches_typescript_reference_fixture_set():
    loader = _repo_root() / "web" / "node_modules" / "tsx" / "dist" / "loader.mjs"
    if shutil.which("node") is None or not loader.is_file():
        pytest.skip("web/node_modules/tsx not installed; cross-runtime check skipped")
    fixtures = _fixtures()
    ts_reference = _load_ts_reference(fixtures)

    for fixture in fixtures:
        quote = compute_combo_quote(
            _to_python_legs(fixture),
            _to_python_quotes(fixture),
        )
        expected = ts_reference[fixture["name"]]

        assert quote.bid == _money(expected["bid"])
        assert quote.ask == _money(expected["ask"])
        assert quote.mid == _money(expected["mid"])


def test_compute_combo_quote_preserves_signed_net_prices_for_credit_structure():
    quote = compute_combo_quote(
        [
            ComboLegSpec(
                contract_id="AAPL_20260417_200_P",
                action="BUY",
                right="P",
                strike=Decimal("200"),
                expiry="20260417",
                quantity=1,
            ),
            ComboLegSpec(
                contract_id="AAPL_20260417_210_P",
                action="SELL",
                right="P",
                strike=Decimal("210"),
                expiry="20260417",
                quantity=1,
            ),
        ],
        {
            "AAPL_20260417_200_P": ComboLegQuote(
                bid=Decimal("2.00"),
                ask=Decimal("2.20"),
            ),
            "AAPL_20260417_210_P": ComboLegQuote(
                bid=Decimal("4.50"),
                ask=Decimal("4.70"),
            ),
        },
    )

    assert quote.net_ask == Decimal("-2.30")
    assert quote.net_bid == Decimal("-2.70")


# ---------------------------------------------------------------------------
# Item 3 + Item 4: env-tunable TTL + streaming-subscription reuse.
# ---------------------------------------------------------------------------


def _seed_wizard_session(db_path: Path, legs: list[dict]) -> str:
    """Seed a wizard session in Postgres. ``db_path`` is kept for signature compat."""
    import uuid

    from xenon.db.queries import combo_wizard as cwq

    sid = f"wiz-{uuid.uuid4().hex[:12]}"
    payload = {"symbol": "AAPL", "type": "combo", "legs": legs}
    engine = _pg_engine()
    with engine.begin() as conn:
        cwq.create_session(
            conn,
            session_id=sid,
            ticker="AAPL",
            state="PROTECTED",
            structure_name="Bull Call Spread",
            intent="OPEN",
            payload=payload,
        )
    engine.dispose()
    return sid


class _FakeTicker:
    def __init__(self, *, bid: float, ask: float, tick_time):
        self.bid = bid
        self.ask = ask
        self.bidSize = 10
        self.askSize = 10
        self.time = tick_time


class _FakeIB:
    def __init__(self, tickers_by_conid: dict[int, _FakeTicker]):
        self._tickers = tickers_by_conid
        self.get_quote_calls: list[int] = []

    def get_quote(self, contract, *, snapshot=False):
        con_id = int(getattr(contract, "conId", 0) or 0)
        self.get_quote_calls.append(con_id)
        return self._tickers[con_id]


def test_default_tick_ttl_env_tunable(tmp_path, monkeypatch):
    """When XENON_WIZARD_QUOTE_TTL_S=120 and a tick is 90s old, the freshness
    gate accepts it; default 30s TTL would reject."""
    monkeypatch.setenv("XENON_WIZARD_QUOTE_TTL_S", "120")

    # Reload the module so it picks up the new env value at import time.
    import importlib

    from xenon.execution.combo_wizard import combo_quote_source as cqs

    cqs = importlib.reload(cqs)

    assert cqs.DEFAULT_TICK_TTL_S == 120.0

    db = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(db))
    legs = [
        {
            "conId": 5001,
            "action": "BUY",
            "ratio": 1,
            "right": "C",
            "strike": 200,
            "expiry": "20260417",
            "symbol": "AAPL",
        },
        {
            "conId": 5002,
            "action": "SELL",
            "ratio": 1,
            "right": "C",
            "strike": 210,
            "expiry": "20260417",
            "symbol": "AAPL",
        },
    ]
    sid = _seed_wizard_session(db, legs)

    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 4, 25, 15, 0, 0, tzinfo=timezone.utc)
    ninety_s_ago = now - timedelta(seconds=90)  # stale under 30s TTL, fresh under 120s
    tickers = {
        5001: _FakeTicker(bid=4.50, ask=4.70, tick_time=ninety_s_ago),
        5002: _FakeTicker(bid=2.00, ask=2.20, tick_time=ninety_s_ago),
    }
    fake_ib = _FakeIB(tickers)

    quote_fn = cqs.build_default_quote_fn(
        lambda: fake_ib,
        now_fn=lambda: now,
    )
    result = quote_fn(sid)
    assert result is not None  # 90s-old tick accepted under 120s TTL
    # Restore module state for the rest of the suite.
    monkeypatch.delenv("XENON_WIZARD_QUOTE_TTL_S", raising=False)
    importlib.reload(cqs)


def test_ticker_cache_reuses_subscription_across_ticks(tmp_path, monkeypatch):
    """Item 4: get_quote MUST be called once per conId even when quote_fn is
    invoked on every tick. The cache keeps the ib_async Ticker and lets it
    live-update in place."""
    from xenon.execution.combo_wizard.combo_quote_source import (
        build_default_quote_fn,
    )

    db = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(db))
    legs = [
        {
            "conId": 6001,
            "action": "BUY",
            "ratio": 1,
            "right": "C",
            "strike": 200,
            "expiry": "20260417",
            "symbol": "AAPL",
        },
        {
            "conId": 6002,
            "action": "SELL",
            "ratio": 1,
            "right": "C",
            "strike": 210,
            "expiry": "20260417",
            "symbol": "AAPL",
        },
    ]
    sid = _seed_wizard_session(db, legs)

    from datetime import datetime, timezone

    now = datetime(2026, 4, 25, 15, 0, 0, tzinfo=timezone.utc)
    tickers = {
        6001: _FakeTicker(bid=4.50, ask=4.70, tick_time=now),
        6002: _FakeTicker(bid=2.00, ask=2.20, tick_time=now),
    }
    fake_ib = _FakeIB(tickers)

    quote_fn = build_default_quote_fn(
        lambda: fake_ib,
        ttl_s=60.0,
        now_fn=lambda: now,
    )

    # Two successive ticks on the same session.
    r1 = quote_fn(sid)
    r2 = quote_fn(sid)
    assert r1 is not None
    assert r2 is not None

    # Each leg should have been subscribed exactly once across both ticks
    # (2 legs × 1 subscription = 2 get_quote calls total, not 4).
    assert fake_ib.get_quote_calls == [6001, 6002]


def test_ticker_cache_cleanup_cancels_mkt_data(tmp_path, monkeypatch):
    """_TickerCache.cleanup invokes ib.ib.cancelMktData for each retained
    contract and clears the cache."""
    from xenon.execution.combo_wizard.combo_quote_source import (
        _TickerCache,
    )

    class _InnerIB:
        def __init__(self):
            self.cancel_calls: list = []

        def cancelMktData(self, contract):
            self.cancel_calls.append(getattr(contract, "conId", None))

    class _WrapperIB:
        def __init__(self, tickers):
            self.ib = _InnerIB()
            self._tickers = tickers
            self.get_quote_calls = 0

        def get_quote(self, contract, *, snapshot=False):
            self.get_quote_calls += 1
            return self._tickers[int(contract.conId)]

    class _C:
        def __init__(self, conId):
            self.conId = conId

    from datetime import datetime, timezone

    now = datetime(2026, 4, 25, 15, 0, 0, tzinfo=timezone.utc)
    t = _FakeTicker(bid=1.0, ask=1.1, tick_time=now)
    wrapper = _WrapperIB({7001: t, 7002: t})

    cache = _TickerCache()
    cache.get(wrapper, _C(7001))
    cache.get(wrapper, _C(7002))
    cache.cleanup(wrapper)

    assert sorted(wrapper.ib.cancel_calls) == [7001, 7002]
    # cache is cleared → next get() would re-subscribe.
    assert cache._tickers == {}
