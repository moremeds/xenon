"""Tests for scripts/utils/ shared utility modules."""

# ══════════════════════════════════════════════════════════════════════
# IB Connection utils
# ══════════════════════════════════════════════════════════════════════


class TestIbDefaults:
    def test_default_host(self):
        from xenon.utils.ib_connection import DEFAULT_HOST

        # DEFAULT_HOST comes from env/dotenv — verify it's a non-empty string
        assert isinstance(DEFAULT_HOST, str) and len(DEFAULT_HOST) > 0

    def test_default_gateway_port(self):
        # DEFAULT_GATEWAY_PORT now derives from XENON_TRADING_MODE — verify it
        # tracks the trading_mode contract rather than asserting a fixed value.
        from xenon.api.trading_mode import EXPECTED_PORT
        from xenon.utils.ib_connection import DEFAULT_GATEWAY_PORT

        assert DEFAULT_GATEWAY_PORT == EXPECTED_PORT

    def test_default_tws_port(self):
        from xenon.utils.ib_connection import DEFAULT_TWS_PORT

        assert DEFAULT_TWS_PORT == 7497
