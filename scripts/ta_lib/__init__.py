"""Compatibility shim. Real home: src/xenon/ta_lib/.

Removed in a follow-up PR after one soak cycle (nightly R2 refresh + 8:30 AM ET trend scan)."""

import sys
from pathlib import Path

# Self-heal for plain `python3.13 -c "import scripts.ta_lib"` invocations where
# neither the editable install nor pytest's conftest has put src/ on sys.path.
_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from xenon.ta_lib import *  # noqa: E402,F401,F403
from xenon.ta_lib import (  # noqa: E402,F401
    apex_sync,
    bars,
    dry_run_store,
    parquet_store,
    r2_store,
    service,
)
from xenon.ta_lib.service import TAService  # noqa: E402,F401

# indicators has a native talib dep; import it only when available.
try:
    from xenon.ta_lib import indicators  # noqa: E402,F401
except ImportError as _e:
    if "talib" not in str(_e):
        raise

# Register submodules under scripts.ta_lib.* so that
# `from scripts.ta_lib.r2_store import X` style imports work.
_submodules = [
    "apex_sync",
    "bars",
    "dry_run_store",
    "parquet_store",
    "r2_store",
    "service",
]
for _name in _submodules:
    sys.modules[f"scripts.ta_lib.{_name}"] = sys.modules[f"xenon.ta_lib.{_name}"]

if "xenon.ta_lib.indicators" in sys.modules:
    sys.modules["scripts.ta_lib.indicators"] = sys.modules["xenon.ta_lib.indicators"]

__all__ = ["TAService"]
