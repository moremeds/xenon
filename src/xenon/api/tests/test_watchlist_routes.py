import pytest
from fastapi.testclient import TestClient

from xenon.api.server import app


@pytest.mark.committed_db
def test_watchlist_crud():
    # TestClient calls arrive as localhost → auth_middleware skips auth (no token
    # needed); routes resolve the operator user_id ("local") server-side.
    with TestClient(app) as client:
        r = client.get("/watchlist")
        assert r.status_code == 200
        assert r.json() == {"watchlist": []}

        r = client.post("/watchlist", json={"symbol": "aapl", "sector": "Tech"})
        assert r.status_code == 200

        r = client.get("/watchlist")
        syms = [w["symbol"] for w in r.json()["watchlist"]]
        assert syms == ["AAPL"]

        r = client.delete("/watchlist/AAPL")
        assert r.status_code == 200
        assert client.get("/watchlist").json()["watchlist"] == []
