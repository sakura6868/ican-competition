"""add observable check results

Revision ID: b7e1c8a92d4f
Revises: f014cb9142f8
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e1c8a92d4f"
down_revision: str | Sequence[str] | None = "f014cb9142f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    table_name = "step_assessments"
    column_name = "observable_check_results"
    inspector = sa.inspect(op.get_bind())
    existing = {
        column["name"]: column for column in inspector.get_columns(table_name)
    }.get(column_name)
    if existing is None:
        op.add_column(
            table_name,
            sa.Column(
                column_name,
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
        )
        return

    # A full metadata.create_all() database may already contain the latest
    # column while its recorded Alembic revision still trails the code.
    if not isinstance(existing["type"], sa.JSON) or bool(existing["nullable"]):
        raise RuntimeError(
            f"existing {table_name}.{column_name} has incompatible structure"
        )


def downgrade() -> None:
    op.drop_column("step_assessments", "observable_check_results")
