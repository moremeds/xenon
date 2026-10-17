"""Tests for the WebSocket ticket system (single-use, 30s TTL)."""

import time
import uuid

# --- ws_ticket tests ---
from xenon.api.ws_ticket import TICKET_TTL_SECONDS, _ticket_store, create_ticket, validate_ticket


class TestCreateTicket:
    def setup_method(self):
        _ticket_store.clear()

    def test_returns_valid_uuid(self):
        ticket = create_ticket("user_123")
        uuid.UUID(ticket)  # raises if not valid UUID

    def test_stores_user_id_and_expiry(self):
        ticket = create_ticket("user_abc")
        assert ticket in _ticket_store
        assert _ticket_store[ticket]["user_id"] == "user_abc"
        assert _ticket_store[ticket]["expires"] > time.time()


class TestValidateTicket:
    def setup_method(self):
        _ticket_store.clear()

    def test_valid_ticket_returns_user_id(self):
        ticket = create_ticket("user_xyz")
        assert validate_ticket(ticket) == "user_xyz"

    def test_ticket_is_single_use(self):
        ticket = create_ticket("user_once")
        assert validate_ticket(ticket) == "user_once"
        assert validate_ticket(ticket) is None

    def test_invalid_ticket_returns_none(self):
        assert validate_ticket("nonexistent-ticket") is None

    def test_expired_ticket_returns_none(self):
        ticket = create_ticket("user_exp")
        _ticket_store[ticket]["expires"] = time.time() - 1
        assert validate_ticket(ticket) is None

    def test_cleanup_removes_expired(self):
        t1 = create_ticket("user_old")
        _ticket_store[t1]["expires"] = time.time() - 1
        t2 = create_ticket("user_new")
        # validate triggers cleanup via _cleanup_expired
        validate_ticket("dummy")
        assert t1 not in _ticket_store
        assert t2 in _ticket_store
