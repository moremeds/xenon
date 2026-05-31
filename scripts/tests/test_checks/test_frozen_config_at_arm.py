"""CI guard self-test. Spec §13.8."""
from __future__ import annotations

import textwrap
from pathlib import Path

from scripts.checks.frozen_config_at_arm import check_module


def test_clean_module_passes(tmp_path: Path):
    src = tmp_path / "stop_loss.py"
    src.write_text(
        textwrap.dedent(
            """
            from xenon.execution.brackets.rules.base import register
            from xenon.execution.brackets.triggers import threshold_crossed_below

            class StopLossRule:
                rule_kind = "stop_loss"
                def arm(self, *, scope, position, config, state_data, executor=None):
                    return None
            """
        )
    )
    violations = check_module(src)
    assert violations == []


def test_module_importing_bracket_policies_fails(tmp_path: Path):
    src = tmp_path / "bad_rule.py"
    src.write_text(
        textwrap.dedent(
            """
            from xenon.db.queries.bracket_policies import resolve_for_scope

            class Bad:
                rule_kind = "bad"
            """
        )
    )
    violations = check_module(src)
    assert violations
    assert any("bracket_policies" in violation for violation in violations)


def test_module_string_referencing_bracket_policies_fails(tmp_path: Path):
    src = tmp_path / "sneaky.py"
    src.write_text(
        textwrap.dedent(
            """
            SQL = "SELECT * FROM xenon.bracket_policies"
            """
        )
    )
    violations = check_module(src)
    assert any("bracket_policies" in violation for violation in violations)
