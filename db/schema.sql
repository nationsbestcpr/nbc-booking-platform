-- =============================================================================
-- NBC Booking Engine — PostgreSQL schema (v1)
-- =============================================================================
-- Target: PostgreSQL 16 (Replit PostgreSQL). Extensions: citext, pg_trgm,
-- btree_gist. Load into an empty database:  psql "$DATABASE_URL" -f db/schema.sql
--
-- How to read this file
--   Every table carries a COMMENT tag saying who owns its data:
--     [OWNED]     The booking engine is the system of record.
--     [MIRROR]    Copied from another platform (named) for speed, integrity or
--                 rate-limit reasons. Never edited here except by the sync.
--     [SNAPSHOT]  Copied once at a moment in time and then frozen on purpose
--                 (price paid, details as entered, terms accepted).
--     [LEDGER]    Engine bookkeeping for integrations (queues, webhooks, runs).
--   Columns named *_id that hold text are external IDs (Stripe, GHL,
--   Airtable, Entra). The engine stores the ID, not the external object.
--   See docs/source-of-truth.md for the platform-by-platform reasoning.
--
-- Conventions
--   * bigint identity primary keys; public_id uuid on anything that leaves the
--     building (URLs, Stripe metadata, email links) so internal ids are never
--     guessable.
--   * Money is integer cents (USD). Times are timestamptz (UTC); every place a
--     local time matters carries the IANA time zone alongside.
--   * Status columns are text + CHECK (easy to extend in a migration).
--   * Nothing that a source platform deletes is hard-deleted here: mirrored
--     rows get source_deleted_at so history (registrations) stays valid.
--   * Live and test data never share a database: staging and production are
--     separate databases with their own Stripe mode and GHL sub-account.
-- =============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS citext;      -- case-insensitive emails
CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- fuzzy CS search on names/emails/phones
CREATE EXTENSION IF NOT EXISTS btree_gist;  -- exclusion constraints (room / instructor clashes)

-- -----------------------------------------------------------------------------
-- Helpers
-- -----------------------------------------------------------------------------
CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;

-- Registration statuses that occupy a seat (a pending registration's seat is
-- counted through its seat_hold instead, so it is not listed here).
CREATE FUNCTION is_seat_consuming(p_status text) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT p_status IN ('awaiting_payment', 'confirmed', 'completed', 'no_show')
$$;


-- =============================================================================
-- 1. ACCOUNTS, ROLES AND PERMISSIONS
-- =============================================================================
-- Everyone who signs in has one user account: customers, instructors and
-- office staff alike. There is one sign-in page; the account's roles decide
-- what the person sees, so nobody picks a role when signing in.
--   * A new account starts as a regular user (role 'user').
--   * Buying a seat makes them a student (role 'student', granted automatically
--     when a registration under their email is confirmed).
--   * Instructor and staff roles are only ever granted by an admin. Someone who
--     signs up "as an instructor" stays a regular user until ops verifies them.
-- Booking does not require an account: guests can still check out.
-- Lead / helper (co-) instructor is NOT a role: it is the job an instructor
-- holds in one class (assignments.role).

CREATE TABLE user_accounts (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  public_id         uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  email             citext NOT NULL UNIQUE,
  display_name      text NOT NULL,
  status            text NOT NULL DEFAULT 'invited'
                      CHECK (status IN ('invited', 'active', 'suspended', 'deactivated')),
  -- What they told us at sign-up. Grants nothing by itself.
  signup_intent     text CHECK (signup_intent IN ('student', 'instructor')),
  person_id         bigint,                 -- their GHL-mirrored contact once they book (FK added after people)
  email_verified_at timestamptz,
  last_sign_in_at   timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE user_accounts IS '[OWNED] One row per person who can sign in: customer, instructor or staff. Roles say what they may do.';

-- How an account signs in: email + password (everyone), Microsoft 365 (staff,
-- later), or a one-time emailed link (later). One account may have several.
CREATE TABLE user_identities (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_account_id   bigint NOT NULL REFERENCES user_accounts (id) ON DELETE CASCADE,
  provider          text NOT NULL CHECK (provider IN ('password', 'microsoft', 'email_link')),
  subject           text NOT NULL,          -- lower-cased email, or the Entra object id
  -- Salted scrypt hash (password provider only). The password itself is never stored.
  password_hash     text,
  password_changed_at timestamptz,
  failed_attempts   integer NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
  locked_until      timestamptz,
  tenant_id         uuid,                   -- Entra tenant (microsoft only)
  first_used_at     timestamptz,
  last_used_at      timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (provider, subject),
  UNIQUE (user_account_id, provider),
  CHECK (provider <> 'microsoft' OR tenant_id IS NOT NULL),
  CHECK ((provider = 'password') = (password_hash IS NOT NULL)),
  CHECK (provider = 'microsoft' OR subject = lower(subject))
);
COMMENT ON TABLE user_identities IS '[OWNED / REF: Entra ID] Sign-in methods bound to an account. Passwords only as salted hashes.';

CREATE TABLE roles (
  code              text PRIMARY KEY CHECK (code ~ '^[a-z][a-z_]*$'),
  name              text NOT NULL,
  description       text NOT NULL,
  dashboard_key     text NOT NULL,          -- which dashboard this role opens
  sign_in_method    text NOT NULL CHECK (sign_in_method IN ('password', 'microsoft', 'email_link')),
  is_staff          boolean NOT NULL DEFAULT false,   -- office roles (granted by an admin only)
  is_self_service   boolean NOT NULL DEFAULT false,   -- granted automatically (user, student)
  grants_all        boolean NOT NULL DEFAULT false,   -- admin: every permission, including future ones
  is_scopable       boolean NOT NULL DEFAULT false,   -- may be limited to one city area
  sort_order        integer NOT NULL DEFAULT 0
);
COMMENT ON TABLE roles IS '[OWNED] The roles people can hold, each with its own dashboard.';

CREATE TABLE permissions (
  code              text PRIMARY KEY CHECK (code ~ '^[a-z_]+(\.[a-z_]+)+$'),  -- 'booking.transfer'
  area              text NOT NULL,          -- grouping for the admin screen
  description       text NOT NULL,
  own_records_only  boolean NOT NULL DEFAULT false   -- instructor permissions: only their own classes/pay
);
COMMENT ON TABLE permissions IS '[OWNED] Every action or screen that is access-controlled.';

CREATE TABLE role_permissions (
  role_code         text NOT NULL REFERENCES roles (code) ON DELETE CASCADE,
  permission_code   text NOT NULL REFERENCES permissions (code) ON DELETE CASCADE,
  PRIMARY KEY (role_code, permission_code)
);
COMMENT ON TABLE role_permissions IS '[OWNED] Which permissions each role grants.';

CREATE TABLE user_roles (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_account_id      bigint NOT NULL REFERENCES user_accounts (id) ON DELETE CASCADE,
  role_code            text NOT NULL REFERENCES roles (code),
  scope_metro_id       bigint,              -- null = all city areas (FK added after metros)
  granted_by_user_id   bigint REFERENCES user_accounts (id),
  granted_at           timestamptz NOT NULL DEFAULT now(),
  revoked_at           timestamptz,
  revoked_by_user_id   bigint REFERENCES user_accounts (id),
  CHECK (revoked_at IS NULL OR revoked_by_user_id IS NOT NULL)
);
CREATE UNIQUE INDEX user_roles_one_live ON user_roles (user_account_id, role_code, COALESCE(scope_metro_id, 0))
  WHERE revoked_at IS NULL;
CREATE INDEX user_roles_account_idx ON user_roles (user_account_id) WHERE revoked_at IS NULL;
COMMENT ON TABLE user_roles IS '[OWNED] Role grants, optionally limited to one city area. Revoked grants are kept for history.';

-- Does this account hold this permission (in this city area, if given)?
-- A grant limited to one city area never satisfies an all-areas check.
CREATE FUNCTION user_has_permission(p_user_id bigint, p_permission text, p_metro_id bigint DEFAULT NULL)
RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT EXISTS (
    SELECT 1
      FROM user_accounts a
      JOIN user_roles ur ON ur.user_account_id = a.id AND ur.revoked_at IS NULL
      JOIN roles r       ON r.code = ur.role_code
     WHERE a.id = p_user_id
       AND a.status = 'active'
       AND (ur.scope_metro_id IS NULL OR ur.scope_metro_id = p_metro_id)
       AND (r.grants_all OR EXISTS (SELECT 1 FROM role_permissions rp
                                     WHERE rp.role_code = r.code AND rp.permission_code = p_permission))
  )
$$;

-- Everything an account may do, for building its menus and dashboards.
CREATE VIEW user_effective_permissions AS
SELECT DISTINCT ur.user_account_id, p.code AS permission_code, ur.scope_metro_id, p.own_records_only
FROM user_roles ur
JOIN user_accounts a ON a.id = ur.user_account_id AND a.status = 'active'
JOIN roles r ON r.code = ur.role_code
JOIN permissions p ON r.grants_all
                   OR EXISTS (SELECT 1 FROM role_permissions rp WHERE rp.role_code = r.code AND rp.permission_code = p.code)
WHERE ur.revoked_at IS NULL;

-- Role grants made by a person (granted_by / revoked_by set) are checked here:
--   * the person needs 'user.manage' in all city areas;
--   * only someone holding an all-permissions role (admin) can grant or revoke one;
--   * 'user' and 'student' are granted automatically, never by a person.
-- Grants with no person (sign-up, a paid seat, the first-admin set-up command)
-- are system grants and pass.
CREATE FUNCTION trg_user_roles_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_actor bigint;
  v_role  roles%ROWTYPE;
BEGIN
  IF TG_OP = 'INSERT' THEN
    v_actor := NEW.granted_by_user_id;
  ELSIF OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL THEN
    v_actor := NEW.revoked_by_user_id;
  ELSE
    RETURN NEW;
  END IF;
  IF v_actor IS NULL THEN
    RETURN NEW;
  END IF;
  SELECT * INTO v_role FROM roles WHERE code = NEW.role_code;
  IF v_role.is_self_service THEN
    RAISE EXCEPTION 'role % is granted automatically, not by a person', NEW.role_code
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF NOT user_has_permission(v_actor, 'user.manage') THEN
    RAISE EXCEPTION 'user % lacks permission user.manage', v_actor
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF v_role.grants_all AND NOT EXISTS (
       SELECT 1 FROM user_roles ur
         JOIN roles r ON r.code = ur.role_code
         JOIN user_accounts a ON a.id = ur.user_account_id
        WHERE ur.user_account_id = v_actor AND ur.revoked_at IS NULL
          AND r.grants_all AND a.status = 'active') THEN
    RAISE EXCEPTION 'only an admin can grant or revoke role %', NEW.role_code
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER user_roles_guard BEFORE INSERT OR UPDATE OF revoked_at ON user_roles
  FOR EACH ROW EXECUTE FUNCTION trg_user_roles_guard();

-- Someone must always be able to manage the system: revoking an admin role or
-- switching off an admin's account is refused if no other active admin is left.
CREATE FUNCTION trg_keep_an_admin() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_TABLE_NAME = 'user_roles' THEN
    IF NOT (SELECT grants_all FROM roles WHERE code = OLD.role_code) THEN
      RETURN NULL;
    END IF;
  ELSIF NOT EXISTS (SELECT 1 FROM user_roles ur JOIN roles r ON r.code = ur.role_code
                     WHERE ur.user_account_id = OLD.id AND ur.revoked_at IS NULL AND r.grants_all) THEN
    RETURN NULL;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM user_roles ur
                   JOIN roles r ON r.code = ur.role_code
                   JOIN user_accounts a ON a.id = ur.user_account_id
                  WHERE ur.revoked_at IS NULL AND r.grants_all AND a.status = 'active') THEN
    RAISE EXCEPTION 'at least one active admin must remain'
      USING ERRCODE = 'restrict_violation';
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER user_roles_keep_an_admin AFTER UPDATE OF revoked_at ON user_roles
  FOR EACH ROW WHEN (OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL)
  EXECUTE FUNCTION trg_keep_an_admin();
CREATE TRIGGER user_accounts_keep_an_admin AFTER UPDATE OF status ON user_accounts
  FOR EACH ROW WHEN (OLD.status = 'active' AND NEW.status <> 'active')
  EXECUTE FUNCTION trg_keep_an_admin();


-- =============================================================================
-- 1b. SETTINGS AND POLICIES
-- =============================================================================

