"""All datetimes persisted by new backfill scripts must be tz-aware UTC."""

from __future__ import annotations

from datetime import timezone

from scripts.migrations import _2026_04_26_backfill_vcg_history as bf_vcg


def test_vcg_parser_returns_utc_aware():
    naive = "2026-04-21T14:11:08.383805"
    parsed = bf_vcg._parse_iso_utc(naive)
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_vcg_parser_normalizes_offset_to_utc():
    eastern = "2026-04-21T10:11:08.383805-04:00"
    parsed = bf_vcg._parse_iso_utc(eastern)
    assert parsed is not None
    assert parsed.tzinfo is timezone.utc
    assert parsed.hour == 14  # 10:11 EDT == 14:11 UTC


def test_vcg_parser_handles_z_suffix():
    z_form = "2026-04-21T14:11:08Z"
    parsed = bf_vcg._parse_iso_utc(z_form)
    assert parsed is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_vcg_parser_returns_none_on_garbage():
    assert bf_vcg._parse_iso_utc(None) is None
    assert bf_vcg._parse_iso_utc("") is None
    assert bf_vcg._parse_iso_utc("not a date") is None
