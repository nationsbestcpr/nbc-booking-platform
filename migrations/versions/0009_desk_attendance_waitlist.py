"""The booking desk's actions, attendance and the waiting list.

Revision ID: 0009
Revises: 0008
Created: 2026-11-16

The SQL is migrations/sql/0009_desk_attendance_waitlist.sql (frozen). Adds:
  * settings: phone_link_hold_minutes, invoice_due_days, attendance_instructor_days;
  * GHL tags for a class move, a payment link and the waiting list (kept if already renamed);
  * checkout_sessions.seat_hold_id may be empty (a transfer's price difference holds no seat);
  * sessions.location_type and waitlist_limit worked out from the room on every change;
  * an index on waitlist_offers (waitlist_entry_id).
"""

from migrations.sqlfile import run_sql, run_sql_file

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0009_desk_attendance_waitlist.sql")


def downgrade() -> None:
    run_sql("""
        DROP INDEX waitlist_offers_entry_idx;
        DROP TRIGGER sessions_waitlist_rules ON sessions;
        DROP FUNCTION trg_session_waitlist_rules();
        DELETE FROM checkout_sessions WHERE seat_hold_id IS NULL;
        ALTER TABLE checkout_sessions ALTER COLUMN seat_hold_id SET NOT NULL;
        UPDATE app_settings SET value = value - 'payment_link' - 'class_moved' - 'waitlist' - 'waitlist_offer'
                                        - 'offer_expired' - 'waitlist_no_seat'
         WHERE key = 'ghl_tags' AND jsonb_typeof(value) = 'object';
        DELETE FROM app_settings WHERE key IN ('phone_link_hold_minutes', 'invoice_due_days',
                                               'attendance_instructor_days');
    """)