CREATE TABLE app_settings (
  key                 text PRIMARY KEY,
  value               jsonb NOT NULL,
  description         text NOT NULL,
  updated_by_user_id bigint REFERENCES user_accounts (id),
  updated_at          timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE app_settings IS '[OWNED] Tunable business numbers (hold minutes, offer window, break-even inputs, alert days, GHL custom field ids). Changing one is a data change, not a deploy.';

CREATE TABLE policy_versions (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind            text NOT NULL CHECK (kind IN (
                    'booking_terms',          -- standard class terms shown at checkout
                    'instructor_course_terms',-- Instructor Training terms
                    'refund_transfer_rules',  -- machine-readable windows and fees for the console
                    'waitlist_terms',         -- 2-hour window, "Offer expired" rule
                    'sms_consent',            -- exact consent wording
                    'privacy_notice',
                    'cookie_notice')),
  version         text NOT NULL,
  title           text NOT NULL,
  body_markdown   text NOT NULL,
  rules           jsonb,              -- e.g. {"refund_full_days_before":7,"free_transfer_hours_before":24,...}
  effective_from  timestamptz NOT NULL,
  effective_to    timestamptz,
  approved_by     text,               -- e.g. 'Jennifer, 2026-10-14'
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (kind, version),
  CHECK (effective_to IS NULL OR effective_to > effective_from)
);
COMMENT ON TABLE policy_versions IS '[OWNED] Versioned terms and refund/transfer rules. Orders, waitlist entries and consents point at the exact version the customer accepted.';


-- =============================================================================
-- 2. CATALOG  (mirrored from Airtable NBC Master Base)
-- =============================================================================

-- Registry of every Airtable record the sync maps. Only a hash of the
-- requested fields is kept (no raw copy): the mapped tables below are the one
-- mirror, and private fields such as access codes live only in the *_private
-- tables. The sync requests only the fields the engine needs, so rental fees,
-- POC payments, tracker approvals etc. never leave Airtable.
CREATE TABLE airtable_records (
  record_id            text PRIMARY KEY,                 -- 'rec...' (globally unique)
  base_id              text NOT NULL,
  table_name           text NOT NULL,                    -- 'Class Schedule', 'Master Locations', 'Course Master'
  fields_hash          bytea NOT NULL,                   -- skip unchanged records cheaply
  airtable_created_at  timestamptz,
  first_seen_at        timestamptz NOT NULL DEFAULT now(),
  last_seen_at         timestamptz NOT NULL DEFAULT now(),
  last_changed_at      timestamptz NOT NULL DEFAULT now(),
  source_deleted_at    timestamptz
);
CREATE INDEX airtable_records_table_idx ON airtable_records (table_name, last_seen_at);
COMMENT ON TABLE airtable_records IS '[LEDGER: Airtable] Which Airtable records the sync has seen, with a hash for change detection and deletion tracking. No field values.';

CREATE TABLE metros (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  slug                 text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),  -- 'richmond-va'
  name                 text NOT NULL,                     -- 'Richmond'
  state_code           char(2) NOT NULL CHECK (state_code ~ '^[A-Z]{2}$'),
  market_code          text NOT NULL UNIQUE,              -- MARKET token in the GHL opportunity name
  time_zone            text NOT NULL,                     -- IANA, e.g. 'America/New_York'
  is_hub               boolean NOT NULL DEFAULT false,    -- DFW and Richmond: 1-week first right of refusal
  booking_system       text NOT NULL DEFAULT 'enrollware'
                         CHECK (booking_system IN ('enrollware', 'engine')),
  cutover_at           timestamptz,                       -- when new bookings moved to the engine
  is_active            boolean NOT NULL DEFAULT true,
  airtable_record_id   text UNIQUE REFERENCES airtable_records (record_id),
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  CHECK (booking_system = 'enrollware' OR cutover_at IS NOT NULL)
);
COMMENT ON TABLE metros IS '[MIRROR: Airtable + OWNED cutover state] A metro is on Enrollware or the engine, never both.';

ALTER TABLE user_roles
  ADD CONSTRAINT user_roles_scope_metro_fk FOREIGN KEY (scope_metro_id) REFERENCES metros (id);

CREATE TABLE sites (
  id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  metro_id              bigint NOT NULL REFERENCES metros (id),
  slug                  text NOT NULL CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),  -- 'henrico'
  name                  text NOT NULL,                    -- 'Henrico (Three Chopt)'
  address_line1         text NOT NULL,
  address_line2         text,                             -- suite
  city                  text NOT NULL,
  state_code            char(2) NOT NULL CHECK (state_code ~ '^[A-Z]{2}$'),
  postal_code           text NOT NULL,
  country_code          char(2) NOT NULL DEFAULT 'US',
  latitude              numeric(9,6) CHECK (latitude BETWEEN -90 AND 90),
  longitude             numeric(9,6) CHECK (longitude BETWEEN -180 AND 180),
  time_zone             text NOT NULL,
  parking_notes_public  text,                             -- safe for public pages
  directions_public     text,                             -- which door / suite, public
  image_url             text,                             -- Event structured-data image
  is_active             boolean NOT NULL DEFAULT true,
  airtable_record_id    text UNIQUE REFERENCES airtable_records (record_id),
  source_synced_at      timestamptz,
  source_deleted_at     timestamptz,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (metro_id, slug),
  UNIQUE (id, metro_id)                                   -- target of sessions' composite FK
);
COMMENT ON TABLE sites IS '[MIRROR: Airtable Master Locations] Public venue details only. Private details are in site_private_details.';

