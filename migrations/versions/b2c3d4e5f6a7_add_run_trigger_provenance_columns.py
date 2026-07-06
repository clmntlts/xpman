"""add run trigger provenance columns

Adds nullable trigger-backend provenance to ``runs``: which trigger backend drove the run
(``trigger_backend`` -- none/parallel/serial) and, where the backend exposes one, the physical
port/address it used (``trigger_port``), plus the ``pyserial`` version the serial backend could run
against (``pyserial_version``). All nullable and additive -- captured from ``TriggerSender.describe()``
/ ``_resolve_versions()`` at Run creation -- so pre-existing Runs remain valid with these left NULL.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(sa.Column("pyserial_version", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("trigger_backend", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("trigger_port", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("trigger_port")
        batch_op.drop_column("trigger_backend")
        batch_op.drop_column("pyserial_version")
