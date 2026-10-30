-- =============================================================================
-- NBC Booking Engine — reference data
-- =============================================================================
-- Values here come from the NBC Booking Launch Hub (as of Oct 13–14, 2026).
-- Locations, sites, rooms, products and classes are NOT seeded: the Airtable
-- sync creates them. Settings marked "OPEN" still need a decision.
-- =============================================================================

BEGIN;

INSERT INTO course_families (code, name, slug, one_instructor_max, min_students_to_run) VALUES
  ('FA_CPR_AED', 'First Aid / CPR / AED',       'first-aid-cpr-aed', 12, NULL),
  ('BLS',        'Basic Life Support (BLS)',     'bls',                9, NULL),
  ('IT',         'Instructor Training',          'instructor-training', 6, 5);  -- "5 students are required to hold a class"

INSERT INTO app_settings (key, value, description) VALUES
  ('checkout_hold_minutes',          '15',    'Seat hold while the customer pays on Stripe (decision D2).'),
  ('stripe_checkout_expiry_minutes', '30',    'Stripe minimum; the engine expires the session itself at the hold limit.'),
  ('waitlist_offer_hours',           '2',     'Pay window for a waitlist offer.'),
  ('waitlist_offer_window_local',    '{"start":"08:00","end":"18:00"}', 'Offers are sent only in these class-local hours.'),
  ('waitlist_cutoff_one_instructor_hours', '24', 'One-instructor locations: waitlist closes 24 h before class.'),
  ('waitlist_cutoff_co_instructor',  '{"local_time":"08:00","days_before":1,"sun_mon_use_saturday":true}',
                                     'Co-instructor locations: 8 AM the day before (Saturday 8 AM for Sunday/Monday classes).'),
  ('waitlist_demand_alert_one_instructor', '3', 'Waiting count that triggers the second-session alert to the OTL.'),
  ('co_instructor_pay_cents',        '11000', 'Co-instructor pay used in the break-even formula ($110).'),
  ('cert_cost_cents_per_student',    '1000',  'Cert cost per student used in the break-even formula ($10).'),
  ('rule_a_bands',                   '[{"days":[2,3],"pct":67},{"days":[4,6],"pct":50},{"days":[7,9],"pct":42},{"days":[10,14],"pct":33},{"days":[15,30],"pct":25}]',
                                     'Ops Hub trigger to start a co-instructor search (fill % by days out).'),
  ('coverage_alert_days',            '[21,14,7]', 'Days before class that an unassigned class alerts ops; 7 escalates to Jennifer or Bre.'),
  ('instructor_drop_deadline_days',  '7',     'Instructors may release a class up to this many days before it.'),
  ('hub_first_right_days',           '7',     'HUB metros (DFW, Richmond): first right of refusal window.'),
  ('large_refund_threshold_cents',   'null',  'OPEN: refund amount that needs manager approval (not set in the Oct 14 policy).'),
  ('retention_days_client_ip',       '90',    'Purge orders.client_ip / user agent after this many days (proposed).'),
  ('retention_days_webhook_payload', '30',    'Purge webhook_events.payload after this many days (proposed).'),
  ('ghl_custom_field_ids',           '{}',    'GHL custom field ids (session id, site, date, product, Reg Type, UTMs). Differ between sandbox and live.'),
  ('environment',                    '"local"', 'local | staging | production. Set per database; staging uses Stripe test mode and GHL Sandbox 2.'),
  ('airtable_writeback_enabled',     'false', 'Only production writes back to the NBC Master Base (there is one base; staging must not write to it).'),
  ('customer_service_contact',       '{"email":"info@nationsbestcpr.com"}', 'Shown in the "Offer expired" message (waitlist rules §10, recommended).');

