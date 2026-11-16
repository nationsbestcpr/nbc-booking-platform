-- 0009: the booking desk's actions (move, cancel, phone and invoice bookings),
-- attendance, and the waiting list (launch hub 9b, 9c, 9d).
-- Frozen: later changes to db/schema.sql never change this file.

-- Business settings the new pages read.
INSERT INTO app_settings (key, value, description) VALUES
  ('phone_link_hold_minutes', '120',
   'How long a payment link sent by customer service holds the seats (30 to 1440 minutes, Stripe''s limits). OPEN: confirm (hub open item 12).'),
  ('invoice_due_days', '30',
   'Days until an invoice or PO booking is due, unless customer service picks another date. OPEN: NBC''s invoice terms (Jennifer).'),
  ('attendance_instructor_days', '7',
   'Instructors can mark attendance from class day until this many days after it. Office staff with the attendance permission can mark any time.');

-- GHL tags for the new moments (a GHL workflow starts on each). Names already
-- changed in Business settings are kept.
UPDATE app_settings
   SET value = '{"payment_link":"nbc-send-payment-link","class_moved":"nbc-class-moved","waitlist":"nbc-waitlist","waitlist_offer":"nbc-waitlist-offer","offer_expired":"nbc-offer-expired","waitlist_no_seat":"nbc-waitlist-no-seat"}'::jsonb || value
 WHERE key = 'ghl_tags' AND jsonb_typeof(value) = 'object';

-- A Stripe page can collect a transfer's price difference: money with no seat
-- to hold. Only those pages have no seat hold (orders.channel 'transfer_adjustment').
ALTER TABLE checkout_sessions ALTER COLUMN seat_hold_id DROP NOT NULL;

-- Waiting-list size and location type come from the room (hub 9c, 9d): a room
-- that holds more than one instructor's max is a co-instructor location; its
-- list is the room space above that max, up to one instructor's worth. A
-- one-instructor location lists one instructor's worth (12 / 9 / 6).
CREATE FUNCTION trg_session_waitlist_rules() RETURNS trigger LANGUAGE plpgsql AS $$
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
CREATE TRIGGER sessions_waitlist_rules BEFORE INSERT OR UPDATE OF one_instructor_max, room_max_student_count
  ON sessions FOR EACH ROW EXECUTE FUNCTION trg_session_waitlist_rules();
UPDATE sessions SET one_instructor_max = one_instructor_max;

-- A waiting-list entry's offers are looked up by entry.
CREATE INDEX waitlist_offers_entry_idx ON waitlist_offers (waitlist_entry_id);
