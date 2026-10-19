"""Shared connect_ib helper: success, CLI exit, and propagate-on-failure modes."""

import pytest

from xenon.execution import ib_connect


class _FakeClient:
    calls: list = []
    fail = False

    def connect(self, **kwargs):
        _FakeClient.calls.append(kwargs)
        if _FakeClient.fail:
            raise RuntimeError("boom")


@pytest.fixture(autouse=True)
def _fake_client(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.fail = False
    monkeypatch.setattr(ib_connect, "IBClient", _FakeClient)


def test_success_passes_host_port_client_id(capsys):
    client = ib_connect.connect_ib("127.0.0.1", 4002, 7)
    assert isinstance(client, _FakeClient)
    assert _FakeClient.calls == [{"host": "127.0.0.1", "port": 4002, "client_id": 7}]
    assert "Connected to IB on 127.0.0.1:4002" in capsys.readouterr().out


def test_quiet_suppresses_success_line(capsys):
    ib_connect.connect_ib("127.0.0.1", 4002, quiet=True)
    assert _FakeClient.calls[0]["client_id"] == "auto"
    assert capsys.readouterr().out == ""


def test_failure_exits_1_by_default(capsys):
    _FakeClient.fail = True
    with pytest.raises(SystemExit) as exc:
        ib_connect.connect_ib("127.0.0.1", 4002)
    assert exc.value.code == 1
    assert "Connection failed: boom" in capsys.readouterr().out


def test_failure_propagates_when_exit_disabled():
    _FakeClient.fail = True
    with pytest.raises(RuntimeError, match="boom"):
        ib_connect.connect_ib("127.0.0.1", 4002, exit_on_failure=False)
