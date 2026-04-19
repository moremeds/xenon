"""JSON cache read/write for scanner output files."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def write_json_cache(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically via temp file + os.replace()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".cache_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_json_cache(path: Path, *, max_age_secs: float = 0) -> Optional[dict[str, Any]]:
    """Read cached JSON. Returns None if file missing. Ignores staleness if max_age_secs=0."""
    if not path.exists():
        return None
    if max_age_secs > 0 and is_stale(path, max_age_secs=max_age_secs):
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read cache %s: %s", path, e)
        return None


def is_stale(path: Path, *, max_age_secs: float) -> bool:
    """Check if a cache file is older than max_age_secs."""
    try:
        mtime = path.stat().st_mtime
        return (time.time() - mtime) > max_age_secs
    except OSError:
        return True
