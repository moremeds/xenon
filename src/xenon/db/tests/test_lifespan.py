import pytest


@pytest.mark.asyncio
async def test_engine_initialized_in_lifespan():
    from xenon.db.engine import dispose_engine, get_engine, init_engine

    engine = init_engine("postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test")
    assert get_engine() is engine
    await dispose_engine()
