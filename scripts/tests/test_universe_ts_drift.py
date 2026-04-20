"""Regression: web/lib/universe.ts must match what generate_universe_ts.py
would produce right now. Guards against silent drift between the Python
registry and the checked-in TS mirror.
"""

from pathlib import Path

from scripts.infra.dev.generate_universe_ts import render


def test_checked_in_universe_ts_matches_codegen():
    repo_root = Path(__file__).resolve().parents[2]
    checked_in = (repo_root / "web" / "lib" / "universe.ts").read_text()
    expected = render()
    assert checked_in == expected, (
        "web/lib/universe.ts is stale. Regenerate with: python3.13 scripts/infra/dev/generate_universe_ts.py"
    )
