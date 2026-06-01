"""GET /futu-stats — exposes FutuApiStats.snapshot for the sidebar."""

from fastapi.testclient import TestClient

from xenon.api.server import app


def test_futu_stats_returns_snapshot_envelope(futu_pytest_app_scope):
    with TestClient(app) as client:
        r = client.get("/futu-stats")
    assert r.status_code == 200
    body = r.json()
    # FutuApiStats.snapshot() returns dict[fn_name -> metrics]. Empty {} is
    # valid when no SDK calls have run in this session.
    assert isinstance(body, dict)
