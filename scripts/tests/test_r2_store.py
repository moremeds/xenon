"""Unit tests for scripts.ta_lib.r2_store. All S3 calls mocked via moto."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from scripts.ta_lib.r2_store import R2NotFoundError, R2PreconditionError, R2Store, _Config


@pytest.fixture
def mock_r2():
    """Set up moto mock and provide a _Config pointing to the mock."""
    with mock_aws():
        # Create bucket in the moto backend
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="apex-data")

        # Create a _Config that uses the real endpoint (moto intercepts all boto3 calls)
        config = _Config(
            endpoint="https://s3.amazonaws.com",
            bucket="apex-data",
            access_key="fake-key",
            secret_key="fake-secret",
        )

        yield config


def test_put_then_get_json_roundtrip(mock_r2):
    r2 = R2Store(config=mock_r2)
    r2.put_json("meta/last_updated.json", {"historical": "2026-04-16T00:00:00Z"})
    got = r2.get_json("meta/last_updated.json")
    assert got == {"historical": "2026-04-16T00:00:00Z"}


def test_head_returns_none_on_404(mock_r2):
    r2 = R2Store(config=mock_r2)
    assert r2.head("missing/key.txt") is None


def test_list_objects_pagination(mock_r2):
    r2 = R2Store(config=mock_r2)
    # Pre-populate some objects
    for i in range(5):
        r2.put_object(f"parquet/historical/1d/T{i}.parquet", f"data{i}".encode())

    keys = [k for k, _, _ in r2.list_objects("parquet/historical/1d/")]
    assert sorted(keys) == [f"parquet/historical/1d/T{i}.parquet" for i in range(5)]


def test_conditional_put_if_match_success(mock_r2):
    r2 = R2Store(config=mock_r2)
    etag1 = r2.put_json("meta/last_updated.json", {"v": 1})
    etag2 = r2.put_json("meta/last_updated.json", {"v": 2}, if_match=etag1)
    assert etag2 != etag1
    assert r2.get_json("meta/last_updated.json") == {"v": 2}


def test_conditional_put_if_match_mismatch_raises(mock_r2):
    r2 = R2Store(config=mock_r2)
    r2.put_json("meta/last_updated.json", {"v": 1})
    with pytest.raises(R2PreconditionError):
        r2.put_json("meta/last_updated.json", {"v": 2}, if_match='"wrong-etag"')


def test_get_object_raises_on_404(mock_r2):
    r2 = R2Store(config=mock_r2)
    with pytest.raises(R2NotFoundError):
        r2.get_object("nope")
