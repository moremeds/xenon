from xenon.api import server


def test_reachable_passthrough(monkeypatch):
    payload = {
        "ib_connected": True,
        "subscribers": [{"id": "alpha", "connected": True, "last_pong_ms_ago": 1000}],
        "anonymous_count": 2,
        "ttl_ms": 900000,
    }
    monkeypatch.setattr(server, "_fetch_realtime_status_json", lambda url, timeout=0.5: payload)
    monkeypatch.setattr(server, "_resolve_realtime_status_url", lambda: "http://127.0.0.1:8765/status")

    out = server._realtime_subscribers_health()
    assert out["reachable"] is True
    assert out["subscribers"] == payload["subscribers"]
    assert out["anonymous_count"] == 2


def test_silent_degrade_when_unreachable(monkeypatch):
    def boom(url, timeout=0.5):
        raise OSError("connection refused")

    monkeypatch.setattr(server, "_fetch_realtime_status_json", boom)
    monkeypatch.setattr(server, "_resolve_realtime_status_url", lambda: "http://127.0.0.1:8765/status")

    out = server._realtime_subscribers_health()
    assert out == {"reachable": False, "subscribers": [], "anonymous_count": 0}


def test_resolve_realtime_port_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.setenv("IB_REALTIME_RUNTIME_FILE", str(tmp_path / "absent.json"))
    assert server._resolve_realtime_port() == 8765


def test_status_url_explicit_env_wins(monkeypatch):
    monkeypatch.setenv("IB_REALTIME_STATUS_URL", "http://realtime:8765/status")
    assert server._resolve_realtime_status_url() == "http://realtime:8765/status"


def test_status_url_defaults_to_loopback(monkeypatch):
    monkeypatch.delenv("IB_REALTIME_STATUS_URL", raising=False)
    monkeypatch.setattr(server, "_resolve_realtime_port", lambda: 8765)
    assert server._resolve_realtime_status_url() == "http://127.0.0.1:8765/status"


def test_fetch_sends_token_header_when_configured(monkeypatch):
    import urllib.request

    monkeypatch.setenv("IB_REALTIME_STATUS_TOKEN", "secret123")
    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"ok": true}'

    def _fake_urlopen(request, timeout=0.5):
        captured["headers"] = dict(request.headers)
        return _FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    out = server._fetch_realtime_status_json("http://realtime:8765/status")
    assert out == {"ok": True}
    assert "secret123" in captured["headers"].values()


def test_fetch_omits_token_header_when_unset(monkeypatch):
    import urllib.request

    monkeypatch.delenv("IB_REALTIME_STATUS_TOKEN", raising=False)
    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"ok": true}'

    def _fake_urlopen(request, timeout=0.5):
        captured["headers"] = dict(request.headers)
        return _FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    server._fetch_realtime_status_json("http://127.0.0.1:8765/status")
    assert "secret123" not in captured["headers"].values()
