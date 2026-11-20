-- 0011: every instructor field the NBC Master Base holds, so the booking engine
-- can replace Enrollware and the NBC Ops Hub can read instructors through the
-- API. Airtable stays the source (read only): the sync fills these; nothing
-- here is written back.
-- Frozen: later changes to db/schema.sql never change this file.

-- Instructors: Airtable's Instructors table. An instructor may have no email
-- there yet, so the email is optional (still one instructor per email).
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
COMMENT ON TABLE instructors IS '[MIRROR: Airtable Instructors] Teaching profile: contact, city areas, certifications, availability, outreach and removal, as Airtable has them. Linked to a user account (role "instructor") once invited to the portal. Lead vs helper is per class, in assignments.role.';

-- The city areas an instructor covers ("Virginia - Richmond"), with the engine's
-- city area when it has one by that name.
CREATE TABLE IF NOT EXISTS instructor_metros (
  instructor_id  bigint NOT NULL REFERENCES instructors (id) ON DELETE CASCADE,
  metro_label    text NOT NULL,
  metro_id       bigint REFERENCES metros (id),
  PRIMARY KEY (instructor_id, metro_label)
);
COMMENT ON TABLE instructor_metros IS '[MIRROR: Airtable Instructors.Metros] The city areas each instructor covers.';

