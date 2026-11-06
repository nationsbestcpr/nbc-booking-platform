"""Integrations page for admins only.

Revision ID: 0006
Revises: 0005
Created: 2026-11-06

The SQL is migrations/sql/0006_integrations_admin_only.sql (frozen). Takes
system.health_view and system.issue_resolve away from customer service,
manager, operations and viewer; admins keep them (all permissions).
"""

from migrations.sqlfile import run_sql, run_sql_file

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0006_integrations_admin_only.sql")


def downgrade() -> None:
    run_sql("""
        INSERT INTO role_permissions (role_code, permission_code) VALUES
          ('customer_service', 'system.health_view'),
          ('manager', 'system.health_view'), ('manager', 'system.issue_resolve'),
          ('operations', 'system.health_view'), ('operations', 'system.issue_resolve'),
          ('viewer', 'system.health_view')
        ON CONFLICT DO NOTHING;
    """)
