"""add run provenance columns

Adds nullable reproducibility columns to ``runs``: the PsychoPy/NumPy versions the run executed
against, and the achieved monitor refresh rate (plus whether it was really measured). All nullable
and additive, so pre-existing Runs remain valid with these left NULL.

Revision ID: a1b2c3d4e5f6
Revises: 436d4dffd084
Create Date: 2026-07-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "436d4dffd084"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(sa.Column("psychopy_version", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("numpy_version", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("measured_refresh_hz", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("refresh_measured_successfully", sa.Boolean(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("refresh_measured_successfully")
        batch_op.drop_column("measured_refresh_hz")
        batch_op.drop_column("numpy_version")
        batch_op.drop_column("psychopy_version")
