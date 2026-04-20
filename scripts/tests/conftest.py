"""Shared pytest configuration and fixtures for scripts tests."""

import sys
from pathlib import Path

# Add repo root, scripts/, and src/ so tests can import via:
#   - legacy bare module paths (`from fetchers...`, `from utils...`)
#   - `scripts.*` package paths (historical in a few tests)
#   - new `xenon.*` package paths (Phase 2 reorg destination)
PROJECT_ROOT = Path(__file__).parent.parent.parent
SCRIPTS_DIR = Path(__file__).parent.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SRC_DIR))
