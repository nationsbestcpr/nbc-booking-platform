-- 0012: running without Enrollware. Every city can book here, Enrollware's upcoming registrations can be
-- imported, and the instructor portal covers class day and the days before it: confirming a class, students
-- signing the roster, and availability changes that can't quietly clash with a class already assigned.
-- Additive only (no DROP), so Replit's publish step has nothing to remove.
-- Frozen: later changes to db/schema.sql never change this file.

-- Settings and permissions -------------------------------------------------------------------------------
INSERT INTO app_settings (key, value, description) VALUES
  ('enrollware_retired', 'false',
   'Enrollware no longer takes bookings: every city books here, and new cities from Airtable start here too (Superadmin → Enrollware cutover).'),
  ('confirm_days_before', '5',
   'Instructors confirm a class in the portal from this many days before it.')
ON CONFLICT (key) DO NOTHING;

INSERT INTO permissions (code, area, description, own_records_only) VALUES
  ('system.cutover',      'Admin',    'Move every city off Enrollware and import Enrollware''s registrations', false),
  ('instructor.requests', 'Teaching', 'See and close instructors'' availability changes and "can''t teach" notices', false)
ON CONFLICT (code) DO NOTHING;
INSERT INTO role_permissions (role_code, permission_code)
SELECT DISTINCT role_code, 'instructor.requests' FROM role_permissions WHERE permission_code = 'instructor.manage'
ON CONFLICT DO NOTHING;

-- Instructors confirm their classes ---------------------------------------------------------------------
ALTER TABLE assignments
  ADD COLUMN IF NOT EXISTS confirmed_at          timestamptz,
  ADD COLUMN IF NOT EXISTS confirmed_by_user_id  bigint REFERENCES user_accounts (id);

-- Students sign the roster on class day ------------------------------------------------------------------
-- The signature is the pen's path as SVG path data ("M10 20 L12 22 ..."), drawn on the instructor's device.
CREATE TABLE IF NOT EXISTS roster_signatures (
  registration_id             bigint PRIMARY KEY REFERENCES registrations (id),
  session_id                  bigint NOT NULL REFERENCES sessions (id),
  signed_name                 text NOT NULL CHECK (length(trim(signed_name)) >= 2),
  signature_path              text NOT NULL CHECK (signature_path ~ '^M[0-9MLml .,-]+$' AND length(signature_path) <= 100000),
  signed_at                   timestamptz NOT NULL DEFAULT now(),
  collected_by_assignment_id  bigint REFERENCES assignments (id),
  collected_by_user_id        bigint REFERENCES user_accounts (id),
  ip                          inet,
  user_agent                  text,
  CHECK (num_nonnulls(collected_by_assignment_id, collected_by_user_id) = 1)
);
CREATE INDEX IF NOT EXISTS roster_signatures_session_idx ON roster_signatures (session_id);
COMMENT ON TABLE roster_signatures IS 'Each student''s signature on the class roster, collected on class day on the instructor''s device.';

CREATE OR REPLACE FUNCTION trg_signature_matches_seat() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM registrations r WHERE r.id = NEW.registration_id AND r.session_id = NEW.session_id
                    AND r.status IN ('confirmed', 'awaiting_payment', 'completed')) THEN
    RAISE EXCEPTION 'Only a student booked in this class can sign its roster.' USING ERRCODE = 'check_violation';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM sessions s WHERE s.id = NEW.session_id AND s.status <> 'cancelled') THEN
    RAISE EXCEPTION 'This class was cancelled.' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS roster_signatures_seat ON roster_signatures;
CREATE TRIGGER roster_signatures_seat BEFORE INSERT OR UPDATE ON roster_signatures
  FOR EACH ROW EXECUTE FUNCTION trg_signature_matches_seat();

-- Instructors' availability changes and "can't teach" notices --------------------------------------------
-- Airtable keeps the instructor's availability and who teaches each class; the booking app only collects
-- what the instructor asks for, and the Ops Hub applies it in Airtable and closes the request.

-- The upcoming classes this instructor is assigned to that a new availability would rule out:
-- away on the day, a day of the week they no longer give, or a time of day they no longer give
-- (Morning before noon, Afternoon until 5 pm, Evening after). Empty days or times = unchanged.
CREATE OR REPLACE FUNCTION instructor_availability_conflicts(p_instructor_id bigint, p_days text[], p_times text[],
                                                             p_away_from date, p_away_to date)
RETURNS TABLE (assignment_id bigint, session_id bigint, why text) LANGUAGE sql STABLE AS $$
  SELECT x.assignment_id, x.session_id, x.why FROM (
    SELECT a.id AS assignment_id, s.id AS session_id,
           CASE WHEN p_away_from IS NOT NULL AND s.local_date BETWEEN p_away_from AND coalesce(p_away_to, p_away_from)
                  THEN 'away'
                WHEN cardinality(coalesce(p_days, '{}')) > 0
                     AND NOT (to_char(timezone(s.time_zone, s.starts_at), 'Dy') = ANY (p_days)) THEN 'day'
                WHEN cardinality(coalesce(p_times, '{}')) > 0
                     AND NOT ((CASE WHEN extract(hour FROM timezone(s.time_zone, s.starts_at)) < 12 THEN 'Morning'
                                    WHEN extract(hour FROM timezone(s.time_zone, s.starts_at)) < 17 THEN 'Afternoon'
                                    ELSE 'Evening' END) = ANY (p_times)) THEN 'time'
           END AS why
      FROM assignments a JOIN sessions s ON s.id = a.session_id
     WHERE a.instructor_id = p_instructor_id AND a.status = 'assigned'
       AND s.status = 'scheduled' AND s.starts_at > now()
  ) x WHERE x.why IS NOT NULL
$$;

