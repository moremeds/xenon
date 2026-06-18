"""xenon-ib-market-depth — point-in-time L2 order-book snapshot CLI.

Mirrors the ib_option_chain.py subprocess pattern (sync IBClient, qualify,
print JSON). These tests stub IBClient so no live IB is needed.

Design contracts under test (see
docs/superpowers/plans/2026-06-18-market-depth-rest-endpoint-plan.md):
- contract resolution: option triplet -> Option(OPT); else underlying (STK/IND)
- all-or-none option tuple: a partial tuple is rejected with exit 2 (never
  silently degrades to stock depth)
- `entitled` reflects the PERMISSION axis only (10089/10092/text), never data
  presence. Empty book with no permission error => entitled:true + note.
- 2152/309 are chatter, not permission failures.
- output includes the qualified `conId`.
"""

from __future__ import annotations

import json
from collections import namedtuple

import pytest
import xenon.execution.ib_market_depth as mod
from ib_async import Option

Lvl = namedtuple("Lvl", "price size marketMaker")


class _FakeEvent:
    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, h):
        self.handlers.append(h)
        return self

    def __isub__(self, h):
        if h in self.handlers:
            self.handlers.remove(h)
        return self

    def fire(self, *args) -> None:
        for h in list(self.handlers):
            h(*args)


class _FakeTicker:
    def __init__(self, bids=None, asks=None) -> None:
        self.domBids = bids or []
        self.domAsks = asks or []


class _FakeIB:
    """Minimal ib_async.IB stand-in.

    error=(reqId, code, msg, contract) fires once on the first sleep() so we
    can exercise the errorEvent path deterministically.
    """

    def __init__(self, ticker: _FakeTicker, error=None) -> None:
        self._ticker = ticker
        self._error = error
        self._fired = False
        self.errorEvent = _FakeEvent()
        self.cancelled = False
        self.depth_contract = None
        self.depth_rows = None
        self.depth_smart = None

    def qualifyContracts(self, *contracts):
        for c in contracts:
            if not getattr(c, "conId", 0):
                c.conId = 265598
        return list(contracts)

    def reqMktDepth(self, contract, numRows, isSmartDepth):
        self.depth_contract = contract
        self.depth_rows = numRows
        self.depth_smart = isSmartDepth
        return self._ticker

    def sleep(self, _secs):
        if self._error is not None and not self._fired:
            self._fired = True
            self.errorEvent.fire(*self._error)

    def cancelMktDepth(self, contract, isSmartDepth):
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


def _run(monkeypatch, capsys, argv, *, ticker=None, error=None):
    ib = _FakeIB(ticker if ticker is not None else _FakeTicker(), error=error)
    client = _FakeClient(ib)
    monkeypatch.setattr(mod, "IBClient", lambda: client)
    monkeypatch.setattr("sys.argv", ["xenon-ib-market-depth", *argv])
    code = 0
    try:
        mod.main()
    except SystemExit as e:  # noqa: PT012
        code = e.code or 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out.splitlines()[-1]) if out else {}
    return payload, code, ib, client


# --------------------------------------------------------------------------- #
# Contract resolution
# --------------------------------------------------------------------------- #


def test_stock_underlying(monkeypatch, capsys):
    payload, code, ib, _ = _run(
        monkeypatch,
        capsys,
        ["--symbol", "AAPL"],
        ticker=_FakeTicker(bids=[Lvl(201.10, 3, "NSDQ")], asks=[Lvl(201.12, 2, "ARCA")]),
    )
    assert code == 0
    assert payload["symbol"] == "AAPL"
    assert payload["secType"] == "STK"
    assert payload["conId"] == 265598
    assert payload["isSmartDepth"] is True
    assert payload["entitled"] is True
    assert payload["bids"] == [{"price": 201.10, "size": 3, "marketMaker": "NSDQ"}]
    assert payload["asks"] == [{"price": 201.12, "size": 2, "marketMaker": "ARCA"}]
    assert "note" not in payload
    assert ib.cancelled is True


def test_index_underlying(monkeypatch, capsys):
    payload, code, ib, _ = _run(
        monkeypatch,
        capsys,
        ["--symbol", "SPX"],
        ticker=_FakeTicker(bids=[Lvl(5000.0, 1, "CBOE")]),
    )
    assert code == 0
    assert payload["secType"] == "IND"


