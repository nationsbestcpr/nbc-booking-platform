-- =============================================================================
-- Demo data for local development and the static preview. NEVER load this
-- into staging or production.
--   flask --app wsgi db upgrade && flask --app wsgi db load-demo
--   (or: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/schema.sql -f db/seed.sql -f db/demo.sql)
-- Every demo account signs in with the password: demo-password
-- Richmond classes for the January 2027 pilot. People, emails and bookings are
-- invented (example.test addresses). Henrico's address is from the launch hub;
-- the other two sites' addresses come from Airtable later, so they say so.
-- =============================================================================

-- City areas -------------------------------------------------------------------
INSERT INTO metros (slug, name, state_code, market_code, time_zone, is_hub, booking_system, cutover_at) VALUES
  ('richmond-va', 'Richmond', 'VA', 'RVA', 'America/New_York', true,  'engine', '2027-01-11 09:00-05'),
  ('houston-tx',  'Houston',  'TX', 'HOU', 'America/Chicago',  false, 'enrollware', NULL),
  ('dallas-fort-worth-tx', 'Dallas–Fort Worth', 'TX', 'DFW', 'America/Chicago', true, 'enrollware', NULL);

INSERT INTO sites (metro_id, slug, name, address_line1, address_line2, city, state_code, postal_code, time_zone,
                   parking_notes_public, directions_public)
SELECT m.id, v.slug, v.name, v.a1, v.a2, 'Richmond', 'VA', v.zip, 'America/New_York', v.parking, v.directions
FROM metros m, (VALUES
  ('henrico', 'Henrico (Three Chopt)', '8100 Three Chopt Rd', 'Suite 124', '23229',
   'Free parking in the front lot.', 'Suite 124 is on the ground floor, left of the main entrance.'),
  ('moorefield', 'Moorefield', 'Street address comes from Airtable', NULL, '23236', NULL, NULL),
  ('our-lady-of-lourdes', 'Our Lady of Lourdes', 'Street address comes from Airtable', NULL, '23226', NULL, NULL)
) AS v(slug, name, a1, a2, zip, parking, directions)
WHERE m.slug = 'richmond-va';

INSERT INTO rooms (site_id, name, max_student_count)
SELECT id, 'Training room', CASE slug WHEN 'henrico' THEN 20 ELSE 12 END FROM sites;

-- Our Lady of Lourdes has no Instructor Training room.
INSERT INTO room_course_families (room_id, course_family_id)
SELECT r.id, cf.id FROM rooms r JOIN sites s ON s.id = r.site_id, course_families cf
WHERE s.slug = 'our-lady-of-lourdes' AND cf.code IN ('FA_CPR_AED', 'BLS');

-- Products ---------------------------------------------------------------------
INSERT INTO products (course_family_id, code, name, short_name, delivery_mode, ghl_course_code, base_price_cents, requires_prerequisites, sort_order)
SELECT cf.id, v.code, v.name, v.short, 'blended', v.ghl, v.price, v.prereq, v.sort
FROM (VALUES
  ('FA_CPR_AED', 'AP_FACPRAED_BL', 'Adult & Pediatric First Aid/CPR/AED – Blended', 'Adult & Pediatric', 'AP-FACPRAED', 10800, false, 1),
  ('FA_CPR_AED', 'AD_FACPRAED_BL', 'Adult First Aid/CPR/AED – Blended',              'Adult only',        'AD-FACPRAED',  8800, false, 2),
  ('BLS',        'BLS_BL',         'Basic Life Support – Blended',                   'BLS',               'BLS',          8800, false, 3),
  ('IT',         'IT_BL',          'Instructor Training – Blended',                  'Instructor Training','IT',         29900, true,  4)
) AS v(family, code, name, short, ghl, price, prereq, sort)
JOIN course_families cf ON cf.code = v.family;

-- Classes ----------------------------------------------------------------------
INSERT INTO sessions (metro_id, site_id, room_id, course_family_id, delivery_mode, starts_at, ends_at, time_zone, slug,
                      is_published, airtable_status, one_instructor_max, room_max_student_count, capacity,
                      location_type, waitlist_limit)