CREATE TABLE IF NOT EXISTS instructor_requests (
  id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  instructor_id         bigint NOT NULL REFERENCES instructors (id),
  kind                  text NOT NULL CHECK (kind IN ('availability', 'cant_teach')),
  assignment_id         bigint REFERENCES assignments (id),
  availability_request_id bigint REFERENCES instructor_requests (id),  -- the availability change a notice came with
  reason                text,
  details               jsonb NOT NULL DEFAULT '{}',
  status                text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'dismissed', 'withdrawn')),
  created_by_user_id    bigint NOT NULL REFERENCES user_accounts (id),
  created_at            timestamptz NOT NULL DEFAULT now(),
  handled_by_user_id    bigint REFERENCES user_accounts (id),
  handled_at            timestamptz,
  handled_note          text,
  updated_at            timestamptz NOT NULL DEFAULT now(),
  CHECK (kind <> 'cant_teach' OR (assignment_id IS NOT NULL AND length(trim(coalesce(reason, ''))) >= 3)),
  CHECK (kind <> 'availability' OR assignment_id IS NULL),
  CHECK (status NOT IN ('done', 'dismissed') OR (handled_by_user_id IS NOT NULL AND handled_at IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS instructor_requests_one_open_notice
  ON instructor_requests (assignment_id) WHERE kind = 'cant_teach' AND status = 'open';
CREATE INDEX IF NOT EXISTS instructor_requests_open_idx ON instructor_requests (status, kind, created_at);
DROP TRIGGER IF EXISTS instructor_requests_set_updated_at ON instructor_requests;
CREATE TRIGGER instructor_requests_set_updated_at BEFORE UPDATE ON instructor_requests
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
COMMENT ON TABLE instructor_requests IS 'What instructors ask NBC from the portal: availability changes and "can''t teach this class" notices. The Ops Hub applies them in Airtable and closes them.';

-- An availability change must say, for every assigned class it would rule out, whether the instructor still
-- teaches it ("keep") or can't ("cant_teach", which comes with a notice). Nothing is unassigned by itself.
CREATE OR REPLACE FUNCTION trg_instructor_request_rules() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  missing integer;
BEGIN
  IF NEW.kind = 'cant_teach' AND TG_OP = 'INSERT' THEN
    IF NOT EXISTS (SELECT 1 FROM assignments a JOIN sessions s ON s.id = a.session_id
                    WHERE a.id = NEW.assignment_id AND a.instructor_id = NEW.instructor_id
                      AND a.status = 'assigned' AND s.status = 'scheduled') THEN
      RAISE EXCEPTION 'You can only send this for a class you''re assigned to that is still on.'
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  IF NEW.kind = 'availability' AND TG_OP = 'INSERT' THEN
    SELECT count(*) INTO missing
      FROM instructor_availability_conflicts(
             NEW.instructor_id,
             ARRAY(SELECT jsonb_array_elements_text(coalesce(NEW.details -> 'available_days', '[]'))),
             ARRAY(SELECT jsonb_array_elements_text(coalesce(NEW.details -> 'available_times', '[]'))),
             NULLIF(NEW.details ->> 'away_from', '')::date, NULLIF(NEW.details ->> 'away_to', '')::date) c
     WHERE NOT (coalesce(NEW.details -> 'classes', '{}') ? c.assignment_id::text
                AND NEW.details -> 'classes' ->> c.assignment_id::text IN ('keep', 'cant_teach'));
    IF missing > 0 THEN
      RAISE EXCEPTION 'This change clashes with % class(es) you''re assigned to. Say for each one whether you''ll still teach it or tell NBC you can''t.', missing
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS instructor_requests_rules ON instructor_requests;
CREATE TRIGGER instructor_requests_rules BEFORE INSERT ON instructor_requests
  FOR EACH ROW EXECUTE FUNCTION trg_instructor_request_rules();

-- Enrollware's registrations, imported at cutover --------------------------------------------------------
CREATE TABLE IF NOT EXISTS enrollware_imports (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  file_name            text NOT NULL,
  rows_total           integer NOT NULL CHECK (rows_total >= 0),
  sent_to_ghl          boolean NOT NULL DEFAULT false,
  summary              jsonb NOT NULL DEFAULT '{}',
  imported_by_user_id  bigint NOT NULL REFERENCES user_accounts (id),
  created_at           timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE enrollware_imports IS 'Each Enrollware export loaded at cutover, and what came of it.';

CREATE TABLE IF NOT EXISTS enrollware_import_rows (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  import_id            bigint NOT NULL REFERENCES enrollware_imports (id) ON DELETE CASCADE,
  row_number           integer NOT NULL CHECK (row_number > 0),
  row_key              text NOT NULL,                -- Enrollware class id + student, the same in every export
  outcome              text NOT NULL CHECK (outcome IN ('imported', 'waitlisted', 'already_imported', 'skipped',
                                                        'unmatched', 'refused')),
  reason               text,
  enrollware_class_ref text,
  session_id           bigint REFERENCES sessions (id),
  registration_id      bigint REFERENCES registrations (id),
  waitlist_entry_id    bigint REFERENCES waitlist_entries (id),
  created_at           timestamptz NOT NULL DEFAULT now(),
  CHECK (outcome <> 'imported' OR registration_id IS NOT NULL),
  CHECK (outcome <> 'waitlisted' OR waitlist_entry_id IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS enrollware_import_rows_once
  ON enrollware_import_rows (row_key) WHERE outcome IN ('imported', 'waitlisted');
CREATE INDEX IF NOT EXISTS enrollware_import_rows_import_idx ON enrollware_import_rows (import_id, outcome);
COMMENT ON TABLE enrollware_import_rows IS 'Each row of an Enrollware export: the seat or waiting-list place it became, or why not. A student is imported once, however many exports are loaded.';
