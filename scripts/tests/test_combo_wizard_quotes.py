from __future__ import annotations

import json
import subprocess
from decimal import Decimal
from pathlib import Path

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
