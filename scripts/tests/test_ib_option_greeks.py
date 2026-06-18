"""xenon-ib-option-greeks — broker-computed option greeks snapshot CLI.

Mirrors the ib_market_depth.py subprocess pattern (sync IBClient, qualify,
reqMktData snapshot, poll modelGreeks, print JSON). These tests stub IBClient
so no live IB is needed.

Design contracts under test:
- greeks are option-only: the full triplet (expiry+strike+right) is mandatory;
  a missing field is rejected with exit 2 (no underlying/stock fallback).
- modelGreeks (impliedVol/delta/gamma/vega/theta/undPrice) are read off the
  ticker once delta populates; NaN/None fields collapse to null.
- no greeks delivered (illiquid / market closed) => greeks:null + note, exit 0
  (a 200-worthy result, not a hard error).
- qualify failure => exit 1; any other exception => exit 1; disconnect always
  runs (finally).
- output includes the qualified `conId`.
"""

from __future__ import annotations

import json
from collections import namedtuple

import pytest
from ib_async import Option

import xenon.execution.ib_option_greeks as mod

# Mirror ib_async.OptionComputation's fields we read.
MG = namedtuple("MG", "impliedVol delta gamma vega theta undPrice")


class _FakeTicker:
    def __init__(self, bid=None, ask=None, greeks=None, greeks_after=0) -> None:
        self.bid = bid
        self.ask = ask
        self._greeks = greeks
        self._greeks_after = greeks_after  # populate modelGreeks after N sleeps
        self._sleeps = 0
        self.modelGreeks = None if greeks_after > 0 else greeks

    def _tick(self) -> None:
        self._sleeps += 1
        if self._greeks is not None and self._sleeps >= self._greeks_after:
            self.modelGreeks = self._greeks


class _FakeIB:
    def __init__(self, ticker: _FakeTicker) -> None:
        self._ticker = ticker
        self.cancelled = False
        self.mkt_contract = None
        self.mkt_snapshot = None
        self.market_data_type = None

    def reqMarketDataType(self, t):
        self.market_data_type = t

    def qualifyContracts(self, *contracts):
        for c in contracts:
            if not getattr(c, "conId", 0):
                c.conId = 770000001
        return list(contracts)

    def reqMktData(self, contract, genericTicks, snapshot, regulatorySnapshot):
        self.mkt_contract = contract
        self.mkt_snapshot = snapshot
        return self._ticker

    def sleep(self, _secs):
        self._ticker._tick()

    def cancelMktData(self, contract):
        self.cancelled = True


class _FakeClient:
    def __init__(self, ib: _FakeIB) -> None:
        self._ib = ib
        self.connected = False
        self.disconnected = False

    def connect(self, **_kw) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.disconnected = True


def _run(monkeypatch, capsys, argv, *, ticker=None):
    ib = _FakeIB(ticker if ticker is not None else _FakeTicker())
    client = _FakeClient(ib)
    monkeypatch.setattr(mod, "IBClient", lambda: client)
    monkeypatch.setattr("sys.argv", ["xenon-ib-option-greeks", *argv])
    code = 0
    try:
        mod.main()
    except SystemExit as e:  # noqa: PT012
        code = e.code or 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out.splitlines()[-1]) if out else {}
    return payload, code, ib, client


_FULL = ["--symbol", "qqq", "--expiry", "20260717", "--strike", "600", "--right", "c"]


# --------------------------------------------------------------------------- #
# Contract resolution / required triplet
# --------------------------------------------------------------------------- #


def test_option_contract_built_and_greeks_returned(monkeypatch, capsys):
    g = MG(impliedVol=0.2134, delta=0.5421, gamma=0.0123, vega=0.45, theta=-0.12, undPrice=601.5)
    payload, code, ib, _ = _run(
        monkeypatch,
        capsys,
        _FULL,
        ticker=_FakeTicker(bid=12.3, ask=12.7, greeks=g, greeks_after=2),
    )
    assert code == 0
    assert payload["secType"] == "OPT"
    assert payload["symbol"] == "QQQ"
    assert payload["conId"] == 770000001
    assert payload["expiry"] == "20260717"
    assert payload["strike"] == 600.0
    assert payload["right"] == "C"
    assert payload["bid"] == 12.3 and payload["ask"] == 12.7
    # the contract handed to reqMktData is an Option, built all-keyword
    assert isinstance(ib.mkt_contract, Option)
    assert ib.mkt_contract.right == "C"  # upper-cased
    assert ib.mkt_contract.strike == 600.0
    assert ib.mkt_contract.lastTradeDateOrContractMonth == "20260717"
    assert ib.mkt_contract.currency == "USD"
    assert ib.mkt_snapshot is True
    assert ib.market_data_type == 2  # frozen fallback so greeks flow off-hours
    assert payload["greeks"] == {
        "impliedVol": 0.2134,
        "delta": 0.5421,
        "gamma": 0.0123,
        "vega": 0.45,
        "theta": -0.12,
        "undPrice": 601.5,
    }
    assert "note" not in payload
    assert ib.cancelled is True


