from xenon.api import server


def test_reachable_passthrough(monkeypatch):
    payload = {
        "ib_connected": True,
        "subscribers": [{"id": "alpha", "connected": True, "last_pong_ms_ago": 1000}],
        "anonymous_count": 2,
        "ttl_ms": 900000,
    }
    monkeypatch.setattr(server, "_fetch_realtime_status_json", lambda port, timeout=0.5: payload)
    monkeypatch.setattr(server, "_resolve_realtime_port", lambda: 8765)

    out = server._realtime_subscribers_health()
    assert out["reachable"] is True
    assert out["subscribers"] == payload["subscribers"]
    assert out["anonymous_count"] == 2


def test_silent_degrade_when_unreachable(monkeypatch):
    def boom(port, timeout=0.5):
        raise OSError("connection refused")

    monkeypatch.setattr(server, "_fetch_realtime_status_json", boom)
    monkeypatch.setattr(server, "_resolve_realtime_port", lambda: 8765)

    out = server._realtime_subscribers_health()
    assert out == {"reachable": False, "subscribers": [], "anonymous_count": 0}


def test_resolve_realtime_port_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.setenv("IB_REALTIME_RUNTIME_FILE", str(tmp_path / "absent.json"))
    assert server._resolve_realtime_port() == 8765
