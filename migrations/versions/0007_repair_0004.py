"""Repair: put back what 0004 added where it has gone missing.

Revision ID: 0007
Revises: 0006
Created: 2026-11-06

The SQL is migrations/sql/0007_repair_0004.sql (frozen). Idempotent: on a
database that already has 0004's column, index, sign-in check and trigger
function, it changes nothing. Written after Replit's publish-time "database
changes" prompt removed orders.buyer_user_id from the test site's database.
"""

from migrations.sqlfile import run_sql_file

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0007_repair_0004.sql")


def downgrade() -> None:
    # Nothing to undo: 0007 only restores what 0004 created (0004's own downgrade removes it).
    pass
