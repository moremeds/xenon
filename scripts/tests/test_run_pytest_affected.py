from pathlib import Path

from scripts.run_pytest_affected import resolve_pytest_targets


def test_resolve_pytest_targets_prefers_direct_matching_test_file():
    targets = resolve_pytest_targets(["scripts/run_pytest_affected.py"])
    assert "scripts/tests/test_run_pytest_affected.py" in targets


def test_resolve_pytest_targets_keeps_changed_test_files():
    targets = resolve_pytest_targets(["scripts/tests/test_combo_entry_date.py"])
    assert targets == ["scripts/tests/test_combo_entry_date.py"]


def test_resolve_pytest_targets_expands_conftest_to_sibling_tree():
    targets = resolve_pytest_targets(["scripts/tests/conftest.py"])
    assert "scripts/tests/test_run_pytest_affected.py" in targets
    assert all(Path(target).parts[:2] == ("scripts", "tests") for target in targets)
