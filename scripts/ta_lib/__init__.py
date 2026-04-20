"""Compatibility shim. Real home: src/xenon/ta_lib/.

Removed in a follow-up PR after one soak cycle (nightly R2 refresh + 8:30 AM ET trend scan)."""

import sys

from xenon.ta_lib import *  # noqa: F401,F403
from xenon.ta_lib import (  # noqa: F401
    apex_sync,
    bars,
    dry_run_store,
    parquet_store,
    r2_store,
    service,
)
from xenon.ta_lib.service import TAService  # noqa: F401

# indicators has a native talib dep; import it only when available.
try:
    from xenon.ta_lib import indicators  # noqa: F401
except ImportError:
    pass

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

# Register indicators only if it was successfully imported.
if "xenon.ta_lib.indicators" in sys.modules:
    sys.modules["scripts.ta_lib.indicators"] = sys.modules["xenon.ta_lib.indicators"]
