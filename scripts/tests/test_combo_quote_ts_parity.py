"""Cross-runtime parity: Python ``compute_combo_quote`` vs TS ``computeNetOptionQuote``.

The combo wizard (Python) and the order builder (TypeScript) must quote the
same natural market for the same legs. Both return SIGNED cross-field quotes
(debits positive, credits negative): TS ``{bid, ask, mid}`` pairs with Python
``{net_bid, net_ask, signed_mid}``. Python's ``bid/ask/mid`` are the
absolute-value display form of the same quote.

No Postgres needed. Runs the TS function through ``web/node_modules/tsx``.
Without tsx the test skips, except when ``XENON_REQUIRE_TS_PARITY=1``
(set by the CI ``web-tests`` job, which installs ``web/node_modules``): then a
missing tsx is a hard failure, so the check cannot go silently dark again.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

from xenon.execution.combo_wizard.combo_quotes import compute_combo_quote
from xenon.execution.combo_wizard.models import ComboLegQuote, ComboLegSpec

_CENT = Decimal("0.01")


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


def _to_ts_legs(fixture: dict) -> list[dict]:
    """Map a fixture leg onto the TS ``OrderLeg`` contract (optionsChainUtils.ts)."""
    return [
        {
            "id": leg["contract_id"],
            "symbol": fixture["ticker"],
            "action": leg["action"],
            "right": leg["right"],
            "strike": float(leg["strike"]),
            "expiry": leg["expiry"],
            "quantity": int(leg["quantity"]),
            "limitPrice": None,
        }
        for leg in fixture["legs"]
    ]


def _load_ts_reference(fixtures: list[dict]) -> dict[str, dict[str, float | None]]:
    web_dir = _repo_root() / "web"
    ts_fixtures = [
        {
            "name": fixture["name"],
            "ticker": fixture["ticker"],
            "legs": _to_ts_legs(fixture),
            "prices": fixture["prices"],
        }
        for fixture in fixtures
    ]
    script = f"""
import {{ computeNetOptionQuote }} from "./lib/optionsChainUtils.ts";

const fixtures = {json.dumps(ts_fixtures)};
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
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"TS reference run failed:\n{result.stderr}"
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


def _money(value: float) -> Decimal:
    # Same rounding as combo_quotes._q.
    return Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)


def test_compute_combo_quote_matches_typescript_reference_fixture_set():
    loader = _repo_root() / "web" / "node_modules" / "tsx" / "dist" / "loader.mjs"
    if shutil.which("node") is None or not loader.is_file():
        reason = "node or web/node_modules/tsx not installed; cross-runtime check skipped"
        if os.environ.get("XENON_REQUIRE_TS_PARITY") == "1":
            pytest.fail(f"XENON_REQUIRE_TS_PARITY=1 but {reason}")
        pytest.skip(reason)
    fixtures = _fixtures()
    ts_reference = _load_ts_reference(fixtures)

    for fixture in fixtures:
        quote = compute_combo_quote(
            _to_python_legs(fixture),
            _to_python_quotes(fixture),
        )
        ts = ts_reference[fixture["name"]]
        assert None not in (ts["bid"], ts["ask"], ts["mid"]), (fixture["name"], ts)
        ts_bid, ts_ask, ts_mid = _money(ts["bid"]), _money(ts["ask"]), _money(ts["mid"])

        # Signed quote: identical on both sides.
        assert (quote.net_bid, quote.net_ask, quote.signed_mid) == (ts_bid, ts_ask, ts_mid), (
            fixture["name"]
        )
        # Display form: Python's absolute-value bid/ask/mid of the same quote.
        assert quote.bid == min(abs(ts_bid), abs(ts_ask)), fixture["name"]
        assert quote.ask == max(abs(ts_bid), abs(ts_ask)), fixture["name"]
        assert quote.mid == abs(ts_mid), fixture["name"]
