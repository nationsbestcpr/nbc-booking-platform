-- =============================================================================
-- Schema checks: run against a database loaded with schema.sql + seed.sql.
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/tests/schema_checks.sql
-- Everything runs in one transaction and is rolled back. Any failed check
-- raises an exception and stops the script.
-- The concurrent last-seat test needs two connections: db/tests/last_seat_race.sh
-- =============================================================================
\set QUIET on
\pset tuples_only on
\pset format unaligned
BEGIN;

-- expect_error(sql, sqlstate): passes only if running sql raises that sqlstate
-- (deferred constraint triggers are forced to fire inside the call).
CREATE FUNCTION pg_temp.expect_error(p_sql text, p_state text, p_label text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  BEGIN
    EXECUTE p_sql;
    SET CONSTRAINTS ALL IMMEDIATE;
    SET CONSTRAINTS ALL DEFERRED;
  EXCEPTION WHEN OTHERS THEN
    IF SQLSTATE = p_state THEN
      RAISE NOTICE 'PASS  %', p_label;
      RETURN;
    END IF;
    RAISE EXCEPTION 'FAIL  % (got % %, wanted %)', p_label, SQLSTATE, SQLERRM, p_state;
  END;
  RAISE EXCEPTION 'FAIL  % (no error raised)', p_label;
END $$;

CREATE FUNCTION pg_temp.check(p_ok boolean, p_label text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  IF NOT p_ok THEN RAISE EXCEPTION 'FAIL  %', p_label; END IF;
  RAISE NOTICE 'PASS  %', p_label;
END $$;

-- ---------------------------------------------------------------------------
-- Fixtures: Richmond, one co-instructor room (20) and one small room (12).
-- ---------------------------------------------------------------------------
INSERT INTO user_accounts (email, display_name, status) VALUES
  ('cs@example.test', 'CS Agent', 'active'),
  ('mgr@example.test', 'Manager', 'active');
INSERT INTO user_roles (user_account_id, role_code)
SELECT id, 'customer_service' FROM user_accounts WHERE email = 'cs@example.test'
UNION ALL SELECT id, 'manager' FROM user_accounts WHERE email = 'mgr@example.test';

INSERT INTO metros (slug, name, state_code, market_code, time_zone, is_hub, booking_system, cutover_at)
VALUES ('richmond-va', 'Richmond', 'VA', 'RVA', 'America/New_York', true, 'engine', now());

INSERT INTO sites (metro_id, slug, name, address_line1, address_line2, city, state_code, postal_code, time_zone)
SELECT id, 'henrico', 'Henrico (Three Chopt)', '8100 Three Chopt Rd', 'Suite 124', 'Richmond', 'VA', '23229', 'America/New_York'
FROM metros WHERE slug = 'richmond-va';

INSERT INTO rooms (site_id, name, max_student_count)
SELECT id, 'Big', 20 FROM sites WHERE slug = 'henrico'
UNION ALL SELECT id, 'Small', 12 FROM sites WHERE slug = 'henrico';

INSERT INTO products (course_family_id, code, name, short_name, delivery_mode, ghl_course_code, base_price_cents)
SELECT id, 'AP_BL', 'A&P FA/CPR/AED Blended', 'A&P', 'blended', 'AP', 10800 FROM course_families WHERE code = 'FA_CPR_AED'
UNION ALL SELECT id, 'AD_BL', 'Adult FA/CPR/AED Blended', 'Adult', 'blended', 'ADULT', 8800 FROM course_families WHERE code = 'FA_CPR_AED'
UNION ALL SELECT id, 'BLS_BL', 'BLS Blended', 'BLS', 'blended', 'BLS', 8800 FROM course_families WHERE code = 'BLS';

-- S1: FA class in the big room (co-instructor possible). S2: FA class in the small room.
INSERT INTO sessions (metro_id, site_id, room_id, course_family_id, delivery_mode, starts_at, ends_at, time_zone, slug,
                      is_published, one_instructor_max, room_max_student_count, capacity, location_type, waitlist_limit)
SELECT m.id, si.id, r.id, cf.id, 'blended',
       '2027-01-16 14:00+00', '2027-01-16 18:00+00', 'America/New_York', '2027-01-16-henrico',
       true, 12, r.max_student_count, 12, 'co_instructor', 8
FROM metros m, sites si, rooms r, course_families cf
WHERE m.slug = 'richmond-va' AND si.slug = 'henrico' AND r.site_id = si.id AND r.name = 'Big' AND cf.code = 'FA_CPR_AED';

INSERT INTO sessions (metro_id, site_id, room_id, course_family_id, delivery_mode, starts_at, ends_at, time_zone, slug,
                      is_published, one_instructor_max, room_max_student_count, capacity, location_type, waitlist_limit)
SELECT m.id, si.id, r.id, cf.id, 'blended',
       '2027-01-16 14:00+00', '2027-01-16 18:00+00', 'America/New_York', '2027-01-16-henrico-small',
       true, 12, r.max_student_count, 12, 'one_instructor', 12
FROM metros m, sites si, rooms r, course_families cf
WHERE m.slug = 'richmond-va' AND si.slug = 'henrico' AND r.site_id = si.id AND r.name = 'Small' AND cf.code = 'FA_CPR_AED';

INSERT INTO session_products (session_id, product_id, course_family_id, delivery_mode, price_cents)
SELECT s.id, p.id, p.course_family_id, p.delivery_mode, p.base_price_cents
FROM sessions s JOIN products p ON p.course_family_id = s.course_family_id AND p.delivery_mode = s.delivery_mode;

CREATE TEMP TABLE ids AS
SELECT
  (SELECT id FROM sessions WHERE slug = '2027-01-16-henrico')        AS s1,
  (SELECT id FROM sessions WHERE slug = '2027-01-16-henrico-small')  AS s2,
  (SELECT id FROM products WHERE code = 'AP_BL')                     AS ap,
  (SELECT id FROM products WHERE code = 'AD_BL')                     AS adult,
  (SELECT id FROM products WHERE code = 'BLS_BL')                    AS bls,
  (SELECT id FROM user_accounts WHERE email = 'cs@example.test')       AS cs,
  (SELECT id FROM user_accounts WHERE email = 'mgr@example.test')      AS mgr;

-- Book n seats of a product as one paid web order (hold → confirmed), the
-- way the engine will: hold first, then convert in the webhook transaction.
CREATE FUNCTION pg_temp.book(p_session bigint, p_product bigint, n int, p_tag text) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE o bigint; h bigint; i int;
BEGIN
  INSERT INTO orders (channel, buyer_first_name, buyer_last_name, buyer_email, livemode)
  VALUES ('web', 'Buyer', p_tag, p_tag || '@example.test', false) RETURNING id INTO o;
  INSERT INTO seat_holds (session_id, order_id, kind, quantity, expires_at)
  VALUES (p_session, o, 'checkout', n, now() + interval '15 minutes') RETURNING id INTO h;
  FOR i IN 1..n LOOP
    INSERT INTO registrations (order_id, seat_hold_id, session_id, product_id,
                               attendee_first_name, attendee_last_name, attendee_email, unit_price_cents)
    VALUES (o, h, p_session, p_product, 'Student', p_tag || i, p_tag || i || '@example.test', 10800);
  END LOOP;
  -- Webhook: confirm registrations BEFORE releasing the hold (worst order).
  UPDATE registrations SET status = 'confirmed', confirmed_at = now() WHERE seat_hold_id = h;
  UPDATE seat_holds SET status = 'converted', released_at = now() WHERE id = h;
  UPDATE orders SET status = 'paid', paid_at = now() WHERE id = o;
  SET CONSTRAINTS ALL IMMEDIATE;
  SET CONSTRAINTS ALL DEFERRED;
  RETURN o;
END $$;

-- ---------------------------------------------------------------------------
-- Seat pool
-- ---------------------------------------------------------------------------
SELECT pg_temp.book(s1, ap, 7, 'ap') FROM ids;
SELECT pg_temp.book(s1, adult, 5, 'adult') FROM ids;
SELECT pg_temp.check((SELECT sold_seats = 12 AND seats_left = 0 FROM session_availability, ids WHERE session_id = s1),
  'A&P (7) and Adult (5) come out of one 12-seat pool, converting holds in any order');

SELECT pg_temp.expect_error(format('SELECT pg_temp.book(%s, %s, 1, %L)', s1, ap, 'thirteenth'), '23514',
  '13th seat refused') FROM ids;

SELECT pg_temp.expect_error(format(
  $q$INSERT INTO session_products (session_id, product_id, course_family_id, delivery_mode, price_cents)
     SELECT %s, %s, course_family_id, delivery_mode, 8800 FROM products WHERE id = %s$q$, s1, bls, bls), '23503',
  'BLS product cannot be sold from an FA/CPR/AED session') FROM ids;

-- ---------------------------------------------------------------------------
-- Holds
-- ---------------------------------------------------------------------------
INSERT INTO orders (channel, buyer_first_name, buyer_last_name, buyer_email, livemode)
VALUES ('web', 'Lapsed', 'Hold', 'lapsed@example.test', false);
INSERT INTO seat_holds (session_id, order_id, kind, quantity, expires_at)
SELECT s2, (SELECT id FROM orders WHERE buyer_email = 'lapsed@example.test'), 'checkout', 12, now() + interval '15 minutes' FROM ids;
SELECT pg_temp.check((SELECT seats_left = 0 FROM session_availability, ids WHERE session_id = s2),
  'active hold takes seats');
UPDATE seat_holds SET expires_at = now() - interval '1 second'
WHERE order_id = (SELECT id FROM orders WHERE buyer_email = 'lapsed@example.test');
SELECT pg_temp.check((SELECT seats_left = 12 FROM session_availability, ids WHERE session_id = s2),
  'expired hold releases its seats even before the expiry job runs');

-- ---------------------------------------------------------------------------
-- Capacity changes, overrides, co-instructors
-- ---------------------------------------------------------------------------
SELECT pg_temp.expect_error(format('UPDATE sessions SET capacity = 11 WHERE id = %s', s1), '23514',
  'class max cannot drop below seats sold') FROM ids;

SELECT pg_temp.expect_error(format('UPDATE sessions SET capacity = 21 WHERE id = %s', s1), '23514',
  'class max cannot exceed the room''s Max Student Count') FROM ids;

SELECT pg_temp.expect_error(format(
  $q$INSERT INTO seat_overrides (session_id, seats, reason, approved_by_user_id) VALUES (%s, 1, 'VIP', %s)$q$, s1, cs),
  '42501', 'customer service cannot approve a capacity override') FROM ids;

INSERT INTO seat_overrides (session_id, seats, reason, approved_by_user_id)
SELECT s1, 1, 'Employer group of 8 needs one more', mgr FROM ids;
SELECT pg_temp.book(s1, ap, 1, 'override') FROM ids;
SELECT pg_temp.check((SELECT sold_seats = 13 AND seats_left = 0 FROM session_availability, ids WHERE session_id = s1),
  'manager override adds a 13th seat');

SELECT pg_temp.expect_error(format(
  'UPDATE seat_overrides SET revoked_at = now(), revoked_by_user_id = %s WHERE session_id = %s', mgr, s1),
  '23514', 'override cannot be revoked once its seat is sold') FROM ids;

INSERT INTO instructors (first_name, last_name, email) VALUES
  ('Lead', 'One', 'lead1@example.test'), ('Co', 'Two', 'co2@example.test'), ('Co', 'Three', 'co3@example.test');

INSERT INTO assignments (session_id, instructor_id, role, source)
SELECT s1, (SELECT id FROM instructors WHERE email = 'lead1@example.test'), 'lead', 'portal' FROM ids;

SELECT pg_temp.expect_error(format(
  $q$INSERT INTO assignments (session_id, instructor_id, role, source)
     SELECT %s, id, 'co', 'console' FROM instructors WHERE email = 'co2@example.test'$q$, s2),
  '23514', 'no co-instructor at a one-instructor room') FROM ids;

SELECT pg_temp.expect_error(format(
  $q$INSERT INTO assignments (session_id, instructor_id, role, source)
     SELECT %s, id, 'lead', 'portal' FROM instructors WHERE email = 'lead1@example.test'$q$, s2),
  '23P01', 'instructor cannot teach two overlapping classes') FROM ids;

INSERT INTO assignments (session_id, instructor_id, role, source)
SELECT s1, (SELECT id FROM instructors WHERE email = 'co2@example.test'), 'co', 'console' FROM ids;
UPDATE sessions SET capacity = 20, capacity_basis = 'co_instructor' FROM ids WHERE id = s1;
SELECT pg_temp.check((SELECT seat_limit = 21 AND seats_left = 8 FROM session_availability, ids WHERE session_id = s1),
  'co-instructor raises class max to the room limit (20 + 1 override)');

SELECT pg_temp.expect_error(format(
  $q$INSERT INTO assignments (session_id, instructor_id, role, source)
     SELECT %s, id, 'co', 'console' FROM instructors WHERE email = 'co3@example.test'$q$, s1),
  '23505', 'only one co-instructor per class') FROM ids;

-- Rescheduling moves the instructors' blocked time with the class.
UPDATE sessions SET starts_at = starts_at + interval '1 day', ends_at = ends_at + interval '1 day',
                    previous_starts_at = starts_at, rescheduled_at = now()
FROM ids WHERE sessions.id = s1;
SELECT pg_temp.check((SELECT bool_and(a.session_range = s.time_range) FROM assignments a JOIN sessions s ON s.id = a.session_id, ids WHERE s.id = s1),
  'reschedule keeps assignment time ranges in step');
SELECT pg_temp.check((SELECT local_date = '2027-01-17' FROM sessions, ids WHERE id = s1),
  'local_date follows the class time zone');

-- ---------------------------------------------------------------------------
-- Rooms, orders, transfers
-- ---------------------------------------------------------------------------
SELECT pg_temp.expect_error(format(
  $q$INSERT INTO sessions (metro_id, site_id, room_id, course_family_id, delivery_mode, starts_at, ends_at, time_zone, slug,
                          one_instructor_max, room_max_student_count, capacity)
     SELECT metro_id, site_id, room_id, course_family_id, delivery_mode, starts_at + interval '1 hour', ends_at + interval '1 hour',
            time_zone, 'clash', 12, 12, 12 FROM sessions WHERE id = %s$q$, s2),
  '23P01', 'two live classes cannot overlap in one room') FROM ids;

SELECT pg_temp.expect_error(
  $q$WITH o AS (INSERT INTO orders (channel, buyer_first_name, buyer_last_name, buyer_email, livemode)
               VALUES ('web','Dup','Email','dup@example.test', false) RETURNING id),
          h AS (INSERT INTO seat_holds (session_id, order_id, kind, quantity, expires_at)
                SELECT (SELECT s2 FROM ids), id, 'checkout', 2, now() + interval '15 min' FROM o RETURNING id, order_id)
     INSERT INTO registrations (order_id, seat_hold_id, session_id, product_id, attendee_first_name, attendee_last_name,
                                attendee_email, unit_price_cents)
     SELECT h.order_id, h.id, (SELECT s2 FROM ids), (SELECT ap FROM ids), 'A', x::text, 'same@example.test', 10800
     FROM h, generate_series(1,2) x$q$,
  '23505', 'two attendees in one order need different emails (else GHL merges them)');

SELECT pg_temp.book(s2, ap, 12, 'small') FROM ids;
SELECT pg_temp.expect_error(format(
  $q$UPDATE registrations SET session_id = %s
     WHERE id = (SELECT id FROM registrations WHERE session_id = %s AND status = 'confirmed' ORDER BY id LIMIT 1)$q$, s2, s1),
  '23514', 'transfer into a full class is refused') FROM ids;

UPDATE sessions SET status = 'cancelled', cancelled_at = now(), cancel_reason = 'test' FROM ids WHERE id = s2;
SELECT pg_temp.expect_error(format(
  $q$INSERT INTO seat_holds (session_id, order_id, kind, quantity, expires_at)
     SELECT %s, id, 'checkout', 1, now() + interval '15 min' FROM orders LIMIT 1$q$, s2),
  '23514', 'no new holds on a cancelled class') FROM ids;

-- ---------------------------------------------------------------------------
-- Refunds, webhooks, audit
-- ---------------------------------------------------------------------------
SELECT pg_temp.expect_error(format(
  $q$INSERT INTO refunds (order_id, amount_cents, reason, requires_manager_approval, status, requested_by_user_id, approved_by_user_id)
     SELECT id, 10800, 'goodwill', true, 'approved', %s, %s FROM orders LIMIT 1$q$, cs, cs),
  '42501', 'customer service cannot approve a large refund') FROM ids;

SELECT pg_temp.expect_error(format(
  $q$INSERT INTO refunds (order_id, amount_cents, reason, requires_manager_approval, status, requested_by_user_id)
     SELECT id, 10800, 'goodwill', true, 'approved', %s FROM orders LIMIT 1$q$, cs),
  '23514', 'large refund cannot be marked approved without an approver') FROM ids;

INSERT INTO webhook_events (provider, event_id, event_type, signature_valid) VALUES ('stripe', 'evt_1', 'checkout.session.completed', true);
SELECT pg_temp.expect_error(
  $q$INSERT INTO webhook_events (provider, event_id, event_type, signature_valid) VALUES ('stripe', 'evt_1', 'checkout.session.completed', true)$q$,
  '23505', 'repeated Stripe event is rejected (processed once)');

INSERT INTO audit_log (actor_type, action, entity_type, entity_id) VALUES ('system', 'test', 'session', 1);
SELECT pg_temp.expect_error($q$UPDATE audit_log SET action = 'tampered'$q$, '42501', 'audit log rows cannot be changed');
SELECT pg_temp.expect_error($q$DELETE FROM audit_log$q$, '42501', 'audit log rows cannot be deleted');

SELECT pg_temp.check(
  (SELECT waitlist_needed_for_co_instructor = 2 AND class_price_cents = 8800 FROM session_break_even, ids WHERE session_id = s1),
  'break-even per class from its lowest price: $88 → 2 (rules doc §4 table)');

-- ---------------------------------------------------------------------------
-- Fixes from the coverage review
-- ---------------------------------------------------------------------------
-- A third class in a metro still on Enrollware, and an unpublished one in Richmond.
INSERT INTO metros (slug, name, state_code, market_code, time_zone) VALUES ('houston-tx', 'Houston', 'TX', 'HOU', 'America/Chicago');
INSERT INTO sites (metro_id, slug, name, address_line1, city, state_code, postal_code, time_zone)
SELECT id, 'galleria', 'Galleria', '1 Main St', 'Houston', 'TX', '77056', 'America/Chicago' FROM metros WHERE slug = 'houston-tx';
INSERT INTO rooms (site_id, max_student_count) SELECT id, 12 FROM sites WHERE slug = 'galleria';
INSERT INTO sessions (metro_id, site_id, room_id, course_family_id, delivery_mode, starts_at, ends_at, time_zone, slug,
                      is_published, one_instructor_max, room_max_student_count, capacity)
SELECT m.id, si.id, r.id, cf.id, 'blended', '2027-01-20 15:00+00', '2027-01-20 19:00+00', 'America/Chicago', 'hou-1',
       true, 12, 12, 12
FROM metros m JOIN sites si ON si.metro_id = m.id JOIN rooms r ON r.site_id = si.id, course_families cf
WHERE m.slug = 'houston-tx' AND cf.code = 'FA_CPR_AED';
INSERT INTO sessions (metro_id, site_id, room_id, course_family_id, delivery_mode, starts_at, ends_at, time_zone, slug,
                      is_published, one_instructor_max, room_max_student_count, capacity)
SELECT metro_id, site_id, room_id, course_family_id, delivery_mode, starts_at + interval '7 days', ends_at + interval '7 days',
       time_zone, 'unpublished', false, 12, 12, 12 FROM sessions WHERE slug = '2027-01-16-henrico-small';

SELECT pg_temp.expect_error(
  $q$INSERT INTO seat_holds (session_id, order_id, kind, quantity, expires_at)
     SELECT (SELECT id FROM sessions WHERE slug = 'hou-1'), id, 'checkout', 1, now() + interval '15 min' FROM orders LIMIT 1$q$,
  '23514', 'no sales in a metro still on Enrollware, even with Status "Enrollware"');
SELECT pg_temp.expect_error(
  $q$INSERT INTO seat_holds (session_id, order_id, kind, quantity, expires_at)
     SELECT (SELECT id FROM sessions WHERE slug = 'unpublished'), id, 'checkout', 1, now() + interval '15 min' FROM orders LIMIT 1$q$,
  '23514', 'no public sales of an unpublished class');

SELECT pg_temp.expect_error(format(
  $q$INSERT INTO session_products (session_id, product_id, course_family_id, delivery_mode, price_cents, stripe_price_id, stripe_price_amount_cents)
     VALUES (%s, %s, (SELECT course_family_id FROM products WHERE id = %s), 'blended', 9500, 'price_X', 10800)
     ON CONFLICT (session_id, product_id) DO UPDATE SET price_cents = 9500, stripe_price_id = 'price_X', stripe_price_amount_cents = 10800$q$,
  s1, ap, ap), '23514', 'a Stripe price id can never charge a different amount than the class page shows') FROM ids;

-- Enrollware seats are facts: recorded even past capacity, then nothing more sells.
INSERT INTO external_seat_reservations (session_id, external_class_ref, seats, counted_at)
SELECT s1, 'EW-123', 30, now() FROM ids;
SET CONSTRAINTS ALL IMMEDIATE; SET CONSTRAINTS ALL DEFERRED;
SELECT pg_temp.check((SELECT seats_left < 0 FROM session_availability, ids WHERE session_id = s1),
  'Enrollware import onto a full class is recorded and shows the class as oversold');
SELECT pg_temp.expect_error(format('SELECT pg_temp.book(%s, %s, 1, %L)', s1, ap, 'after_import'), '23514',
  '...and the engine sells nothing more in it') FROM ids;
DELETE FROM external_seat_reservations;

-- Waitlist size limit (s1 limit 8).
INSERT INTO waitlist_entries (session_id, preferred_product_id, first_name, last_name, email, phone_e164, party_size)
SELECT s1, ap, 'Wait', 'Group', 'group@example.test', '+18045551212', 6 FROM ids;
SELECT pg_temp.expect_error(format(
  $q$INSERT INTO waitlist_entries (session_id, preferred_product_id, first_name, last_name, email, phone_e164, party_size)
     VALUES (%s, %s, 'Too', 'Big', 'big@example.test', '+18045551213', 3)$q$, s1, ap),
  '23514', 'a group larger than the waitlist space left cannot join') FROM ids;

-- An offered seat is held from the moment the offer is queued.
SELECT pg_temp.expect_error(format(
  $q$INSERT INTO waitlist_offers (waitlist_entry_id, offered_session_id, seats, trigger, send_after)
     SELECT id, %s, 1, 'seat_freed', now() + interval '12 hours' FROM waitlist_entries LIMIT 1$q$, s1),
  '23514', 'a queued overnight waitlist offer must hold its seat (no public resale)') FROM ids;

-- Room equipped only for FA/CPR/AED cannot host IT.
INSERT INTO room_course_families (room_id, course_family_id)
SELECT r.id, cf.id FROM rooms r JOIN sites si ON si.id = r.site_id, course_families cf
WHERE si.slug = 'galleria' AND cf.code = 'FA_CPR_AED';
SELECT pg_temp.expect_error(
  $q$UPDATE sessions SET course_family_id = (SELECT id FROM course_families WHERE code = 'IT') WHERE slug = 'hou-1'$q$,
  '23514', 'a room without an IT setup cannot host Instructor Training');

-- Sessions must agree on room/site/metro.
SELECT pg_temp.expect_error(
  $q$UPDATE sessions SET metro_id = (SELECT id FROM metros WHERE slug = 'richmond-va') WHERE slug = 'hou-1'$q$,
  '23503', 'a class''s room, site and metro must match');

-- Customer messages must go through GHL.
SELECT pg_temp.expect_error(
  $q$INSERT INTO notifications (kind, audience, channel, provider, recipient, dedupe_key)
     VALUES ('no_seat_opened', 'customer', 'email', 'email_service', 'x@example.test', 'k1')$q$,
  '23514', 'customer messages cannot bypass GHL');

-- Cancelling a class frees its instructors.
UPDATE sessions SET status = 'cancelled', cancelled_at = now() FROM ids WHERE id = s1;
SELECT pg_temp.check((SELECT count(*) = 0 FROM assignments, ids WHERE session_id = s1 AND status = 'assigned'),
  'cancelling a class releases its lead and co-instructor');

-- Forfeit needs a reason code.
SELECT pg_temp.expect_error(
  $q$UPDATE registrations SET status = 'forfeited', cancelled_at = now()
     WHERE id = (SELECT min(id) FROM registrations WHERE status = 'confirmed')$q$,
  '23514', 'a forfeit or cancellation must record its reason code');

-- ---------------------------------------------------------------------------
-- Accounts, roles and permissions
-- ---------------------------------------------------------------------------
INSERT INTO user_accounts (email, display_name, status) VALUES
  ('hou-mgr@example.test', 'Houston Manager', 'active'),
  ('admin@example.test',   'Admin',           'active'),
  ('teach@example.test',   'Instructor',      'active');
INSERT INTO user_roles (user_account_id, role_code, scope_metro_id)
SELECT id, 'manager', (SELECT id FROM metros WHERE slug = 'houston-tx') FROM user_accounts WHERE email = 'hou-mgr@example.test'
UNION ALL SELECT id, 'admin', NULL FROM user_accounts WHERE email = 'admin@example.test'
UNION ALL SELECT id, 'instructor', NULL FROM user_accounts WHERE email = 'teach@example.test';

SELECT pg_temp.expect_error(
  $q$INSERT INTO seat_overrides (session_id, seats, reason, approved_by_user_id)
     SELECT (SELECT id FROM sessions WHERE slug = 'unpublished'), 1, 'test',
            (SELECT id FROM user_accounts WHERE email = 'hou-mgr@example.test')$q$,
  '42501', 'a Houston-only manager cannot add seats to a Richmond class');
INSERT INTO seat_overrides (session_id, seats, reason, approved_by_user_id)
SELECT (SELECT id FROM sessions WHERE slug = 'hou-1'), 1, 'test',
       (SELECT id FROM user_accounts WHERE email = 'hou-mgr@example.test');
SELECT pg_temp.check(true, 'a Houston-only manager can add seats to a Houston class');

SELECT pg_temp.check(
  user_has_permission((SELECT id FROM user_accounts WHERE email = 'admin@example.test'), 'pay.approve')
  AND user_has_permission((SELECT id FROM user_accounts WHERE email = 'admin@example.test'), 'settings.manage'),
  'admin holds every permission');

SELECT pg_temp.check(
  user_has_permission((SELECT id FROM user_accounts WHERE email = 'teach@example.test'), 'own.classes_view')
  AND NOT user_has_permission((SELECT id FROM user_accounts WHERE email = 'teach@example.test'), 'class.view')
  AND NOT user_has_permission((SELECT id FROM user_accounts WHERE email = 'teach@example.test'), 'student.view'),
  'instructors see only their own classes, not all classes or students');

SELECT pg_temp.check(
  (SELECT count(*) FROM user_effective_permissions WHERE user_account_id = (SELECT mgr FROM ids))
  > (SELECT count(*) FROM user_effective_permissions WHERE user_account_id = (SELECT cs FROM ids)),
  'manager includes everything customer service has, plus more');

UPDATE user_roles SET revoked_at = now(), revoked_by_user_id = (SELECT id FROM user_accounts WHERE email = 'admin@example.test')
WHERE user_account_id = (SELECT cs FROM ids);
SELECT pg_temp.check(NOT user_has_permission((SELECT cs FROM ids), 'booking.transfer'),
  'a revoked role stops working at once');

UPDATE user_accounts SET status = 'suspended' WHERE id = (SELECT mgr FROM ids);
SELECT pg_temp.check(NOT user_has_permission((SELECT mgr FROM ids), 'refund.approve_large'),
  'a suspended account loses all permissions');

SELECT pg_temp.expect_error(
  $q$INSERT INTO user_roles (user_account_id, role_code)
     SELECT id, 'instructor' FROM user_accounts WHERE email = 'teach@example.test'$q$,
  '23505', 'the same role cannot be granted twice');

-- Role grants and revokes made by a person (admin dashboard), migration 0003.
INSERT INTO user_accounts (email, display_name, status) VALUES
  ('newhire@example.test', 'New Hire', 'active'),
  ('people@example.test',  'People Admin', 'active'),
  ('admin2@example.test',  'Second Admin', 'active');
SELECT pg_temp.expect_error(
  $q$INSERT INTO user_roles (user_account_id, role_code, granted_by_user_id)
     SELECT (SELECT id FROM user_accounts WHERE email = 'newhire@example.test'), 'viewer',
            (SELECT id FROM user_accounts WHERE email = 'teach@example.test')$q$,
  '42501', 'someone without user.manage cannot grant a role');
INSERT INTO user_roles (user_account_id, role_code, granted_by_user_id)
SELECT (SELECT id FROM user_accounts WHERE email = 'newhire@example.test'), 'viewer',
       (SELECT id FROM user_accounts WHERE email = 'admin@example.test');
SELECT pg_temp.check(true, 'an admin can grant a staff role');
SELECT pg_temp.expect_error(
  $q$INSERT INTO user_roles (user_account_id, role_code, granted_by_user_id)
     SELECT (SELECT id FROM user_accounts WHERE email = 'newhire@example.test'), 'student',
            (SELECT id FROM user_accounts WHERE email = 'admin@example.test')$q$,
  '42501', 'nobody grants the student role by hand (only a paid seat does)');
INSERT INTO roles (code, name, description, dashboard_key, sign_in_method, is_staff, sort_order)
VALUES ('people_admin', 'People admin', 'test role', 'admin', 'microsoft', true, 15);
INSERT INTO role_permissions VALUES ('people_admin', 'user.manage');
INSERT INTO user_roles (user_account_id, role_code)
SELECT id, 'people_admin' FROM user_accounts WHERE email = 'people@example.test';
INSERT INTO user_roles (user_account_id, role_code, granted_by_user_id)
SELECT (SELECT id FROM user_accounts WHERE email = 'newhire@example.test'), 'operations',
       (SELECT id FROM user_accounts WHERE email = 'people@example.test');
SELECT pg_temp.check(true, 'anyone with user.manage can grant a staff role');
SELECT pg_temp.expect_error(
  $q$INSERT INTO user_roles (user_account_id, role_code, granted_by_user_id)
     SELECT (SELECT id FROM user_accounts WHERE email = 'newhire@example.test'), 'admin',
            (SELECT id FROM user_accounts WHERE email = 'people@example.test')$q$,
  '42501', 'only an admin can grant the admin role');
SELECT pg_temp.expect_error(
  $q$UPDATE user_roles SET revoked_at = now(),
            revoked_by_user_id = (SELECT id FROM user_accounts WHERE email = 'admin@example.test')
      WHERE role_code = 'admin' AND user_account_id = (SELECT id FROM user_accounts WHERE email = 'admin@example.test')$q$,
  '23001', 'the last active admin cannot lose the admin role');
SELECT pg_temp.expect_error(
  $q$UPDATE user_accounts SET status = 'suspended' WHERE email = 'admin@example.test'$q$,
  '23001', 'the last active admin cannot be suspended');
INSERT INTO user_roles (user_account_id, role_code, granted_by_user_id)
SELECT (SELECT id FROM user_accounts WHERE email = 'admin2@example.test'), 'admin',
       (SELECT id FROM user_accounts WHERE email = 'admin@example.test');
UPDATE user_roles SET revoked_at = now(),
       revoked_by_user_id = (SELECT id FROM user_accounts WHERE email = 'admin2@example.test')
 WHERE role_code = 'admin' AND user_account_id = (SELECT id FROM user_accounts WHERE email = 'admin@example.test');
SELECT pg_temp.check(
  NOT user_has_permission((SELECT id FROM user_accounts WHERE email = 'admin@example.test'), 'user.manage')
  AND user_has_permission((SELECT id FROM user_accounts WHERE email = 'admin2@example.test'), 'role.manage'),
  'with a second admin, an admin role can be handed over');

-- ---------------------------------------------------------------------------
-- Self-service accounts: password sign-in and automatic student role
-- ---------------------------------------------------------------------------
INSERT INTO user_accounts (email, display_name, status, signup_intent) VALUES
  ('student2b@example.test', 'Future Student', 'active', 'student'),
  ('wannabe@example.test',   'Says Instructor', 'active', 'instructor');
INSERT INTO user_roles (user_account_id, role_code)
SELECT id, 'user' FROM user_accounts WHERE email IN ('student2b@example.test', 'wannabe@example.test');

SELECT pg_temp.expect_error(
  $q$INSERT INTO user_identities (user_account_id, provider, subject)
     SELECT id, 'password', email FROM user_accounts WHERE email = 'wannabe@example.test'$q$,
  '23514', 'a password sign-in must have a password hash');
SELECT pg_temp.expect_error(
  $q$INSERT INTO user_identities (user_account_id, provider, subject, password_hash)
     SELECT id, 'password', 'Wannabe@Example.test', 'scrypt:x' FROM user_accounts WHERE email = 'wannabe@example.test'$q$,
  '23514', 'sign-in emails are stored lower-cased');

SELECT pg_temp.check(
  NOT user_has_permission((SELECT id FROM user_accounts WHERE email = 'wannabe@example.test'), 'own.classes_view'),
  'signing up "as an instructor" grants no instructor access');

-- Buying a seat makes the account a student (attendee email matches).
INSERT INTO orders (channel, buyer_first_name, buyer_last_name, buyer_email, livemode)
VALUES ('web', 'Future', 'Student', 'student2b@example.test', false);
UPDATE metros SET booking_system = 'engine', cutover_at = now() WHERE slug = 'houston-tx';
INSERT INTO seat_holds (session_id, order_id, kind, quantity, expires_at)
SELECT (SELECT id FROM sessions WHERE slug = 'hou-1'), (SELECT max(id) FROM orders), 'checkout', 1, now() + interval '15 min';
SELECT pg_temp.check(
  NOT user_has_permission((SELECT id FROM user_accounts WHERE email = 'student2b@example.test'), 'self.bookings_view'),
  'a regular user cannot see bookings before buying');
INSERT INTO session_products (session_id, product_id, course_family_id, delivery_mode, price_cents)
SELECT s.id, p.id, p.course_family_id, p.delivery_mode, p.base_price_cents
FROM sessions s JOIN products p ON p.id = (SELECT ap FROM ids) WHERE s.slug = 'hou-1'
ON CONFLICT DO NOTHING;
INSERT INTO registrations (order_id, seat_hold_id, session_id, product_id, attendee_first_name, attendee_last_name,
                           attendee_email, unit_price_cents, status, confirmed_at)
SELECT (SELECT max(id) FROM orders), (SELECT max(id) FROM seat_holds),
       (SELECT id FROM sessions WHERE slug = 'hou-1'), (SELECT ap FROM ids),
       'Future', 'Student', 'student2b@example.test', 10800, 'confirmed', now();
SELECT pg_temp.check(
  user_has_permission((SELECT id FROM user_accounts WHERE email = 'student2b@example.test'), 'self.bookings_view'),
  'paying for a seat makes the account a student automatically');

-- ap1@example.test booked earlier in this script as a guest (no account then).
INSERT INTO user_accounts (email, display_name, status) VALUES
  ('ap1@example.test', 'Booked Before Signing Up', 'active'),
  ('never-booked@example.test', 'Never Booked', 'active');
CREATE TEMP TABLE grant_results AS
SELECT grant_student_role_if_booked((SELECT id FROM user_accounts WHERE email = 'ap1@example.test'))          AS booked,
       grant_student_role_if_booked((SELECT id FROM user_accounts WHERE email = 'never-booked@example.test')) AS not_booked;
SELECT pg_temp.check(
  (SELECT booked AND NOT not_booked FROM grant_results)
  AND user_has_permission((SELECT id FROM user_accounts WHERE email = 'ap1@example.test'), 'self.bookings_view')
  AND NOT user_has_permission((SELECT id FROM user_accounts WHERE email = 'never-booked@example.test'), 'self.bookings_view'),
  'someone who booked as a guest becomes a student when they create an account; others do not');

\echo 'All schema checks passed.'
ROLLBACK;
