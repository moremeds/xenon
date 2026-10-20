"""Tests for IB client ID range-based allocation with retry.

Validates that:
1. Persistent services (pool, relay) get fixed IDs from reserved ranges
2. On-demand scripts get IDs from the subprocess range with auto-retry
3. Client ID conflicts trigger automatic rotation to next available ID
4. Range exhaustion raises a clear error
5. The pool's reserved IDs are never allocated to subprocesses
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure scripts/ is importable
SCRIPTS_DIR = Path(__file__).parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# Mock-transport tests — bypass the broker *boundary* check; the socket
# denylist still refuses any real dial.
pytestmark = pytest.mark.usefixtures("bypass_broker_boundary")


# ---------------------------------------------------------------------------
# 1. Range constants are correctly defined and non-overlapping
# ---------------------------------------------------------------------------

class TestClientIdRanges:
    """Verify the ID range constants exist and don't overlap."""

    def test_pool_range_exists(self):
        from xenon.clients.ib_client import POOL_ID_RANGE
        assert POOL_ID_RANGE == (0, 9)

    def test_relay_range_exists(self):
        from xenon.clients.ib_client import RELAY_ID_RANGE
        assert RELAY_ID_RANGE == (10, 19)

    def test_subprocess_range_exists(self):
        from xenon.clients.ib_client import SUBPROCESS_ID_RANGE
        assert SUBPROCESS_ID_RANGE == (20, 49)

    def test_ranges_do_not_overlap(self):
        from xenon.clients.ib_client import POOL_ID_RANGE, RELAY_ID_RANGE, SUBPROCESS_ID_RANGE
        pool = set(range(POOL_ID_RANGE[0], POOL_ID_RANGE[1] + 1))
        relay = set(range(RELAY_ID_RANGE[0], RELAY_ID_RANGE[1] + 1))
        subproc = set(range(SUBPROCESS_ID_RANGE[0], SUBPROCESS_ID_RANGE[1] + 1))
        assert pool.isdisjoint(relay), "Pool and relay ranges overlap"
        assert pool.isdisjoint(subproc), "Pool and subprocess ranges overlap"
        assert relay.isdisjoint(subproc), "Relay and subprocess ranges overlap"

    def test_pool_roles_use_pool_range(self):
        from xenon.clients.ib_client import POOL_ROLES, POOL_ID_RANGE
        lo, hi = POOL_ID_RANGE
        for role, cid in POOL_ROLES.items():
            assert lo <= cid <= hi, f"Pool role '{role}' uses ID {cid} outside pool range {lo}-{hi}"

    def test_pool_roles_have_unique_ids(self):
        from xenon.clients.ib_client import POOL_ROLES
        ids = list(POOL_ROLES.values())
        assert len(ids) == len(set(ids)), f"Duplicate pool role IDs: {ids}"


# ---------------------------------------------------------------------------
# 2. connect() with auto-allocate from subprocess range
# ---------------------------------------------------------------------------

