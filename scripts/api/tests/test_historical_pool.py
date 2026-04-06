"""Tests for historical endpoint ib_pool resolution via app.state."""

import pytest
from unittest.mock import MagicMock

from fastapi import HTTPException
from api.routes.historical import _get_pool


class FakeState:
    pass


class FakeApp:
    def __init__(self, pool=None):
        self.state = FakeState()
        if pool is not None:
            self.state.ib_pool = pool


class FakeRequest:
    def __init__(self, app):
        self.app = app


class TestGetPool:
    def test_returns_pool_from_app_state(self):
        mock_pool = MagicMock()
        request = FakeRequest(FakeApp(pool=mock_pool))
        assert _get_pool(request) is mock_pool

    def test_raises_503_when_pool_is_none(self):
        request = FakeRequest(FakeApp(pool=None))
        with pytest.raises(HTTPException) as exc_info:
            _get_pool(request)
        assert exc_info.value.status_code == 503

    def test_raises_503_when_pool_not_set(self):
        request = FakeRequest(FakeApp())  # no ib_pool on state
        with pytest.raises(HTTPException) as exc_info:
            _get_pool(request)
        assert exc_info.value.status_code == 503
