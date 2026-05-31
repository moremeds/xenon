"""Pure close-claim logic tests. Spec §5.6, §10.2."""
from __future__ import annotations

import pytest

from xenon.execution.brackets.close_claim import (
    derive_order_ref,
    parse_order_ref_claim_id,
    should_skip_resubmit,
)


def test_derive_order_ref():
    assert derive_order_ref(claim_id=42) == "xenon-pr-42"
    assert derive_order_ref(claim_id=1_000_000) == "xenon-pr-1000000"


def test_parse_order_ref_claim_id():
    assert parse_order_ref_claim_id("xenon-pr-42") == 42
    assert parse_order_ref_claim_id("xenon-pr-1000000") == 1_000_000


def test_parse_order_ref_rejects_other_prefixes():
    with pytest.raises(ValueError):
        parse_order_ref_claim_id("xenon-cancel-42")
    with pytest.raises(ValueError):
        parse_order_ref_claim_id("xenon-pr-abc")


def test_should_skip_resubmit_when_open_order_with_orderref():
    """N-C3 retry idempotency."""
    open_orders = [{"orderRef": "xenon-pr-42", "permId": 12345}]
    skip, perm_id = should_skip_resubmit(
        order_ref="xenon-pr-42",
        open_orders=open_orders,
        executions=[],
    )
    assert skip is True
    assert perm_id == 12345


def test_should_skip_resubmit_when_execution_with_orderref():
    """Order already filled at broker; retry must not double-submit."""
    executions = [{"orderRef": "xenon-pr-42", "permId": 99999}]
    skip, perm_id = should_skip_resubmit(
        order_ref="xenon-pr-42",
        open_orders=[],
        executions=executions,
    )
    assert skip is True
    assert perm_id == 99999


def test_should_resubmit_when_no_match():
    skip, perm_id = should_skip_resubmit(order_ref="xenon-pr-42", open_orders=[], executions=[])
    assert skip is False
    assert perm_id is None