-- Private fields are physically separate so a public page query cannot
-- select them by accident.
CREATE TABLE site_private_details (
  site_id              bigint PRIMARY KEY REFERENCES sites (id) ON DELETE CASCADE,
  access_code          text,
  access_instructions  text,
  poc_name             text,
  poc_phone            text,
  poc_email            citext,
  emergency_contact    text,
  updated_at           timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE site_private_details IS '[MIRROR: Airtable Master Locations] Class-packet data: access code, POC, emergency contact. Signed-in instructor portal and console only; never public pages.';

CREATE TABLE course_families (
  id                     smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code                   text NOT NULL UNIQUE,            -- 'FA_CPR_AED', 'BLS', 'IT'
  name                   text NOT NULL,
  slug                   text NOT NULL UNIQUE,            -- URL segment: 'bls'
  one_instructor_max     integer NOT NULL CHECK (one_instructor_max > 0),  -- 12 / 9 / 6
  min_students_to_run    integer CHECK (min_students_to_run > 0),          -- IT: 5
  default_duration_min   integer CHECK (default_duration_min > 0),
  airtable_group_key     text UNIQUE,                     -- Course Master "Concurrent Group Key"
  image_url              text,                            -- Event structured-data image
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE course_families IS '[MIRROR: Airtable Course Master] A seat pool. Every product in a family sells from the same session seats (A&P and Adult share FA/CPR/AED).';

CREATE TABLE rooms (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  site_id              bigint NOT NULL REFERENCES sites (id),
  name                 text NOT NULL DEFAULT 'Main',
  max_student_count    integer NOT NULL CHECK (max_student_count > 0),  -- caps co-instructor raises
  is_active            boolean NOT NULL DEFAULT true,
  airtable_record_id   text UNIQUE REFERENCES airtable_records (record_id),
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  UNIQUE (site_id, name),
  UNIQUE (id, site_id)                                    -- target of sessions' composite FK
);
COMMENT ON TABLE rooms IS '[MIRROR: Airtable Master Locations] Physical rooms. Max Student Count decides co-instructor vs one-instructor location.';

-- Which course families a room can host (Our Lady of Lourdes: no IT room).
-- Enforced only for rooms that have rows here: a room with no rows may host any family.
CREATE TABLE room_course_families (
  room_id           bigint NOT NULL REFERENCES rooms (id) ON DELETE CASCADE,
  course_family_id  smallint NOT NULL REFERENCES course_families (id),
  PRIMARY KEY (room_id, course_family_id)
);
COMMENT ON TABLE room_course_families IS '[MIRROR: Airtable] Course families a room is equipped for.';

CREATE TABLE products (
  id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  course_family_id      smallint NOT NULL REFERENCES course_families (id),
  code                  text NOT NULL UNIQUE,             -- 'AP_FACPRAED_BLENDED'
  name                  text NOT NULL,                    -- 'Adult & Pediatric FA/CPR/AED – Blended'
  short_name            text NOT NULL,                    -- 'A&P', 'Adult', 'BLS', 'IT'
  delivery_mode         text NOT NULL CHECK (delivery_mode IN ('blended', 'classroom')),
  ghl_course_code       text NOT NULL,                    -- COURSE token in the opportunity name
  base_price_cents      integer NOT NULL CHECK (base_price_cents >= 0),
  stripe_product_id     text UNIQUE,                      -- 'prod_...'
  stripe_price_id       text UNIQUE,                      -- 'price_...' for base_price_cents
  stripe_price_amount_cents integer,                      -- what that Stripe price charges (Stripe prices can't be edited)
  requires_prerequisites boolean NOT NULL DEFAULT false,  -- Instructor Training
  is_refundable         boolean NOT NULL DEFAULT true,
  sort_order            integer NOT NULL DEFAULT 0,
  is_active             boolean NOT NULL DEFAULT true,
  airtable_record_id    text UNIQUE REFERENCES airtable_records (record_id),
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, course_family_id, delivery_mode),          -- target of composite FKs
  CHECK (stripe_price_id IS NULL OR stripe_price_amount_cents = base_price_cents)
);
COMMENT ON TABLE products IS '[MIRROR: Airtable Course Master + REF: Stripe product/price ids] What a customer buys. Each maps to one course family (seat pool).';


-- =============================================================================
-- 3. SESSIONS (classes) AND SEAT INVENTORY
-- =============================================================================

CREATE TABLE sessions (
  id                        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  public_id                 uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  metro_id                  bigint NOT NULL REFERENCES metros (id),    -- denormalised for search
  site_id                   bigint NOT NULL REFERENCES sites (id),
  room_id                   bigint NOT NULL REFERENCES rooms (id),
  course_family_id          smallint NOT NULL REFERENCES course_families (id),
  delivery_mode             text NOT NULL CHECK (delivery_mode IN ('blended', 'classroom')),
  starts_at                 timestamptz NOT NULL,
  ends_at                   timestamptz NOT NULL,
  time_zone                 text NOT NULL,                             -- site's zone at sync time
  local_date                date GENERATED ALWAYS AS ((timezone(time_zone, starts_at))::date) STORED,
  time_range                tstzrange GENERATED ALWAYS AS (tstzrange(starts_at, ends_at, '[)')) STORED,
  slug                      text NOT NULL CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),  -- '2026-10-10-henrico'; add '-0900' when a site runs two of a course that day
  status                    text NOT NULL DEFAULT 'scheduled'
                              CHECK (status IN ('scheduled', 'cancelled', 'completed')),
  is_published              boolean NOT NULL DEFAULT false,            -- Airtable Status includes "Enrollware"
  airtable_status           text[] NOT NULL DEFAULT '{}',              -- raw multi-select, for transparency
  sales_close_at            timestamptz,                               -- normal registration close
  previous_starts_at        timestamptz,                               -- schema.org previousStartDate
  rescheduled_at            timestamptz,
  cancelled_at              timestamptz,
  cancel_reason             text,
  -- Capacity. Seats available = capacity + overrides − holds − sold − external.
  one_instructor_max        integer NOT NULL CHECK (one_instructor_max > 0),  -- 12 / 9 / 6 at sync
  room_max_student_count    integer NOT NULL CHECK (room_max_student_count > 0),
  capacity                  integer NOT NULL CHECK (capacity >= 0),            -- current class max
  -- Why capacity is what it is. The Airtable sync only resets capacity while
  -- the basis is 'one_instructor'; a co-instructor raise survives syncs.
  capacity_basis            text NOT NULL DEFAULT 'one_instructor'
                              CHECK (capacity_basis IN ('one_instructor', 'co_instructor')),
  -- Waitlist rules depend on this (rules doc §2: room space above the
  -- one-instructor max ≥ break-even). Computed by the engine on sync and on
  -- price changes; see open question in docs/database-architecture.md.
  location_type             text NOT NULL DEFAULT 'one_instructor'
                              CHECK (location_type IN ('one_instructor', 'co_instructor')),
  waitlist_limit            integer NOT NULL DEFAULT 0 CHECK (waitlist_limit >= 0),  -- seats-worth of people who may wait
  overflow_of_session_id    bigint REFERENCES sessions (id),           -- second session opened for a waitlist
  -- Pre-portal instructor names exactly as Airtable has them (display only).
  airtable_lead_instructor  text,
  airtable_co_instructor    text,
  roster_exported_at        timestamptz,                               -- last RCLC roster export
  source_synced_at          timestamptz,
  source_deleted_at         timestamptz,
  created_at                timestamptz NOT NULL DEFAULT now(),
  updated_at                timestamptz NOT NULL DEFAULT now(),
  CHECK (ends_at > starts_at),
  CHECK (capacity <= room_max_student_count),
  CHECK (status <> 'cancelled' OR cancelled_at IS NOT NULL),
  CHECK (overflow_of_session_id IS NULL OR overflow_of_session_id <> id),
  CHECK (location_type = 'co_instructor' OR capacity_basis = 'one_instructor'),
  UNIQUE (id, course_family_id, delivery_mode),                       -- target of composite FKs
  UNIQUE (metro_id, course_family_id, slug),
  -- Room, site and metro must agree.
  FOREIGN KEY (room_id, site_id) REFERENCES rooms (id, site_id),
  FOREIGN KEY (site_id, metro_id) REFERENCES sites (id, metro_id),
  -- A room cannot hold two live classes at once.
  CONSTRAINT sessions_no_room_overlap
    EXCLUDE USING gist (room_id WITH =, time_range WITH &&) WHERE (status <> 'cancelled')
);
CREATE INDEX sessions_search_idx ON sessions (metro_id, course_family_id, starts_at)
  WHERE status = 'scheduled' AND is_published;
CREATE INDEX sessions_site_idx ON sessions (site_id, starts_at);
CREATE INDEX sessions_starts_idx ON sessions (starts_at) WHERE status = 'scheduled';
COMMENT ON TABLE sessions IS '[MIRROR: Airtable Class Schedule + OWNED capacity/status] One physical class: one room, one time, one seat pool. One or several Airtable rows (e.g. concurrent A&P and Adult rows) map to it through session_airtable_records.';

-- Airtable Class Schedule rows → session. One row can carry both A&P and
-- Adult (Price + Companion Price); two concurrent rows can merge into one
-- session (Concurrent Group Key). Write-backs (Instructor Assigned,
-- Co-Instructor, Enrollment Snapshot) and the Sync-now button use this map.
CREATE TABLE session_airtable_records (
  airtable_record_id  text PRIMARY KEY REFERENCES airtable_records (record_id),
  session_id          bigint NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
  created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX session_airtable_records_session_idx ON session_airtable_records (session_id);
COMMENT ON TABLE session_airtable_records IS '[REF: Airtable] Which Class Schedule records make up each session.';

CREATE TABLE session_private_details (
  session_id           bigint PRIMARY KEY REFERENCES sessions (id) ON DELETE CASCADE,
  access_code          text,                 -- overrides the site's when a class has its own
  instructor_notes     text,
  poc_override         text,
  updated_at           timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE session_private_details IS '[MIRROR: Airtable] Per-class private packet data. Never on public pages.';

-- Products offered at a session, at that session's price. Prices can vary by
-- location (waitlist rules §4), so the price lives here, not only on products.
CREATE TABLE session_products (
  session_id          bigint NOT NULL,
  product_id          bigint NOT NULL,
  course_family_id    smallint NOT NULL,
  delivery_mode       text NOT NULL,
  price_cents         integer NOT NULL CHECK (price_cents >= 0),
  -- A Stripe price may be used only if it charges exactly price_cents;
  -- otherwise checkout sends price_data on the product's stripe_product_id.
  stripe_price_id     text,
  stripe_price_amount_cents integer,
  is_offered          boolean NOT NULL DEFAULT true,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, product_id),
  FOREIGN KEY (session_id, course_family_id, delivery_mode)
    REFERENCES sessions (id, course_family_id, delivery_mode) ON DELETE CASCADE ON UPDATE CASCADE,
  FOREIGN KEY (product_id, course_family_id, delivery_mode)
    REFERENCES products (id, course_family_id, delivery_mode),
  CHECK (stripe_price_id IS NULL OR stripe_price_amount_cents = price_cents)
);
COMMENT ON TABLE session_products IS '[MIRROR: Airtable Class Schedule/Course Master] Which products a session sells and at what price. Composite FKs guarantee a product is only sold from a session of its own seat pool and delivery mode, and a Stripe price id can never disagree with the displayed price.';

-- Seats taken outside the engine (Enrollware registrations that stay until
-- class day after cutover). They count against capacity.
CREATE TABLE external_seat_reservations (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  session_id    bigint NOT NULL REFERENCES sessions (id),
  source        text NOT NULL DEFAULT 'enrollware' CHECK (source IN ('enrollware', 'manual')),
  external_class_ref text,              -- Enrollware class id
  seats         integer NOT NULL CHECK (seats >= 0),
  counted_at    timestamptz NOT NULL,   -- as-of time of the count
  note          text,
  imported_at   timestamptz NOT NULL DEFAULT now(),
  imported_by_user_id bigint REFERENCES user_accounts (id),
  UNIQUE (session_id, source)
);
COMMENT ON TABLE external_seat_reservations IS '[SNAPSHOT: Enrollware] Seat counts sold in Enrollware before cutover. Counts only; the rosters stay in Enrollware. Recorded even if they overfill a class (they are facts, not engine sales); the engine then sells nothing more and flags it.';

-- Manager-only extra seats over capacity, with a reason.
CREATE TABLE seat_overrides (
  id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  session_id             bigint NOT NULL REFERENCES sessions (id),
  seats                  integer NOT NULL CHECK (seats > 0),
  reason                 text NOT NULL CHECK (length(trim(reason)) > 0),
  approved_by_user_id   bigint NOT NULL REFERENCES user_accounts (id),
  revoked_at             timestamptz,
  revoked_by_user_id    bigint REFERENCES user_accounts (id),
  created_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX seat_overrides_session_idx ON seat_overrides (session_id) WHERE revoked_at IS NULL;
COMMENT ON TABLE seat_overrides IS '[OWNED] Manager capacity overrides. Logged, reasoned, revocable.';


-- =============================================================================
-- 4. PEOPLE (students, buyers) — contact details are GHL's
-- =============================================================================

CREATE TABLE people (
  id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  public_id              uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  first_name             text NOT NULL,
  last_name              text NOT NULL,
  email                  citext,
  phone_e164             text CHECK (phone_e164 ~ '^\+[1-9][0-9]{7,14}$'),
  ghl_contact_id         text UNIQUE,
  ghl_synced_at          timestamptz,
  ghl_last_message_at    timestamptz,        -- from Inbound/OutboundMessage; drives the console's "new message" marker
  first_order_id         bigint,             -- write-once anchor for Source / first-touch (FK added below)
  merged_into_person_id  bigint REFERENCES people (id),  -- contact merged in GHL
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  CHECK (email IS NOT NULL OR phone_e164 IS NOT NULL),
  CHECK (merged_into_person_id IS NULL OR merged_into_person_id <> id)
);
CREATE INDEX people_email_idx ON people (email);
CREATE INDEX people_phone_idx ON people (phone_e164);
CREATE INDEX people_phone_trgm ON people USING gin (phone_e164 gin_trgm_ops);
CREATE INDEX people_name_trgm ON people USING gin ((first_name || ' ' || last_name) gin_trgm_ops);
CREATE INDEX people_email_trgm ON people USING gin ((email::text) gin_trgm_ops);
ALTER TABLE user_accounts
  ADD CONSTRAINT user_accounts_person_fk FOREIGN KEY (person_id) REFERENCES people (id);
CREATE INDEX user_accounts_person_idx ON user_accounts (person_id);

COMMENT ON TABLE people IS '[MIRROR: GHL contact core fields + REF ghl_contact_id] One row per student/buyer, linked 1:1 to a GHL contact. GHL is the system of record for contact details; this copy exists so console search and rosters can join to registrations without calling GHL.';


-- =============================================================================
-- 5. ORDERS, HOLDS, CHECKOUT
-- =============================================================================

CREATE TABLE orders (
  id                        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  public_id                 uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  channel                   text NOT NULL CHECK (channel IN (
                              'web', 'phone', 'invoice', 'waitlist_offer', 'transfer_adjustment', 'admin')),
  status                    text NOT NULL DEFAULT 'open' CHECK (status IN (
                              'open',              -- hold active, checkout not finished
                              'awaiting_payment',  -- invoice / PO, seats confirmed on credit
                              'paid',
                              'partially_refunded',
                              'refunded',
                              'expired',           -- hold lapsed unpaid
                              'cancelled')),
  buyer_person_id           bigint REFERENCES people (id),
  -- Buyer details as entered (an employer may buy seats without attending).
  buyer_first_name          text NOT NULL,
  buyer_last_name           text NOT NULL,
  buyer_email               citext NOT NULL,
  buyer_phone_e164          text CHECK (buyer_phone_e164 ~ '^\+[1-9][0-9]{7,14}$'),
  buyer_company             text,
  po_number                 text,
  invoice_due_on            date,
  currency                  char(3) NOT NULL DEFAULT 'USD',
  subtotal_cents            integer NOT NULL DEFAULT 0 CHECK (subtotal_cents >= 0),
  discount_cents            integer NOT NULL DEFAULT 0 CHECK (discount_cents >= 0),
  tax_cents                 integer NOT NULL DEFAULT 0 CHECK (tax_cents >= 0),
  total_cents               integer NOT NULL DEFAULT 0 CHECK (total_cents >= 0),
  amount_paid_cents         integer NOT NULL DEFAULT 0 CHECK (amount_paid_cents >= 0),
  amount_refunded_cents     integer NOT NULL DEFAULT 0 CHECK (amount_refunded_cents >= 0),
  promotion_code            text,               -- code text the customer typed (as Stripe reports it)
  stripe_promotion_code_id  text,               -- 'promo_...'
  stripe_coupon_id          text,
  stripe_customer_id        text,               -- 'cus_...' only if Stripe created one
  stripe_invoice_id         text UNIQUE,        -- 'in_...' for invoice/PO orders
  terms_version_id          bigint REFERENCES policy_versions (id),
  created_by_user_id       bigint REFERENCES user_accounts (id),  -- phone / invoice / admin orders
  -- Ad tracking: one shared event id so browser + server conversions dedupe.
  conversion_event_id       uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  tracking_consent          jsonb,              -- banner state at checkout: {"analytics":true,"ads":false}
  ga_client_id              text,               -- GA4 client id, for the server-side purchase event
  client_ip                 inet,               -- for Meta CAPI; purged after the retention window
  client_user_agent         text,               -- same
  livemode                  boolean NOT NULL,   -- guard against test/live mix-ups
  created_at                timestamptz NOT NULL DEFAULT now(),
  paid_at                   timestamptz,
  expired_at                timestamptz,
  cancelled_at              timestamptz,
  updated_at                timestamptz NOT NULL DEFAULT now(),
  CHECK (total_cents = subtotal_cents - discount_cents + tax_cents),
  CHECK (amount_refunded_cents <= amount_paid_cents),
  CHECK (status <> 'awaiting_payment' OR invoice_due_on IS NOT NULL),
  CHECK (status <> 'paid' OR paid_at IS NOT NULL),
  CHECK (channel NOT IN ('phone', 'invoice', 'admin') OR created_by_user_id IS NOT NULL)
);
CREATE INDEX orders_buyer_email_idx ON orders (buyer_email);
CREATE INDEX orders_status_idx ON orders (status, created_at);
CREATE INDEX orders_buyer_person_idx ON orders (buyer_person_id);
COMMENT ON TABLE orders IS '[OWNED + REF Stripe ids] One checkout / invoice. Amounts are the engine''s record of what was charged; Stripe holds the card and payment objects.';

ALTER TABLE people
  ADD CONSTRAINT people_first_order_fk FOREIGN KEY (first_order_id) REFERENCES orders (id);

-- First-touch and last-touch attribution captured from the visitor's
-- first-party cookie when the seat was held.
CREATE TABLE order_attribution (
  order_id        bigint NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
  touch           text NOT NULL CHECK (touch IN ('first', 'last')),
  utm_source      text,
  utm_medium      text,
  utm_campaign    text,
  utm_content     text,
  utm_term        text,
  gclid           text,
  gbraid          text,
  wbraid          text,
  fbclid          text,
  fbp             text,
  fbc             text,
  landing_page    text,
  referrer        text,
  touched_at      timestamptz,
  PRIMARY KEY (order_id, touch)
);
CREATE INDEX order_attribution_campaign_idx ON order_attribution (utm_campaign) WHERE touch = 'last';
COMMENT ON TABLE order_attribution IS '[SNAPSHOT] Where the buyer came from, frozen at purchase. Feeds GHL Source fields, Meta CAPI and Google Ads; GA4 keeps the browsing journey.';

CREATE TABLE seat_holds (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  public_id        uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),   -- goes in Stripe metadata
  session_id       bigint NOT NULL REFERENCES sessions (id),
  order_id         bigint REFERENCES orders (id),
  kind             text NOT NULL CHECK (kind IN ('checkout', 'phone_link', 'waitlist_offer')),
  quantity         integer NOT NULL CHECK (quantity > 0),
  status           text NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'converted', 'released', 'expired')),
  expires_at       timestamptz NOT NULL,
  released_at      timestamptz,
  release_reason   text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CHECK (kind = 'waitlist_offer' OR order_id IS NOT NULL),
  CHECK (status = 'active' OR released_at IS NOT NULL)
);
CREATE INDEX seat_holds_session_active_idx ON seat_holds (session_id) WHERE status = 'active';
CREATE INDEX seat_holds_expiry_idx ON seat_holds (expires_at) WHERE status = 'active';
CREATE INDEX seat_holds_order_idx ON seat_holds (order_id);
COMMENT ON TABLE seat_holds IS '[OWNED] Temporary seat reservations: 15-minute checkout, phone payment link, 2-hour waitlist offer. Active, unexpired holds count against capacity.';

