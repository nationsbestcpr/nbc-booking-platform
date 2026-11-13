"""Who booked whom, attendee invites, the same-course rule, questions, add-ons, bring a friend.

Revision ID: 0008
Revises: 0007
Created: 2026-11-13

The SQL is migrations/sql/0008_booking_round_two.sql (frozen). Adds:
  * orders.parent_order_id (a friend's seat added from the confirmation page);
  * registrations.is_bring_a_friend;
  * invite links (action_tokens purpose 'account_invite');
  * the 18-month same-course rule as a trigger on registrations;
  * booking_questions / booking_question_courses / booking_answers;
  * addons / addon_courses / order_addons;
  * the settings and the 'booking_setup.manage' permission they need.
"""

from migrations.sqlfile import run_sql, run_sql_file

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0008_booking_round_two.sql")


def downgrade() -> None:
    run_sql("""
        DROP TABLE order_addons;
        DROP TABLE addon_courses;
        DROP TABLE addons;
        DROP TABLE booking_answers;
        DROP TABLE booking_question_courses;
        DROP TABLE booking_questions;
        DROP TRIGGER registrations_same_course ON registrations;
        DROP FUNCTION trg_same_course_block();
        DROP INDEX action_tokens_registration_idx;
        DELETE FROM action_tokens WHERE purpose = 'account_invite';
        ALTER TABLE action_tokens DROP CONSTRAINT action_tokens_invite_has_registration;
        ALTER TABLE action_tokens DROP CONSTRAINT action_tokens_purpose_check;
        ALTER TABLE action_tokens ADD CONSTRAINT action_tokens_purpose_check
          CHECK (purpose IN ('sign_in_link', 'waitlist_offer', 'rebook_choice', 'phone_checkout'));
        ALTER TABLE registrations DROP COLUMN is_bring_a_friend;
        ALTER TABLE orders DROP CONSTRAINT orders_parent_not_self;
        DROP INDEX orders_parent_idx;
        ALTER TABLE orders DROP COLUMN parent_order_id;
        DELETE FROM role_permissions WHERE permission_code = 'booking_setup.manage';
        DELETE FROM permissions WHERE code = 'booking_setup.manage';
        DELETE FROM app_settings WHERE key IN ('same_course_block_months', 'bring_a_friend_enabled',
                                               'bring_a_friend_discount_cents', 'invite_link_days', 'ghl_tags');
    """)