SELECT m.id, s.id, r.id, cf.id, 'blended',
       (v.day || ' ' || v.t1)::timestamp AT TIME ZONE 'America/New_York',
       (v.day || ' ' || v.t2)::timestamp AT TIME ZONE 'America/New_York',
       'America/New_York', v.day || '-' || v.site || v.suffix, true, ARRAY['Enrollware'],
       cf.one_instructor_max, r.max_student_count, cf.one_instructor_max,
       CASE WHEN r.max_student_count > cf.one_instructor_max THEN 'co_instructor' ELSE 'one_instructor' END,
       CASE WHEN r.max_student_count > cf.one_instructor_max
            THEN least(r.max_student_count - cf.one_instructor_max, cf.one_instructor_max)
            ELSE cf.one_instructor_max END
FROM (VALUES
  ('FA_CPR_AED', 'henrico',             '2027-01-16', '09:00', '12:00', ''),
  ('BLS',        'henrico',             '2027-01-16', '13:00', '16:00', ''),
  ('FA_CPR_AED', 'moorefield',          '2027-01-19', '18:00', '21:00', ''),
  ('BLS',        'our-lady-of-lourdes', '2027-01-23', '09:00', '12:00', ''),
  ('IT',         'henrico',             '2027-01-30', '08:30', '17:30', ''),
  ('FA_CPR_AED', 'our-lady-of-lourdes', '2027-02-06', '09:00', '12:00', ''),
  ('FA_CPR_AED', 'henrico',             '2027-02-13', '09:00', '12:00', '')
) AS v(family, site, day, t1, t2, suffix)
JOIN course_families cf ON cf.code = v.family
JOIN sites s ON s.slug = v.site
JOIN rooms r ON r.site_id = s.id
JOIN metros m ON m.id = s.metro_id;

INSERT INTO session_products (session_id, product_id, course_family_id, delivery_mode, price_cents)
SELECT s.id, p.id, p.course_family_id, p.delivery_mode, p.base_price_cents
FROM sessions s JOIN products p ON p.course_family_id = s.course_family_id AND p.delivery_mode = s.delivery_mode;