@pytest.mark.parametrize(
    "argv",
    [
        ["--symbol", "QQQ", "--expiry", "20260717", "--strike", "600"],  # no right
        ["--symbol", "QQQ", "--expiry", "20260717", "--right", "C"],  # no strike
        ["--symbol", "QQQ", "--strike", "600", "--right", "C"],  # no expiry
    ],
)
def test_partial_triplet_rejected_exit_2(monkeypatch, capsys, argv):
    payload, code, ib, _ = _run(monkeypatch, capsys, argv)
    assert code == 2
    assert "error" in payload
    # never reached the IB market-data call
    assert ib.mkt_contract is None


# --------------------------------------------------------------------------- #
# Greeks presence semantics
# --------------------------------------------------------------------------- #


def test_no_greeks_returned_is_exit_0_with_note(monkeypatch, capsys):
    # ticker never populates modelGreeks (illiquid / closed)
    payload, code, ib, _ = _run(
        monkeypatch,
        capsys,
        _FULL,
        ticker=_FakeTicker(bid=None, ask=None, greeks=None),
    )
    assert code == 0
    assert payload["greeks"] is None
    assert payload["note"] == "no greeks returned"
    assert ib.cancelled is True


def test_nan_greek_fields_collapse_to_null(monkeypatch, capsys):
    nan = float("nan")
    g = MG(impliedVol=0.19, delta=0.4, gamma=nan, vega=nan, theta=-0.1, undPrice=nan)
    payload, code, _, _ = _run(
        monkeypatch, capsys, _FULL, ticker=_FakeTicker(bid=1.0, ask=1.1, greeks=g, greeks_after=1)
    )
    assert code == 0
    assert payload["greeks"]["delta"] == 0.4
    assert payload["greeks"]["gamma"] is None
    assert payload["greeks"]["vega"] is None
    assert payload["greeks"]["undPrice"] is None


def test_nan_bid_ask_collapse_to_null(monkeypatch, capsys):
    g = MG(impliedVol=0.19, delta=0.4, gamma=0.01, vega=0.2, theta=-0.1, undPrice=600.0)
    payload, code, _, _ = _run(
        monkeypatch,
        capsys,
        _FULL,
        ticker=_FakeTicker(bid=float("nan"), ask=float("nan"), greeks=g, greeks_after=1),
    )
    assert code == 0
    assert payload["bid"] is None and payload["ask"] is None


def test_negative_bid_ask_sentinel_collapses_to_null(monkeypatch, capsys):
    # IB returns bid/ask = -1 when no quote is active (e.g. outside RTH). It is a
    # "no data" sentinel, not a price — must surface as null, not -1. Greeks,
    # which can be legitimately negative (theta), are unaffected.
    g = MG(impliedVol=0.19, delta=0.4, gamma=0.01, vega=0.2, theta=-0.1, undPrice=600.0)
    payload, code, _, _ = _run(monkeypatch, capsys, _FULL, ticker=_FakeTicker(bid=-1, ask=-1, greeks=g, greeks_after=1))
    assert code == 0
    assert payload["bid"] is None and payload["ask"] is None
    assert payload["greeks"]["theta"] == -0.1  # negative greek preserved


# --------------------------------------------------------------------------- #
# Failure modes
# --------------------------------------------------------------------------- #


def test_qualify_failure_exits_1(monkeypatch, capsys):
    ib = _FakeIB(_FakeTicker())
    ib.qualifyContracts = lambda *c: list(c)  # leaves conId 0
    client = _FakeClient(ib)
    monkeypatch.setattr(mod, "IBClient", lambda: client)
    monkeypatch.setattr("sys.argv", ["xenon-ib-option-greeks", *_FULL])
    with pytest.raises(SystemExit) as ei:
        mod.main()
    assert ei.value.code == 1
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "could not qualify" in payload["error"]
    assert client.disconnected is True


def test_hard_failure_exits_1(monkeypatch, capsys):
    ib = _FakeIB(_FakeTicker())

    def boom(*_a, **_k):
        raise RuntimeError("connect call failed")

    ib.reqMktData = boom  # type: ignore[assignment]
    client = _FakeClient(ib)
    monkeypatch.setattr(mod, "IBClient", lambda: client)
    monkeypatch.setattr("sys.argv", ["xenon-ib-option-greeks", *_FULL])
    with pytest.raises(SystemExit) as ei:
        mod.main()
    assert ei.value.code == 1
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "error" in payload
    assert client.disconnected is True  # finally ran
