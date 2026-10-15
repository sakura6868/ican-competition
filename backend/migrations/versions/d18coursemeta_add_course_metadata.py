"""Add teacher-maintained course metadata to SOP versions."""

import sqlalchemy as sa
from alembic import op

revision = "d18coursemeta"
down_revision = "c213practice"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("sop_versions")}
    if "course_meta" not in columns:
        with op.batch_alter_table("sop_versions") as batch:
            batch.add_column(
                sa.Column("course_meta", sa.JSON(), nullable=False, server_default="{}")
            )


def downgrade() -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("sop_versions")}
    if "course_meta" in columns:
        with op.batch_alter_table("sop_versions") as batch:
            batch.drop_column("course_meta")
