"""Tests for xenon.ta_lib.dry_run_store."""

from __future__ import annotations


def test_list_objects_emits_posix_paths(tmp_path):
    """C9: keys returned from list_objects must use forward slashes regardless of OS."""
    from xenon.ta_lib.dry_run_store import DryRunStore

    r2 = DryRunStore(tmp_path / "preview")
    r2.put_object("parquet/historical/1d/AAPL.parquet", b"x")

    keys = [k for k, _size, _mtime in r2.list_objects("parquet/")]
    assert "parquet/historical/1d/AAPL.parquet" in keys
    for k in keys:
        assert "\\" not in k, f"key contains OS-native separator: {k!r}"


def test_put_then_get_roundtrip(tmp_path):
    from xenon.ta_lib.dry_run_store import DryRunStore

    r2 = DryRunStore(tmp_path / "preview")
    r2.put_object("meta/test.bin", b"payload")
    assert r2.get_object("meta/test.bin") == b"payload"


def test_delete_object_roundtrip(tmp_path):
    from xenon.ta_lib.dry_run_store import DryRunStore
    from xenon.ta_lib.r2_store import R2NotFoundError

    r2 = DryRunStore(tmp_path / "preview")
    r2.put_object("meta/test.bin", b"payload")
    r2.delete_object("meta/test.bin")
    import pytest

    with pytest.raises(R2NotFoundError):
        r2.get_object("meta/test.bin")


def test_delete_missing_key_is_noop(tmp_path):
    from xenon.ta_lib.dry_run_store import DryRunStore

    r2 = DryRunStore(tmp_path / "preview")
    r2.delete_object("does/not/exist")  # must not raise