-- -----------------------------------------------------------------------------
-- Roles (one dashboard each) and permissions
-- -----------------------------------------------------------------------------
-- sort_order decides which role the app treats as someone's main one after sign-in.
INSERT INTO roles (code, name, description, dashboard_key, sign_in_method, is_staff, is_self_service, grants_all, is_scopable, sort_order) VALUES
  ('admin',             'Admin',                     'Everything, including people, roles, settings and billing items.',                 'admin',             'microsoft', true,  false, true,  false, 10),
  ('manager',           'Manager',                   'Customer service plus large refunds, extra seats over the limit and the audit log.', 'manager',           'microsoft', true,  false, false, true,  20),
  ('customer_service',  'Customer service',          'Find classes and students; book by phone or invoice; transfer, cancel, refund within policy.', 'customer_service', 'microsoft', true, false, false, true, 30),
  ('operations',        'Operations',                'Teaching side: post classes to instructors, approve who teaches, add helpers, coverage.',     'operations',       'microsoft', true, false, false, true, 40),
  ('helper_recruiting', 'Helper instructor recruiting', 'Gets alerts when a waiting list justifies a helper instructor; sees demand and coverage.', 'helper_recruiting', 'microsoft', true, false, false, true, 50),
  ('location_approver', 'Second-class approver',     'Gets alerts when a small room has enough people waiting for a second class.',      'location_approver', 'microsoft', true,  false, false, true,  60),
  ('viewer',            'Viewer',                    'Read-only overview: classes, waiting lists, coverage and reports.',                'viewer',            'microsoft', true,  false, false, true,  70),
  ('instructor',        'Instructor',                'Own classes only: requests, schedule, class lists, attendance, availability, pay. Same role whether lead or helper in a class.', 'instructor', 'password', false, false, false, false, 80),
  ('student',           'Student',                   'Has booked a class: sees their bookings and certificates. Granted automatically when a seat is paid for.', 'student', 'password', false, true, false, false, 90),
  ('user',              'User',                      'Anyone with an account: browse and book classes, manage their profile. Every new account starts here.',     'user',    'password', false, true, false, false, 100);

INSERT INTO permissions (code, area, description, own_records_only) VALUES
  -- Classes
  ('class.view',               'Classes',     'See every class, live seat numbers and class lists', false),
  ('class.packet_view',        'Classes',     'See private class details (access code, on-site contact)', false),
  ('class.roster_export',      'Classes',     'Export a class list for the Red Cross', false),
  ('class.capacity_override',  'Classes',     'Add seats beyond a class''s limit (with a reason)', false),
  ('class.sync_now',           'Classes',     'Pull a class from the planning spreadsheet right now', false),
  -- Students and bookings
  ('student.view',             'Students',    'Find students; see their bookings, payments, transfers, notes and messages', false),
  ('student.edit',             'Students',    'Fix a student''s name, email or phone; resend the confirmation', false),
  ('student.note',             'Students',    'Add notes', false),
  ('booking.create_phone',     'Bookings',    'Book a caller by sending a payment link', false),
  ('booking.create_invoice',   'Bookings',    'Book a company on an invoice or purchase order', false),
  ('booking.transfer',         'Bookings',    'Move a student to another class', false),
  ('booking.transfer_bulk',    'Bookings',    'Move all students of a cancelled class, or send pick-a-date links', false),
  ('booking.cancel',           'Bookings',    'Cancel a booking', false),
  ('booking.attendance',       'Bookings',    'Check students in and mark Complete / No Show on any class', false),
  -- Money
  ('payment.view',             'Money',       'See payments and refunds', false),
  ('refund.issue',             'Money',       'Refund within the written policy, below the large-refund threshold', false),
  ('refund.approve_large',     'Money',       'Approve refunds above the large-refund threshold', false),
  -- Waiting lists
  ('waitlist.view',            'Waiting lists', 'See waiting lists and offers', false),
  ('waitlist.manage',          'Waiting lists', 'Remove people; re-offer a seat after "Offer expired"', false),
  -- Teaching (office side)
  ('instructor.view',          'Teaching',    'See instructor profiles, certifications and availability', false),
  ('instructor.manage',        'Teaching',    'Add instructors, verify certifications and equipment, invite to the portal', false),
  ('teaching.coverage_view',   'Teaching',    'See classes that still need an instructor', false),
  ('teaching.post_offer',      'Teaching',    'Post classes for instructors to request', false),
  ('teaching.award',           'Teaching',    'Approve who teaches a class', false),
  ('teaching.helper_assign',   'Teaching',    'Add or remove a helper instructor', false),
  ('pay.view_all',             'Teaching',    'See every instructor pay record', false),
  ('pay.approve',              'Teaching',    'Approve pay records for export to bookkeeping', false),
  -- Alerts (who receives them)
  ('alert.coverage',           'Alerts',      'Receive 21 / 14 / 7-day "no instructor yet" alerts', false),
  ('alert.helper_demand',      'Alerts',      'Receive "waiting list justifies a helper instructor" alerts', false),
  ('alert.second_session',     'Alerts',      'Receive "enough waiting for a second class" alerts', false),
  -- Instructor's own records
  ('own.classes_view',         'My teaching', 'My classes: live seats, waiting count, class list and private details', true),
  ('own.attendance',           'My teaching', 'Check in and mark my students Complete / No Show', true),
  ('own.roster_export',        'My teaching', 'Export my class list for the Red Cross', true),
  ('own.offers_request',       'My teaching', 'See open classes I qualify for and request them', true),
  ('own.class_release',        'My teaching', 'Release a class up to 7 days before it', true),
  ('own.availability_manage',  'My teaching', 'Set my availability and time off', true),
  ('own.certs_view',           'My teaching', 'See my certifications and expiry dates', true),
  ('own.pay_view',             'My teaching', 'See my pay records', true),
  -- Customer self-service
  ('self.profile_manage',      'My account',  'Update my name, email, phone and password', true),
  ('self.book',                'My account',  'Book classes for myself or a group', true),
  ('self.bookings_view',       'My account',  'See my bookings, receipts and certificates', true),
  -- Administration
  ('user.manage',              'Admin',       'Invite people; grant and revoke roles', false),
  ('role.manage',              'Admin',       'Change which permissions each role grants', false),
  ('settings.manage',          'Admin',       'Change business settings (hold minutes, alert days, ...)', false),
  ('content.edit',             'Admin',       'Edit public page wording', false),
  ('content.approve',          'Admin',       'Approve public page wording', false),
  ('system.health_view',       'Admin',       'See sync status, the unsynced list and data problems', false),
  ('system.issue_resolve',     'Admin',       'Mark data problems resolved', false),
  ('audit.view',               'Admin',       'See the full action log', false),
  ('report.view',              'Admin',       'See reports and numbers', false);

