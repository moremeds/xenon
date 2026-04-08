"""Shared pytest configuration and fixtures for scripts tests."""
import sys
from pathlib import Path

# Add both the repo root and scripts/ so tests can import via either
# `scripts.*` package paths or the legacy bare module paths.
PROJECT_ROOT = Path(__file__).parent.parent.parent
SCRIPTS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "trade_blotter"))
