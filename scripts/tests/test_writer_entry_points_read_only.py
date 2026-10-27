"""XENON_READ_ONLY=1 must stop writer entry points before they touch the DB or a broker."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from xenon.cli import futu_statement_sync as futu_stmt_cli
from xenon.jobs import flex_divergence_check as divergence


def test_flex_divergence_apply_refused_under_read_only(monkeypatch, capsys):
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    with (
        patch.object(divergence, "resolve_from_env") as resolve,
        patch.object(divergence, "record_run") as record,
    ):
        rc = divergence.main(["--apply"])
    assert rc == 3
    assert "READ_ONLY" in capsys.readouterr().err
    resolve.assert_not_called()
    record.assert_not_called()


def test_futu_statement_sync_refused_under_read_only(monkeypatch, capsys):
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    args = futu_stmt_cli._parse_args([])
    with patch.object(futu_stmt_cli, "sync_statements") as sync:
        rc = asyncio.run(futu_stmt_cli._run(args))
    assert rc == 3
    assert "READ_ONLY" in capsys.readouterr().err
    sync.assert_not_called()
