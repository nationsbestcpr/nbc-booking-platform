"""Google sign-in, and orders linked to the account that booked them.

Revision ID: 0004
Revises: 0003
Created: 2026-11-06

The SQL is migrations/sql/0004_social_sign_in.sql (frozen). Allows the
'google' sign-in method on user_identities, adds orders.buyer_user_id (online
bookings are made from an account, so the dashboard can show them), and makes
the buying account a student when its seat is booked.
"""

from migrations.sqlfile import run_sql, run_sql_file

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0004_social_sign_in.sql")


def downgrade() -> None:
    run_sql("""
        CREATE OR REPLACE FUNCTION trg_registration_grants_student() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          INSERT INTO user_roles (user_account_id, role_code)
          SELECT a.id, 'student'
            FROM user_accounts a
           WHERE a.email IN (NEW.attendee_email, (SELECT buyer_email FROM orders WHERE id = NEW.order_id))
             AND NOT EXISTS (SELECT 1 FROM user_roles ur WHERE ur.user_account_id = a.id
                                AND ur.role_code = 'student' AND ur.revoked_at IS NULL);
          RETURN NULL;
        END $$;
        DROP INDEX IF EXISTS orders_buyer_user_idx;
        ALTER TABLE orders DROP COLUMN IF EXISTS buyer_user_id;
        DELETE FROM user_identities WHERE provider = 'google';
        ALTER TABLE user_identities DROP CONSTRAINT user_identities_provider_check;
        ALTER TABLE user_identities ADD CONSTRAINT user_identities_provider_check
          CHECK (provider IN ('password', 'microsoft', 'email_link'));
    """)