-- Bookings (paid web orders, the way checkout will create them) -----------------
CREATE FUNCTION pg_temp.demo_book(p_slug text, p_product text, n int, p_tag text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE sid bigint; pid bigint; price int; o bigint; h bigint; i int;
BEGIN
  SELECT s.id, p.id, sp.price_cents INTO sid, pid, price
    FROM products p
    JOIN sessions s ON s.slug = p_slug AND s.course_family_id = p.course_family_id
    JOIN session_products sp ON sp.product_id = p.id AND sp.session_id = s.id
   WHERE p.code = p_product;
  INSERT INTO orders (channel, buyer_first_name, buyer_last_name, buyer_email, livemode, subtotal_cents, total_cents)
  VALUES ('web', 'Demo', p_tag, lower(p_tag) || '@example.test', false, price * n, price * n) RETURNING id INTO o;
  INSERT INTO seat_holds (session_id, order_id, kind, quantity, expires_at)
  VALUES (sid, o, 'checkout', n, now() + interval '15 minutes') RETURNING id INTO h;
  FOR i IN 1..n LOOP
    INSERT INTO registrations (order_id, seat_hold_id, session_id, product_id, attendee_first_name, attendee_last_name,
                               attendee_email, unit_price_cents)
    VALUES (o, h, sid, pid, 'Student', p_tag || ' ' || i, lower(p_tag) || i || '@example.test', price);
  END LOOP;
  UPDATE seat_holds SET status = 'converted', released_at = now() WHERE id = h;
  UPDATE registrations SET status = 'confirmed', confirmed_at = now(), amount_paid_cents = price WHERE seat_hold_id = h;
  UPDATE orders SET status = 'paid', paid_at = now(), amount_paid_cents = price * n WHERE id = o;
END $$;

DO $$
BEGIN
  PERFORM pg_temp.demo_book('2027-01-16-henrico',             'AP_FACPRAED_BL', 7, 'Alder');
  PERFORM pg_temp.demo_book('2027-01-16-henrico',             'AD_FACPRAED_BL', 5, 'Birch');   -- full: 12 of 12
  PERFORM pg_temp.demo_book('2027-01-16-henrico',             'BLS_BL',         4, 'Cedar');
  PERFORM pg_temp.demo_book('2027-01-19-moorefield',          'AP_FACPRAED_BL', 6, 'Dogwood');
  PERFORM pg_temp.demo_book('2027-01-19-moorefield',          'AD_FACPRAED_BL', 3, 'Elm');     -- 3 left
  PERFORM pg_temp.demo_book('2027-01-30-henrico',             'IT_BL',          2, 'Fir');
  PERFORM pg_temp.demo_book('2027-02-06-our-lady-of-lourdes', 'AD_FACPRAED_BL', 5, 'Gum');
END $$;

-- Waiting list on the full Henrico class
INSERT INTO waitlist_entries (session_id, preferred_product_id, first_name, last_name, email, phone_e164, party_size, joined_at)
SELECT s.id, p.id, v.fn, v.ln, v.em, v.ph, v.party, now() - v.ago
FROM sessions s, products p, (VALUES
  ('Jordan', 'Waiter', 'jordan.waiter@example.test', '+18045550101', 1, interval '3 days'),
  ('Sam',    'Waiter', 'sam.waiter@example.test',    '+18045550102', 2, interval '2 days')
) AS v(fn, ln, em, ph, party, ago)
WHERE s.slug = '2027-01-16-henrico' AND p.code = 'AP_FACPRAED_BL' AND s.course_family_id = p.course_family_id;

-- Instructors --------------------------------------------------------------------
INSERT INTO user_accounts (email, display_name, status) VALUES
  ('dana.whitfield@example.test', 'Dana Whitfield', 'active'),
  ('marcus.hale@example.test',    'Marcus Hale',    'active'),
  ('priya.raman@example.test',    'Priya Raman',    'active');

INSERT INTO user_roles (user_account_id, role_code)
SELECT id, 'instructor' FROM user_accounts WHERE email LIKE '%.%@example.test';

INSERT INTO instructors (user_account_id, first_name, last_name, email, home_metro_id, hub_metro_id, hub_priority, performance_tier, travel_radius_miles)
SELECT a.id, split_part(a.display_name, ' ', 1), split_part(a.display_name, ' ', 2), a.email, m.id, m.id, v.prio, v.tier, 40
FROM user_accounts a, metros m, (VALUES
  ('dana.whitfield@example.test', 'primary', 1),
  ('marcus.hale@example.test',    'secondary', 2),
  ('priya.raman@example.test',    'primary', 1)
) AS v(email, prio, tier)
WHERE a.email = v.email AND m.slug = 'richmond-va';

INSERT INTO instructor_certs (instructor_id, cert_type, expires_on)
SELECT i.id, v.cert, v.exp::date FROM instructors i, (VALUES
  ('dana.whitfield@example.test', 'ARC_FA_CPR_AED_INSTRUCTOR', '2028-03-31'),
  ('dana.whitfield@example.test', 'ARC_INSTRUCTOR_TRAINER',    '2027-11-30'),
  ('marcus.hale@example.test',    'ARC_BLS_INSTRUCTOR',        '2027-06-30'),
  ('priya.raman@example.test',    'ARC_BLS_INSTRUCTOR',        '2028-01-31'),
  ('priya.raman@example.test',    'ARC_FA_CPR_AED_INSTRUCTOR', '2027-09-30')
) AS v(email, cert, exp) WHERE i.email = v.email;

-- Who teaches what. The Henrico BLS class has a helper, which raised its max to 18.
INSERT INTO assignments (session_id, instructor_id, role, source)
SELECT s.id, i.id, v.role, 'portal'
FROM (VALUES
  ('2027-01-16-henrico',    'FA_CPR_AED', 'dana.whitfield@example.test', 'lead'),
  ('2027-01-16-henrico',    'BLS',        'priya.raman@example.test',    'lead'),
  ('2027-01-16-henrico',    'BLS',        'marcus.hale@example.test',    'co'),
  ('2027-01-19-moorefield', 'FA_CPR_AED', 'priya.raman@example.test',    'lead'),
  ('2027-01-30-henrico',    'IT',         'dana.whitfield@example.test', 'lead'),
  ('2027-02-13-henrico',    'FA_CPR_AED', 'marcus.hale@example.test',    'lead')
) AS v(slug, family, email, role)
JOIN course_families cf ON cf.code = v.family
JOIN sessions s ON s.slug = v.slug AND s.course_family_id = cf.id
JOIN instructors i ON i.email = v.email;
UPDATE sessions s SET capacity = 18, capacity_basis = 'co_instructor'
FROM course_families cf WHERE cf.id = s.course_family_id AND cf.code = 'BLS' AND s.slug = '2027-01-16-henrico';

-- Classes posted for instructors to request (no rate card yet: rate shows once set)
INSERT INTO class_offers (session_id, role, rate_cents)
SELECT id, 'lead', 0 FROM sessions WHERE slug IN ('2027-01-23-our-lady-of-lourdes', '2027-02-06-our-lady-of-lourdes');

-- Staff accounts, one per role, plus one person with two roles -----------------
INSERT INTO user_accounts (email, display_name, status) VALUES
  ('admin@example.test',     'Demo Admin',                  'active'),
  ('manager@example.test',   'Demo Manager',                'active'),
  ('cs@example.test',        'Demo Customer Service',       'active'),
  ('ops@example.test',       'Demo Operations',             'active'),
  ('recruiting@example.test','Demo Helper Recruiting',      'active'),
  ('approver@example.test',  'Demo Second-Class Approver',  'active'),
  ('viewer@example.test',    'Demo Viewer',                 'active'),
  ('opslead@example.test',   'Demo Ops Lead (two roles)',   'active');
INSERT INTO user_roles (user_account_id, role_code)
SELECT a.id, v.role FROM user_accounts a, (VALUES
  ('admin@example.test', 'admin'), ('manager@example.test', 'manager'), ('cs@example.test', 'customer_service'),
  ('ops@example.test', 'operations'), ('recruiting@example.test', 'helper_recruiting'),
  ('approver@example.test', 'location_approver'), ('viewer@example.test', 'viewer'),
  ('opslead@example.test', 'manager'), ('opslead@example.test', 'operations')
) AS v(email, role) WHERE a.email = v.email;

-- Customers: a regular user, one who signed up as an instructor (still a
-- regular user until verified), and a student who booked as a guest first.
INSERT INTO user_accounts (email, display_name, status, signup_intent) VALUES
  ('riley.brooks@example.test', 'Riley Brooks', 'active', 'student'),
  ('jamie.ortiz@example.test',  'Jamie Ortiz',  'active', 'instructor'),
  ('alder1@example.test',       'Alex Alder',   'active', 'student');
INSERT INTO user_roles (user_account_id, role_code)
SELECT id, 'user' FROM user_accounts WHERE email IN ('riley.brooks@example.test', 'jamie.ortiz@example.test', 'alder1@example.test');
SELECT grant_student_role_if_booked(id) FROM user_accounts WHERE email = 'alder1@example.test';

-- Everyone signs in with email + "demo-password" (local only).
INSERT INTO user_identities (user_account_id, provider, subject, password_hash, password_changed_at)
SELECT id, 'password', lower(email), 'scrypt:32768:8:1$lDs2WhELxSIHv6oh$ec7f1878edbf45db277b5c2d83944773604e041c02af38289a0663e2e796be1f2f9158291bcc528dfd3791e54fa8bdf8f1ad0678580a908c5ec7bdab55f45920', now() FROM user_accounts;

-- Demo emails count as confirmed (as if each had signed in with Google once), so
-- dashboards show the bookings made for these emails in the demo data.
UPDATE user_accounts SET email_verified_at = now();

-- A schedule copy that ran, and one data problem for Bre to fix ----------------
INSERT INTO integration_runs (kind, trigger, status, stats, started_at, finished_at)
VALUES ('airtable_sync', 'schedule', 'succeeded', '{"seen": 7, "created": 0, "updated": 1, "skipped": 6}',
        now() - interval '4 minutes', now() - interval '4 minutes' + interval '3 seconds');
INSERT INTO integration_issues (source, severity, code, entity_type, external_ref, message)
VALUES ('airtable', 'warning', 'MISSING_PARKING_NOTES', 'site', 'recDEMOmoorefield',
        'Moorefield has no public parking notes in Master Locations.');


