"""add experiment block-order randomization column

Adds ``experiments.randomize_block_order_per_subject`` (issue #31): Block.order_index fixes
one Block sequence at freeze time, identical for every subject who runs a given Instance --
there was no analogous per-subject mechanism for Block order itself, only for Trial order
within a Block (``randomize_per_subject``). This flag lets a researcher opt an Experiment's
Block order into a fresh per-subject shuffle at runtime (``runtime/engine.py``), the same
relationship ``randomize_per_subject`` already has to Trial order. Non-nullable with a
``false`` server default so existing rows (behavior unchanged: fixed frozen Block order) stay
valid.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("experiments") as batch_op:
        batch_op.add_column(
            sa.Column(
                "randomize_block_order_per_subject",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("experiments") as batch_op:
        batch_op.drop_column("randomize_block_order_per_subject")
