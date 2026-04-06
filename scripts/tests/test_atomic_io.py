"""Tests for atomic_io module — atomic JSON writes with SHA-256 checksum verification."""

import hashlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure scripts/ is on sys.path so we can import utils.atomic_io
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.atomic_io import atomic_save, verified_load


class TestAtomicSave:
    """Tests for atomic_save()."""

    def test_writes_valid_json_with_checksum(self, tmp_path):
        """atomic_save writes a JSON file containing a _checksum field."""
        filepath = tmp_path / "test.json"
        data = {"bankroll": 100000, "positions": []}
        atomic_save(str(filepath), data)

        assert filepath.exists()
        written = json.loads(filepath.read_text())
        assert "_checksum" in written
        assert isinstance(written["_checksum"], str)
        assert len(written["_checksum"]) == 64  # SHA-256 hex digest

    def test_returns_checksum_string(self, tmp_path):
        """atomic_save returns the SHA-256 hex digest."""
        filepath = tmp_path / "test.json"
        data = {"key": "value"}
        checksum = atomic_save(str(filepath), data)

        assert isinstance(checksum, str)
        assert len(checksum) == 64

    def test_checksum_is_deterministic(self, tmp_path):
        """Same data produces the same checksum."""
        f1 = tmp_path / "a.json"
        f2 = tmp_path / "b.json"
        data = {"x": 1, "y": [2, 3], "z": {"nested": True}}
        c1 = atomic_save(str(f1), data)
        c2 = atomic_save(str(f2), data)
        assert c1 == c2

    def test_checksum_computed_without_checksum_field(self, tmp_path):
        """The checksum is computed on the payload WITHOUT _checksum."""
        filepath = tmp_path / "test.json"
        data = {"a": 1, "b": 2}
        atomic_save(str(filepath), data)

        written = json.loads(filepath.read_text())
        stored_checksum = written.pop("_checksum")

        # Recompute on the data without _checksum
        canonical = json.dumps(written, sort_keys=True, separators=(",", ":"))
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        assert stored_checksum == expected

    def test_overwrites_existing_file(self, tmp_path):
        """atomic_save overwrites a pre-existing file."""
        filepath = tmp_path / "test.json"
        filepath.write_text('{"old": true}')
        atomic_save(str(filepath), {"new": True})

        written = json.loads(filepath.read_text())
        assert "new" in written
        assert "old" not in written

    def test_nested_dicts_lists_floats(self, tmp_path):
        """Handles nested structures, lists, and floats correctly."""
        filepath = tmp_path / "test.json"
        data = {
            "positions": [
                {"ticker": "AAPL", "legs": [{"strike": 150.5, "delta": 0.35}]},
                {"ticker": "GOOG", "legs": []},
            ],
            "bankroll": 99999.99,
            "metadata": {"nested": {"deep": [1, 2.5, 3]}},
        }
        atomic_save(str(filepath), data)
        result = verified_load(str(filepath))
        assert result["positions"][0]["legs"][0]["strike"] == 150.5
        assert result["bankroll"] == 99999.99

    def test_atomic_no_partial_write_on_error(self, tmp_path):
        """If serialization fails, the original file is not corrupted."""
        filepath = tmp_path / "test.json"
        original = {"original": True}
        atomic_save(str(filepath), original)

        # Try to save non-serializable data — should raise
        class NotSerializable:
            pass

        with pytest.raises((TypeError, ValueError)):
            atomic_save(str(filepath), {"bad": NotSerializable()})

        # Original file should be intact
        loaded = json.loads(filepath.read_text())
        assert loaded.get("original") is True

    def test_temp_file_cleaned_up_on_success(self, tmp_path):
        """No leftover temp files after a successful write."""
        filepath = tmp_path / "test.json"
        atomic_save(str(filepath), {"clean": True})

        # Only the target file should exist (no .tmp files)
        files = list(tmp_path.iterdir())
        assert len(files) == 1
        assert files[0].name == "test.json"


class TestVerifiedLoad:
    """Tests for verified_load()."""

    def test_loads_and_verifies_checksum(self, tmp_path):
        """verified_load returns data when checksum is valid."""
        filepath = tmp_path / "test.json"
        data = {"bankroll": 50000, "positions": ["a", "b"]}
        atomic_save(str(filepath), data)

        result = verified_load(str(filepath))
        assert result == data

    def test_strips_checksum_from_returned_data(self, tmp_path):
        """verified_load does NOT include _checksum in returned dict."""
        filepath = tmp_path / "test.json"
        atomic_save(str(filepath), {"x": 1})

        result = verified_load(str(filepath))
        assert "_checksum" not in result

    def test_raises_on_corrupted_checksum(self, tmp_path):
        """verified_load raises ValueError when checksum doesn't match."""
        filepath = tmp_path / "test.json"
        atomic_save(str(filepath), {"legit": True})

        # Corrupt the data but keep the old checksum
        raw = json.loads(filepath.read_text())
        raw["legit"] = False  # tamper
        filepath.write_text(json.dumps(raw, indent=2))

        with pytest.raises(ValueError, match="(?i)checksum"):
            verified_load(str(filepath))

    def test_handles_legacy_files_without_checksum(self, tmp_path):
        """verified_load returns data as-is for files without _checksum."""
        filepath = tmp_path / "legacy.json"
        legacy_data = {"bankroll": 75000, "positions": []}
        filepath.write_text(json.dumps(legacy_data, indent=2))

        result = verified_load(str(filepath))
        assert result == legacy_data

    def test_legacy_file_no_checksum_in_result(self, tmp_path):
        """Legacy files also don't have _checksum in returned data."""
        filepath = tmp_path / "legacy.json"
        filepath.write_text('{"a": 1}')

        result = verified_load(str(filepath))
        assert "_checksum" not in result

    def test_file_not_found_raises(self, tmp_path):
        """verified_load raises FileNotFoundError for missing files."""
        with pytest.raises(FileNotFoundError):
            verified_load(str(tmp_path / "nonexistent.json"))

    def test_roundtrip_preserves_data(self, tmp_path):
        """Data survives a save → load roundtrip exactly."""
        filepath = tmp_path / "rt.json"
        data = {
            "bankroll": 123456.78,
            "positions": [
                {"ticker": "AMD", "contracts": 5, "entry_price": 3.45},
            ],
            "metadata": {"synced_at": "2026-03-11T10:00:00Z"},
        }
        atomic_save(str(filepath), data)
        result = verified_load(str(filepath))
        assert result == data
