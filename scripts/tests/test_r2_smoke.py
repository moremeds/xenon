"""End-to-end R2 reachability test. Skipped by default (requires live creds)."""

from __future__ import annotations

import os

import pytest

_REQUIRED_ENV = ("R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")


@pytest.mark.e2e
def test_r2_apex_data_bucket_reachable() -> None:
    """PUT + GET a tiny test object under test/ prefix. Requires live R2 creds."""
    missing = [k for k in _REQUIRED_ENV if not os.getenv(k)]
    if missing:
        pytest.skip(f"R2 env vars not set: {', '.join(missing)}")

    import boto3

    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    bucket = os.environ["R2_BUCKET"]
    key = "test/smoke.txt"
    payload = b"xenon-apex-smoke"

    s3.put_object(Bucket=bucket, Key=key, Body=payload)
    try:
        got = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        assert got == payload
    finally:
        s3.delete_object(Bucket=bucket, Key=key)
