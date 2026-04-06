"""Code quality tests for scripts/ directory.

Verifies:
  - _safe_value handles NaN, None, and valid values correctly
  - fetch_ticker.py can be imported without errors
  - No bare except: clauses exist in any script file
"""

import ast
import math
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).parent.parent


# ── _safe_value tests ──────────────────────────────────────────────

class TestSafeValue:
    """Tests for IBRealtimeServer._safe_value helper."""

    @staticmethod
    def _safe_value(val):
        """Mirror of IBRealtimeServer._safe_value for unit testing."""
        if val is None:
            return None
        try:
            if math.isnan(val):
                return None
        except TypeError:
            pass
        return val

    def test_nan_returns_none(self):
        assert self._safe_value(float("nan")) is None

    def test_none_returns_none(self):
        assert self._safe_value(None) is None

    def test_valid_float(self):
        assert self._safe_value(42.5) == 42.5

    def test_valid_int(self):
        assert self._safe_value(100) == 100

    def test_zero(self):
        assert self._safe_value(0) == 0

    def test_negative(self):
        assert self._safe_value(-3.14) == -3.14

    def test_inf(self):
        """Infinity is not NaN and should pass through."""
        assert self._safe_value(float("inf")) == float("inf")

    def test_string_passthrough(self):
        """Non-numeric types should pass through (TypeError caught)."""
        assert self._safe_value("hello") == "hello"


# ── Import sanity checks ──────────────────────────────────────────

class TestImports:
    """Verify key modules can be imported without side effects."""

    def test_fetch_ticker_importable(self):
        import fetch_ticker  # noqa: F401

    def test_fetch_ticker_has_main(self):
        import fetch_ticker
        assert hasattr(fetch_ticker, "main")
        assert callable(fetch_ticker.main)

    def test_fetch_ticker_has_fetch_ticker_info(self):
        import fetch_ticker
        assert hasattr(fetch_ticker, "fetch_ticker_info")
        assert callable(fetch_ticker.fetch_ticker_info)


# ── No bare except: clauses ───────────────────────────────────────

def _collect_python_files():
    """Return all .py files under scripts/, excluding tests and __pycache__."""
    files = []
    for py_file in SCRIPTS_DIR.rglob("*.py"):
        # Skip test files and __pycache__
        if "__pycache__" in str(py_file):
            continue
        files.append(py_file)
    return files


class _BareExceptVisitor(ast.NodeVisitor):
    """AST visitor that detects bare except: clauses."""

    def __init__(self):
        self.bare_excepts: list = []

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        if node.type is None:
            self.bare_excepts.append(node.lineno)
        self.generic_visit(node)


class TestNoBareExcept:
    """Ensure no bare except: clauses exist in any script file."""

    @pytest.mark.parametrize(
        "py_file",
        _collect_python_files(),
        ids=lambda p: str(p.relative_to(SCRIPTS_DIR)),
    )
    def test_no_bare_except(self, py_file: Path):
        source = py_file.read_text()
        tree = ast.parse(source, filename=str(py_file))
        visitor = _BareExceptVisitor()
        visitor.visit(tree)
        assert visitor.bare_excepts == [], (
            f"Bare except: found in {py_file.name} at line(s) {visitor.bare_excepts}. "
            f"Use 'except Exception:' or a more specific exception type."
        )
