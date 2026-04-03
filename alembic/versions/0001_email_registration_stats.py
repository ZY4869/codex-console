"""create email registration stats table

Revision ID: 0001_email_registration_stats
Revises:
Create Date: 2026-04-03 00:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0001_email_registration_stats"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_registration_stats",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("email_address", sa.String(length=255), nullable=False),
        sa.Column("email_domain", sa.String(length=255), nullable=True),
        sa.Column("email_service", sa.String(length=50), nullable=True),
        sa.Column("total_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("add_phone_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_status", sa.String(length=40), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("last_add_phone_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email_address"),
    )
    op.create_index("ix_email_registration_stats_email_address", "email_registration_stats", ["email_address"], unique=False)
    op.create_index("ix_email_registration_stats_email_domain", "email_registration_stats", ["email_domain"], unique=False)
    op.create_index("ix_email_registration_stats_email_service", "email_registration_stats", ["email_service"], unique=False)
    op.create_index("ix_email_registration_stats_last_status", "email_registration_stats", ["last_status"], unique=False)
    op.create_index("ix_email_registration_stats_last_used_at", "email_registration_stats", ["last_used_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_email_registration_stats_last_used_at", table_name="email_registration_stats")
    op.drop_index("ix_email_registration_stats_last_status", table_name="email_registration_stats")
    op.drop_index("ix_email_registration_stats_email_service", table_name="email_registration_stats")
    op.drop_index("ix_email_registration_stats_email_domain", table_name="email_registration_stats")
    op.drop_index("ix_email_registration_stats_email_address", table_name="email_registration_stats")
    op.drop_table("email_registration_stats")