CREATE TABLE checkout_sessions (
  id                          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  order_id                    bigint NOT NULL REFERENCES orders (id),
  seat_hold_id                bigint NOT NULL REFERENCES seat_holds (id),
  stripe_checkout_session_id  text NOT NULL UNIQUE,     -- 'cs_...'
  url                         text,
  status                      text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'complete', 'expired')),
  stripe_expires_at           timestamptz NOT NULL,     -- ≥ 30 min (Stripe minimum)
  expired_by_engine_at        timestamptz,              -- engine called expire at 15 min (D2)
  completed_at                timestamptz,
  created_at                  timestamptz NOT NULL DEFAULT now(),
  updated_at                  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX checkout_sessions_order_idx ON checkout_sessions (order_id);
CREATE INDEX checkout_sessions_open_idx ON checkout_sessions (seat_hold_id) WHERE status = 'open';
COMMENT ON TABLE checkout_sessions IS '[REF: Stripe Checkout] Stripe session ids per order attempt, so a webhook can be matched to its hold and the engine can expire it.';


-- =============================================================================
-- 6. WAITLIST
-- =============================================================================

CREATE TABLE waitlist_entries (
  id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  public_id              uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  session_id             bigint NOT NULL REFERENCES sessions (id),
  preferred_product_id   bigint NOT NULL,                  -- A&P or Adult
  person_id              bigint REFERENCES people (id),
  first_name             text NOT NULL,
  last_name              text NOT NULL,
  email                  citext NOT NULL,
  phone_e164             text NOT NULL CHECK (phone_e164 ~ '^\+[1-9][0-9]{7,14}$'),
  party_size             integer NOT NULL DEFAULT 1 CHECK (party_size > 0),
  status                 text NOT NULL DEFAULT 'waiting' CHECK (status IN (
                           'waiting', 'offered', 'offer_expired', 'converted', 'removed', 'closed_no_seat')),
  terms_version_id       bigint REFERENCES policy_versions (id),
  sms_consent_id         bigint,                           -- FK added after consent_records
  ghl_opportunity_id     text UNIQUE,                      -- Waitlisted-stage opportunity
  joined_at              timestamptz NOT NULL DEFAULT now(),
  status_changed_at      timestamptz NOT NULL DEFAULT now(),
  no_seat_notice_sent_at timestamptz,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (session_id, preferred_product_id) REFERENCES session_products (session_id, product_id)
);
-- One live entry per email per class; a removed/closed student may rejoin.
CREATE UNIQUE INDEX waitlist_entries_one_live ON waitlist_entries (session_id, email)
  WHERE status IN ('waiting', 'offered', 'offer_expired');
CREATE INDEX waitlist_entries_email_idx ON waitlist_entries (email);
CREATE INDEX waitlist_entries_person_idx ON waitlist_entries (person_id);
-- Waitlist order = joined_at, then id.
CREATE INDEX waitlist_queue_idx ON waitlist_entries (session_id, joined_at, id)
  WHERE status IN ('waiting', 'offered');
COMMENT ON TABLE waitlist_entries IS '[OWNED] Free waitlist, one shared A&P/Adult list per session. "Offer expired" students keep their place but are not re-offered automatically.';

CREATE TABLE waitlist_offers (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  public_id            uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  waitlist_entry_id    bigint NOT NULL REFERENCES waitlist_entries (id),
  offered_session_id   bigint NOT NULL REFERENCES sessions (id),  -- original, or a second session
  seats                integer NOT NULL CHECK (seats > 0),
  trigger              text NOT NULL CHECK (trigger IN ('seat_freed', 'co_instructor_added', 'second_session', 'manual')),
  offered_by_user_id  bigint REFERENCES user_accounts (id),  -- 'manual': CS re-offer after "Offer expired" 
  status               text NOT NULL DEFAULT 'queued'
                         CHECK (status IN ('queued', 'sent', 'accepted', 'expired', 'withdrawn')),
  send_after           timestamptz NOT NULL,      -- 8 AM–6 PM class local time
  sent_at              timestamptz,
  expires_at           timestamptz,               -- 2-hour hold from sending, or the cut-off; null for second-session links
  seat_hold_id         bigint UNIQUE REFERENCES seat_holds (id),
  order_id             bigint REFERENCES orders (id),
  accepted_at          timestamptz,
  expired_at           timestamptz,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  -- The seat is held from the moment the offer is queued (even overnight),
  -- so an offered seat never goes back on public sale (rules §6).
  CHECK (trigger = 'second_session' OR seat_hold_id IS NOT NULL),
  CHECK (trigger <> 'manual' OR offered_by_user_id IS NOT NULL)
);
CREATE UNIQUE INDEX waitlist_offers_one_open ON waitlist_offers (waitlist_entry_id)
  WHERE status IN ('queued', 'sent');
CREATE INDEX waitlist_offers_dispatch_idx ON waitlist_offers (send_after) WHERE status = 'queued';
CREATE INDEX waitlist_offers_expiry_idx ON waitlist_offers (expires_at) WHERE status = 'sent';
COMMENT ON TABLE waitlist_offers IS '[OWNED] Each seat offer to a waitlisted student, with its hold and timing.';


-- =============================================================================
-- 7. REGISTRATIONS (one per attendee seat = one GHL opportunity)
-- =============================================================================

CREATE TABLE registrations (
  id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  public_id               uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  order_id                bigint NOT NULL REFERENCES orders (id),
  seat_hold_id            bigint REFERENCES seat_holds (id),
  session_id              bigint NOT NULL,                 -- changes on transfer; history in registration_transfers
  product_id              bigint NOT NULL,
  person_id               bigint REFERENCES people (id),   -- linked when confirmed and upserted to GHL
  waitlist_entry_id       bigint REFERENCES waitlist_entries (id),
  -- Attendee details as entered at booking (edited only through the console).
  attendee_first_name     text NOT NULL,
  attendee_last_name      text NOT NULL,
  attendee_email          citext NOT NULL,
  attendee_phone_e164     text CHECK (attendee_phone_e164 ~ '^\+[1-9][0-9]{7,14}$'),
  status                  text NOT NULL DEFAULT 'pending' CHECK (status IN (
                            'pending',          -- in an active hold, not paid
                            'awaiting_payment', -- invoice / PO: seat confirmed, money due
                            'confirmed',        -- paid
                            'displaced',        -- NBC cancelled the class; awaiting transfer or refund
                            'completed',        -- attended, marked Complete
                            'no_show',
                            'cancelled',        -- cancelled (refunded per policy, or not)
                            'forfeited',        -- fee lost: under-24-hour notice, late arrival, weather no-show
                            'expired')),        -- hold lapsed unpaid
  cancel_reason_code      text CHECK (cancel_reason_code IN (
                            'customer_request', 'late_notice', 'late_arrival', 'nbc_low_enrollment',
                            'nbc_weather', 'nbc_other', 'duplicate', 'admin')),
  rebooked_from_registration_id bigint REFERENCES registrations (id),  -- 50%-off re-registration after a forfeit
  unit_price_cents        integer NOT NULL CHECK (unit_price_cents >= 0),
  discount_cents          integer NOT NULL DEFAULT 0 CHECK (discount_cents >= 0),
  amount_paid_cents       integer NOT NULL DEFAULT 0 CHECK (amount_paid_cents >= 0),
  amount_refunded_cents   integer NOT NULL DEFAULT 0 CHECK (amount_refunded_cents >= 0),
  reg_type                text CHECK (reg_type IN ('new', 'existing')),   -- GHL "Reg Type"
  prerequisites_done_at   timestamptz,                     -- Instructor Training prerequisites received
  checked_in_at           timestamptz,
  ghl_opportunity_id      text UNIQUE,
  ghl_stage_pushed        text,                            -- last stage the engine wrote (drift detection)
  ghl_synced_at           timestamptz,
  confirmed_at            timestamptz,
  cancelled_at            timestamptz,
  cancel_reason           text,
  completed_at            timestamptz,
  created_at              timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (session_id, product_id) REFERENCES session_products (session_id, product_id),
  -- Each named attendee needs their own email, or GHL would merge them into one contact.
  UNIQUE (order_id, attendee_email),
  CHECK (amount_refunded_cents <= amount_paid_cents),
  CHECK (discount_cents <= unit_price_cents),
  CHECK (status <> 'pending' OR seat_hold_id IS NOT NULL),
  CHECK (status NOT IN ('confirmed', 'completed', 'no_show') OR confirmed_at IS NOT NULL),
  CHECK (status NOT IN ('cancelled', 'forfeited') OR (cancelled_at IS NOT NULL AND cancel_reason_code IS NOT NULL)),
  CHECK (rebooked_from_registration_id IS NULL OR rebooked_from_registration_id <> id)
);
CREATE INDEX registrations_session_idx ON registrations (session_id, status);
CREATE INDEX registrations_order_idx ON registrations (order_id);
CREATE INDEX registrations_person_idx ON registrations (person_id);
CREATE INDEX registrations_hold_idx ON registrations (seat_hold_id);
CREATE INDEX registrations_email_idx ON registrations (attendee_email);
CREATE INDEX registrations_phone_idx ON registrations (attendee_phone_e164);
CREATE INDEX registrations_name_trgm ON registrations
  USING gin ((attendee_first_name || ' ' || attendee_last_name) gin_trgm_ops);
-- The same person cannot hold two live seats in one class.
CREATE UNIQUE INDEX registrations_one_seat_per_person ON registrations (session_id, person_id)
  WHERE person_id IS NOT NULL AND status IN ('awaiting_payment', 'confirmed', 'completed', 'no_show');
COMMENT ON TABLE registrations IS '[OWNED + REF ghl_opportunity_id] One seat for one named attendee: the enrollment. Maps 1:1 to a GHL opportunity whose stage the engine drives. attendee_* is the name on the certificate and the details as booked (engine-owned, edited only in the console); live contact details are read through person_id → people (GHL mirror). Seat-consuming statuses: awaiting_payment, confirmed, completed, no_show (pending seats count via their hold). Transfers used per policy are counted from registration_transfers.';

CREATE TABLE registration_transfers (
  id                       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  registration_id          bigint NOT NULL REFERENCES registrations (id),
  from_session_id          bigint NOT NULL REFERENCES sessions (id),
  from_product_id          bigint NOT NULL REFERENCES products (id),
  to_session_id            bigint NOT NULL REFERENCES sessions (id),
  to_product_id            bigint NOT NULL REFERENCES products (id),
  reason                   text NOT NULL CHECK (reason IN (
                             'customer_request', 'class_cancelled', 'class_rescheduled', 'admin_correction')),
  via                      text NOT NULL CHECK (via IN ('console', 'bulk', 'customer_link')),
  bulk_batch_id            uuid,                       -- groups a cancelled class's bulk transfer
  price_difference_cents   integer NOT NULL DEFAULT 0, -- + charged to customer, − credited
  fee_cents                integer NOT NULL DEFAULT 0 CHECK (fee_cents >= 0),
  policy_rule              text,                       -- which rule applied, e.g. 'IT_7_13_DAYS_FEE'
  policy_version_id        bigint REFERENCES policy_versions (id),
  settlement_order_id      bigint REFERENCES orders (id),   -- extra charge
  performed_by_user_id    bigint REFERENCES user_accounts (id),
  created_at               timestamptz NOT NULL DEFAULT now(),
  CHECK (from_session_id <> to_session_id OR from_product_id <> to_product_id),
  CHECK (via = 'customer_link' OR performed_by_user_id IS NOT NULL)
);
CREATE INDEX registration_transfers_reg_idx ON registration_transfers (registration_id);
CREATE INDEX registration_transfers_from_idx ON registration_transfers (from_session_id);
CREATE INDEX registration_transfers_to_idx ON registration_transfers (to_session_id);
COMMENT ON TABLE registration_transfers IS '[OWNED] Every move of a registration between classes, with the price difference, fee and rule used. The registration keeps its GHL opportunity across transfers.';


