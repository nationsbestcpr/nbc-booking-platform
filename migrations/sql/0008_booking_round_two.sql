-- 0008: who booked whom, attendee invites, the same-course rule, booking
-- questions, add-ons and bring a friend.
-- Frozen: later changes to db/schema.sql never change this file.

-- Business settings the new rules read.
INSERT INTO app_settings (key, value, description) VALUES
  ('same_course_block_months', '18',
   'One email can''t book the same course again within this many months of another class of it (Nov 6 decision). 0 turns the rule off.'),
  ('bring_a_friend_enabled', 'true',
   'Offer "Bring a friend" on the booking page and again on the confirmation page.'),
  ('bring_a_friend_discount_cents', 'null',
   'OPEN: discount on a friend''s seat (to confirm). Empty means the friend pays the class price.'),
  ('invite_link_days', '60',
   'How long the link in an attendee''s invite works (someone booked them a seat).'),
  ('ghl_tags',
   '{"student":"nbc-student","booked_self":"nbc-booked-self","booked_by_other":"nbc-booked-by-someone-else","booker":"nbc-booker","bring_a_friend":"nbc-bring-a-friend","send_invite":"nbc-send-invite","course_prefix":"nbc-course-","city_prefix":"nbc-city-","addon_prefix":"nbc-addon-"}',
   'Tags the engine puts on GHL contacts. GHL workflows (emails from templates) start on them; change a name here if a workflow uses another.');

INSERT INTO permissions (code, area, description, own_records_only) VALUES
  ('booking_setup.manage', 'Admin', 'Add and change booking questions and add-ons', false);

-- A purchase made from an earlier one (a friend added from the confirmation page).
ALTER TABLE orders ADD COLUMN parent_order_id bigint REFERENCES orders (id);
CREATE INDEX orders_parent_idx ON orders (parent_order_id) WHERE parent_order_id IS NOT NULL;
ALTER TABLE orders ADD CONSTRAINT orders_parent_not_self CHECK (parent_order_id IS NULL OR parent_order_id <> id);

-- A friend's seat (bring a friend), so it can be priced, tagged and counted.
ALTER TABLE registrations ADD COLUMN is_bring_a_friend boolean NOT NULL DEFAULT false;

-- Invite links for attendees someone else booked.
ALTER TABLE action_tokens DROP CONSTRAINT action_tokens_purpose_check;
ALTER TABLE action_tokens ADD CONSTRAINT action_tokens_purpose_check
  CHECK (purpose IN ('sign_in_link', 'waitlist_offer', 'rebook_choice', 'phone_checkout', 'account_invite'));
ALTER TABLE action_tokens ADD CONSTRAINT action_tokens_invite_has_registration
  CHECK (purpose <> 'account_invite' OR registration_id IS NOT NULL);
CREATE INDEX action_tokens_registration_idx ON action_tokens (registration_id) WHERE registration_id IS NOT NULL;

-- One email, one seat in a course per 18 months (Red Cross certifies by email).
CREATE FUNCTION trg_same_course_block() RETURNS trigger LANGUAGE plpgsql AS $$
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
CREATE TRIGGER registrations_same_course BEFORE INSERT OR UPDATE OF status, session_id, attendee_email
  ON registrations FOR EACH ROW WHEN (NEW.status IN ('pending', 'awaiting_payment', 'confirmed'))
  EXECUTE FUNCTION trg_same_course_block();

