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

-- 0008: who booked whom, bring a friend, invite links, the same-course rule.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS parent_order_id bigint REFERENCES orders (id);
CREATE INDEX IF NOT EXISTS orders_parent_idx ON orders (parent_order_id) WHERE parent_order_id IS NOT NULL;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'orders'::regclass AND conname = 'orders_parent_not_self') THEN
    ALTER TABLE orders ADD CONSTRAINT orders_parent_not_self CHECK (parent_order_id IS NULL OR parent_order_id <> id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'action_tokens'::regclass
                    AND conname = 'action_tokens_purpose_check'
                    AND pg_get_constraintdef(oid) LIKE '%account_invite%') THEN
    ALTER TABLE action_tokens DROP CONSTRAINT IF EXISTS action_tokens_purpose_check;
    ALTER TABLE action_tokens ADD CONSTRAINT action_tokens_purpose_check
      CHECK (purpose IN ('sign_in_link', 'waitlist_offer', 'rebook_choice', 'phone_checkout', 'account_invite'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'action_tokens'::regclass
                    AND conname = 'action_tokens_invite_has_registration') THEN
    ALTER TABLE action_tokens ADD CONSTRAINT action_tokens_invite_has_registration
      CHECK (purpose <> 'account_invite' OR registration_id IS NOT NULL);
  END IF;
END $$;
ALTER TABLE registrations ADD COLUMN IF NOT EXISTS is_bring_a_friend boolean NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS action_tokens_registration_idx ON action_tokens (registration_id) WHERE registration_id IS NOT NULL;
CREATE OR REPLACE FUNCTION trg_same_course_block() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_months integer; v_family smallint; v_starts timestamptz; v_clash timestamptz; v_course text;
BEGIN
  SELECT (value #>> '{}')::integer INTO v_months FROM app_settings WHERE key = 'same_course_block_months';
  IF COALESCE(v_months, 0) <= 0 THEN
    RETURN NEW;
  END IF;
  SELECT s.course_family_id, s.starts_at, cf.name INTO v_family, v_starts, v_course
    FROM sessions s JOIN course_families cf ON cf.id = s.course_family_id WHERE s.id = NEW.session_id;
  SELECT s.starts_at INTO v_clash
    FROM registrations r JOIN sessions s ON s.id = r.session_id
   WHERE r.attendee_email = NEW.attendee_email AND r.id <> NEW.id AND r.order_id <> NEW.order_id
     AND s.course_family_id = v_family
     AND s.starts_at > v_starts - make_interval(months => v_months)
     AND s.starts_at < v_starts + make_interval(months => v_months)
     AND (r.status IN ('awaiting_payment', 'confirmed', 'completed', 'displaced')
          OR (r.status = 'pending' AND EXISTS (SELECT 1 FROM seat_holds h WHERE h.id = r.seat_hold_id
                                                 AND h.status = 'active' AND h.expires_at > now())))
   ORDER BY s.starts_at LIMIT 1;
  IF FOUND THEN
    RAISE EXCEPTION 'same course within % months: % already has % on %', v_months, NEW.attendee_email, v_course,
      to_char(v_clash, 'YYYY-MM-DD') USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'registrations_same_course'
                    AND tgrelid = 'registrations'::regclass) THEN
    CREATE TRIGGER registrations_same_course BEFORE INSERT OR UPDATE OF status, session_id, attendee_email
      ON registrations FOR EACH ROW WHEN (NEW.status IN ('pending', 'awaiting_payment', 'confirmed'))
      EXECUTE FUNCTION trg_same_course_block();
  END IF;
END $$;

-- 0009: the booking desk's actions, attendance and the waiting list.
ALTER TABLE checkout_sessions ALTER COLUMN seat_hold_id DROP NOT NULL;
CREATE INDEX IF NOT EXISTS waitlist_offers_entry_idx ON waitlist_offers (waitlist_entry_id);
CREATE OR REPLACE FUNCTION trg_session_waitlist_rules() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.room_max_student_count > NEW.one_instructor_max THEN
    NEW.location_type := 'co_instructor';
    NEW.waitlist_limit := LEAST(NEW.room_max_student_count - NEW.one_instructor_max, NEW.one_instructor_max);
  ELSE
    NEW.location_type := 'one_instructor';
    NEW.capacity_basis := 'one_instructor';
    NEW.waitlist_limit := NEW.one_instructor_max;
  END IF;
  RETURN NEW;
END $$;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'sessions_waitlist_rules'
                    AND tgrelid = 'sessions'::regclass) THEN
    CREATE TRIGGER sessions_waitlist_rules BEFORE INSERT OR UPDATE OF one_instructor_max, room_max_student_count
      ON sessions FOR EACH ROW EXECUTE FUNCTION trg_session_waitlist_rules();
    UPDATE sessions SET one_instructor_max = one_instructor_max;
  END IF;
END $$;

-- 0011: Airtable's instructor fields on instructors (the new tables are made by the migration).
ALTER TABLE instructors ALTER COLUMN email DROP NOT NULL;
ALTER TABLE instructors
  ADD COLUMN IF NOT EXISTS full_name                 text,
  ADD COLUMN IF NOT EXISTS hub_key                   text,
  ADD COLUMN IF NOT EXISTS city                      text,
  ADD COLUMN IF NOT EXISTS state                     text,
  ADD COLUMN IF NOT EXISTS postal_code               text,
  ADD COLUMN IF NOT EXISTS active_in_enrollware      boolean,
  ADD COLUMN IF NOT EXISTS is_hub_instructor         boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS interested_in_leading_hub boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS additional_certs          text,
  ADD COLUMN IF NOT EXISTS certs_last_synced_on      date,
  ADD COLUMN IF NOT EXISTS available_days            text[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS available_times           text[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS classes_per_month         integer CHECK (classes_per_month >= 0),
  ADD COLUMN IF NOT EXISTS locations_to_avoid        text,
  ADD COLUMN IF NOT EXISTS notice_needed             text,
  ADD COLUMN IF NOT EXISTS availability_notes        text,
  ADD COLUMN IF NOT EXISTS availability_updated_on   date,
  ADD COLUMN IF NOT EXISTS availability_updated_by   text,
  ADD COLUMN IF NOT EXISTS outreach_status           text,
  ADD COLUMN IF NOT EXISTS last_contact_on           date,
  ADD COLUMN IF NOT EXISTS next_follow_up_on         date,
  ADD COLUMN IF NOT EXISTS outreach_notes            text,
  ADD COLUMN IF NOT EXISTS removal_recommended_on    date,
  ADD COLUMN IF NOT EXISTS removal_reason            text,
  ADD COLUMN IF NOT EXISTS removal_status            text,
  ADD COLUMN IF NOT EXISTS remove_from               text[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS associate                 boolean,
  ADD COLUMN IF NOT EXISTS page_type                 text,
  ADD COLUMN IF NOT EXISTS date_approval_url         text,
  ADD COLUMN IF NOT EXISTS other_enrollware_accounts text,
  ADD COLUMN IF NOT EXISTS source_synced_at          timestamptz,
  ADD COLUMN IF NOT EXISTS source_deleted_at         timestamptz;
