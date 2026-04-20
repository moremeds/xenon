"""Unit tests for xenon.ta_lib.apex_sync."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def test_sync_skips_when_local_fresh(tmp_path: Path):
    from xenon.ta_lib.apex_sync import sync_if_stale

    remote_ts = "2026-04-16T00:00:00+00:00"
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / ".last_sync.json").write_text(
        json.dumps({"historical": remote_ts, "indicators": remote_ts, "schema_version": 1})
    )
    r2 = MagicMock()
    r2.get_json.return_value = {"historical": remote_ts, "indicators": remote_ts, "schema_version": 1}

    result = sync_if_stale(mirror_dir=tmp_path, r2=r2)
    assert result.synced is False
    r2.list_objects.assert_not_called()


def test_sync_downloads_when_remote_newer(tmp_path: Path):
    from xenon.ta_lib.apex_sync import sync_if_stale

    (tmp_path / ".last_sync.json").write_text(
        json.dumps(
            {"historical": "2026-04-15T00:00:00+00:00", "indicators": "2026-04-15T00:00:00+00:00", "schema_version": 1}
        )
    )
    r2 = MagicMock()
    r2.get_json.return_value = {
        "historical": "2026-04-16T00:00:00+00:00",
        "indicators": "2026-04-16T00:00:00+00:00",
        "schema_version": 1,
    }
    r2.list_objects.side_effect = lambda prefix: iter(
        [
            (f"{prefix}AAPL.parquet", 100, None),
        ]
    )
    r2.get_object.return_value = b"fake-parquet-bytes"

    result = sync_if_stale(mirror_dir=tmp_path, r2=r2, timeframes=("1d",))
    assert result.synced is True
    assert result.files_downloaded >= 2  # hist + ind for AAPL (+ universe.json)
    assert (tmp_path / "parquet" / "historical" / "1d" / "AAPL.parquet").exists()
    assert (tmp_path / "parquet" / "indicators" / "1d" / "AAPL.parquet").exists()
    assert (tmp_path / "meta" / "universe.json").exists()


def test_sync_rejects_unknown_schema_version(tmp_path: Path):
    from xenon.ta_lib.apex_sync import SchemaVersionError, sync_if_stale

    r2 = MagicMock()
    r2.get_json.return_value = {
        "historical": "2026-04-16T00:00:00+00:00",
        "indicators": "2026-04-16T00:00:00+00:00",
        "schema_version": 99,
    }
    with pytest.raises(SchemaVersionError):
        sync_if_stale(mirror_dir=tmp_path, r2=r2)


def test_sync_a15_r2_outage_falls_back_to_local_mirror(tmp_path: Path):
    """A15: R2Error on manifest GET + existing local mirror → synced=False with warning."""
    from xenon.ta_lib.apex_sync import sync_if_stale
    from xenon.ta_lib.r2_store import R2Error

    # Set up a local mirror with at least meta/universe.json
    (tmp_path / "meta").mkdir(parents=True)
    (tmp_path / "meta" / "universe.json").write_text('{"tickers": []}')

    r2 = MagicMock()
    r2.get_json.side_effect = R2Error("network down")

    result = sync_if_stale(mirror_dir=tmp_path, r2=r2)
    assert result.synced is False
    assert any("R2 unreachable" in e for e in result.errors)
    # Mirror intact
    assert (tmp_path / "meta" / "universe.json").exists()


def test_sync_a15_r2_outage_hard_fails_without_local_mirror(tmp_path: Path):
    """A15: R2Error + no local mirror = raise."""
    from xenon.ta_lib.apex_sync import sync_if_stale
    from xenon.ta_lib.r2_store import R2Error

    r2 = MagicMock()
    r2.get_json.side_effect = R2Error("network down")

    # tmp_path has no meta/universe.json
    with pytest.raises(R2Error):
        sync_if_stale(mirror_dir=tmp_path, r2=r2)


def test_sync_a13_atomic_swap_on_success(tmp_path: Path):
    """A13: verify that a mid-sync observer would never see partial state.

    We can't directly observe mid-sync atomicity in a unit test without threads,
    so we verify the tmp directory does NOT exist after a successful sync, and
    that existing mirror files survive.
    """
    from xenon.ta_lib.apex_sync import sync_if_stale

    # Pre-populate a file that should survive the sync (copied into .tmp then
    # swapped back). This exercises the shutil.copytree(mirror, tmp) path.
    (tmp_path / "parquet" / "historical" / "1d").mkdir(parents=True)
    (tmp_path / "parquet" / "historical" / "1d" / "EXISTING.parquet").write_bytes(b"existing")

    r2 = MagicMock()
    r2.get_json.return_value = {
        "historical": "2026-04-16T00:00:00+00:00",
        "indicators": "2026-04-16T00:00:00+00:00",
        "schema_version": 1,
    }
    r2.list_objects.side_effect = lambda prefix: iter(
        [
            (f"{prefix}NEW.parquet", 100, None),
        ]
    )
    r2.get_object.return_value = b"fake-bytes"

    result = sync_if_stale(mirror_dir=tmp_path, r2=r2, timeframes=("1d",))
    assert result.synced is True
    # .tmp directory must not exist after swap
    assert not tmp_path.with_name(tmp_path.name + ".tmp").exists()
    # Existing file survived
    assert (tmp_path / "parquet" / "historical" / "1d" / "EXISTING.parquet").exists()
    # New file is present
    assert (tmp_path / "parquet" / "historical" / "1d" / "NEW.parquet").exists()


def test_sync_a14_partial_failure_leaves_mirror_untouched(tmp_path: Path):
    """A14: if any download errors, do NOT swap — existing mirror stays as-is,
    .last_sync.json is NOT updated, tmp is cleaned up, synced=False returned."""
    from xenon.ta_lib.apex_sync import sync_if_stale

    # Pre-populate existing mirror + .last_sync with yesterday's ts
    (tmp_path / "parquet" / "historical" / "1d").mkdir(parents=True)
    (tmp_path / "parquet" / "historical" / "1d" / "EXISTING.parquet").write_bytes(b"existing")
    (tmp_path / ".last_sync.json").write_text(
        json.dumps(
            {
                "historical": "2026-04-15T00:00:00+00:00",
                "indicators": "2026-04-15T00:00:00+00:00",
                "schema_version": 1,
            }
        )
    )

    r2 = MagicMock()
    r2.get_json.return_value = {
        "historical": "2026-04-16T00:00:00+00:00",
        "indicators": "2026-04-16T00:00:00+00:00",
        "schema_version": 1,
    }
    r2.list_objects.side_effect = lambda prefix: iter(
        [
            (f"{prefix}FAIL.parquet", 100, None),
        ]
    )
    r2.get_object.side_effect = RuntimeError("simulated network failure")

    result = sync_if_stale(mirror_dir=tmp_path, r2=r2, timeframes=("1d",))
    assert result.synced is False
    assert any("FAIL.parquet" in e for e in result.errors)
    # tmp cleaned up
    assert not tmp_path.with_name(tmp_path.name + ".tmp").exists()
    # Existing mirror + old sync sentinel intact
    assert (tmp_path / "parquet" / "historical" / "1d" / "EXISTING.parquet").exists()
    sync = json.loads((tmp_path / ".last_sync.json").read_text())
    assert sync["historical"] == "2026-04-15T00:00:00+00:00"  # NOT updated


def test_sync_force_overrides_freshness(tmp_path: Path):
    from xenon.ta_lib.apex_sync import sync_if_stale

    remote_ts = "2026-04-16T00:00:00+00:00"
    (tmp_path / ".last_sync.json").write_text(
        json.dumps({"historical": remote_ts, "indicators": remote_ts, "schema_version": 1})
    )
    r2 = MagicMock()
    r2.get_json.return_value = {"historical": remote_ts, "indicators": remote_ts, "schema_version": 1}
    r2.list_objects.side_effect = lambda prefix: iter([])
    r2.get_object.return_value = b"{}"

    result = sync_if_stale(mirror_dir=tmp_path, r2=r2, timeframes=("1d",), force=True)
    assert result.synced is True
    assert result.stale_reason == "force=True"


def test_sync_downloads_universe_metadata(tmp_path: Path):
    """meta/universe.json must be downloaded so Stage A can join against it."""
    from xenon.ta_lib.apex_sync import sync_if_stale

    r2 = MagicMock()
    r2.get_json.return_value = {
        "historical": "2026-04-16T00:00:00+00:00",
        "indicators": "2026-04-16T00:00:00+00:00",
        "schema_version": 1,
    }
    r2.list_objects.return_value = iter([])
    r2.get_object.return_value = b'{"tickers": []}'

    sync_if_stale(mirror_dir=tmp_path, r2=r2, timeframes=("1d",))
    assert (tmp_path / "meta" / "universe.json").exists()


def test_download_prefix_counter_deterministic_under_concurrency(tmp_path):
    """T6: with many keys and 10 workers, the returned `downloaded` count must
    equal the number of successful downloads, never miscount via the nonlocal += race."""
    import shutil
    from unittest.mock import MagicMock

    from xenon.ta_lib.apex_sync import _download_prefix

    n_keys = 200
    keys = [(f"parquet/historical/1d/T{i:03d}.parquet", 100, None) for i in range(n_keys)]

    r2 = MagicMock()
    r2.list_objects.return_value = iter(keys)
    r2.get_object.return_value = b"x" * 100

    # Run multiple iterations to surface any timing race
    for _ in range(5):
        target = tmp_path / "sync"
        if target.exists():
            shutil.rmtree(target)
        target.mkdir()
        # Re-set return_value because iter() is consumed
        r2.list_objects.return_value = iter(keys)
        downloaded, errors = _download_prefix(r2, "parquet/historical/1d/", target, max_workers=10)
        assert errors == []
        assert downloaded == n_keys, f"expected {n_keys}, got {downloaded}"


def test_download_prefix_counts_successes_ignores_errors(tmp_path):
    """T6: when some gets raise, downloaded must equal the success count only."""
    from unittest.mock import MagicMock

    from xenon.ta_lib.apex_sync import _download_prefix

    r2 = MagicMock()
    keys = [(f"parquet/historical/1d/T{i}.parquet", 100, None) for i in range(10)]
    r2.list_objects.return_value = iter(keys)

    # Half the gets raise
    call_count = [0]

    def fake_get(key):
        call_count[0] += 1
        if call_count[0] % 2 == 0:
            raise RuntimeError("simulated")
        return b"payload"

    r2.get_object.side_effect = fake_get

    downloaded, errors = _download_prefix(r2, "parquet/historical/1d/", tmp_path, max_workers=5)
    assert downloaded == 5
    assert len(errors) == 5


def test_sync_recovers_from_interrupted_swap_where_only_old_exists(tmp_path):
    """T3: mirror_dir missing + <mirror_dir>.old present → rename .old back BEFORE R2 call."""
    import json
    import shutil
    from unittest.mock import MagicMock

    from xenon.ta_lib.apex_sync import sync_if_stale

    # Simulate interrupted swap: only .old exists, live mirror gone
    old_dir = tmp_path.with_name(tmp_path.name + ".old")
    (old_dir / "meta").mkdir(parents=True, exist_ok=True)
    (old_dir / "meta" / "universe.json").write_text('{"tickers": []}')
    (old_dir / ".last_sync.json").write_text(
        json.dumps(
            {
                "historical": "2026-04-15T00:00:00+00:00",
                "indicators": "2026-04-15T00:00:00+00:00",
                "schema_version": 1,
            }
        )
    )
    if tmp_path.exists():
        shutil.rmtree(tmp_path)

    # R2 reports same ts as recovered mirror → not stale → no download
    r2 = MagicMock()
    r2.get_json.return_value = {
        "historical": "2026-04-15T00:00:00+00:00",
        "indicators": "2026-04-15T00:00:00+00:00",
        "schema_version": 1,
    }

    result = sync_if_stale(mirror_dir=tmp_path, r2=r2)
    assert result.synced is False
    # Live mirror is back
    assert (tmp_path / "meta" / "universe.json").exists()
    # .old is gone
    assert not old_dir.exists()


def test_sync_recovers_from_interrupted_swap_where_only_tmp_exists(tmp_path):
    """T3: mirror_dir missing + <mirror_dir>.tmp present (with .last_sync.json) → promote .tmp."""
    import json
    import shutil
    from unittest.mock import MagicMock

    from xenon.ta_lib.apex_sync import sync_if_stale

    tmp_dir = tmp_path.with_name(tmp_path.name + ".tmp")
    (tmp_dir / "meta").mkdir(parents=True, exist_ok=True)
    (tmp_dir / "meta" / "universe.json").write_text('{"tickers": []}')
    (tmp_dir / ".last_sync.json").write_text(
        json.dumps(
            {
                "historical": "2026-04-16T00:00:00+00:00",
                "indicators": "2026-04-16T00:00:00+00:00",
                "schema_version": 1,
            }
        )
    )
    if tmp_path.exists():
        shutil.rmtree(tmp_path)

    r2 = MagicMock()
    r2.get_json.return_value = {
        "historical": "2026-04-16T00:00:00+00:00",
        "indicators": "2026-04-16T00:00:00+00:00",
        "schema_version": 1,
    }

    result = sync_if_stale(mirror_dir=tmp_path, r2=r2)
    assert result.synced is False  # matches remote manifest now, no new sync
    assert (tmp_path / "meta" / "universe.json").exists()
    assert not tmp_dir.exists()


def test_sync_leaves_mirror_alone_when_already_present(tmp_path):
    """T3: no self-heal when mirror_dir is already present (normal case)."""
    import json
    from unittest.mock import MagicMock

    from xenon.ta_lib.apex_sync import sync_if_stale

    (tmp_path / ".last_sync.json").write_text(
        json.dumps(
            {
                "historical": "2026-04-16T00:00:00+00:00",
                "indicators": "2026-04-16T00:00:00+00:00",
                "schema_version": 1,
            }
        )
    )

    # Put stale sibling dirs around — they should NOT be touched while mirror_dir exists
    old_dir = tmp_path.with_name(tmp_path.name + ".old")
    old_dir.mkdir()
    (old_dir / "stale-marker").write_text("left over")

    r2 = MagicMock()
    r2.get_json.return_value = {
        "historical": "2026-04-16T00:00:00+00:00",
        "indicators": "2026-04-16T00:00:00+00:00",
        "schema_version": 1,
    }

    result = sync_if_stale(mirror_dir=tmp_path, r2=r2)
    assert result.synced is False
    # .old was not promoted (mirror_dir was present, so self-heal skipped)
    assert (old_dir / "stale-marker").exists()