-- HUB teams: who leads whom (Airtable's HUB Lead / HUB Team Members).
CREATE TABLE IF NOT EXISTS instructor_hub_members (
  lead_instructor_id    bigint NOT NULL REFERENCES instructors (id) ON DELETE CASCADE,
  member_instructor_id  bigint NOT NULL REFERENCES instructors (id) ON DELETE CASCADE,
  PRIMARY KEY (lead_instructor_id, member_instructor_id),
  CHECK (lead_instructor_id <> member_instructor_id)
);
COMMENT ON TABLE instructor_hub_members IS '[MIRROR: Airtable Instructors.HUB Lead / HUB Team Members] HUB team membership.';

-- Locations an instructor is pre-approved for, has taught at, or taught at
-- before the location went inactive. Keyed by the Master Locations record, so
-- locations with no class on sale (no site here) are kept too.
CREATE TABLE IF NOT EXISTS instructor_locations (
  instructor_id       bigint NOT NULL REFERENCES instructors (id) ON DELETE CASCADE,
  location_record_id  text NOT NULL,
  relation            text NOT NULL CHECK (relation IN ('pre_approved', 'taught', 'taught_inactive')),
  site_id             bigint REFERENCES sites (id),
  PRIMARY KEY (instructor_id, location_record_id, relation)
);
CREATE INDEX IF NOT EXISTS instructor_locations_site_idx ON instructor_locations (site_id, relation);
COMMENT ON TABLE instructor_locations IS '[MIRROR: Airtable Instructors.Locations - Pre-Approval / Taught At] Instructor-location relations.';

-- How each location is staffed (Master Locations' instructor fields). Staff
-- only: it holds pay and private notes, so it is kept apart from sites, which
-- public pages read.
CREATE TABLE IF NOT EXISTS location_instructor_settings (
  location_record_id             text PRIMARY KEY,
  site_id                        bigint UNIQUE REFERENCES sites (id),
  location_name                  text,
  assignment_types               text[] NOT NULL DEFAULT '{}',
  primary_instructor_name        text,
  primary_instructor_id          bigint REFERENCES instructors (id),
  hub_primary_instructor_name    text,
  hub_primary_instructor_id      bigint REFERENCES instructors (id),
  hub_secondary_instructor_name  text,
  hub_secondary_instructor_id    bigint REFERENCES instructors (id),
  is_instructor_training_location boolean,
  instructor_notes               text,
  extra_pay_cents                integer CHECK (extra_pay_cents >= 0),
  extra_pay_reason               text,
  updated_at                     timestamptz NOT NULL DEFAULT now()
);
DROP TRIGGER IF EXISTS location_instructor_settings_set_updated_at ON location_instructor_settings;
CREATE TRIGGER location_instructor_settings_set_updated_at BEFORE UPDATE ON location_instructor_settings
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
COMMENT ON TABLE location_instructor_settings IS '[MIRROR: Airtable Master Locations] Instructor assignment type, primary and HUB instructors, instructor notes and extra pay per location. Staff only; never on public pages.';

-- Per class: the lead named in Airtable matched to an instructor, and the
-- class's instructor planning fields.
CREATE TABLE IF NOT EXISTS session_instructor_details (
  session_id           bigint PRIMARY KEY REFERENCES sessions (id) ON DELETE CASCADE,
  lead_instructor_id   bigint REFERENCES instructors (id),
  instructor_assigned  boolean NOT NULL DEFAULT false,
  availability_status  text,
  performance_tier     text,
  updated_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS session_instructor_details_lead_idx ON session_instructor_details (lead_instructor_id);
DROP TRIGGER IF EXISTS session_instructor_details_set_updated_at ON session_instructor_details;
CREATE TRIGGER session_instructor_details_set_updated_at BEFORE UPDATE ON session_instructor_details
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
COMMENT ON TABLE session_instructor_details IS '[MIRROR: Airtable Class Schedule] ASSIGN INSTRUCTOR matched to an instructor, Instructor Assigned, availability status and performance tier.';

-- Instructor Recruiting Needs: locations that need an instructor, and why.
CREATE TABLE IF NOT EXISTS recruiting_needs (
  id                                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  airtable_record_id                text NOT NULL UNIQUE REFERENCES airtable_records (record_id),
  need                              text,
  location_record_ids               text[] NOT NULL DEFAULT '{}',
  need_reasons                      text[] NOT NULL DEFAULT '{}',
  recruiting_status                 text,
  recruiting_notes                  text,
  enrollware_no_instructor_cancels  integer,
  cls_lost_cents                    bigint,
  profit_while_running_cents        bigint,
  students_per_class                numeric(8,1),
  last_month_held                   text,
  airtable_created_at               timestamptz,
  source_synced_at                  timestamptz,
  source_deleted_at                 timestamptz
);
COMMENT ON TABLE recruiting_needs IS '[MIRROR: Airtable Instructor Recruiting Needs] Locations that need an instructor, with the no-instructor cancellations behind each.';

CREATE TABLE IF NOT EXISTS recruiting_need_classes (
  recruiting_need_id  bigint NOT NULL REFERENCES recruiting_needs (id) ON DELETE CASCADE,
  class_record_id     text NOT NULL,
  session_id          bigint REFERENCES sessions (id),
  PRIMARY KEY (recruiting_need_id, class_record_id)
);
COMMENT ON TABLE recruiting_need_classes IS '[MIRROR: Airtable Instructor Recruiting Needs.No-Instructor Cancelled Classes] The cancelled classes behind a need.';

-- Airtable's three r.25 instructor certifications, and what each lets someone teach.
INSERT INTO cert_type_course_families (cert_type, course_family_id)
SELECT c.cert_type, f.id FROM (VALUES ('ARC_FA_CPR_AED_INSTRUCTOR_R25', 'FA_CPR_AED'),
                                      ('ARC_BLS_INSTRUCTOR_R25', 'BLS'),
                                      ('ARC_INSTRUCTOR_TRAINER_R25', 'IT')) AS c (cert_type, family)
  JOIN course_families f ON f.code = c.family
ON CONFLICT DO NOTHING;

-- Airtable's Cert Status, worked out from the r.25 certifications.
CREATE OR REPLACE VIEW instructor_cert_status AS
SELECT i.id AS instructor_id,
       CASE WHEN NOT EXISTS (SELECT 1 FROM instructor_certs c WHERE c.instructor_id = i.id
                               AND c.cert_type LIKE '%\_R25' ESCAPE '\') THEN 'No r.25'
            WHEN EXISTS (SELECT 1 FROM instructor_certs c WHERE c.instructor_id = i.id
                           AND c.cert_type LIKE '%\_R25' ESCAPE '\' AND c.expires_on < current_date) THEN 'Expired'
            WHEN EXISTS (SELECT 1 FROM instructor_certs c WHERE c.instructor_id = i.id
                           AND c.cert_type LIKE '%\_R25' ESCAPE '\' AND c.expires_on < current_date + 60)
              THEN 'Expiring within 60 days'
            ELSE 'Current' END AS cert_status,
       (SELECT min(c.expires_on) FROM instructor_certs c WHERE c.instructor_id = i.id
           AND c.expires_on >= current_date) AS next_expiry
  FROM instructors i;
