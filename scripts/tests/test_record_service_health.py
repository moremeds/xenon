import sqlalchemy as sa

from xenon.db.engine import get_sync_engine
from xenon.db.schema import service_health
from xenon.db.service_health import record_service_health


def _row(service):
    with get_sync_engine().connect() as c:
        return c.execute(sa.select(service_health).where(service_health.c.service == service)).mappings().first()


def test_insert_then_update(pg_test_engine):
    record_service_health("unit_test_writer", "ok")
    r = _row("unit_test_writer")
    assert r is not None
    assert r["state"] == "ok"
    assert r["broker"] == "IB"

    record_service_health("unit_test_writer", "error", error={"msg": "boom"})
    r2 = _row("unit_test_writer")
    assert r2["state"] == "error"
    assert "boom" in (r2["last_error"] or "")


def test_read_only_noop(pg_test_engine, monkeypatch):
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    record_service_health("ro_writer", "ok")
    assert _row("ro_writer") is None


def test_never_raises(monkeypatch):
    monkeypatch.setenv("XENON_READ_ONLY", "0")
    import xenon.db.service_health as mod

    def _boom():
        raise RuntimeError("no db")

    monkeypatch.setattr(mod, "get_sync_engine", _boom, raising=False)
    record_service_health("x", "ok")  # must not raise