class TestSubprocessAutoAllocate:
    """On-demand scripts should auto-allocate IDs from the subprocess range."""

    def test_connect_with_auto_allocate_uses_subprocess_range(self):
        """When client_id='auto', connect should try IDs from subprocess range."""
        from xenon.clients.ib_client import IBClient, SUBPROCESS_ID_RANGE

        client = IBClient()
        mock_ib = MagicMock()
        client._ib = mock_ib
        # First connect succeeds
        mock_ib.connect.return_value = None
        mock_ib.isConnected.return_value = True

        client.connect(client_id="auto", timeout=1)

        # Should have connected with an ID in the subprocess range
        called_id = mock_ib.connect.call_args[1].get("clientId") or mock_ib.connect.call_args[0][2]
        lo, hi = SUBPROCESS_ID_RANGE
        assert lo <= called_id <= hi, f"Auto-allocated ID {called_id} outside subprocess range {lo}-{hi}"

    def test_auto_allocate_retries_on_client_id_conflict(self):
        """When first ID is in use, auto-allocate should try a different one."""
        from xenon.clients.ib_client import IBClient, SUBPROCESS_ID_RANGE

        client = IBClient()
        mock_ib = MagicMock()
        client._ib = mock_ib

        call_count = 0

        def mock_connect(host, port, clientId, timeout=3):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("client id is already in use")
            # Second call succeeds

        mock_ib.connect.side_effect = mock_connect

        client.connect(client_id="auto", timeout=1)

        assert call_count == 2
        # The two calls should use different IDs
        first_id = mock_ib.connect.call_args_list[0][1]["clientId"]
        second_id = mock_ib.connect.call_args_list[1][1]["clientId"]
        assert first_id != second_id, "Retry should use a different client ID"
        lo, hi = SUBPROCESS_ID_RANGE
        assert lo <= second_id <= hi

    def test_auto_allocate_wraps_around_range(self):
        """When approaching range end, should wrap to start."""
        from xenon.clients.ib_client import IBClient, SUBPROCESS_ID_RANGE

        client = IBClient()
        mock_ib = MagicMock()
        client._ib = mock_ib

        lo, hi = SUBPROCESS_ID_RANGE
        range_size = hi - lo + 1

        # Fail for all IDs in range except the very last attempt
        attempts = 0

        def mock_connect(host, port, clientId, timeout=3):
            nonlocal attempts
            attempts += 1
            if attempts < range_size:
                raise ConnectionError("client id is already in use")
            # Last attempt succeeds

        mock_ib.connect.side_effect = mock_connect

        client.connect(client_id="auto", timeout=1)
        assert attempts == range_size

    def test_auto_allocate_raises_when_range_exhausted(self):
        """When all IDs in range are taken, should raise IBConnectionError."""
        from xenon.clients.ib_client import IBClient, IBConnectionError, SUBPROCESS_ID_RANGE

        client = IBClient()
        mock_ib = MagicMock()
        client._ib = mock_ib

        # ALL IDs fail
        mock_ib.connect.side_effect = ConnectionError("client id is already in use")

        with pytest.raises(IBConnectionError, match="all client IDs.*in use"):
            client.connect(client_id="auto", timeout=1)

        lo, hi = SUBPROCESS_ID_RANGE
        expected_attempts = hi - lo + 1
        assert mock_ib.connect.call_count == expected_attempts

    def test_auto_allocate_does_not_retry_non_conflict_errors(self):
        """Connection errors other than 'client id in use' should not trigger rotation."""
        from xenon.clients.ib_client import IBClient, IBConnectionError

        client = IBClient()
        mock_ib = MagicMock()
        client._ib = mock_ib

        mock_ib.connect.side_effect = ConnectionError("Connection refused")

        with pytest.raises(IBConnectionError):
            client.connect(client_id="auto", timeout=1, max_retries=1)

        # Should only try once (no ID rotation for non-conflict errors)
        assert mock_ib.connect.call_count == 1


# ---------------------------------------------------------------------------
# 3. Explicit client_id still works (backward compat)
# ---------------------------------------------------------------------------

class TestExplicitClientId:
    """Explicit integer client_id should bypass auto-allocation."""

    def test_explicit_int_id_used_directly(self):
        from xenon.clients.ib_client import IBClient

        client = IBClient()
        mock_ib = MagicMock()
        client._ib = mock_ib

        client.connect(client_id=99, timeout=1)

        called_id = mock_ib.connect.call_args[1].get("clientId") or mock_ib.connect.call_args[0][2]
        assert called_id == 99

    @patch("time.sleep")
    def test_explicit_id_no_retry_on_conflict(self, _mock_sleep):
        """Explicit IDs should NOT auto-rotate — the caller chose that ID deliberately."""
        from xenon.clients.ib_client import IBClient, IBConnectionError

        client = IBClient()
        mock_ib = MagicMock()
        client._ib = mock_ib
        mock_ib.connect.side_effect = ConnectionError("client id is already in use")

        with pytest.raises(IBConnectionError):
            client.connect(client_id=99, timeout=1, max_retries=2)

        # Should retry max_retries times with the SAME ID (no rotation)
        assert mock_ib.connect.call_count == 2
        for c in mock_ib.connect.call_args_list:
            cid = c[1].get("clientId") or c[0][2]
            assert cid == 99

    def test_client_name_lookup_still_works(self):
        """client_name resolves to base + the registry offset."""
        from xenon.clients.ib_client import CLIENT_IDS, IBClient

        client = IBClient()
        mock_ib = MagicMock()
        client._ib = mock_ib

        client.connect(client_name="fetch_analyst_ratings", timeout=1)

        called_id = mock_ib.connect.call_args[1].get("clientId")
        if called_id is None:
            called_id = mock_ib.connect.call_args[0][2]
        assert called_id == CLIENT_IDS["fetch_analyst_ratings"]

    def test_pool_role_name_is_not_a_client_name(self):
        """PR-10 / A5: a pool role name no longer resolves outside the pool."""
        from xenon.clients.ib_client import IBClient

        client = IBClient()
        client._ib = MagicMock()
        with pytest.raises(ValueError, match="Unknown client name"):
            client.connect(client_name="sync", timeout=1)
        client._ib.connect.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Randomized start offset prevents thundering herd
# ---------------------------------------------------------------------------

