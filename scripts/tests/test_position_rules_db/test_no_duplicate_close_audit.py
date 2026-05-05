"""Spec §13.8 T4 audit script."""
from __future__ import annotations

import json
import os
import subprocess

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine


@pytest.fixture
def engine():
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_close_claims WHERE position_key LIKE 'TEST_AUDIT::%'"))
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key LIKE 'TEST_AUDIT::%'"))
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_close_claims WHERE position_key LIKE 'TEST_AUDIT::%'"))
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key LIKE 'TEST_AUDIT::%'"))


def _run_audit(scope_account: str = "DU1234567") -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "DATABASE_URL": os.environ.get(
            "DATABASE_URL_TEST",
            "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
        ),
    }
    return subprocess.run(
        [
            "uv",
            "run",
            "python",
            "scripts/checks/no_duplicate_close_audit.py",
            "--since",
            "1d",
            "--scope-account",
            scope_account,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_audit_runs_clean_with_empty_data():
    result = _run_audit(scope_account="DU0000000")
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout or "{}")
    assert body.get("violations") == []


def test_audit_ignores_abandoned_claims_for_duplicate_close_count(engine):
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO xenon.position_close_claims
                    (broker, account_env, broker_account, position_key,
                     claimed_by_protection_id, claim_kind, status, order_ref,
                     submitted_at, terminal_at)
                VALUES
                    ('IB', 'paper', 'DU1234567', 'TEST_AUDIT::ABANDONED_OK',
                     1001, 'synthetic_close', 'FILLED', 'xenon-pr-test-filled',
                     NOW(), NOW()),
                    ('IB', 'paper', 'DU1234567', 'TEST_AUDIT::ABANDONED_OK',
                     1002, 'synthetic_close', 'ABANDONED', 'xenon-pr-test-abandoned',
                     NOW(), NOW())
                """
            )
        )

    result = _run_audit()

    assert result.returncode == 0, result.stdout + result.stderr
    body = json.loads(result.stdout or "{}")
    assert body["violations"] == []


def test_audit_flags_duplicate_submitted_close_orders(engine):
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO xenon.position_close_claims
                    (broker, account_env, broker_account, position_key,
                     claimed_by_protection_id, claim_kind, status, order_ref,
                     submitted_at)
                VALUES
                    ('IB', 'paper', 'DU1234567', 'TEST_AUDIT::DUPLICATE_SUBMITTED',
                     2001, 'synthetic_close', 'SUBMITTED', 'xenon-pr-test-submitted-a',
                     NOW()),
                    ('IB', 'paper', 'DU1234567', 'TEST_AUDIT::DUPLICATE_SUBMITTED',
                     2002, 'native_reconcile_close', 'FILLED', 'xenon-pr-test-submitted-b',
                     NOW())
                """
            )
        )

    result = _run_audit()

    assert result.returncode == 1
    body = json.loads(result.stdout or "{}")
    assert body["violations"] == [
        {
            "rule": "at_most_one_close_per_position_per_day",
            "broker_account": "DU1234567",
            "position_key": "TEST_AUDIT::DUPLICATE_SUBMITTED",
            "day": body["violations"][0]["day"],
            "count": 2,
        }
    ]
