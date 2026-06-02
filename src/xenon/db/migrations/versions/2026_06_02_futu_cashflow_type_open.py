"""futu_cash_flow.cashflow_type — drop type CHECK constraint

Live OpenD probe surfaced that Futu's get_acc_cash_flow returns
descriptive English strings (Cash Dividend / Fund Subscription /
IPO Subscription / Others / etc), not the small enum I assumed
(DEPOSIT/WITHDRAW/TRANSFER_IN/TRANSFER_OUT). v1 persists every USD
row verbatim and lets M5 walk decide which types move NAV externally.

Revision ID: 2026_06_02_cf_open
Revises: 2026_06_02_futu
Create Date: 2026-06-02

"""

from typing import Sequence, Union

from alembic import op

revision: str = "2026_06_02_cf_open"
down_revision: Union[str, Sequence[str], None] = "2026_06_02_futu"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_futu_cash_flow_type",
        "futu_cash_flow",
        schema="xenon",
    )


def downgrade() -> None:
    op.create_check_constraint(
        "ck_futu_cash_flow_type",
        "futu_cash_flow",
        "cashflow_type IN ('DEPOSIT', 'WITHDRAW', 'TRANSFER_IN', 'TRANSFER_OUT')",
        schema="xenon",
    )
