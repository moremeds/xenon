"""Tests for the position-rules acceptance snapshot helper."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from scripts.checks.position_rules_acceptance_snapshot import run_snapshot


def test_run_snapshot_writes_runbook_artifacts(tmp_path: Path):
    calls: list[list[str]] = []
    responses = iter(
        [
            '{"violations": [], "count": 0}\n',
            "[]\n",
            '{"daemon_alive": true, "outbox_dlq_count": 0}\n',
        ]
    )

    def fake_runner(cmd: list[str]) -> str:
        calls.append(cmd)
        return next(responses)

    result = run_snapshot(
        day="2026-05-05",
        logs_dir=tmp_path,
        duplicate_close_since="1d",
        events_since="24h",
        runner=fake_runner,
    )

    assert result["day"] == "2026-05-05"
    assert (
        Path(result["files"]["duplicate_close"]).read_text()
        == '{"violations": [], "count": 0}\n'
    )
    assert Path(result["files"]["transitions"]).read_text() == "[]\n"
    assert (
        Path(result["files"]["health"]).read_text()
        == '{"daemon_alive": true, "outbox_dlq_count": 0}\n'
    )

    assert calls == [
        [
            sys.executable,
            "scripts/checks/no_duplicate_close_audit.py",
            "--since",
            "1d",
        ],
        ["xenon-position-rules", "events", "--since", "24h"],
        ["xenon-position-rules", "health", "--json"],
    ]


def test_run_snapshot_printable_result_is_json_serializable(tmp_path: Path):
    def fake_runner(_cmd: list[str]) -> str:
        return "{}\n"

    result = run_snapshot(day="2026-05-05", logs_dir=tmp_path, runner=fake_runner)

    assert json.loads(json.dumps(result))["files"]["health"].endswith(
        "health-2026-05-05.json"
    )