-- =============================================================================
-- 8. MONEY: PAYMENTS AND REFUNDS
-- =============================================================================

CREATE TABLE payments (
  id                        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  order_id                  bigint NOT NULL REFERENCES orders (id),
  method                    text NOT NULL CHECK (method IN ('card', 'invoice', 'manual')),
  status                    text NOT NULL CHECK (status IN ('succeeded', 'pending', 'failed', 'disputed')),
  amount_cents              integer NOT NULL CHECK (amount_cents > 0),
  currency                  char(3) NOT NULL DEFAULT 'USD',
  stripe_payment_intent_id  text UNIQUE,   -- 'pi_...'
  stripe_charge_id          text UNIQUE,   -- 'ch_...'
  stripe_invoice_id         text,
  received_at               timestamptz,
  disputed_at               timestamptz,
  manual_reference          text,          -- cheque no. etc. for 'manual'
  recorded_by_user_id      bigint REFERENCES user_accounts (id),
  livemode                  boolean NOT NULL,
  created_at                timestamptz NOT NULL DEFAULT now(),
  updated_at                timestamptz NOT NULL DEFAULT now(),
  CHECK (method <> 'manual' OR recorded_by_user_id IS NOT NULL)
);
CREATE INDEX payments_order_idx ON payments (order_id);
CREATE INDEX payments_invoice_idx ON payments (stripe_invoice_id);
COMMENT ON TABLE payments IS '[REF: Stripe + OWNED amounts] Money received against an order. Card data never reaches the engine.';

CREATE TABLE refunds (
  id                         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  order_id                   bigint NOT NULL REFERENCES orders (id),
  payment_id                 bigint REFERENCES payments (id),
  registration_id            bigint REFERENCES registrations (id),
  transfer_id                bigint REFERENCES registration_transfers (id),
  amount_cents               integer NOT NULL CHECK (amount_cents > 0),
  reason                     text NOT NULL CHECK (reason IN (
                               'customer_cancel', 'class_cancelled', 'transfer_credit',
                               'late_payment_no_seat', 'duplicate', 'goodwill', 'other')),
  source                     text NOT NULL DEFAULT 'console'
                               CHECK (source IN ('console', 'engine_auto', 'stripe_dashboard')),
  policy_rule                text,
  policy_version_id          bigint REFERENCES policy_versions (id),
  status                     text NOT NULL DEFAULT 'requested' CHECK (status IN (
                               'requested', 'approved', 'processing', 'succeeded', 'failed', 'rejected')),
  requires_manager_approval  boolean NOT NULL DEFAULT false,
  requested_by_user_id      bigint REFERENCES user_accounts (id),
  approved_by_user_id       bigint REFERENCES user_accounts (id),
  approved_at                timestamptz,
  stripe_refund_id           text UNIQUE,   -- 're_...'
  processed_at               timestamptz,
  failure_reason             text,
  note                       text,
  created_at                 timestamptz NOT NULL DEFAULT now(),
  updated_at                 timestamptz NOT NULL DEFAULT now(),
  CHECK (NOT requires_manager_approval
         OR status IN ('requested', 'rejected')
         OR approved_by_user_id IS NOT NULL),
  CHECK (source <> 'console' OR requested_by_user_id IS NOT NULL)
);
CREATE INDEX refunds_order_idx ON refunds (order_id);
CREATE INDEX refunds_registration_idx ON refunds (registration_id);
CREATE INDEX refunds_pending_approval_idx ON refunds (created_at)
  WHERE status = 'requested' AND requires_manager_approval;
COMMENT ON TABLE refunds IS '[OWNED + REF stripe_refund_id] Every refund, whether from the console, automatic (late payment, no seat), or done directly in the Stripe Dashboard (caught by webhook).';

ALTER TABLE registration_transfers
  ADD COLUMN settlement_refund_id bigint REFERENCES refunds (id);


-- =============================================================================
-- 9. INSTRUCTORS (portal: Jan 11 – Feb 5 build)
-- =============================================================================

CREATE TABLE instructors (
  id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  public_id             uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  first_name            text NOT NULL,
  last_name             text NOT NULL,
  user_account_id       bigint UNIQUE REFERENCES user_accounts (id),  -- set when invited to the portal
  email                 citext NOT NULL UNIQUE,         -- contact address (sign-in lives on the account)
  phone_e164            text CHECK (phone_e164 ~ '^\+[1-9][0-9]{7,14}$'),
  status                text NOT NULL DEFAULT 'active'
                          CHECK (status IN ('onboarding', 'active', 'inactive', 'suspended')),
  home_metro_id         bigint REFERENCES metros (id),
  hub_metro_id          bigint REFERENCES metros (id),  -- member of a HUB (first right of refusal)
  hub_priority          text CHECK (hub_priority IN ('primary', 'secondary')),
  performance_tier      smallint CHECK (performance_tier BETWEEN 1 AND 5),  -- 1 = top
  reliability_score     numeric(5,2) CHECK (reliability_score BETWEEN 0 AND 100),
  home_latitude         numeric(9,6),
  home_longitude        numeric(9,6),
  travel_radius_miles   integer CHECK (travel_radius_miles > 0),
  airtable_record_id    text UNIQUE REFERENCES airtable_records (record_id),
  qbo_vendor_id         text,                           -- QuickBooks Online vendor reference
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  CHECK ((hub_metro_id IS NULL) = (hub_priority IS NULL))
);
COMMENT ON TABLE instructors IS '[OWNED from portal launch; MIRROR: Airtable before] Teaching profile used for eligibility, ranking and pay. Linked to a user account (role "instructor") once invited to the portal. Lead vs helper is per class, in assignments.role.';

CREATE TABLE instructor_certs (
  id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  instructor_id          bigint NOT NULL REFERENCES instructors (id) ON DELETE CASCADE,
  cert_type              text NOT NULL,                 -- e.g. 'ARC_BLS_INSTRUCTOR'
  cert_number            text,
  issued_on              date,
  expires_on             date NOT NULL,
  verified_by_user_id   bigint REFERENCES user_accounts (id),
  verified_at            timestamptz,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  CHECK (issued_on IS NULL OR expires_on > issued_on)
);
CREATE INDEX instructor_certs_lookup_idx ON instructor_certs (instructor_id, cert_type, expires_on);
COMMENT ON TABLE instructor_certs IS '[OWNED] ARC certifications. A lead or co-instructor needs one valid on the class date.';

-- Which certification qualifies someone to teach which course family.
CREATE TABLE cert_type_course_families (
  cert_type         text NOT NULL,
  course_family_id  smallint NOT NULL REFERENCES course_families (id),
  PRIMARY KEY (cert_type, course_family_id)
);
COMMENT ON TABLE cert_type_course_families IS '[OWNED] Eligibility map: cert type → course families.';

CREATE TABLE instructor_equipment (
  instructor_id     bigint NOT NULL REFERENCES instructors (id) ON DELETE CASCADE,
  course_family_id  smallint NOT NULL REFERENCES course_families (id),
  verified_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (instructor_id, course_family_id)
);
COMMENT ON TABLE instructor_equipment IS '[OWNED] "Has the equipment" eligibility check, per course family.';

CREATE TABLE availability_blocks (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  instructor_id   bigint NOT NULL REFERENCES instructors (id) ON DELETE CASCADE,
  during          tstzrange NOT NULL CHECK (NOT isempty(during)),
  note            text,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX availability_blocks_idx ON availability_blocks USING gist (instructor_id, during);
COMMENT ON TABLE availability_blocks IS '[OWNED] Times an instructor says they can teach (recurring patterns are expanded into rows by the portal).';

CREATE TABLE time_off (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  instructor_id   bigint NOT NULL REFERENCES instructors (id) ON DELETE CASCADE,
  during          tstzrange NOT NULL CHECK (NOT isempty(during)),
  reason          text,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX time_off_idx ON time_off USING gist (instructor_id, during);
COMMENT ON TABLE time_off IS '[OWNED] Blocks an instructor from offers.';

CREATE TABLE rate_card (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  course_family_id  smallint NOT NULL REFERENCES course_families (id),
  role              text NOT NULL CHECK (role IN ('lead', 'co')),
  rate_cents        integer NOT NULL CHECK (rate_cents >= 0),
  effective         daterange NOT NULL CHECK (NOT isempty(effective)),
  created_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT rate_card_no_overlap
    EXCLUDE USING gist (course_family_id WITH =, role WITH =, effective WITH &&)
);
COMMENT ON TABLE rate_card IS '[OWNED] Set pay per class by course family and role. No rate bidding.';

CREATE TABLE class_offers (
  id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  session_id            bigint NOT NULL REFERENCES sessions (id),
  role                  text NOT NULL CHECK (role IN ('lead', 'co')),
  rate_cents            integer NOT NULL CHECK (rate_cents >= 0),
  priority_until        timestamptz,             -- end of the HUB first-right week (null = open board now)
  status                text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'awarded', 'cancelled')),
  opened_at             timestamptz NOT NULL DEFAULT now(),
  closed_at             timestamptz,
  created_by_user_id   bigint REFERENCES user_accounts (id),
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX class_offers_one_open ON class_offers (session_id, role) WHERE status = 'open';
COMMENT ON TABLE class_offers IS '[OWNED] A teaching slot posted for requests (HUB priority window, then open board).';

CREATE TABLE class_offer_requests (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  class_offer_id   bigint NOT NULL REFERENCES class_offers (id),
  instructor_id    bigint NOT NULL REFERENCES instructors (id),
  status           text NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'awarded', 'declined', 'withdrawn')),
  rank_score       numeric(8,3),             -- tier, distance, fair share of hours, reliability
  rank_inputs      jsonb,                    -- the numbers behind the score, for ops to see
  requested_at     timestamptz NOT NULL DEFAULT now(),
  decided_at       timestamptz,
  UNIQUE (class_offer_id, instructor_id)
);
COMMENT ON TABLE class_offer_requests IS '[OWNED] "Request this class" taps, with the ranking ops sees before approving.';

CREATE TABLE assignments (
  id                       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  session_id               bigint NOT NULL REFERENCES sessions (id),
  instructor_id            bigint NOT NULL REFERENCES instructors (id),
  role                     text NOT NULL CHECK (role IN ('lead', 'co')),
  status                   text NOT NULL DEFAULT 'assigned'
                             CHECK (status IN ('assigned', 'released', 'removed', 'completed')),
  source                   text NOT NULL CHECK (source IN ('airtable', 'portal', 'console')),
  class_offer_request_id   bigint UNIQUE REFERENCES class_offer_requests (id),
  rate_cents               integer CHECK (rate_cents >= 0),   -- snapshot from the offer / rate card
  awarded_by_user_id      bigint REFERENCES user_accounts (id),
  awarded_at               timestamptz NOT NULL DEFAULT now(),
  released_at              timestamptz,
  released_by              text CHECK (released_by IN ('instructor', 'ops')),
  release_reason           text,
  session_range            tstzrange NOT NULL,                -- kept equal to the session's time_range
  airtable_written_back_at timestamptz,
  created_at               timestamptz NOT NULL DEFAULT now(),
  updated_at               timestamptz NOT NULL DEFAULT now(),
  CHECK (status NOT IN ('released', 'removed') OR released_at IS NOT NULL),
  -- An instructor cannot teach two overlapping classes.
  CONSTRAINT assignments_no_instructor_clash
    EXCLUDE USING gist (instructor_id WITH =, session_range WITH &&) WHERE (status = 'assigned')
);
-- One lead and at most one co-instructor per class.
CREATE UNIQUE INDEX assignments_one_per_role ON assignments (session_id, role) WHERE status = 'assigned';
CREATE INDEX assignments_instructor_idx ON assignments (instructor_id, status);
COMMENT ON TABLE assignments IS '[OWNED from portal launch; MIRROR: Airtable before] Who teaches a class, as lead or co-instructor. Written back to Airtable.';

