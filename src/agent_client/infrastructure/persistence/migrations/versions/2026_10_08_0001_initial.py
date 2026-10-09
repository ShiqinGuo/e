import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("workspace", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("record_hash", sa.String(), nullable=False),
        sa.Column("byte_offset", sa.Integer(), nullable=False),
        sa.Column("context_epoch", sa.Integer(), nullable=False),
    )
    op.create_table(
        "event_index",
        sa.Column("event_id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), sa.ForeignKey("sessions.id"), primary_key=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("byte_offset", sa.Integer(), nullable=False),
        sa.Column("byte_length", sa.Integer(), nullable=False),
        sa.Column("record_hash", sa.String(), nullable=False),
        sa.UniqueConstraint("session_id", "seq"),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), sa.ForeignKey("sessions.id"), primary_key=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("stop_reason", sa.String(), nullable=True),
    )
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), sa.ForeignKey("sessions.id"), primary_key=True),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
    )
    op.create_table(
        "inputs",
        sa.Column("command_id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), sa.ForeignKey("sessions.id"), primary_key=True),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
    )
    op.create_table(
        "context_checkpoints",
        sa.Column("event_id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), sa.ForeignKey("sessions.id"), primary_key=True),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("source_seq", sa.Integer(), nullable=False),
        sa.Column("reference", sa.JSON(), nullable=False),
        sa.UniqueConstraint("session_id", "epoch"),
    )


def downgrade():
    for table in ("context_checkpoints", "inputs", "tool_calls", "runs", "event_index", "sessions"):
        op.drop_table(table)
