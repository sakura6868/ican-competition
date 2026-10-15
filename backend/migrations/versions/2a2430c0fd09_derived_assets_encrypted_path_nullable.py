"""derived_assets encrypted_path nullable

Revision ID: 2a2430c0fd09
Revises: df4f65bdbb9a
Create Date: 2026-08-16 00:21:17.893451

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2a2430c0fd09'
down_revision: Union[str, Sequence[str], None] = 'df4f65bdbb9a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # SQLite does not support ALTER COLUMN directly; batch mode recreates the
    # table while preserving rows and indexes.
    with op.batch_alter_table('derived_assets', recreate='always') as batch_op:
        batch_op.alter_column(
            'encrypted_path',
            existing_type=sa.VARCHAR(length=512),
            nullable=True,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('derived_assets', recreate='always') as batch_op:
        batch_op.alter_column(
            'encrypted_path',
            existing_type=sa.VARCHAR(length=512),
            nullable=False,
        )
