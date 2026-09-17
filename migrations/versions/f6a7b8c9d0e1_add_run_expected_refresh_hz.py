"""add run expected_refresh_hz column

Adds ``runs.expected_refresh_hz`` (nullable): the refresh rate the operator declared as expected at
launch, against which the engine cross-checks the measured rate (a wrong OS display mode -- 60 Hz
when 120 was intended, a mirrored display halving the rate -- is otherwise invisible until the EEG
is analysed). Additive/nullable so existing Runs stay valid (NULL == no expected rate recorded).

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-09-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(sa.Column("expected_refresh_hz", sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("expected_refresh_hz")
