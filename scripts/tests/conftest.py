"""Shared pytest configuration and fixtures for scripts tests."""
import sys
from pathlib import Path

# Add scripts/ and scripts/trade_blotter/ to sys.path so tests can import modules
SCRIPTS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "trade_blotter"))
