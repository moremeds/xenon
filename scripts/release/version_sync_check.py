"""Fail if VERSION and package.json.version disagree."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", type=Path)
    args = ap.parse_args()

    version = (args.root / "VERSION").read_text().strip()
    pkg = json.loads((args.root / "package.json").read_text())
    pkg_version = pkg.get("version", "")

    if version != pkg_version:
        print(
            f"version mismatch: VERSION={version!r} package.json={pkg_version!r}",
            file=sys.stderr,
        )
        return 1
    print(f"OK: {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