CREATE TABLE pay_records (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  assignment_id   bigint NOT NULL UNIQUE REFERENCES assignments (id),
  instructor_id   bigint NOT NULL REFERENCES instructors (id),
  session_id      bigint NOT NULL REFERENCES sessions (id),
  role            text NOT NULL CHECK (role IN ('lead', 'co')),
  amount_cents    integer NOT NULL CHECK (amount_cents >= 0),
  status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'exported', 'paid', 'void')),
  qbo_reference   text,              -- bill / line id once the billing pipeline has it
  exported_at     timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX pay_records_status_idx ON pay_records (status, created_at);
CREATE INDEX pay_records_instructor_idx ON pay_records (instructor_id, created_at);
COMMENT ON TABLE pay_records IS '[OWNED until export; then REF: QuickBooks Online] One pay record per instructor per completed class.';

CREATE TABLE attendance_marks (
  id                       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  registration_id          bigint NOT NULL REFERENCES registrations (id),
  mark                     text NOT NULL CHECK (mark IN ('checked_in', 'complete', 'no_show', 'cleared')),
  marked_by_assignment_id  bigint REFERENCES assignments (id),   -- which instructor (lead or co)
  marked_by_user_id       bigint REFERENCES user_accounts (id),
  marked_at                timestamptz NOT NULL DEFAULT now(),
  CHECK (num_nonnulls(marked_by_assignment_id, marked_by_user_id) = 1)   -- instructors mark through their assignment
);
CREATE INDEX attendance_marks_reg_idx ON attendance_marks (registration_id, marked_at);
COMMENT ON TABLE attendance_marks IS '[OWNED] Append-only check-in and Complete / No Show marks; each records who made it. registrations.status holds the current outcome.';


-- =============================================================================
-- 10. CONSENT AND NOTES
-- =============================================================================

CREATE TABLE consent_records (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  person_id           bigint REFERENCES people (id),
  email               citext,
  phone_e164          text,
  kind                text NOT NULL CHECK (kind IN (
                        'sms', 'email_marketing', 'terms', 'analytics_cookies', 'ad_cookies', 'enhanced_conversions')),
  granted             boolean NOT NULL,
  policy_version_id   bigint REFERENCES policy_versions (id),   -- exact wording shown
  source              text NOT NULL CHECK (source IN ('waitlist_form', 'checkout', 'cookie_banner', 'console')),
  order_id            bigint REFERENCES orders (id),
  waitlist_entry_id   bigint REFERENCES waitlist_entries (id),
  ip                  inet,
  user_agent          text,
  captured_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX consent_records_person_idx ON consent_records (person_id, kind, captured_at);
COMMENT ON TABLE consent_records IS '[OWNED] Proof of consent as captured (who, what wording, when, where). GHL gets the resulting DND/opt-in state; the evidence stays here.';

ALTER TABLE waitlist_entries
  ADD CONSTRAINT waitlist_entries_sms_consent_fk FOREIGN KEY (sms_consent_id) REFERENCES consent_records (id);

CREATE TABLE notes (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  person_id         bigint REFERENCES people (id),
  registration_id   bigint REFERENCES registrations (id),
  order_id          bigint REFERENCES orders (id),
  session_id        bigint REFERENCES sessions (id),
  author_user_id   bigint NOT NULL REFERENCES user_accounts (id),
  body              text NOT NULL CHECK (length(trim(body)) > 0),
  created_at        timestamptz NOT NULL DEFAULT now(),
  CHECK (num_nonnulls(person_id, registration_id, order_id, session_id) >= 1)
);
CREATE INDEX notes_person_idx ON notes (person_id);
CREATE INDEX notes_registration_idx ON notes (registration_id);
CREATE INDEX notes_order_idx ON notes (order_id);
CREATE INDEX notes_session_idx ON notes (session_id);
COMMENT ON TABLE notes IS '[OWNED] CS notes about bookings. Conversations (texts/emails) stay in GHL.';


-- =============================================================================
-- 11. SIGN-IN: SESSIONS AND ONE-TIME LINKS
-- =============================================================================

CREATE TABLE auth_sessions (
  token_hash       bytea PRIMARY KEY,               -- sha256 of the cookie value
  user_account_id  bigint NOT NULL REFERENCES user_accounts (id) ON DELETE CASCADE,
  identity_id      bigint REFERENCES user_identities (id),  -- which sign-in method was used
  active_role_code text REFERENCES roles (code),     -- dashboard currently shown (multi-role users switch)
  created_at       timestamptz NOT NULL DEFAULT now(),
  expires_at       timestamptz NOT NULL,
  last_seen_at     timestamptz NOT NULL DEFAULT now(),
  revoked_at       timestamptz,
  ip               inet,
  user_agent       text
);
CREATE INDEX auth_sessions_expiry_idx ON auth_sessions (expires_at);
CREATE INDEX auth_sessions_account_idx ON auth_sessions (user_account_id) WHERE revoked_at IS NULL;
COMMENT ON TABLE auth_sessions IS '[OWNED] Server-side sign-in sessions (revocable). Staff sign in through Microsoft 365; instructors through emailed links.';

CREATE TABLE action_tokens (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  purpose            text NOT NULL CHECK (purpose IN (
                       'sign_in_link',        -- one-time sign-in link (instructors)
                       'waitlist_offer',      -- pay-for-your-offered-seat link
                       'rebook_choice',       -- pick-a-new-date link after a cancelled class
                       'phone_checkout')),    -- CS payment link landing page
  token_hash         bytea NOT NULL UNIQUE,   -- only the hash is stored
  user_account_id    bigint REFERENCES user_accounts (id),
  registration_id    bigint REFERENCES registrations (id),
  waitlist_offer_id  bigint REFERENCES waitlist_offers (id),
  order_id           bigint REFERENCES orders (id),
  expires_at         timestamptz NOT NULL,
  used_at            timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  CHECK (purpose <> 'sign_in_link' OR user_account_id IS NOT NULL),
  CHECK (purpose <> 'waitlist_offer'     OR waitlist_offer_id IS NOT NULL),
  CHECK (purpose <> 'rebook_choice'      OR registration_id IS NOT NULL),
  CHECK (purpose <> 'phone_checkout'     OR order_id IS NOT NULL)
);
COMMENT ON TABLE action_tokens IS '[OWNED] Hashed one-time links sent by email or text.';


-- =============================================================================
-- 12. INTEGRATION LEDGER
-- =============================================================================

-- GHL OAuth tokens. They live in the database, not in Replit Secrets, because
-- every refresh returns a new refresh token that must be saved. Encrypted by
-- the app with a key from Secrets.
CREATE TABLE oauth_connections (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  provider             text NOT NULL CHECK (provider IN ('ghl')),
  account_ref          text NOT NULL,                -- GHL locationId (sub-account)
  company_ref          text,                         -- GHL companyId (agency)
  access_token_enc     bytea NOT NULL,
  refresh_token_enc    bytea NOT NULL,
  key_version          smallint NOT NULL DEFAULT 1,  -- which encryption key
  scopes               text[] NOT NULL,
  access_expires_at    timestamptz NOT NULL,
  refreshed_at         timestamptz NOT NULL DEFAULT now(),
  installed_at         timestamptz NOT NULL DEFAULT now(),
  revoked_at           timestamptz,
  UNIQUE (provider, account_ref)
);
COMMENT ON TABLE oauth_connections IS '[LEDGER] Encrypted GHL OAuth tokens for the private Marketplace app.';

-- Maps engine states to GHL pipeline stages. Ids differ between the sandbox
-- and live sub-accounts, so they are data, not code.
CREATE TABLE ghl_stage_map (
  internal_state   text PRIMARY KEY CHECK (internal_state IN (
                     'waitlisted', 'booked', 'displaced', 'cancelled', 'completed', 'no_show')),
  pipeline_id      text NOT NULL,
  stage_id         text NOT NULL,
  stage_name       text NOT NULL,                 -- 'Waitlisted', 'Pending', 'Cancelled', 'Complete', 'No Show'
  updated_at       timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE ghl_stage_map IS '[REF: GHL] Engine state → GHL pipeline stage id.';

CREATE TABLE webhook_events (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  provider           text NOT NULL CHECK (provider IN ('stripe', 'ghl')),
  event_id           text NOT NULL,               -- Stripe evt_...; GHL webhookId or body hash
  event_type         text NOT NULL,
  livemode           boolean,
  signature_valid    boolean NOT NULL,
  payload            jsonb,                       -- purged after the retention window
  payload_purged_at  timestamptz,
  status             text NOT NULL DEFAULT 'received'
                       CHECK (status IN ('received', 'processed', 'ignored', 'failed')),
  attempts           integer NOT NULL DEFAULT 0,
  error              text,
  received_at        timestamptz NOT NULL DEFAULT now(),
  processed_at       timestamptz,
  UNIQUE (provider, event_id)                     -- retries/duplicates are skipped
);
CREATE INDEX webhook_events_pending_idx ON webhook_events (received_at) WHERE status IN ('received', 'failed');
COMMENT ON TABLE webhook_events IS '[LEDGER] Every inbound webhook, de-duplicated by event id (Stripe retries 3 days, GHL up to 12 times).';

-- Transactional outbox: anything the engine must tell another platform is
-- written here in the same transaction as the change, then delivered by a
-- worker with retries. Failed rows are the console's "unsynced" list.
CREATE TABLE outbox_jobs (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  target            text NOT NULL CHECK (target IN (
                      'ghl', 'airtable', 'stripe', 'meta_capi', 'google_ads', 'ga4', 'email', 'internal')),
  operation         text NOT NULL,     -- 'upsert_contact', 'create_opportunity', 'set_stage', 'expire_checkout', 'writeback_assignment', ...
  entity_type       text,
  entity_id         bigint,
  payload           jsonb NOT NULL DEFAULT '{}',
  idempotency_key   text NOT NULL UNIQUE,
  status            text NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'dead', 'cancelled')),
  attempts          integer NOT NULL DEFAULT 0,
  max_attempts      integer NOT NULL DEFAULT 12,
  run_after         timestamptz NOT NULL DEFAULT now(),
  locked_at         timestamptz,
  locked_by         text,
  last_error        text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  completed_at      timestamptz
);
CREATE INDEX outbox_jobs_ready_idx ON outbox_jobs (run_after) WHERE status IN ('pending', 'failed');
CREATE INDEX outbox_jobs_entity_idx ON outbox_jobs (entity_type, entity_id);
COMMENT ON TABLE outbox_jobs IS '[LEDGER] Outbound integration queue (GHL, Airtable write-back, Stripe expire, Meta CAPI, Google Ads, email). Workers claim rows with FOR UPDATE SKIP LOCKED.';

CREATE TABLE integration_runs (
  id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind                   text NOT NULL CHECK (kind IN (
                           'airtable_sync', 'airtable_writeback', 'enrollment_snapshot',
                           'ghl_reconcile', 'stripe_reconcile', 'hold_expiry', 'waitlist_dispatch',
                           'waitlist_cutoff', 'coverage_scan', 'sitemap_build', 'data_retention',
                           'enrollware_import', 'kpi_rollup')),
  trigger                text NOT NULL CHECK (trigger IN ('schedule', 'button', 'manual', 'webhook')),
  triggered_by_user_id  bigint REFERENCES user_accounts (id),
  scope                  jsonb,              -- e.g. {"record_id":"rec..."} for a Sync-now button
  status                 text NOT NULL DEFAULT 'running'
                           CHECK (status IN ('running', 'succeeded', 'partial', 'failed')),
  stats                  jsonb,              -- {"seen":120,"created":3,"updated":7,"skipped":110}
  error                  text,
  started_at             timestamptz NOT NULL DEFAULT now(),
  finished_at            timestamptz
);
CREATE INDEX integration_runs_kind_idx ON integration_runs (kind, started_at DESC);
COMMENT ON TABLE integration_runs IS '[LEDGER] One row per background job run, for monitoring and the console''s job status.';

