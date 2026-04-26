"""add outbox notify trigger

Revision ID: 9b645325b50d
Revises: 0cf835b06d68
Create Date: 2026-04-26 10:19:33.750889

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9b645325b50d"
down_revision: Union[str, Sequence[str], None] = "0cf835b06d68"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION events.notify_outbox()
        RETURNS TRIGGER AS $$
        BEGIN
            PERFORM pg_notify(NEW.channel, NEW.id::text);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER outbox_notify_trigger
        AFTER INSERT ON events.outbox
        FOR EACH ROW EXECUTE FUNCTION events.notify_outbox()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS outbox_notify_trigger ON events.outbox")
    op.execute("DROP FUNCTION IF EXISTS events.notify_outbox()")
