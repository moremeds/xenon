"""Tests for IBClient resilient reconnection — subscription tracking + disconnect recovery.

Red/Green TDD: these tests are written FIRST (RED phase), then implementation follows.
All tests mock ib_insync.IB — no real IB connection needed.
"""

import logging
from unittest.mock import MagicMock, patch, call

import pytest

from clients.ib_client import IBClient, IBConnectionError


# ---------------------------------------------------------------------------
# Subscription tracking
# ---------------------------------------------------------------------------


class TestSubscriptionTracking:
    """Track active market data subscriptions for recovery after disconnect."""

    @patch("clients.ib_client.IB")
    def test_get_quote_streaming_adds_subscription(self, MockIB):
        """get_quote with snapshot=False should record the subscription."""
        mock_ib = MockIB.return_value
        mock_ib.isConnected.return_value = True
        mock_ib.reqMktData.return_value = MagicMock()

        client = IBClient()
        client.connect(client_id=1)

        contract = MagicMock()
        client.get_quote(contract, snapshot=False, generic_ticks="233")

        assert len(client._subscriptions) == 1
        sub = client._subscriptions[0]
        assert sub["contract"] is contract
        assert sub["generic_ticks"] == "233"

    @patch("clients.ib_client.IB")
    def test_get_quote_snapshot_does_not_add_subscription(self, MockIB):
        """get_quote with snapshot=True should NOT record a subscription."""
        mock_ib = MockIB.return_value
        mock_ib.isConnected.return_value = True
        mock_ib.reqMktData.return_value = MagicMock()

        client = IBClient()
        client.connect(client_id=1)

        contract = MagicMock()
        client.get_quote(contract, snapshot=True)

        assert len(client._subscriptions) == 0

    @patch("clients.ib_client.IB")
    def test_multiple_subscriptions_tracked(self, MockIB):
        """Multiple streaming subscriptions should all be tracked."""
        mock_ib = MockIB.return_value
        mock_ib.isConnected.return_value = True
        mock_ib.reqMktData.return_value = MagicMock()

        client = IBClient()
        client.connect(client_id=1)

        c1, c2, c3 = MagicMock(), MagicMock(), MagicMock()
        client.get_quote(c1, snapshot=False)
        client.get_quote(c2, snapshot=False, generic_ticks="100,101")
        client.get_quote(c3, snapshot=True)  # snapshot — not tracked

        assert len(client._subscriptions) == 2

    @patch("clients.ib_client.IB")
    def test_clear_subscriptions(self, MockIB):
        """clear_subscriptions should empty the tracking list."""
        mock_ib = MockIB.return_value
        mock_ib.isConnected.return_value = True
        mock_ib.reqMktData.return_value = MagicMock()

        client = IBClient()
        client.connect(client_id=1)

        client.get_quote(MagicMock(), snapshot=False)
        assert len(client._subscriptions) == 1

        client.clear_subscriptions()
        assert len(client._subscriptions) == 0


# ---------------------------------------------------------------------------
# Disconnect handler
# ---------------------------------------------------------------------------


