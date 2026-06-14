"""Fail if VERSION and the tracked package.json versions disagree.

VERSION is the source of truth. Both the root package.json and web/package.json
must match it: backend and frontend ship from a single release procedure today,
so they version in lockstep. If/when the frontend gets its own release pipeline,
drop web/package.json from TRACKED_PACKAGES and let it version independently.

site/package.json is intentionally NOT tracked — the marketing site (xenon.run)
is a separate Vercel deployment with its own lifecycle and versions on its own.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Relative to --root. Each must carry the same version as VERSION.
TRACKED_PACKAGES = ("package.json", "web/package.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", type=Path)
    args = ap.parse_args()

    version = (args.root / "VERSION").read_text().strip()

    mismatches = []
    for rel in TRACKED_PACKAGES:
        path = args.root / rel
        if not path.exists():
            continue
        pkg_version = json.loads(path.read_text()).get("version", "")
        if pkg_version != version:
            mismatches.append((rel, pkg_version))

    if mismatches:
        for rel, pkg_version in mismatches:
            print(
                f"version mismatch: VERSION={version!r} {rel}={pkg_version!r}",
                file=sys.stderr,
            )
        return 1
    print(f"OK: {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