def test_option_contract_built(monkeypatch, capsys):
    payload, code, ib, _ = _run(
        monkeypatch,
        capsys,
        ["--symbol", "aapl", "--expiry", "20260618", "--strike", "200", "--right", "c"],
        ticker=_FakeTicker(bids=[Lvl(1.20, 5, "AMEX")]),
    )
    assert code == 0
    assert payload["secType"] == "OPT"
    # the contract handed to reqMktDepth is an Option built all-keyword
    assert isinstance(ib.depth_contract, Option)
    assert ib.depth_contract.right == "C"
    assert ib.depth_contract.strike == 200.0
    assert ib.depth_contract.lastTradeDateOrContractMonth == "20260618"
    assert ib.depth_contract.currency == "USD"  # not jammed into multiplier


def test_partial_option_tuple_rejected(monkeypatch, capsys):
    payload, code, ib, client = _run(
        monkeypatch,
        capsys,
        ["--symbol", "AAPL", "--expiry", "20260618", "--strike", "200"],  # no --right
    )
    assert code == 2
    assert "error" in payload
    assert "expiry" in payload["error"] and "right" in payload["error"]
    # never reached the IB calls
    assert ib.depth_contract is None


# --------------------------------------------------------------------------- #
# Entitlement / data-presence semantics
# --------------------------------------------------------------------------- #


def test_permission_denied_10089(monkeypatch, capsys):
    payload, code, _, _ = _run(
        monkeypatch,
        capsys,
        ["--symbol", "AAPL"],
        ticker=_FakeTicker(),  # empty
        error=(7, 10089, "Requested market data is not subscribed", None),
    )
    assert code == 0
    assert payload["entitled"] is False
    assert payload["bids"] == [] and payload["asks"] == []
    assert payload["note"] == "no L2 entitlement"


def test_permission_denied_by_text(monkeypatch, capsys):
    payload, code, _, _ = _run(
        monkeypatch,
        capsys,
        ["--symbol", "AAPL"],
        ticker=_FakeTicker(),
        error=(7, 999, "Depth is not supported for this combination", None),
    )
    assert payload["entitled"] is False
    assert payload["note"] == "no L2 entitlement"


def test_entitled_but_empty_is_not_unentitled(monkeypatch, capsys):
    payload, code, _, _ = _run(
        monkeypatch,
        capsys,
        ["--symbol", "AAPL"],
        ticker=_FakeTicker(),  # empty, no error fired
    )
    assert code == 0
    assert payload["entitled"] is True  # never observed a permission rejection
    assert payload["bids"] == [] and payload["asks"] == []
    assert payload["note"] == "no depth returned"


def test_309_chatter_with_book_is_entitled(monkeypatch, capsys):
    payload, code, _, _ = _run(
        monkeypatch,
        capsys,
        ["--symbol", "AAPL"],
        ticker=_FakeTicker(bids=[Lvl(10.0, 1, "ISLAND")]),
        error=(7, 309, "Max number (3) of market depth requests has been reached", None),
    )
    assert code == 0
    assert payload["entitled"] is True  # 309 is chatter, not a permission failure
    assert payload["bids"]


def test_num_rows_clamped(monkeypatch, capsys):
    _, code, ib, _ = _run(
        monkeypatch,
        capsys,
        ["--symbol", "AAPL", "--num-rows", "9999"],
        ticker=_FakeTicker(bids=[Lvl(1.0, 1, "X")]),
    )
    assert code == 0
    assert ib.depth_rows == 20  # clamped to <= 20


def test_hard_failure_exits_1(monkeypatch, capsys):
    ib = _FakeIB(_FakeTicker())

    def boom(*_a, **_k):
        raise RuntimeError("connect call failed")

    ib.reqMktDepth = boom  # type: ignore[assignment]
    client = _FakeClient(ib)
    monkeypatch.setattr(mod, "IBClient", lambda: client)
    monkeypatch.setattr("sys.argv", ["xenon-ib-market-depth", "--symbol", "AAPL"])
    with pytest.raises(SystemExit) as ei:
        mod.main()
    assert ei.value.code == 1
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "error" in payload
    assert client.disconnected is True  # finally ran