CREATE TABLE integration_issues (
  id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id                 bigint REFERENCES integration_runs (id),
  source                 text NOT NULL CHECK (source IN ('airtable', 'ghl', 'stripe', 'engine')),
  severity               text NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
  code                   text NOT NULL,      -- 'MISSING_START_TIME', 'ROOM_OVERLAP', 'GHL_STAGE_DRIFT', 'PAID_AFTER_HOLD_LAPSED', ...
  entity_type            text,
  entity_id              bigint,
  external_ref           text,               -- Airtable record id, GHL opportunity id, Stripe id
  message                text NOT NULL,
  details                jsonb,
  occurrences            integer NOT NULL DEFAULT 1,
  first_seen_at          timestamptz NOT NULL DEFAULT now(),
  last_seen_at           timestamptz NOT NULL DEFAULT now(),
  resolved_at            timestamptz,
  resolved_by_user_id   bigint REFERENCES user_accounts (id)
);
CREATE UNIQUE INDEX integration_issues_open_uniq ON integration_issues (source, code, COALESCE(external_ref, ''))
  WHERE resolved_at IS NULL;
COMMENT ON TABLE integration_issues IS '[LEDGER] Problems found by syncs and reconciliations (bad Airtable rows, stage drift in GHL, payment after a lapsed hold) for Bre / CS to fix.';

CREATE TABLE notifications (
  id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind                  text NOT NULL,       -- 'coverage_alert_21', 'demand_alert', 'sign_in_link', ...
  audience              text NOT NULL CHECK (audience IN ('staff', 'instructor', 'customer')),
  channel               text NOT NULL CHECK (channel IN ('email', 'sms', 'teams')),
  provider              text NOT NULL CHECK (provider IN ('ghl', 'email_service', 'teams_webhook')),
  recipient             text NOT NULL,
  subject_type          text,
  subject_id            bigint,
  dedupe_key            text NOT NULL UNIQUE,  -- e.g. 'coverage:123:14' → each alert sent once
  status                text NOT NULL DEFAULT 'queued'
                          CHECK (status IN ('queued', 'sent', 'failed', 'suppressed')),
  provider_message_id   text,
  error                 text,
  scheduled_for         timestamptz NOT NULL DEFAULT now(),
  sent_at               timestamptz,
  created_at            timestamptz NOT NULL DEFAULT now(),
  -- Anything a customer receives goes through GHL, so it lands in their
  -- conversation history and respects their DND settings.
  CHECK (audience <> 'customer' OR provider = 'ghl')
);
CREATE INDEX notifications_queue_idx ON notifications (scheduled_for) WHERE status = 'queued';
COMMENT ON TABLE notifications IS '[LEDGER] Messages the engine sends to staff and instructors (sign-in links, coverage and demand alerts), plus one-off customer messages it asks GHL to send (e.g. "no seat opened"). Confirmations and reminders are GHL workflows, not rows here.';


-- =============================================================================
-- 13. PUBLIC SITE CONTENT
-- =============================================================================

CREATE TABLE page_content (
  id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  scope                 text NOT NULL CHECK (scope IN ('global', 'state', 'metro', 'course', 'metro_course', 'site')),
  state_code            char(2),
  metro_id              bigint REFERENCES metros (id),
  course_family_id      smallint REFERENCES course_families (id),
  site_id               bigint REFERENCES sites (id),
  block_key             text NOT NULL,       -- 'intro', 'who_its_for', 'what_to_bring', 'faq', ...
  body_markdown         text NOT NULL,
  status                text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved')),
  approved_by_user_id  bigint REFERENCES user_accounts (id),
  approved_at           timestamptz,
  updated_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE NULLS NOT DISTINCT (scope, state_code, metro_id, course_family_id, site_id, block_key),
  CHECK (status <> 'approved' OR approved_by_user_id IS NOT NULL),
  CHECK (CASE scope
           WHEN 'global'       THEN num_nonnulls(state_code, metro_id, course_family_id, site_id) = 0
           WHEN 'state'        THEN state_code IS NOT NULL AND num_nonnulls(metro_id, course_family_id, site_id) = 0
           WHEN 'metro'        THEN metro_id IS NOT NULL AND num_nonnulls(state_code, course_family_id, site_id) = 0
           WHEN 'course'       THEN course_family_id IS NOT NULL AND num_nonnulls(state_code, metro_id, site_id) = 0
           WHEN 'metro_course' THEN num_nonnulls(metro_id, course_family_id) = 2 AND num_nonnulls(state_code, site_id) = 0
           WHEN 'site'         THEN site_id IS NOT NULL AND num_nonnulls(state_code, metro_id, course_family_id) = 0
         END)
);
COMMENT ON TABLE page_content IS '[OWNED] Unique copy per metro + course page, location notes and FAQs, approved by Bre. Lets wording change without a deploy.';

CREATE TABLE url_redirects (
  from_path     text PRIMARY KEY CHECK (from_path ~ '^/'),
  to_path       text NOT NULL CHECK (to_path ~ '^/'),
  status_code   smallint NOT NULL DEFAULT 301 CHECK (status_code IN (301, 302, 410)),
  reason        text NOT NULL CHECK (reason IN ('slug_changed', 'class_rescheduled', 'site_renamed', 'manual')),
  session_id    bigint REFERENCES sessions (id),
  created_at    timestamptz NOT NULL DEFAULT now(),
  CHECK (from_path <> to_path)
);
COMMENT ON TABLE url_redirects IS '[OWNED] Old URLs that must keep working after a slug or date change. Past-class redirects to the metro + course page are computed from the session, not stored.';


-- =============================================================================
-- 14. AUDIT LOG (append-only)
-- =============================================================================

CREATE TABLE audit_log (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  occurred_at     timestamptz NOT NULL DEFAULT now(),
  actor_type      text NOT NULL CHECK (actor_type IN ('user', 'customer', 'system', 'webhook')),
  user_account_id bigint REFERENCES user_accounts (id),
  acting_role     text REFERENCES roles (code),   -- the role/dashboard the action was taken from
  action          text NOT NULL,       -- 'registration.transfer', 'refund.approve', 'session.capacity_override', ...
  entity_type     text NOT NULL,
  entity_id       bigint,
  reason          text,
  before          jsonb,
  after           jsonb,
  request_id      uuid,
  ip              inet,
  CHECK ((actor_type = 'user') = (user_account_id IS NOT NULL))
);
CREATE INDEX audit_log_entity_idx ON audit_log (entity_type, entity_id, occurred_at);
CREATE INDEX audit_log_user_idx ON audit_log (user_account_id, occurred_at);
COMMENT ON TABLE audit_log IS '[OWNED] Every CS, ops and instructor action. Rows can never be changed or deleted.';

CREATE FUNCTION audit_log_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is append-only' USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE TRIGGER audit_log_no_update BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION audit_log_immutable();


-- =============================================================================
-- 15. SEAT ACCOUNTING AND THE CAPACITY GUARANTEE
-- =============================================================================
-- The app books a seat like this, in one transaction:
--   SELECT ... FROM sessions WHERE id = $1 FOR NO KEY UPDATE;   -- serialise buyers
--   check session_seats_available($1) >= qty;
--   INSERT seat_holds ...; INSERT registrations (status 'pending') ...;
--   COMMIT;  then create the Stripe Checkout session.
-- The constraint triggers below are the safety net: whatever code path writes
-- holds, registrations, overrides or capacity, the commit fails if the class
-- would be oversold. They are DEFERRED so a hold can be converted into
-- confirmed registrations within one transaction in any order.

CREATE FUNCTION session_seats_used(p_session_id bigint) RETURNS integer
LANGUAGE sql STABLE AS $$
  SELECT
      COALESCE((SELECT sum(h.quantity) FROM seat_holds h
                 WHERE h.session_id = p_session_id AND h.status = 'active' AND h.expires_at > now()), 0)::integer
    + (SELECT count(*) FROM registrations r
        WHERE r.session_id = p_session_id AND is_seat_consuming(r.status))::integer
    + COALESCE((SELECT sum(x.seats) FROM external_seat_reservations x
                 WHERE x.session_id = p_session_id), 0)::integer
$$;

CREATE FUNCTION session_seat_limit(p_session_id bigint) RETURNS integer
LANGUAGE sql STABLE AS $$
  SELECT s.capacity + COALESCE((SELECT sum(o.seats) FROM seat_overrides o
                                 WHERE o.session_id = s.id AND o.revoked_at IS NULL), 0)::integer
  FROM sessions s WHERE s.id = p_session_id
$$;

CREATE FUNCTION session_seats_available(p_session_id bigint) RETURNS integer
LANGUAGE sql STABLE AS $$
  SELECT session_seat_limit(p_session_id) - session_seats_used(p_session_id)
$$;

CREATE FUNCTION assert_session_capacity(p_session_id bigint) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  v_limit integer;
  v_used  integer;
BEGIN
  -- Lock the class row so concurrent bookings are checked one at a time.
  -- NO KEY UPDATE (not UPDATE): it does not conflict with the KEY SHARE locks
  -- that every foreign-key insert into seat_holds/registrations takes on this
  -- row, so buyers queue instead of deadlocking.
  PERFORM 1 FROM sessions WHERE id = p_session_id FOR NO KEY UPDATE;
  v_limit := session_seat_limit(p_session_id);
  v_used  := session_seats_used(p_session_id);
  IF v_used > v_limit THEN
    RAISE EXCEPTION 'session % is full: % seats used, limit %', p_session_id, v_used, v_limit
      USING ERRCODE = 'check_violation', HINT = 'nbc_session_full';
  END IF;
END $$;

CREATE FUNCTION trg_capacity_from_session_id() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM assert_session_capacity(NEW.session_id);
  RETURN NULL;
END $$;

CREATE FUNCTION trg_capacity_from_sessions() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM assert_session_capacity(NEW.id);
  RETURN NULL;
END $$;

-- Holds: new, reactivated, enlarged, extended or moved.
CREATE CONSTRAINT TRIGGER seat_holds_capacity
  AFTER INSERT OR UPDATE OF status, quantity, expires_at, session_id ON seat_holds
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW WHEN (NEW.status = 'active')
  EXECUTE FUNCTION trg_capacity_from_session_id();

-- Registrations that start consuming a seat, or move to another class.
-- (confirmed → completed does not re-check.)
CREATE CONSTRAINT TRIGGER registrations_capacity_ins
  AFTER INSERT ON registrations
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW WHEN (is_seat_consuming(NEW.status))
  EXECUTE FUNCTION trg_capacity_from_session_id();
CREATE CONSTRAINT TRIGGER registrations_capacity_upd
  AFTER UPDATE OF status, session_id ON registrations
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW WHEN (is_seat_consuming(NEW.status)
                     AND (NOT is_seat_consuming(OLD.status) OR OLD.session_id IS DISTINCT FROM NEW.session_id))
  EXECUTE FUNCTION trg_capacity_from_session_id();

-- Revoking an override can't strand sold seats.
CREATE CONSTRAINT TRIGGER seat_overrides_capacity
  AFTER UPDATE OF revoked_at, seats ON seat_overrides
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION trg_capacity_from_session_id();

-- Lowering a class max (co-instructor removed, Airtable seat change) can't
-- drop below seats already sold.
CREATE CONSTRAINT TRIGGER sessions_capacity
  AFTER UPDATE OF capacity ON sessions
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW WHEN (NEW.capacity < OLD.capacity)
  EXECUTE FUNCTION trg_capacity_from_sessions();

-- Seats can only be sold in a live class of a metro that has cut over to the
-- engine. "Status includes Enrollware" is set in every metro, so is_published
-- alone is not enough: the metro must be on the engine too (never both).
CREATE FUNCTION trg_hold_requires_live_session() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s record;
BEGIN
  SELECT se.status, se.is_published, se.starts_at, se.sales_close_at, m.booking_system
    INTO s
    FROM sessions se JOIN metros m ON m.id = se.metro_id
   WHERE se.id = NEW.session_id;
  IF s.status <> 'scheduled' OR s.booking_system <> 'engine' OR s.starts_at <= now() THEN
    RAISE EXCEPTION 'session % is not open for booking (status %, metro on %)',
      NEW.session_id, s.status, s.booking_system USING ERRCODE = 'check_violation';
  END IF;
  -- Public and phone sales also need the publish flag and an open sales window.
  -- (Waitlist offers run to the waitlist cut-off; invoice registrations skip holds.)
  IF TG_TABLE_NAME = 'seat_holds' THEN
    IF NEW.kind IN ('checkout', 'phone_link')
       AND (NOT s.is_published OR (s.sales_close_at IS NOT NULL AND now() >= s.sales_close_at)) THEN
      RAISE EXCEPTION 'session % is not on sale', NEW.session_id USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER seat_holds_live_session BEFORE INSERT ON seat_holds
  FOR EACH ROW EXECUTE FUNCTION trg_hold_requires_live_session();