-- customer_service
INSERT INTO role_permissions (role_code, permission_code)
SELECT 'customer_service', unnest(ARRAY[
  'class.view', 'student.view', 'student.edit', 'student.note',
  'booking.create_phone', 'booking.create_invoice', 'booking.transfer', 'booking.transfer_bulk', 'booking.cancel',
  'payment.view', 'refund.issue', 'waitlist.view', 'waitlist.manage', 'system.health_view']);

-- manager = customer service + the manager-only decisions
INSERT INTO role_permissions (role_code, permission_code)
SELECT 'manager', permission_code FROM role_permissions WHERE role_code = 'customer_service'
UNION
SELECT 'manager', unnest(ARRAY[
  'refund.approve_large', 'class.capacity_override', 'class.packet_view', 'booking.attendance',
  'teaching.coverage_view', 'audit.view', 'report.view', 'system.issue_resolve', 'content.approve']);

-- operations
INSERT INTO role_permissions (role_code, permission_code)
SELECT 'operations', unnest(ARRAY[
  'class.view', 'class.packet_view', 'class.roster_export', 'class.sync_now', 'booking.attendance',
  'waitlist.view', 'instructor.view', 'instructor.manage',
  'teaching.coverage_view', 'teaching.post_offer', 'teaching.award', 'teaching.helper_assign',
  'pay.view_all', 'alert.coverage', 'system.health_view', 'system.issue_resolve', 'content.edit']);

-- helper_recruiting
INSERT INTO role_permissions (role_code, permission_code)
SELECT 'helper_recruiting', unnest(ARRAY[
  'class.view', 'waitlist.view', 'instructor.view', 'teaching.coverage_view', 'alert.helper_demand']);

-- location_approver
INSERT INTO role_permissions (role_code, permission_code)
SELECT 'location_approver', unnest(ARRAY['class.view', 'waitlist.view', 'alert.second_session']);

-- viewer
INSERT INTO role_permissions (role_code, permission_code)
SELECT 'viewer', unnest(ARRAY['class.view', 'waitlist.view', 'teaching.coverage_view', 'report.view', 'system.health_view']);

-- user (every account) and student (has booked)
INSERT INTO role_permissions (role_code, permission_code)
SELECT 'user', unnest(ARRAY['self.profile_manage', 'self.book']);
INSERT INTO role_permissions (role_code, permission_code)
SELECT 'student', unnest(ARRAY['self.profile_manage', 'self.book', 'self.bookings_view']);

-- instructor (lead or helper: same permissions; limited to their own classes)
INSERT INTO role_permissions (role_code, permission_code)
SELECT 'instructor', code FROM permissions WHERE code LIKE 'own.%';

COMMIT;
