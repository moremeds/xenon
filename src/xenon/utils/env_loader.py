"""Small .env loader that does not depend on python-dotenv."""

from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: str | Path, *, override: bool = False) -> bool:
    """Load simple KEY=VALUE pairs from *path* into ``os.environ``.

    Supports optional ``export`` prefixes and strips matching single or double
    quotes around values. Lines without ``=`` and comments are ignored.
    Returns ``True`` when the file exists, otherwise ``False``.
    """

    env_path = Path(path)
    if not env_path.exists():
        return False

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value
    return True