class TestRandomizedStart:
    """Auto-allocate should start at a random offset in the range to avoid
    multiple scripts always competing for ID 20 first."""

    def test_auto_allocate_start_varies(self):
        """Multiple auto-allocate calls should not always start at the same ID."""
        from xenon.clients.ib_client import IBClient

        observed_first_ids = set()
        for _ in range(20):
            client = IBClient()
            mock_ib = MagicMock()
            client._ib = mock_ib
            mock_ib.connect.return_value = None

            client.connect(client_id="auto", timeout=1)

            cid = mock_ib.connect.call_args[1].get("clientId") or mock_ib.connect.call_args[0][2]
            observed_first_ids.add(cid)

        # With 30 slots and 20 attempts, we should see more than 1 unique starting ID
        # (probability of all 20 hitting the same slot is (1/30)^19 ≈ 0)
        assert len(observed_first_ids) > 1, f"All 20 auto-allocations started at the same ID: {observed_first_ids}"


# ---------------------------------------------------------------------------
# 5. Pool uses POOL_ROLES, not CLIENT_IDS
# ---------------------------------------------------------------------------

class TestPoolRoles:
    """The IB pool should use POOL_ROLES for its connections."""

    def test_pool_roles_exported(self):
        from xenon.clients.ib_client import POOL_ROLES
        assert "sync" in POOL_ROLES
        assert "orders" in POOL_ROLES
        assert "data" in POOL_ROLES

    def test_pool_roles_in_pool_range(self):
        from xenon.clients.ib_client import POOL_ROLES, POOL_ID_RANGE
        lo, hi = POOL_ID_RANGE
        for role, cid in POOL_ROLES.items():
            assert lo <= cid <= hi


# ---------------------------------------------------------------------------
# 5. PR-10 / A5 — XENON_IB_CLIENT_ID_BASE zones, enforced at IBClient.connect
# ---------------------------------------------------------------------------


def _mock_client():
    from xenon.clients.ib_client import IBClient

    client = IBClient()
    client._ib = MagicMock()
    return client


def _called_id(client):
    call = client._ib.connect.call_args
    return call[1].get("clientId") if call[1].get("clientId") is not None else call[0][2]


