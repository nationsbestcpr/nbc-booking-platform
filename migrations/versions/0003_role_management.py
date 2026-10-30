"""Role management: role.manage permission and the role-grant rules.

Revision ID: 0003
Revises: 0002
Created: 2026-10-30

The SQL is migrations/sql/0003_role_management.sql (frozen). Adds the
'role.manage' permission for the admin dashboard's Roles & permissions tab,
and two database rules: role grants and revokes made by a person need
'user.manage' (and an admin for the admin role; 'user' and 'student' are
never granted by hand), and the last active admin can't be removed or
switched off.
"""

from migrations.sqlfile import run_sql, run_sql_file

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0003_role_management.sql")


def downgrade() -> None:
    run_sql("""
        DROP TRIGGER IF EXISTS user_accounts_keep_an_admin ON user_accounts;
        DROP TRIGGER IF EXISTS user_roles_keep_an_admin ON user_roles;
        DROP TRIGGER IF EXISTS user_roles_guard ON user_roles;
        DROP FUNCTION IF EXISTS trg_keep_an_admin();
        DROP FUNCTION IF EXISTS trg_user_roles_guard();
        DELETE FROM role_permissions WHERE permission_code = 'role.manage';
        DELETE FROM permissions WHERE code = 'role.manage';
    """)
