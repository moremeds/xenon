"""scripts/checks/order_path_caller_allowlist.py must see multiline imports.

The guard used to match imports line by line, so
``from xenon.execution import (\\n    ib_place_order,\\n)`` slipped through.
These tests run the guard CLI against a throwaway repo root.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[1] / "checks" / "order_path_caller_allowlist.py"

MULTILINE_PACKAGE_IMPORT = "from xenon.execution import (\n    ib_place_order,\n)\n"
MULTILINE_MODULE_IMPORT = (
    "from xenon.execution.ib_order_manage import (\n    cancel_order_on_client,\n    modify_order_on_client,\n)\n"
)


def _run(root: Path, files: dict[str, str]) -> subprocess.CompletedProcess[str]:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(GUARD), "--repo-root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "source",
    [
        MULTILINE_PACKAGE_IMPORT,
        MULTILINE_MODULE_IMPORT,
        "import xenon.execution.ib_place_order as placer\n",
        "from xenon.execution.ib_place_order import place_order_on_client\n",
        "from xenon.execution import orders_store, ib_order_manage\n",
        "def late():\n    from xenon.execution import (\n        ib_place_order,\n    )\n",
    ],
)
def test_unauthorized_import_fails(tmp_path: Path, source: str) -> None:
    result = _run(tmp_path, {"src/xenon/api/sneaky.py": source})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "src/xenon/api/sneaky.py" in result.stderr


def test_relative_multiline_import_fails(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        {"src/xenon/execution/other.py": "from . import (\n    ib_place_order,\n)\n"},
    )
    assert result.returncode == 1, result.stdout + result.stderr


def test_allowlisted_caller_may_import_multiline(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        {
            "src/xenon/api/services/order_entry.py": MULTILINE_PACKAGE_IMPORT + MULTILINE_MODULE_IMPORT,
            "src/xenon/execution/ib_place_order.py": "",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_test_files_are_exempt(tmp_path: Path) -> None:
    result = _run(tmp_path, {"src/xenon/api/tests/test_x.py": MULTILINE_PACKAGE_IMPORT})
    assert result.returncode == 0, result.stdout + result.stderr


def test_unrelated_multiline_import_passes(tmp_path: Path) -> None:
    source = "from xenon.execution import (\n    orders_store,\n    preflight,\n)\n"
    result = _run(tmp_path, {"src/xenon/api/fine.py": source})
    assert result.returncode == 0, result.stdout + result.stderr


def test_import_text_in_docstring_and_comment_passes(tmp_path: Path) -> None:
    source = (
        '"""Only the order owner may do:\n\n'
        "    from xenon.execution import (\n        ib_place_order,\n    )\n"
        '"""\n'
        "# from xenon.execution.ib_place_order import place_order_on_client\n"
    )
    result = _run(tmp_path, {"src/xenon/api/docs_only.py": source})
    assert result.returncode == 0, result.stdout + result.stderr


def test_removed_cli_name_still_fails_everywhere(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        {"pyproject.toml": '[project.scripts]\nxenon-ib-place-order = "x:main"\n'},
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "pyproject.toml" in result.stderr


def test_removed_cli_name_in_python_comment_is_ignored(tmp_path: Path) -> None:
    result = _run(tmp_path, {"src/xenon/api/note.py": "# xenon-ib-place-order was removed\n"})
    assert result.returncode == 0, result.stdout + result.stderr


def test_non_python_file_keeps_text_match(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        {"scripts/run.sh": "python -c 'x'\nfrom xenon.execution.ib_place_order import place_order_on_client\n"},
    )
    assert result.returncode == 1, result.stdout + result.stderr


def test_unparseable_python_falls_back_to_line_match(tmp_path: Path) -> None:
    source = "from xenon.execution.ib_place_order import place_order_on_client\ndef broken(:\n"
    result = _run(tmp_path, {"src/xenon/api/broken.py": source})
    assert result.returncode == 1, result.stdout + result.stderr
