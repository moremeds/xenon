"""Test that scan archival is best-effort — scanner responses succeed even when Postgres fails."""

from __future__ import annotations

from unittest.mock import patch


def test_write_scan_to_postgres_suppresses_errors():
    """Postgres archive failure must not propagate to the caller."""
    from xenon.api.server import _write_scan_to_postgres

    with patch("xenon.api.server.os.environ.get", return_value="postgresql+asyncpg://bad:bad@localhost:9999/nope"):
        # Should not raise — best-effort semantics
        _write_scan_to_postgres("scanner.json", {"tickers": ["AAPL"]})


def test_write_scan_to_postgres_ignores_unknown_filename():
    """Unknown filenames (not in _SCAN_TYPE_MAP) are silently skipped."""
    from xenon.api.server import _write_scan_to_postgres

    _write_scan_to_postgres("unknown.json", {"data": True})
