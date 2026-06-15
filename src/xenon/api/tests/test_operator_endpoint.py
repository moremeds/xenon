from fastapi.testclient import TestClient

from xenon.api import server as server_mod


def test_operator_payload_shape(pg_session):
    # No per-route Depends; the global auth_middleware passes through in tests
    # (CLERK_JWKS_URL unset) — same as every other data-route test. No token needed.
    r = TestClient(server_mod.app).get("/admin/operator")
    assert r.status_code == 200
    body = r.json()
    for key in (
        "generated_at",
        "ib_gateway",
        "ib_pool",
        "ib_auth",
        "trading_mode",
        "snapshotter",
        "order_submissions",
        "flex_divergence",
        "realtime_subscribers",
        "futu",
        "writers",
    ):
        assert key in body
    # `uw` was removed: the live quota tile self-fetches /api/admin/uw-quota,
    # so the aggregate no longer carries the dead uw_api_stats block.
    assert "uw" not in body
    assert body["ib_auth"] in {"authenticated", "awaiting", "unreachable", "unknown"}
    # nested-shape contract asserts (not just top-level keys)
    assert isinstance(body["ib_gateway"]["port_listening"], bool)
    assert "stale_seconds" in body["snapshotter"]
    assert isinstance(body["order_submissions"]["alarm"], bool)
    assert isinstance(body["writers"], list)
    # missing-writer synthesis means every expected writer is present even cold
    services = {w["service"] for w in body["writers"]}
    assert {"ib_activity_poller", "naked_short_audit"} <= services