-- Booking questions, set up by staff (no code change to add or edit one).
CREATE TABLE booking_questions (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  label                text NOT NULL CHECK (length(trim(label)) > 0),
  help_text            text,
  kind                 text NOT NULL CHECK (kind IN ('short_text', 'long_text', 'yes_no', 'choice')),
  choices              text[] NOT NULL DEFAULT '{}',
  is_required          boolean NOT NULL DEFAULT false,
  asked_for            text NOT NULL DEFAULT 'attendee' CHECK (asked_for IN ('attendee', 'booking')),
  delivery_mode        text CHECK (delivery_mode IN ('blended', 'classroom')),
  ghl_field_key        text CHECK (ghl_field_key ~ '^opportunity\.[a-z0-9_]+$'),
  is_active            boolean NOT NULL DEFAULT true,
  sort_order           integer NOT NULL DEFAULT 0,
  created_by_user_id   bigint REFERENCES user_accounts (id),
  updated_by_user_id   bigint REFERENCES user_accounts (id),
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT booking_questions_choices_check CHECK (kind <> 'choice' OR cardinality(choices) >= 2)
);
COMMENT ON TABLE booking_questions IS '[OWNED] Questions the booking page asks, per attendee or once per booking. Edited by staff on the Booking questions page; no course rows = every course.';
CREATE TRIGGER booking_questions_set_updated_at BEFORE UPDATE ON booking_questions
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE booking_question_courses (
  question_id       bigint NOT NULL REFERENCES booking_questions (id) ON DELETE CASCADE,
  course_family_id  smallint NOT NULL REFERENCES course_families (id),
  PRIMARY KEY (question_id, course_family_id)
);
COMMENT ON TABLE booking_question_courses IS '[OWNED] Courses a booking question is asked for (none = every course).';

CREATE TABLE booking_answers (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  order_id         bigint NOT NULL REFERENCES orders (id),
  registration_id  bigint REFERENCES registrations (id),
  question_id      bigint NOT NULL REFERENCES booking_questions (id),
  question_label   text NOT NULL,
  answer           text NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT booking_answers_one_per_question UNIQUE NULLS NOT DISTINCT (order_id, registration_id, question_id)
);
CREATE INDEX booking_answers_registration_idx ON booking_answers (registration_id);
COMMENT ON TABLE booking_answers IS '[SNAPSHOT] Answers given at booking, with the question as it was worded then. registration_id is empty for once-per-booking questions.';

-- Add-ons offered on the booking page, set up by staff.
CREATE TABLE addons (
  id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code                  text NOT NULL UNIQUE CHECK (code ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
  name                  text NOT NULL CHECK (length(trim(name)) > 0),
  description           text,
  price_cents           integer NOT NULL CHECK (price_cents >= 0),
  per                   text NOT NULL DEFAULT 'seat' CHECK (per IN ('seat', 'booking')),
  show_on_booking_page  boolean NOT NULL DEFAULT false,
  sort_order            integer NOT NULL DEFAULT 0,
  created_by_user_id    bigint REFERENCES user_accounts (id),
  updated_by_user_id    bigint REFERENCES user_accounts (id),
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE addons IS '[OWNED] Extras sold with a seat or a booking (wallet card, kit, ...). Staff create them and choose whether booking pages show them; no course rows = every course.';
CREATE TRIGGER addons_set_updated_at BEFORE UPDATE ON addons
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE addon_courses (
  addon_id          bigint NOT NULL REFERENCES addons (id) ON DELETE CASCADE,
  course_family_id  smallint NOT NULL REFERENCES course_families (id),
  PRIMARY KEY (addon_id, course_family_id)
);
COMMENT ON TABLE addon_courses IS '[OWNED] Courses an add-on is offered with (none = every course).';

CREATE TABLE order_addons (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  order_id           bigint NOT NULL REFERENCES orders (id),
  addon_id           bigint NOT NULL REFERENCES addons (id),
  registration_id    bigint REFERENCES registrations (id),
  name               text NOT NULL,
  unit_price_cents   integer NOT NULL CHECK (unit_price_cents >= 0),
  quantity           integer NOT NULL DEFAULT 1 CHECK (quantity > 0),
  discount_cents     integer NOT NULL DEFAULT 0 CHECK (discount_cents >= 0),
  amount_paid_cents  integer NOT NULL DEFAULT 0 CHECK (amount_paid_cents >= 0),
  status             text NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'paid', 'expired', 'cancelled', 'refunded')),
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT order_addons_one_per_seat UNIQUE NULLS NOT DISTINCT (order_id, addon_id, registration_id),
  CONSTRAINT order_addons_discount_check CHECK (discount_cents <= unit_price_cents * quantity)
);
CREATE INDEX order_addons_registration_idx ON order_addons (registration_id);
COMMENT ON TABLE order_addons IS '[SNAPSHOT + OWNED status] Add-ons bought in an order, with the name and price as sold. registration_id is the seat for per-seat add-ons.';
CREATE TRIGGER order_addons_set_updated_at BEFORE UPDATE ON order_addons
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
