"""Postgres advisory-lock helper.

Phase 0.5 of the VCG-R + CRI rewiring. The helper guards singleton
background loops in multi-worker FastAPI deployments. Three things
to prove:

  1. Uncontested acquire returns True.
  2. Exiting the context releases the lock so a re-acquire succeeds.
  3. A concurrent acquirer on the same key gets False while the lock is held.

Each test uses a distinct key so parallel test runs don't collide.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from xenon.api.services.advisory_lock import pg_try_advisory_lock


def _async_test_db_url() -> str:
    return os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    )


# Test-only keys; well above the constants the production loops use to
# avoid any chance of stomping on a real loop running on the dev DB.
_KEY_UNCONTESTED = 9_990_001
_KEY_RELEASE = 9_990_002
_KEY_CONCURRENT = 9_990_003


@pytest.fixture
def async_engine():
    eng = create_async_engine(_async_test_db_url())
    yield eng
    asyncio.run(eng.dispose())


def test_uncontested_acquire_yields_true(async_engine):
    async def _go():
        async with pg_try_advisory_lock(_KEY_UNCONTESTED, engine=async_engine) as got:
            assert got is True

    asyncio.run(_go())


def test_lock_released_on_exit_allows_reacquire(async_engine):
    async def _go():
        async with pg_try_advisory_lock(_KEY_RELEASE, engine=async_engine) as first:
            assert first is True
        # First connection has been closed; lock is released.
        async with pg_try_advisory_lock(_KEY_RELEASE, engine=async_engine) as second:
            assert second is True

    asyncio.run(_go())


def test_concurrent_acquirer_gets_false_while_first_holds(async_engine):
    """Two distinct sessions hitting the same key — only the first wins."""

    second_engine = create_async_engine(_async_test_db_url())

    async def _go():
        try:
            async with pg_try_advisory_lock(_KEY_CONCURRENT, engine=async_engine) as first:
                assert first is True
                # Inside the first lock, a second acquirer on a *different*
                # session must be told no.
                async with pg_try_advisory_lock(_KEY_CONCURRENT, engine=second_engine) as second:
                    assert second is False
            # Outside the first block — lock released; second acquirer
            # would now succeed.
            async with pg_try_advisory_lock(_KEY_CONCURRENT, engine=second_engine) as after:
                assert after is True
        finally:
            await second_engine.dispose()

    asyncio.run(_go())