class TestClientIdBaseZones:
    @pytest.mark.parametrize("raw", ["50", "900", "-100", "abc", "1e2", "150"])
    def test_invalid_base_is_refused_before_connect(self, monkeypatch, raw):
        from xenon.clients.ib_client import ClientIdNotAllowed

        monkeypatch.setenv("XENON_IB_CLIENT_ID_BASE", raw)
        client = _mock_client()
        with pytest.raises(ClientIdNotAllowed):
            client.connect(client_id="auto", timeout=1)
        client._ib.connect.assert_not_called()

    @pytest.mark.parametrize(("raw", "base"), [(None, 0), ("", 0), ("0", 0), ("100", 100), (" 800 ", 800)])
    def test_base_values(self, monkeypatch, raw, base):
        from xenon.clients.ib_client import client_id_base

        if raw is None:
            monkeypatch.delenv("XENON_IB_CLIENT_ID_BASE", raising=False)
        else:
            monkeypatch.setenv("XENON_IB_CLIENT_ID_BASE", raw)
        assert client_id_base() == base

    @pytest.mark.parametrize(("base", "role", "expected"), [("0", "orders", 4), ("100", "orders", 104), ("100", "sync", 103), ("800", "data", 805)])
    def test_pool_role_connect_uses_base_offset(self, monkeypatch, base, role, expected):
        monkeypatch.setenv("XENON_IB_CLIENT_ID_BASE", base)
        client = _mock_client()
        client.connect(pool_role=role, timeout=1)
        assert _called_id(client) == expected

    def test_pool_role_with_wrong_id_is_refused(self, monkeypatch):
        from xenon.clients.ib_client import ClientIdNotAllowed

        monkeypatch.setenv("XENON_IB_CLIENT_ID_BASE", "100")
        client = _mock_client()
        with pytest.raises(ClientIdNotAllowed, match="must use clientId 104"):
            client.connect(client_id=4, pool_role="orders", timeout=1)
        with pytest.raises(ClientIdNotAllowed, match="never 'auto'"):
            client.connect(client_id="auto", pool_role="orders", timeout=1)
        client._ib.connect.assert_not_called()

    @pytest.mark.parametrize("cid", [0, 3, 4, 5, 9])
    def test_pool_zone_ids_refused_without_pool_role(self, monkeypatch, cid):
        """Only the pool may open pool ids — incl. master clientId 0 at base 0."""
        from xenon.clients.ib_client import ClientIdNotAllowed

        monkeypatch.delenv("XENON_IB_CLIENT_ID_BASE", raising=False)
        client = _mock_client()
        with pytest.raises(ClientIdNotAllowed, match="pool zone"):
            client.connect(client_id=cid, timeout=1)
        client._ib.connect.assert_not_called()

    @pytest.mark.parametrize(("base", "cid"), [("0", 10), ("0", 12), ("0", 19), ("100", 110), ("100", 119)])
    def test_explicit_relay_id_from_python_is_refused(self, monkeypatch, base, cid):
        from xenon.clients.ib_client import ClientIdNotAllowed

        monkeypatch.setenv("XENON_IB_CLIENT_ID_BASE", base)
        client = _mock_client()
        with pytest.raises(ClientIdNotAllowed, match="relay zone"):
            client.connect(client_id=cid, timeout=1)
        client._ib.connect.assert_not_called()

    @pytest.mark.parametrize("cid", [4, 26, 99, 204, 990, 999])
    def test_other_environment_ids_refused(self, monkeypatch, cid):
        """At base 100 the prod block (0-99), other blocks and 900+ are off limits."""
        from xenon.clients.ib_client import ClientIdNotAllowed

        monkeypatch.setenv("XENON_IB_CLIENT_ID_BASE", "100")
        client = _mock_client()
        with pytest.raises(ClientIdNotAllowed, match="outside this environment"):
            client.connect(client_id=cid, timeout=1)
        client._ib.connect.assert_not_called()

    @pytest.mark.parametrize("cid", [120, 126, 149, 150, 199])
    def test_own_block_non_pool_ids_allowed(self, monkeypatch, cid):
        monkeypatch.setenv("XENON_IB_CLIENT_ID_BASE", "100")
        client = _mock_client()
        client.connect(client_id=cid, timeout=1)
        assert _called_id(client) == cid

    def test_auto_allocates_inside_the_base_block(self, monkeypatch):
        monkeypatch.setenv("XENON_IB_CLIENT_ID_BASE", "100")
        seen = set()
        for _ in range(30):
            client = _mock_client()
            client.connect(client_id="auto", timeout=1)
            seen.add(_called_id(client))
        assert seen and all(120 <= cid <= 149 for cid in seen)

    def test_client_name_resolves_relative_to_base(self, monkeypatch):
        monkeypatch.setenv("XENON_IB_CLIENT_ID_BASE", "100")
        client = _mock_client()
        client.connect(client_name="ib_option_chain", timeout=1)
        assert _called_id(client) == 127

    @pytest.mark.parametrize("name", ["ib_sync", "ib_orders", "cri_scanner", "ib_realtime_server"])
    def test_registry_names_in_pool_or_relay_zone_are_refused(self, monkeypatch, name):
        from xenon.clients.ib_client import ClientIdNotAllowed

        monkeypatch.delenv("XENON_IB_CLIENT_ID_BASE", raising=False)
        client = _mock_client()
        with pytest.raises(ClientIdNotAllowed):
            client.connect(client_name=name, timeout=1)
        client._ib.connect.assert_not_called()

    @patch("xenon.clients.ib_client.time.sleep")
    def test_reconnect_rechecks_allocation(self, _sleep, monkeypatch):
        """_on_disconnect goes through the same check (A5: reconnects covered)."""
        from xenon.clients.ib_client import ClientIdNotAllowed

        monkeypatch.delenv("XENON_IB_CLIENT_ID_BASE", raising=False)
        client = _mock_client()
        client.connect(client_id=91, timeout=1)
        client._ib.connect.reset_mock()
        monkeypatch.setenv("XENON_IB_CLIENT_ID_BASE", "100")
        with pytest.raises(ClientIdNotAllowed):
            client._on_disconnect()
        client._ib.connect.assert_not_called()

    @patch("xenon.clients.ib_client.time.sleep")
    def test_pool_reconnect_keeps_its_pool_role(self, _sleep, monkeypatch):
        monkeypatch.setenv("XENON_IB_CLIENT_ID_BASE", "100")
        client = _mock_client()
        client.connect(pool_role="orders", timeout=1)
        client._ib.connect.reset_mock()
        client._on_disconnect()
        assert _called_id(client) == 104

    def test_order_manage_classifies_refused_owner_id_as_ownership(self):
        """A subprocess asked to reconnect as a pool/relay/other-base owner id
        is refused locally and maps to 409 ownership, never a connect."""
        from xenon.clients.ib_client import ClientIdNotAllowed
        from xenon.execution.ib_order_manage import classify_failure

        assert classify_failure(None, ClientIdNotAllowed("clientId 4 is in the pool zone")) == "ownership"

