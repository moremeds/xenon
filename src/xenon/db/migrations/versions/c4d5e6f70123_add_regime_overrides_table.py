"""regime_overrides: replace single-column FK with composite scope FK (ISSUE-5)

Revision ID: c4d5e6f70123
Revises: a1b2c3d4e5f6
Create Date: 2026-04-30 19:30:00.000000

Phase 0/1/2/4 (rev 48343156f9b7) created regime_overrides with a
single-column FK on submission_id alone. Tribunal review ISSUE-5
flagged that this allows an override row's
(broker, account_env, broker_account) to drift from the parent
submission's scope — a paper-account override row could reference a
live-account submission. This migration replaces the FK with a
composite 4-tuple FK that requires scope match.

Postgres composite FKs need a matching UNIQUE constraint on the
referenced columns. submission_id is already the primary key on
order_submissions so the 4-tuple is logically unique under PK, but
Postgres requires the explicit UNIQUE target — we add it as
uq_order_sub_submission_scope.

The new FK keeps DEFERRABLE INITIALLY DEFERRED so audit insert and
order_submissions reservation can land in the same transaction.

regime_overrides has no rows yet (no production code path inserts
into it before this Phase 3 PR), so the FK swap is safe — there is
nothing to validate against the new constraint.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "c4d5e6f70123"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_order_sub_submission_scope",
        "order_submissions",
        ["submission_id", "broker", "account_env", "broker_account"],
        schema="xenon",
    )

    op.drop_constraint(
        "fk_regime_overrides_submission",
        "regime_overrides",
        schema="xenon",
        type_="foreignkey",
    )

    op.create_foreign_key(
        "fk_regime_overrides_submission_scope",
        source_table="regime_overrides",
        referent_table="order_submissions",
        local_cols=["submission_id", "broker", "account_env", "broker_account"],
        remote_cols=["submission_id", "broker", "account_env", "broker_account"],
        source_schema="xenon",
        referent_schema="xenon",
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_regime_overrides_submission_scope",
        "regime_overrides",
        schema="xenon",
        type_="foreignkey",
    )

    op.create_foreign_key(
        "fk_regime_overrides_submission",
        source_table="regime_overrides",
        referent_table="order_submissions",
        local_cols=["submission_id"],
        remote_cols=["submission_id"],
        source_schema="xenon",
        referent_schema="xenon",
        deferrable=True,
        initially="DEFERRED",
    )

    op.drop_constraint(
        "uq_order_sub_submission_scope",
        "order_submissions",
        schema="xenon",
        type_="unique",
    )
