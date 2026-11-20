"""Running without Enrollware.

Revision ID: 0012
Revises: 0011
Created: 2026-11-20

The SQL is migrations/sql/0012_running_without_enrollware.sql (frozen). Adds:
  * settings enrollware_retired and confirm_days_before; permissions system.cutover and instructor.requests;
  * assignments.confirmed_at / confirmed_by_user_id (instructors confirm their classes);
  * roster_signatures (students sign the roster on class day);
  * instructor_requests and instructor_availability_conflicts() (availability changes and "can't teach"
    notices; a change can't quietly clash with an assigned class);
  * enrollware_imports and enrollware_import_rows (Enrollware's registrations imported at cutover).
"""

from migrations.sqlfile import run_sql, run_sql_file

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0012_running_without_enrollware.sql")


def downgrade() -> None:
    run_sql("""
        DROP TABLE enrollware_import_rows;
        DROP TABLE enrollware_imports;
        DROP TABLE instructor_requests;
        DROP FUNCTION trg_instructor_request_rules();
        DROP FUNCTION instructor_availability_conflicts(bigint, text[], text[], date, date);
        DROP TABLE roster_signatures;
        DROP FUNCTION trg_signature_matches_seat();
        ALTER TABLE assignments DROP COLUMN confirmed_by_user_id, DROP COLUMN confirmed_at;
        DELETE FROM role_permissions WHERE permission_code IN ('system.cutover', 'instructor.requests');
        DELETE FROM permissions WHERE code IN ('system.cutover', 'instructor.requests');
        DELETE FROM app_settings WHERE key IN ('enrollware_retired', 'confirm_days_before');
    """)
