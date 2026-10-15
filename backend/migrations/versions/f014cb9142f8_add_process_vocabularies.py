"""add process vocabularies

Revision ID: f014cb9142f8
Revises: 26e2aa3871e6
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f014cb9142f8"
down_revision: str | Sequence[str] | None = "26e2aa3871e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    table_name = "process_vocabularies"
    index_name = op.f("ix_process_vocabularies_approved")
    inspector = sa.inspect(op.get_bind())
    table_exists = table_name in inspector.get_table_names()

    if not table_exists:
        op.create_table(
            table_name,
            sa.Column("process_key", sa.String(length=128), nullable=False),
            sa.Column("tools", sa.JSON(), nullable=False),
            sa.Column("objects", sa.JSON(), nullable=False),
            sa.Column("actions", sa.JSON(), nullable=False),
            sa.Column("approved", sa.Boolean(), nullable=False),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("created_by", sa.String(length=32), nullable=True),
            sa.Column("updated_by", sa.String(length=32), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("process_key"),
        )
        op.create_index(index_name, table_name, ["approved"], unique=False)
        return

    # Earlier application startup used metadata.create_all(), which could create
    # this new table before Alembic recorded the revision. Accept only the exact
    # compatible table shape; an incomplete table must stop instead of being stamped.
    expected_columns = {
        "process_key": (sa.String, False),
        "tools": (sa.JSON, False),
        "objects": (sa.JSON, False),
        "actions": (sa.JSON, False),
        "approved": (sa.Boolean, False),
        "source": (sa.String, False),
        "created_by": (sa.String, True),
        "updated_by": (sa.String, True),
        "created_at": (sa.DateTime, False),
        "updated_at": (sa.DateTime, False),
    }
    present_columns = {
        column["name"]: column for column in inspector.get_columns(table_name)
    }
    if set(present_columns) != set(expected_columns):
        raise RuntimeError(f"existing {table_name} table has incompatible columns")
    for name, (expected_type, expected_nullable) in expected_columns.items():
        column = present_columns[name]
        if not isinstance(column["type"], expected_type):
            raise RuntimeError(f"existing {table_name}.{name} has incompatible type")
        if bool(column["nullable"]) is not expected_nullable:
            raise RuntimeError(f"existing {table_name}.{name} has incompatible nullability")

    primary_key = inspector.get_pk_constraint(table_name).get("constrained_columns")
    if primary_key != ["process_key"]:
        raise RuntimeError(f"existing {table_name} table has incompatible primary key")

    matching_index = next(
        (
            index
            for index in inspector.get_indexes(table_name)
            if index["name"] == str(index_name)
        ),
        None,
    )
    if matching_index is None:
        op.create_index(index_name, table_name, ["approved"], unique=False)
    elif matching_index.get("column_names") != ["approved"] or bool(
        matching_index.get("unique")
    ):
        raise RuntimeError(f"existing {index_name} index has incompatible definition")


def downgrade() -> None:
    op.drop_index(op.f("ix_process_vocabularies_approved"), table_name="process_vocabularies")
    op.drop_table("process_vocabularies")