-- Invoice registrations and transfers bypass holds, so check them directly.
CREATE TRIGGER registrations_live_session BEFORE INSERT OR UPDATE OF session_id ON registrations
  FOR EACH ROW WHEN (NEW.status IN ('pending', 'awaiting_payment', 'confirmed'))
  EXECUTE FUNCTION trg_hold_requires_live_session();

-- Waitlist size: people waiting (by party size) may not exceed the session's
-- list limit, which the engine sets from the location type (rules doc §3).
CREATE FUNCTION trg_waitlist_limit() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_limit integer; v_waiting integer;
BEGIN
  SELECT waitlist_limit INTO v_limit FROM sessions WHERE id = NEW.session_id FOR NO KEY UPDATE;
  SELECT COALESCE(sum(party_size), 0) INTO v_waiting FROM waitlist_entries
   WHERE session_id = NEW.session_id AND status IN ('waiting', 'offered');
  IF v_waiting > v_limit THEN
    RAISE EXCEPTION 'waitlist for session % is full (% waiting, limit %)', NEW.session_id, v_waiting, v_limit
      USING ERRCODE = 'check_violation', HINT = 'nbc_waitlist_full';
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER waitlist_entries_limit
  AFTER INSERT OR UPDATE OF status, party_size, session_id ON waitlist_entries
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW WHEN (NEW.status IN ('waiting', 'offered'))
  EXECUTE FUNCTION trg_waitlist_limit();

-- A room only hosts the course families it is equipped for (when listed).
CREATE FUNCTION trg_room_hosts_family() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM room_course_families WHERE room_id = NEW.room_id)
     AND NOT EXISTS (SELECT 1 FROM room_course_families
                      WHERE room_id = NEW.room_id AND course_family_id = NEW.course_family_id) THEN
    RAISE EXCEPTION 'room % is not set up for course family %', NEW.room_id, NEW.course_family_id
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER sessions_room_hosts_family BEFORE INSERT OR UPDATE OF room_id, course_family_id ON sessions
  FOR EACH ROW EXECUTE FUNCTION trg_room_hosts_family();

-- The two money-and-seat decisions the plan reserves for managers are also
-- checked in the database: extra seats over the limit, and large refunds.
CREATE FUNCTION trg_require_permission() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_user bigint; v_perm text; v_metro bigint;
BEGIN
  v_perm := TG_ARGV[0];
  v_user := NEW.approved_by_user_id;
  IF TG_TABLE_NAME = 'seat_overrides' THEN
    SELECT metro_id INTO v_metro FROM sessions WHERE id = NEW.session_id;
  ELSE
    SELECT se.metro_id INTO v_metro
      FROM registrations r JOIN sessions se ON se.id = r.session_id WHERE r.id = NEW.registration_id;
  END IF;
  IF v_user IS NOT NULL AND NOT user_has_permission(v_user, v_perm, v_metro) THEN
    RAISE EXCEPTION 'user % lacks permission %', v_user, v_perm USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER seat_overrides_permission BEFORE INSERT OR UPDATE OF approved_by_user_id ON seat_overrides
  FOR EACH ROW EXECUTE FUNCTION trg_require_permission('class.capacity_override');
CREATE TRIGGER refunds_permission BEFORE INSERT OR UPDATE OF approved_by_user_id ON refunds
  FOR EACH ROW WHEN (NEW.requires_manager_approval)
  EXECUTE FUNCTION trg_require_permission('refund.approve_large');

-- Co-instructors only at co-instructor locations.
CREATE FUNCTION trg_co_instructor_room_check() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s sessions%ROWTYPE;
BEGIN
  SELECT * INTO s FROM sessions WHERE id = NEW.session_id;
  IF s.location_type <> 'co_instructor' OR s.room_max_student_count <= s.one_instructor_max THEN
    RAISE EXCEPTION 'session % is at a one-instructor location (room max %, one-instructor max %)',
      s.id, s.room_max_student_count, s.one_instructor_max USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER assignments_co_room BEFORE INSERT OR UPDATE OF role, status ON assignments
  FOR EACH ROW WHEN (NEW.role = 'co' AND NEW.status = 'assigned')
  EXECUTE FUNCTION trg_co_instructor_room_check();

-- Keep assignment time ranges in step with the class (clash checks stay true
-- after a reschedule; a reschedule that creates a clash fails and is flagged).
CREATE FUNCTION trg_assignment_range_default() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  SELECT time_range INTO NEW.session_range FROM sessions WHERE id = NEW.session_id;
  RETURN NEW;
END $$;
CREATE TRIGGER assignments_range BEFORE INSERT OR UPDATE OF session_id ON assignments
  FOR EACH ROW EXECUTE FUNCTION trg_assignment_range_default();

CREATE FUNCTION trg_session_time_to_assignments() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  UPDATE assignments SET session_range = NEW.time_range WHERE session_id = NEW.id;
  RETURN NULL;
END $$;
CREATE TRIGGER sessions_time_to_assignments AFTER UPDATE OF starts_at, ends_at ON sessions
  FOR EACH ROW EXECUTE FUNCTION trg_session_time_to_assignments();

-- A cancelled class frees its instructors' time.
CREATE FUNCTION trg_session_cancel_releases_instructors() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  UPDATE assignments
     SET status = 'removed', released_at = now(), released_by = 'ops', release_reason = 'class_cancelled'
   WHERE session_id = NEW.id AND status = 'assigned';
  RETURN NULL;
END $$;
CREATE TRIGGER sessions_cancel_releases_instructors AFTER UPDATE OF status ON sessions
  FOR EACH ROW WHEN (NEW.status = 'cancelled' AND OLD.status <> 'cancelled')
  EXECUTE FUNCTION trg_session_cancel_releases_instructors();


-- Buying a seat makes an account holder a student. When a registration is
-- confirmed, the account with the attendee's (or buyer's) email gets the
-- 'student' role if it doesn't have it yet. Guests without an account are
-- untouched; they become students the moment they create one (the sign-up
-- code calls grant_student_role_if_booked).
CREATE FUNCTION grant_student_role_if_booked(p_user_id bigint) RETURNS boolean
LANGUAGE plpgsql AS $$
DECLARE v_email citext;
BEGIN
  SELECT email INTO v_email FROM user_accounts WHERE id = p_user_id;
  IF NOT EXISTS (SELECT 1 FROM registrations r JOIN orders o ON o.id = r.order_id
                  WHERE r.status IN ('awaiting_payment', 'confirmed', 'completed', 'no_show')
                    AND (r.attendee_email = v_email OR o.buyer_email = v_email)) THEN
    RETURN false;
  END IF;
  INSERT INTO user_roles (user_account_id, role_code)
  SELECT p_user_id, 'student'
  WHERE NOT EXISTS (SELECT 1 FROM user_roles WHERE user_account_id = p_user_id
                       AND role_code = 'student' AND revoked_at IS NULL);
  RETURN true;
END $$;

CREATE FUNCTION trg_registration_grants_student() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO user_roles (user_account_id, role_code)
  SELECT a.id, 'student'
    FROM user_accounts a
   WHERE a.email IN (NEW.attendee_email, (SELECT buyer_email FROM orders WHERE id = NEW.order_id))
     AND NOT EXISTS (SELECT 1 FROM user_roles ur WHERE ur.user_account_id = a.id
                        AND ur.role_code = 'student' AND ur.revoked_at IS NULL);
  RETURN NULL;
END $$;
CREATE TRIGGER registrations_grant_student
  AFTER INSERT OR UPDATE OF status ON registrations
  FOR EACH ROW WHEN (NEW.status IN ('awaiting_payment', 'confirmed'))
  EXECUTE FUNCTION trg_registration_grants_student();


-- =============================================================================
-- 16. VIEWS
-- =============================================================================

-- Live seat numbers for class pages, console and instructor portal.
CREATE VIEW session_availability AS
SELECT
  s.id                                   AS session_id,
  s.capacity,
  COALESCE(ov.seats, 0)                  AS override_seats,
  s.capacity + COALESCE(ov.seats, 0)     AS seat_limit,
  COALESCE(ex.seats, 0)                  AS external_seats,
  COALESCE(h.seats, 0)                   AS held_seats,
  COALESCE(r.sold, 0)                    AS sold_seats,
  s.capacity + COALESCE(ov.seats, 0)
    - COALESCE(ex.seats, 0) - COALESCE(h.seats, 0) - COALESCE(r.sold, 0) AS seats_left,
  COALESCE(w.waiting, 0)                 AS waitlist_count,
  s.waitlist_limit,
  s.location_type
FROM sessions s
LEFT JOIN LATERAL (SELECT sum(seats)::int AS seats FROM seat_overrides
                    WHERE session_id = s.id AND revoked_at IS NULL) ov ON true
LEFT JOIN LATERAL (SELECT sum(seats)::int AS seats FROM external_seat_reservations
                    WHERE session_id = s.id) ex ON true
LEFT JOIN LATERAL (SELECT sum(quantity)::int AS seats FROM seat_holds
                    WHERE session_id = s.id AND status = 'active' AND expires_at > now()) h ON true
LEFT JOIN LATERAL (SELECT count(*)::int AS sold FROM registrations
                    WHERE session_id = s.id AND is_seat_consuming(status)) r ON true
LEFT JOIN LATERAL (SELECT sum(party_size)::int AS waiting FROM waitlist_entries
                    WHERE session_id = s.id AND status IN ('waiting', 'offered')) w ON true;

-- Waitlist with positions (offer-expired students keep a place but are skipped).
CREATE VIEW waitlist_queue AS
SELECT
  e.*,
  row_number() OVER (PARTITION BY e.session_id ORDER BY e.joined_at, e.id) AS position
FROM waitlist_entries e
WHERE e.status IN ('waiting', 'offered', 'offer_expired');

-- Break-even per class: ceil(co-instructor pay / (class price − cert cost)).
-- Uses the class's lowest offered price (conservative when A&P and Adult
-- differ); see open question in docs/database-architecture.md.
CREATE VIEW session_break_even AS
SELECT
  sp.session_id,
  min(sp.price_cents) AS class_price_cents,
  CASE WHEN min(sp.price_cents) > max(cert.v)
       THEN ceil(max(pay.v)::numeric / (min(sp.price_cents) - max(cert.v)))::int END
    AS waitlist_needed_for_co_instructor
FROM session_products sp
LEFT JOIN LATERAL (SELECT (value #>> '{}')::int AS v FROM app_settings WHERE key = 'co_instructor_pay_cents') pay ON true
LEFT JOIN LATERAL (SELECT (value #>> '{}')::int AS v FROM app_settings WHERE key = 'cert_cost_cents_per_student') cert ON true
WHERE sp.is_offered
GROUP BY sp.session_id;

-- The console's "unsynced with GHL" list.
CREATE VIEW unsynced_items AS
SELECT j.id AS outbox_job_id, j.target, j.operation, j.entity_type, j.entity_id,
       j.status, j.attempts, j.last_error, j.created_at, j.run_after
FROM outbox_jobs j
WHERE j.status IN ('failed', 'dead')
   OR (j.status = 'pending' AND j.created_at < now() - interval '10 minutes');

-- Ops coverage board: published classes with no lead instructor yet.
CREATE VIEW sessions_needing_instructor AS
SELECT s.id AS session_id, s.metro_id, s.site_id, s.course_family_id, s.starts_at,
       (s.starts_at::date - now()::date) AS days_out,
       EXISTS (SELECT 1 FROM class_offers o
                WHERE o.session_id = s.id AND o.role = 'lead' AND o.status = 'open') AS offer_open
FROM sessions s
WHERE s.status = 'scheduled' AND s.is_published AND s.starts_at > now()
  AND NOT EXISTS (SELECT 1 FROM assignments a
                   WHERE a.session_id = s.id AND a.role = 'lead' AND a.status = 'assigned');


-- =============================================================================
-- 17. updated_at TRIGGERS
-- =============================================================================
DO $$
DECLARE t text;
BEGIN
  FOR t IN
    SELECT c.table_name FROM information_schema.columns c
    JOIN information_schema.tables tb
      ON tb.table_name = c.table_name AND tb.table_schema = c.table_schema
    WHERE c.table_schema = 'public' AND c.column_name = 'updated_at' AND tb.table_type = 'BASE TABLE'
  LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION set_updated_at()',
                   t || '_set_updated_at', t);
  END LOOP;
END $$;

COMMIT;
