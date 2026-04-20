"""Cloudflare R2 S3-compatible wrapper.

Sole owner of boto3 S3 calls within the xenon codebase. Everything else
goes through R2Store to keep retry policy and error mapping centralized.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class R2Error(Exception):
    """Base class."""


class R2NotFoundError(R2Error):
    """Object missing (404)."""


class R2PreconditionError(R2Error):
    """If-Match failed (412)."""


class R2ClientError(R2Error):
    """Non-retryable 4xx."""


@dataclass(frozen=True)
class _Config:
    endpoint: str
    bucket: str
    access_key: str
    secret_key: str

    @classmethod
    def from_env(cls) -> "_Config":
        try:
            return cls(
                endpoint=os.environ["R2_ENDPOINT"],
                bucket=os.environ["R2_BUCKET"],
                access_key=os.environ["R2_ACCESS_KEY_ID"],
                secret_key=os.environ["R2_SECRET_ACCESS_KEY"],
            )
        except KeyError as exc:
            raise R2Error(f"Missing R2 env var: {exc.args[0]}") from exc


class R2Store:
    """Thin S3 wrapper with retry, ETag support, and typed errors."""

    def __init__(self, config: _Config | None = None):
        self._cfg = config or _Config.from_env()
        self._client = boto3.client(
            "s3",
            endpoint_url=self._cfg.endpoint,
            aws_access_key_id=self._cfg.access_key,
            aws_secret_access_key=self._cfg.secret_key,
            region_name="auto",
            config=Config(
                retries={"max_attempts": 3, "mode": "adaptive"},
                connect_timeout=10,
                read_timeout=60,
            ),
        )

    @property
    def bucket(self) -> str:
        return self._cfg.bucket

    def get_object(self, key: str) -> bytes:
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=key)
            return resp["Body"].read()
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404"):
                raise R2NotFoundError(key) from exc
            raise R2ClientError(f"get_object({key!r}): {code}") from exc

    def put_object(self, key: str, body: bytes, if_match: str | None = None) -> str:
        kwargs: dict = {"Bucket": self.bucket, "Key": key, "Body": body}
        if if_match is not None:
            kwargs["IfMatch"] = if_match
        try:
            resp = self._client.put_object(**kwargs)
            return resp["ETag"]
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("PreconditionFailed", "412"):
                raise R2PreconditionError(key) from exc
            raise R2ClientError(f"put_object({key!r}): {code}") from exc

    def head(self, key: str) -> dict | None:
        try:
            return self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
                return None
            raise R2ClientError(f"head({key!r})") from exc

    def delete_object(self, key: str) -> None:
        """Idempotent delete — raises R2ClientError only on non-404 failure."""
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404"):
                return
            raise R2ClientError(f"delete_object({key!r}): {code}") from exc

    def list_objects(self, prefix: str) -> Iterator[tuple[str, int, datetime]]:
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"], obj["Size"], obj["LastModified"]

    def get_json(self, key: str) -> dict:
        return json.loads(self.get_object(key).decode("utf-8"))

    def put_json(self, key: str, data: dict, if_match: str | None = None) -> str:
        body = json.dumps(data, default=str).encode("utf-8")
        return self.put_object(key, body, if_match=if_match)
