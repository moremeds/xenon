"""Flex reconcile backfills externally-placed IB fills into order_fills.

The live pool's reqExecutions is own-client (no master client ID on the
Gateway), so manually/mobile-placed fills never reach order_fills and the
snapshot-<permId> row stays WORKING. IB Flex is account-level and sees them
all. reconcile_flex_fills pulls Flex executions, inserts the missing ones
(idempotent by exec_id), and marks covered WORKING snapshot rows FILLED.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import insert, select

from xenon.api.services.flex_fill_reconcile import reconcile_flex_fills
from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_fills, order_submissions
from xenon.execution.account_scope import AccountScope
from xenon.trade_blotter.models import Execution, SecurityType, Side

# Synthetic paper scope + synthetic perm_ids — never the real account/permId, so
# the snapshot-<permId> PK can't collide with rows in core_test (a nightly mirror
# of prod). Each test uses a distinct perm_id/exec_id for isolation.
SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DU-FLEXTEST")
NOW = datetime(2026, 6, 17, 14, 8, tzinfo=timezone.utc)


class _StubFlex:
    def __init__(self, execs):
        self._execs = execs

    def fetch_executions(self, days_back: int = 30):
        return list(self._execs)


def _seed_working(perm_id: str, *, qty: int = 1) -> str:
    sid = f"snapshot-{perm_id}"
    with get_sync_engine().begin() as conn:
        conn.execute(
            insert(order_submissions).values(
                submission_id=sid,
                user_id="snapshot",
                client_attempt_id=f"ca-{perm_id}",
                state="WORKING",
                ticker="SPX",
                security_type="OPT",
                action="BUY",
                quantity=qty,
                strike=Decimal("6855.00"),
                right="P",
                limit_price=Decimal("14.40"),
                tif="DAY",
                perm_id=perm_id,
                ib_order_id="0",
                submitted_at=NOW,
                updated_at=NOW,
                modify_sequence=0,
                broker=SCOPE.broker,
                account_env=SCOPE.account_env,
                broker_account=SCOPE.broker_account,
            )
        )
    return sid


def _seed_fill(perm_id: str, exec_id: str, qty: int = 1) -> None:
    """Insert an existing order_fills row (as the live mirror would) under an
    API-style execId, to test perm_id-level dedup against Flex's tradeID."""
    with get_sync_engine().begin() as conn:
        conn.execute(
            insert(order_fills).values(
                exec_id=exec_id,
                submission_id=f"snapshot-{perm_id}",
                perm_id=perm_id,
                ticker="SPX",
                side="BUY",
                qty=Decimal(qty),
                price=Decimal("14.40"),
                commission=Decimal("1.64"),
                filled_at=NOW,
                metadata={"sec_type": "OPT", "legacy_source": "test"},
                broker=SCOPE.broker,
                account_env=SCOPE.account_env,
                broker_account=SCOPE.broker_account,
            )
        )


def _exec(perm_id: str, exec_id: str, qty: int = 1) -> Execution:
    return Execution(
        exec_id=exec_id,
        time=NOW,
        symbol="SPX",
        sec_type=SecurityType.OPTION,
        side=Side.BUY,
        quantity=Decimal(qty),
        price=Decimal("14.40"),
        commission=Decimal("1.64"),
        strike=Decimal("6855"),
        right="P",
        expiry="20260717",
        perm_id=perm_id,
    )


def _state(sid: str) -> str:
    with get_sync_engine().connect() as conn:
        return conn.execute(
            select(order_submissions.c.state).where(order_submissions.c.submission_id == sid)
        ).scalar_one()


def _fill_exists(exec_id: str) -> bool:
    with get_sync_engine().connect() as conn:
        return conn.execute(select(order_fills.c.exec_id).where(order_fills.c.exec_id == exec_id)).first() is not None


def test_flex_reconcile_inserts_external_fill_and_marks_working_filled():
    sid = _seed_working("FLEXTEST-100")
    stub = _StubFlex([_exec("FLEXTEST-100", "fxr-exec-1")])
    result = reconcile_flex_fills(scope=SCOPE, flex_client=stub)
    assert result["inserted"] == 1
    assert _fill_exists("fxr-exec-1")
    # The WORKING snapshot is reconciled to FILLED because the Flex fill covers it.
    assert _state(sid) == "FILLED"
    assert result["reconciled"] == 1


def test_flex_reconcile_is_idempotent_on_exec_id():
    _seed_working("FLEXTEST-200")
    stub = _StubFlex([_exec("FLEXTEST-200", "fxr-exec-2")])
    reconcile_flex_fills(scope=SCOPE, flex_client=stub)
    result2 = reconcile_flex_fills(scope=SCOPE, flex_client=stub)
    # Same exec_id → record_fill on_conflict_do_nothing → no double-insert.
    assert result2["inserted"] == 0


def test_flex_reconcile_noop_in_read_only(monkeypatch):
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    stub = _StubFlex([_exec("FLEXTEST-300", "fxr-exec-ro")])
    result = reconcile_flex_fills(scope=SCOPE, flex_client=stub)
    assert result.get("skipped") == "read_only"
    assert not _fill_exists("fxr-exec-ro")


def test_flex_reconcile_skips_perm_ids_already_covered_by_live_mirror():
    # The live mirror already recorded this fill under an API-style execId. Flex
    # returns the SAME fill under a different tradeID — it must NOT double-insert,
    # because Flex tradeID != API execId so an exec_id check alone wouldn't catch it.
    perm = "FLEXTEST-400"
    _seed_working(perm)
    _seed_fill(perm, "live-execId-0001.01")
    stub = _StubFlex([_exec(perm, "flex-tradeID-9999")])
    result = reconcile_flex_fills(scope=SCOPE, flex_client=stub)
    assert result["skipped_covered"] == 1
    assert result["inserted"] == 0
    assert not _fill_exists("flex-tradeID-9999")


def test_default_flex_client_imports_and_constructs(monkeypatch):
    # Guards the real FlexQueryFetcher import/construction in _default_flex_client —
    # the production loop's path, which the stub-injected tests above never exercise
    # (an earlier wrong class name `FlexQueryClient` would have ImportError'd in prod).
    from xenon.api.services.flex_fill_reconcile import _default_flex_client

    monkeypatch.delenv("XENON_READ_ONLY", raising=False)
    monkeypatch.setenv("IB_FLEX_TOKEN", "tok")
    monkeypatch.setenv("IB_FLEX_QUERY_ID", "qid")
    client = _default_flex_client()
    assert client is not None
    assert hasattr(client, "fetch_executions")
    monkeypatch.delenv("IB_FLEX_TOKEN")
    assert _default_flex_client() is None  # unconfigured → None, no import error
