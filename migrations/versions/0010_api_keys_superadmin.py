"""The booking engine's API for the NBC Ops Hub, and the Superadmin role name.

Revision ID: 0010
Revises: 0009
Created: 2026-11-20

The SQL is migrations/sql/0010_api_keys_superadmin.sql (frozen). Adds:
  * permission api_key.manage (the Superadmin holds it through grants_all);
  * the 'admin' role is shown as Superadmin;
  * api_keys and api_key_permissions, with a guard that only api_key.manage may make or revoke a key.
"""

from migrations.sqlfile import run_sql, run_sql_file

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0010_api_keys_superadmin.sql")


def downgrade() -> None:
    run_sql("""
        DROP TABLE api_key_permissions;
        DROP TABLE api_keys;
        DROP FUNCTION trg_api_keys_guard();
        UPDATE roles SET name = 'Admin',
                         description = 'Everything, including people, roles, settings and billing items.'
         WHERE code = 'admin';
        DELETE FROM role_permissions WHERE permission_code = 'api_key.manage';
        DELETE FROM permissions WHERE code = 'api_key.manage';
    """)
