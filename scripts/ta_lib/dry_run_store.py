"""Local-filesystem stand-in for R2Store for --dry-run mode.

Writes to `data/apex_mirror_preview/` instead of Cloudflare R2. Exposes the
same public surface (get_object, put_object, head, list_objects, get_json,
put_json) so callers can use it interchangeably.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterator


class DryRunStore:
    """R2Store-compatible stub that writes to a local directory."""

    def __init__(self, root: Path):
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def bucket(self) -> str:
        return f"dry-run:{self._root}"

    def _path(self, key: str) -> Path:
        return self._root / key

    def get_object(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            from scripts.ta_lib.r2_store import R2NotFoundError

            raise R2NotFoundError(key)
        return path.read_bytes()

    def put_object(self, key: str, body: bytes, if_match: str | None = None) -> str:
        # if_match is ignored in dry-run (no concurrent writers)
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return '"dryrun"'

    def head(self, key: str) -> dict | None:
        path = self._path(key)
        if not path.exists():
            return None
        return {"ETag": '"dryrun"', "ContentLength": path.stat().st_size}

    def delete_object(self, key: str) -> None:
        """Idempotent delete for dry-run / tests."""
        path = self._path(key)
        if path.exists():
            path.unlink()

    def list_objects(self, prefix: str) -> Iterator[tuple[str, int, datetime]]:
        root = self._path(prefix)
        if not root.exists():
            return
        base = self._root
        for p in root.rglob("*"):
            if p.is_file():
                yield (
                    str(p.relative_to(base)),
                    p.stat().st_size,
                    datetime.fromtimestamp(p.stat().st_mtime),
                )

    def get_json(self, key: str) -> dict:
        return json.loads(self.get_object(key).decode("utf-8"))

    def put_json(self, key: str, data: dict, if_match: str | None = None) -> str:
        body = json.dumps(data, default=str).encode("utf-8")
        return self.put_object(key, body, if_match=if_match)
