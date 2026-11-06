-- 0004: Google sign-in, and orders linked to the account that booked them.
-- Frozen: later changes to db/schema.sql never change this file.

-- Accounts may also sign in with Google (subject = Google's account id, "sub").
ALTER TABLE user_identities DROP CONSTRAINT user_identities_provider_check;
ALTER TABLE user_identities ADD CONSTRAINT user_identities_provider_check
  CHECK (provider IN ('password', 'microsoft', 'google', 'email_link'));

-- Online bookings need an account: the order remembers whose it is, so the
-- customer's dashboard shows it whatever email they typed.
ALTER TABLE orders ADD COLUMN buyer_user_id bigint REFERENCES user_accounts (id);
CREATE INDEX orders_buyer_user_idx ON orders (buyer_user_id);

-- The account that bought a seat becomes a student too (not only by email).
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
