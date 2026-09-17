"""add structured subject demographic columns

Adds optional, structured demographic fields to ``subjects`` -- ``sex``, ``handedness``,
``birth_date``, ``subject_code`` -- recovering the typed/filterable participant metadata the legacy
app captured (which xpman previously only kept as free text inside ``info_json``). All nullable and
additive: existing rows stay valid (NULL == "unspecified"), and the free-text ``info_json`` notes
field is unchanged. ``sex``/``handedness`` are stored as plain VARCHAR (the ORM uses a
``native_enum=False`` enum, so there is no DB-level CHECK constraint to add -- a plain ADD COLUMN
works on SQLite).

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("subjects") as batch_op:
        batch_op.add_column(sa.Column("sex", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("handedness", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("birth_date", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("subject_code", sa.String(length=100), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("subjects") as batch_op:
        batch_op.drop_column("subject_code")
        batch_op.drop_column("birth_date")
        batch_op.drop_column("handedness")
        batch_op.drop_column("sex")
