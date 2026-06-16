import pytest

from xenon.db.queries import watchlist


def test_add_list_remove_roundtrip(pg_test_engine):
    uid = "user_test_1"
    watchlist.add(uid, "AAPL", sector="Technology")
    rows = watchlist.list_for_user(uid)
    assert [r["symbol"] for r in rows] == ["AAPL"]
    assert rows[0]["sector"] == "Technology"

    # idempotent add (UNIQUE user_id+symbol) — no duplicate, no error
    watchlist.add(uid, "AAPL", sector="Technology")
    assert len(watchlist.list_for_user(uid)) == 1

    watchlist.remove(uid, "AAPL")
    assert watchlist.list_for_user(uid) == []


def test_scoped_per_user(pg_test_engine):
    watchlist.add("user_a", "TSLA")
    watchlist.add("user_b", "NVDA")
    assert [r["symbol"] for r in watchlist.list_for_user("user_a")] == ["TSLA"]
    assert [r["symbol"] for r in watchlist.list_for_user("user_b")] == ["NVDA"]