class TestDisconnectHandler:
    """Test _on_disconnect auto-reconnect with exponential backoff."""

    @patch("clients.ib_client.time.sleep")
    @patch("clients.ib_client.IB")
    def test_on_disconnect_attempts_reconnection(self, MockIB, mock_sleep):
        """_on_disconnect should attempt to reconnect."""
        mock_ib = MockIB.return_value
        mock_ib.isConnected.return_value = True

        client = IBClient()
        client.connect(client_id=1)

        # Simulate disconnect — reconnect succeeds on first try
        mock_ib.connect.reset_mock()
        client._on_disconnect()

        # Should have called connect again
        assert mock_ib.connect.call_count >= 1

    @patch("clients.ib_client.time.sleep")
    @patch("clients.ib_client.IB")
    def test_on_disconnect_exponential_backoff(self, MockIB, mock_sleep):
        """_on_disconnect should use exponential backoff between attempts."""
        mock_ib = MockIB.return_value
        mock_ib.isConnected.return_value = True

        client = IBClient()
        client.connect(client_id=1)

        # Make reconnect fail repeatedly, then succeed
        mock_ib.connect.reset_mock()
        mock_ib.connect.side_effect = [
            ConnectionRefusedError("fail"),
            ConnectionRefusedError("fail"),
            None,  # succeeds on 3rd attempt
        ]

        client._on_disconnect()

        # Check backoff delays: 2^0=1, 2^1=2 (first two fail, third succeeds)
        # Backoff formula: min(2**attempt, 30)
        sleep_calls = [c.args[0] for c in mock_sleep.call_args_list]
        assert len(sleep_calls) >= 2
        # First delay should be small, second larger
        assert sleep_calls[0] <= sleep_calls[1]

    @patch("clients.ib_client.time.sleep")
    @patch("clients.ib_client.IB")
    def test_on_disconnect_restores_subscriptions(self, MockIB, mock_sleep):
        """After reconnect, tracked subscriptions should be restored."""
        mock_ib = MockIB.return_value
        mock_ib.isConnected.return_value = True
        mock_ib.reqMktData.return_value = MagicMock()

        client = IBClient()
        client.connect(client_id=1)

        # Add subscriptions
        c1 = MagicMock()
        c2 = MagicMock()
        client.get_quote(c1, snapshot=False, generic_ticks="233")
        client.get_quote(c2, snapshot=False)

        # Record initial reqMktData call count
        initial_count = mock_ib.reqMktData.call_count
        assert initial_count == 2

        # Reconnect succeeds
        mock_ib.connect.reset_mock()
        client._on_disconnect()

        # reqMktData should have been called again for each subscription
        assert mock_ib.reqMktData.call_count == initial_count + 2

    @patch("clients.ib_client.time.sleep")
    @patch("clients.ib_client.IB")
    def test_reconnecting_flag_prevents_concurrent_reconnects(self, MockIB, mock_sleep):
        """_reconnecting flag should prevent concurrent reconnect attempts."""
        mock_ib = MockIB.return_value
        mock_ib.isConnected.return_value = True

        client = IBClient()
        client.connect(client_id=1)

        # Manually set flag
        client._reconnecting = True

        mock_ib.connect.reset_mock()
        client._on_disconnect()

        # Should not have attempted to reconnect
        mock_ib.connect.assert_not_called()

    @patch("clients.ib_client.time.sleep")
    @patch("clients.ib_client.IB")
    def test_reconnection_gives_up_after_max_attempts(self, MockIB, mock_sleep):
        """After max attempts (5), _on_disconnect should give up."""
        mock_ib = MockIB.return_value
        mock_ib.isConnected.return_value = True

        client = IBClient()
        client.connect(client_id=1)

        # All reconnect attempts fail
        mock_ib.connect.reset_mock()
        mock_ib.connect.side_effect = ConnectionRefusedError("refused")

        client._on_disconnect()

        # Should have tried exactly 5 times (MAX_RECONNECT_ATTEMPTS)
        assert mock_ib.connect.call_count == 5

    @patch("clients.ib_client.time.sleep")
    @patch("clients.ib_client.IB")
    def test_reconnecting_flag_cleared_after_success(self, MockIB, mock_sleep):
        """_reconnecting flag should be cleared after successful reconnect."""
        mock_ib = MockIB.return_value
        mock_ib.isConnected.return_value = True

        client = IBClient()
        client.connect(client_id=1)

        mock_ib.connect.reset_mock()
        client._on_disconnect()

        assert client._reconnecting is False

    @patch("clients.ib_client.time.sleep")
    @patch("clients.ib_client.IB")
    def test_reconnecting_flag_cleared_after_failure(self, MockIB, mock_sleep):
        """_reconnecting flag should be cleared even after failed reconnect."""
        mock_ib = MockIB.return_value
        mock_ib.isConnected.return_value = True

        client = IBClient()
        client.connect(client_id=1)

        mock_ib.connect.reset_mock()
        mock_ib.connect.side_effect = ConnectionRefusedError("refused")

        client._on_disconnect()

        assert client._reconnecting is False

    @patch("clients.ib_client.time.sleep")
    @patch("clients.ib_client.IB")
    def test_on_disconnect_logs_restored_count(self, MockIB, mock_sleep, caplog):
        """Should log how many subscriptions were restored vs failed."""
        mock_ib = MockIB.return_value
        mock_ib.isConnected.return_value = True
        mock_ib.reqMktData.return_value = MagicMock()

        client = IBClient()
        client.connect(client_id=1)

        c1 = MagicMock()
        client.get_quote(c1, snapshot=False)

        mock_ib.connect.reset_mock()
        with caplog.at_level(logging.INFO, logger="ib_client"):
            client._on_disconnect()

        assert any("restor" in r.message.lower() for r in caplog.records)

    @patch("clients.ib_client.time.sleep")
    @patch("clients.ib_client.IB")
    def test_backoff_capped_at_30_seconds(self, MockIB, mock_sleep):
        """Exponential backoff should be capped at 30 seconds."""
        mock_ib = MockIB.return_value
        mock_ib.isConnected.return_value = True

        client = IBClient()
        client.connect(client_id=1)

        # All reconnect attempts fail
        mock_ib.connect.reset_mock()
        mock_ib.connect.side_effect = ConnectionRefusedError("refused")

        client._on_disconnect()

        sleep_calls = [c.args[0] for c in mock_sleep.call_args_list]
        for delay in sleep_calls:
            assert delay <= 30
