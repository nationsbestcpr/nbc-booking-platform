-- Repairs run on every start when `flask db verify` finds parts missing.
-- Unlike migrations, this file is kept up to date: every statement must be
-- idempotent (safe on a complete database), and it covers objects later
-- migrations add that a schema sync could remove.
--
-- On the test site, Replit's "database changes" prompt on publish made the
-- published database match the workspace's, which was still at 0003: it
-- dropped orders.buyer_user_id (and its index), while the migration record
-- stayed at the latest version. Everything here is idempotent: on a database
-- that already has it all, nothing changes.

ALTER TABLE orders ADD COLUMN IF NOT EXISTS buyer_user_id bigint REFERENCES user_accounts (id);
CREATE INDEX IF NOT EXISTS orders_buyer_user_idx ON orders (buyer_user_id);

-- The sign-in method list with 'google' (whatever the old check was called).
DO $$
DECLARE c record;
BEGIN
  FOR c IN SELECT conname FROM pg_constraint
            WHERE conrelid = 'user_identities'::regclass AND contype = 'c'
              AND pg_get_constraintdef(oid) LIKE '%email_link%'
              AND pg_get_constraintdef(oid) NOT LIKE '%google%' LOOP
    EXECUTE format('ALTER TABLE user_identities DROP CONSTRAINT %I', c.conname);
  END LOOP;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'user_identities'::regclass
                    AND conname = 'user_identities_provider_check') THEN
    ALTER TABLE user_identities ADD CONSTRAINT user_identities_provider_check
      CHECK (provider IN ('password', 'microsoft', 'google', 'email_link'));
  END IF;
END $$;

-- The account that bought a seat becomes a student too (as in 0004).
CREATE OR REPLACE FUNCTION trg_registration_grants_student() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO user_roles (user_account_id, role_code)
  SELECT a.id, 'student'
    FROM user_accounts a
   WHERE (a.email IN (NEW.attendee_email, (SELECT buyer_email FROM orders WHERE id = NEW.order_id))
          OR a.id = (SELECT buyer_user_id FROM orders WHERE id = NEW.order_id))
     AND NOT EXISTS (SELECT 1 FROM user_roles ur WHERE ur.user_account_id = a.id
                        AND ur.role_code = 'student' AND ur.revoked_at IS NULL);
  RETURN NULL;
END $$;
