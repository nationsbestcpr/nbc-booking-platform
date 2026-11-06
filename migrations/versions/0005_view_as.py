"""'View as' (impersonation) permission.

Revision ID: 0005
Revises: 0004
Created: 2026-11-06

The SQL is migrations/sql/0005_view_as.sql (frozen). Adds the
'user.impersonate' permission behind the "View as" button on a user's page.
"""

from migrations.sqlfile import run_sql, run_sql_file

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0005_view_as.sql")


def downgrade() -> None:
    run_sql("""
        DELETE FROM role_permissions WHERE permission_code = 'user.impersonate';
        DELETE FROM permissions WHERE code = 'user.impersonate';
    """)
