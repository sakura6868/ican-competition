"""Add persistent interactive teaching sessions, observations and events."""

import sqlalchemy as sa
from alembic import op

revision = "c213practice"
down_revision = "b7e1c8a92d4f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    expected = {
        "practice_sessions": {
            "id",
            "sop_id",
            "learner_id",
            "status",
            "revision",
            "steps",
            "state",
            "allow_provider_export",
            "created_at",
            "updated_at",
        },
        "practice_observations": {
            "id",
            "session_id",
            "request_key",
            "step_revision",
            "status",
            "frames",
            "encrypted_frames",
            "result",
            "error",
            "receipt_id",
            "created_at",
        },
        "practice_events": {"id", "session_id", "event_type", "payload", "created_at"},
    }
    if existing & expected.keys():
        inspector = sa.inspect(op.get_bind())
        for name, columns in expected.items():
            if name not in existing or {c["name"] for c in inspector.get_columns(name)} != columns:
                raise RuntimeError("Existing practice tables have incompatible structure")
            if inspector.get_pk_constraint(name)["constrained_columns"] != ["id"]:
                raise RuntimeError("Existing practice table primary key is incompatible")
        # Fully metadata-created databases have already passed startup's compare_metadata.
        return
    op.create_table(
        "practice_sessions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("sop_id", sa.String(32), sa.ForeignKey("sop_versions.id"), nullable=False),
        sa.Column("learner_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("allow_provider_export", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_practice_sessions_sop_id", "practice_sessions", ["sop_id"])
    op.create_index("ix_practice_sessions_learner_id", "practice_sessions", ["learner_id"])
    op.create_table(
        "practice_observations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "session_id", sa.String(32), sa.ForeignKey("practice_sessions.id"), nullable=False
        ),
        sa.Column("request_key", sa.String(80), nullable=False),
        sa.Column("step_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("frames", sa.JSON(), nullable=False),
        sa.Column("encrypted_frames", sa.Text(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("receipt_id", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("session_id", "request_key"),
    )
    op.create_index("ix_practice_observations_session_id", "practice_observations", ["session_id"])
    op.create_table(
        "practice_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "session_id", sa.String(32), sa.ForeignKey("practice_sessions.id"), nullable=False
        ),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_practice_events_session_id", "practice_events", ["session_id"])


def downgrade() -> None:
    op.drop_table("practice_events")
    op.drop_table("practice_observations")
    op.drop_table("practice_sessions")
