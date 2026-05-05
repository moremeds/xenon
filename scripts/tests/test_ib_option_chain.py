import sys

from xenon.clients.ib_client import DEFAULT_GATEWAY_PORT
from xenon.execution import ib_option_chain


class _FakeIB:
    def qualifyContracts(self, contract):
        contract.conId = 12345
        return [contract]

    def reqSecDefOptParams(self, *_args):
        return []


class _FakeClient:
    def __init__(self):
        self._ib = _FakeIB()
        self.connected = None

    def connect(self, *, port, client_id):
        self.connected = {"port": port, "client_id": client_id}

    def disconnect(self):
        return None


def test_option_chain_defaults_to_mode_gateway_port_and_auto_client(monkeypatch, capsys):
    fake = _FakeClient()
    monkeypatch.setattr(ib_option_chain, "IBClient", lambda: fake)
    monkeypatch.setattr(sys, "argv", ["xenon-ib-option-chain", "--symbol", "AAPL"])

    ib_option_chain.main()

    assert fake.connected == {"port": DEFAULT_GATEWAY_PORT, "client_id": "auto"}
    body = capsys.readouterr().out
    assert '"symbol": "AAPL"' in body
