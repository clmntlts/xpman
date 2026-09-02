"""drop unused events table

The ``events`` table (``core.models.Event``, a documented "lightweight DB mirror of a
high-value event") was never written to anywhere in the codebase -- the real event stream has
always lived entirely in each Run's Parquet/CSV file (``runtime/logging_sink.py``), not in SQL.
Left in place it's a landmine: a reader sees the table in the schema and reasonably assumes
events are queryable via the database, which was never true. Dropped rather than wired up (issue
#32) -- no data is lost since no row was ever inserted.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-09-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_table("events")


def downgrade() -> None:
    """Downgrade schema."""
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("result_id", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["result_id"], ["results.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
