"""Test data for the test site (never the live site).

Loads a handful of invented classes, with bookings, at a real city and location from the schedule,
so every desk action can be tried at once: full classes with people waiting, a full class with
nobody waiting yet, classes with paid, invoiced and group bookings, a refund waiting for approval, a
class running today (attendance) and a cancelled class whose students need moving.

The classes are made here, not in Airtable, so the Airtable sync leaves them alone. Their web
address starts with `nbc-test-`; "Remove" deletes them with every booking, waiting-list entry,
payment and refund that hangs off them (moves to and from them included), and "Load" removes the
previous set first. Nothing is sent to GHL or Stripe while loading: attendees have @example.test
addresses. Acting on them later (cancel, move, offer a seat) sends the usual GHL updates.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from flask import current_app, has_request_context
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from . import audit, db

PREFIX = "nbc-test-"
RETIRED = "nbc-test-retired-"
SLOTS = (9, 13, 17, 7, 11, 15, 19)                     # class-time start hours to try, in order


class TestDataError(Exception):
    pass


@dataclass(frozen=True)
class Kind:
    key: str
    title: str
    try_it: str


KINDS = {k.key: k for k in (
    Kind("full-waiting", "Full, 2 groups waiting",
         "Cancel a seat: the first person waiting is offered it (8 AM – 6 PM class time, 2 hours to pay). "
         "Cancel a second seat to see the party of 2 offered only once both seats are free."),
    Kind("full-co", "Full, co-instructor location, 1 waiting",
         "The list closes at 8 AM the day before (Saturday 8 AM for Sunday and Monday classes)."),
    Kind("full-nobody", "Full, nobody waiting",
         "Join the waiting list from the class's booking page with your own email to see the GHL "
         "messages. Move a seat in here to see that only a manager can add a seat over the limit."),
    Kind("open", "Open, with bookings to change",
         "Move, cancel and refund: card bookings (Adult and A&P), a group booked by someone else, an "
         "invoice waiting for payment and a refund waiting for approval."),
    Kind("open-later", "Open, a later date to move to",
         "Pick this class when moving a seat, or when moving everyone from the cancelled class."),
    Kind("today", "Running today",
         "Attendance: check in, mark Complete or No show, finish the class, download the Red Cross list."),
    Kind("cancelled", "Cancelled, students still to move",
         "Move everyone to another date in one go (the open later class has room)."),
)}

PEOPLE = [("Avery", "Quinn"), ("Jordan", "Blake"), ("Riley", "Moss"), ("Casey", "Hart"), ("Morgan", "Lane"),
          ("Taylor", "Reed"), ("Jamie", "Cole"), ("Drew", "Park"), ("Skyler", "Ford"), ("Rowan", "Shaw"),
          ("Emerson", "Price"), ("Hayden", "Cross"), ("Parker", "Wade"), ("Reese", "Grant"), ("Sage", "Holt"),
          ("Quinn", "Ellis"), ("Blair", "Nash"), ("Dakota", "Rhodes"), ("Finley", "Stone"), ("Harper", "Vale"),
          ("Kendall", "Brooks"), ("Logan", "Pierce"), ("Marley", "Dunn"), ("Noel", "Frost"), ("Oakley", "Hale"),
          ("Peyton", "Kerr"), ("Remy", "Lowe"), ("Shay", "Marsh"), ("Tatum", "North"), ("Wren", "Oliver"),
          ("Ari", "Banks"), ("Bailey", "Carr"), ("Cameron", "Doyle"), ("Devon", "Eaton"), ("Ellis", "Fox"),
          ("Frankie", "Gale"), ("Gray", "Hunt"), ("Indy", "Irwin"), ("Jules", "Joyce"), ("Kai", "Knox")]


# --- Where it may run -----------------------------------------------------------------------

def refused() -> str | None:
    """Why test data can't be loaded here, or None. Never on the live site or a live database."""
    if current_app.config["APP_ENV"] == "production":
        return "This is the live site. Test data can only be loaded on the test site."
    label = db.fetch_value("SELECT value #>> '{}' FROM app_settings WHERE key = 'environment'")
    if label == "production":
        return "This database is labelled as the live one, so test data can't be loaded into it."
    return None


def classes() -> list[dict]:
    """The loaded test classes, in the order of KINDS, with what to try in each."""
    rows = db.fetch_all("""
        SELECT s.id, s.public_id, s.slug, s.starts_at, s.ends_at, s.time_zone, s.status, s.location_type,
               cf.name AS course_name, si.name AS site_name, m.name AS city_name,
               av.sold_seats, av.seat_limit, av.seats_left, av.waitlist_count
          FROM sessions s
          JOIN course_families cf ON cf.id = s.course_family_id
          JOIN sites si ON si.id = s.site_id
          JOIN metros m ON m.id = s.metro_id
          JOIN session_availability av ON av.session_id = s.id
         WHERE s.slug LIKE :p AND s.slug NOT LIKE :r
    """, p=PREFIX + "%", r=RETIRED + "%")
    order = list(KINDS)
    out = []
    for r in rows:
        key = next((k for k in sorted(order, key=len, reverse=True)       # "open-later" before "open"
                    if r["slug"].startswith(f"{PREFIX}{k}-")), None)
        if key:
            out.append({**r, "kind": KINDS[key], "sort": order.index(key)})
    return sorted(out, key=lambda r: r["sort"])


# --- Loading --------------------------------------------------------------------------------

def load(user_id: int) -> dict:
    """Remove any earlier test data, then load a fresh set. Returns {city, site, course, classes}."""
    why = refused()
    if why:
        raise TestDataError(why)
    with db.transaction() as conn:
        removed = _remove(conn)
        t = _template(conn)
        people = iter(PEOPLE)
        made = _Maker(conn, t, user_id, people)
        made.build()
        _audit(conn, "test_data.load", {"city": t["city_name"], "site": t["site_name"],
                                        "course": t["course_name"], "classes": made.created,
                                        "removed_before": removed})
    return {"city": t["city_name"], "site": t["site_name"], "course": t["course_name"],
            "classes": len(made.created), "removed": removed}


def remove(user_id: int) -> dict:
    why = refused()
    if why:
        raise TestDataError(why)
    with db.transaction() as conn:
        result = _remove(conn)
        _audit(conn, "test_data.remove", result)
    return result


def _audit(conn: Connection, action: str, after: dict) -> None:
    """From the admin page: the signed-in admin did it. From the command line: the system did."""
    if has_request_context():
        audit.record(conn, action, "session", None, after=after)
        return
    import json
    conn.execute(text("""
        INSERT INTO audit_log (actor_type, action, entity_type, after)
        VALUES ('system', :a, 'session', CAST(:after AS jsonb))
    """), {"a": action, "after": json.dumps(after, default=str)})


def default_admin() -> int | None:
    """For the command line: the first active admin account."""
    return db.fetch_value("""
        SELECT a.id FROM user_accounts a JOIN user_roles ur ON ur.user_account_id = a.id
          JOIN roles r ON r.code = ur.role_code
         WHERE r.grants_all AND ur.revoked_at IS NULL AND a.status = 'active' ORDER BY a.id LIMIT 1
    """)


def _template(conn: Connection) -> dict:
    """A class from the schedule to copy the place, course and prices from: an upcoming in-person
    class in a city that books here, offering both A&P and Adult if possible."""
    t = conn.execute(text("""
        SELECT s.*, cf.name AS course_name, si.name AS site_name, m.name AS city_name,
               (SELECT count(*) FROM session_products sp WHERE sp.session_id = s.id AND sp.is_offered) AS offers
          FROM sessions s
          JOIN metros m ON m.id = s.metro_id
          JOIN sites si ON si.id = s.site_id
          JOIN course_families cf ON cf.id = s.course_family_id
         WHERE m.booking_system = 'engine' AND s.status = 'scheduled' AND s.slug NOT LIKE :p
           AND s.room_id IS NOT NULL
           AND EXISTS (SELECT 1 FROM session_products sp WHERE sp.session_id = s.id AND sp.is_offered)
         ORDER BY (SELECT count(*) FROM session_products sp WHERE sp.session_id = s.id AND sp.is_offered) >= 2 DESC,
                  s.starts_at > now() DESC, s.starts_at
         LIMIT 1
    """), {"p": PREFIX + "%"}).mappings().first()
    if not t:
        raise TestDataError("No class at a location in a city that books here yet. Sync the schedule from Airtable "
                            "first (Integrations), then load the test data.")
    products = conn.execute(text("""
        SELECT product_id, price_cents FROM session_products WHERE session_id = :s AND is_offered
         ORDER BY price_cents
    """), {"s": t["id"]}).mappings().all()
    rooms = [r for (r,) in conn.execute(text("""
        SELECT DISTINCT s.room_id FROM sessions s
         WHERE s.metro_id = :m AND s.course_family_id = :cf AND s.delivery_mode = :dm
           AND s.room_id IS NOT NULL AND s.room_id <> :r
    """), {"m": t["metro_id"], "cf": t["course_family_id"], "dm": t["delivery_mode"], "r": t["room_id"]})]
    return {**t, "products": [dict(p) for p in products], "rooms": [t["room_id"], *rooms]}


class _Maker:
    def __init__(self, conn: Connection, t: dict, user_id: int, people):
        self.conn, self.t, self.user_id, self.people = conn, t, user_id, people
        self.tz = ZoneInfo(t["time_zone"])
        self.today = datetime.now(self.tz).date()
        self.adult = t["products"][0]
        self.ap = t["products"][-1]
        self.created: list[str] = []

    # -- the set --
    def build(self) -> None:
        full = self._class("full-waiting", self.today + timedelta(days=8), seats=4)
        self._seats(full, 4)
        self._wait(full, party=1, minutes_ago=90)
        self._wait(full, party=2, minutes_ago=45)

        co = self._class("full-co", self.today + timedelta(days=9), seats=6, one_max=4)
        self._seats(co, 6)
        self._wait(co, party=1, minutes_ago=30)

        nobody = self._class("full-nobody", self.today + timedelta(days=10), seats=3)
        self._seats(nobody, 3)

        later = self._class("open-later", self.today + timedelta(days=14), seats=12)
        self._seats(later, 2)

        open_ = self._class("open", self.today + timedelta(days=11), seats=12)
        self._seats(open_, 3, product=self.adult)
        regs = self._seats(open_, 2, product=self.ap)
        self._group(open_)
        self._invoice(open_)
        self._refund_waiting(regs[0])

        gone = self._class("cancelled", self.today + timedelta(days=12), seats=8)
        self._seats(gone, 3)
        self.conn.execute(text("""
            UPDATE sessions SET status = 'cancelled', cancelled_at = now(),
                   cancel_reason = 'Test class: cancelled so its students can be moved together' WHERE id = :s"""),
            {"s": gone["id"]})

        today = self._class("today", self.today + timedelta(days=13), seats=8)
        self._seats(today, 5)
        self._run_today(today)

    # -- classes --
    def _class(self, key: str, day: date, *, seats: int, one_max: int | None = None) -> dict:
        """A class like the template, `seats` seats, at the first free slot on or after `day`."""
        one = one_max or seats
        duration = self.t["ends_at"] - self.t["starts_at"]
        for offset in range(0, 21):
            for hour in SLOTS:
                for room in self.t["rooms"]:
                    start = datetime.combine(day + timedelta(days=offset), time(hour), self.tz)
                    try:
                        with self.conn.begin_nested():
                            row = self.conn.execute(text("""
                                INSERT INTO sessions (metro_id, site_id, room_id, course_family_id, delivery_mode,
                                                      starts_at, ends_at, time_zone, slug, status, is_published,
                                                      one_instructor_max, room_max_student_count, capacity)
                                SELECT :m, r.site_id, r.id, :cf, :dm, :st, :en, :tz, :slug, 'scheduled', true,
                                       :one, :room_max, :cap
                                  FROM rooms r WHERE r.id = :room
                                RETURNING id, public_id
                            """), {"m": self.t["metro_id"], "cf": self.t["course_family_id"],
                                   "dm": self.t["delivery_mode"], "st": start, "en": start + duration,
                                   "tz": self.t["time_zone"], "slug": f"{PREFIX}{key}-{uuid.uuid4().hex[:6]}",
                                   "one": one, "room_max": seats, "cap": seats, "room": room}).mappings().one()
                            self.conn.execute(text("""
                                INSERT INTO session_products (session_id, product_id, course_family_id, delivery_mode,
                                                              price_cents)
                                SELECT :s, product_id, course_family_id, delivery_mode, price_cents
                                  FROM session_products WHERE session_id = :t AND is_offered
                            """), {"s": row["id"], "t": self.t["id"]})
                    except DBAPIError:
                        continue                      # room busy then (or can't host it): next slot
                    self.created.append(key)
                    return {"id": row["id"], "public_id": row["public_id"], "starts_at": start,
                            "duration": duration, "room_id": room}
        raise TestDataError(f"No free room and time for the test class '{KINDS[key].title}' in the next three weeks.")

    def _run_today(self, s: dict) -> None:
        """Move the class (already booked) to today, started an hour ago, and give the signed-in
        account the lead if they're an instructor."""
        now = datetime.now(self.tz)
        candidates = [max(now - timedelta(hours=1), datetime.combine(self.today, time(0, 5), self.tz))]
        candidates += [datetime.combine(self.today, time(h), self.tz) for h in (6, 7, 8, 9, 10)]
        for start in candidates:
            for room in self.t["rooms"]:
                try:
                    with self.conn.begin_nested():
                        self.conn.execute(text("""
                            UPDATE sessions SET starts_at = :st, ends_at = :en, room_id = r.id, site_id = r.site_id
                              FROM rooms r WHERE r.id = :room AND sessions.id = :s
                        """), {"st": start, "en": start + s["duration"], "room": room, "s": s["id"]})
                except DBAPIError:
                    continue
                self._assign_me(s["id"])
                return
        raise TestDataError("No free room today for the test class that runs today.")

    def _assign_me(self, session_id: int) -> None:
        inst = self.conn.execute(text("SELECT id FROM instructors WHERE user_account_id = :u"),
                                 {"u": self.user_id}).scalar()
        if not inst:
            return
        try:
            with self.conn.begin_nested():
                self.conn.execute(text("""
                    INSERT INTO assignments (session_id, instructor_id, role, source, rate_cents, session_range)
                    SELECT id, :i, 'lead', 'console', 0, time_range FROM sessions WHERE id = :s
                """), {"i": inst, "s": session_id})
        except DBAPIError:
            pass                                       # teaching something else then: the office can still mark

    # -- bookings --
    def _person(self) -> tuple[str, str, str]:
        first, last = next(self.people)
        return first, last, f"test.{first.lower()}.{last.lower()}.{uuid.uuid4().hex[:4]}@example.test"

    def _order(self, *, channel: str, buyer: tuple[str, str, str], total: int, status: str, paid: int,
               extra: dict | None = None) -> int:
        from .integrations.checkout import _person
        extra = extra or {}
        oid = self.conn.execute(text("""
            INSERT INTO orders (channel, status, buyer_first_name, buyer_last_name, buyer_email, buyer_phone_e164,
                                buyer_company, po_number, invoice_due_on, subtotal_cents, total_cents, livemode,
                                paid_at, amount_paid_cents, created_by_user_id)
            VALUES (:ch, :st, :f, :l, :e, '+15555550100', :co, :po, :due, :t, :t, false,
                    CASE WHEN :st = 'paid' THEN now() END, :paid, CASE WHEN :ch <> 'web' THEN CAST(:u AS bigint) END)
            RETURNING id
        """), {"ch": channel, "st": status, "f": buyer[0], "l": buyer[1], "e": buyer[2], "t": total, "paid": paid,
               "co": extra.get("company"), "po": extra.get("po"), "due": extra.get("due"), "u": self.user_id}).scalar_one()
        pid = _person(self.conn, buyer[0], buyer[1], buyer[2], "+15555550100")
        self.conn.execute(text("UPDATE orders SET buyer_person_id = :p WHERE id = :o"), {"p": pid, "o": oid})
        return oid

    def _register(self, s: dict, order_id: int, product: dict, who: tuple[str, str, str], *, paid: bool,
                  hold: int | None) -> int:
        from .integrations.checkout import _person
        pid = _person(self.conn, who[0], who[1], who[2], None)
        rid = self.conn.execute(text("""
            INSERT INTO registrations (order_id, seat_hold_id, session_id, product_id, person_id, attendee_first_name,
                                       attendee_last_name, attendee_email, unit_price_cents, status, confirmed_at,
                                       amount_paid_cents)
            VALUES (:o, :h, :s, :p, :pid, :f, :l, :e, :c, :st, CASE WHEN :paid THEN now() END, :a)
            RETURNING id
        """), {"o": order_id, "h": hold, "s": s["id"], "p": product["product_id"], "pid": pid, "f": who[0],
               "l": who[1], "e": who[2], "c": product["price_cents"], "paid": paid,
               "st": "confirmed" if paid else "awaiting_payment",
               "a": product["price_cents"] if paid else 0}).scalar_one()
        self.conn.execute(text("UPDATE people SET first_order_id = COALESCE(first_order_id, :o) WHERE id = :p"),
                          {"o": order_id, "p": pid})
        return rid

    def _hold(self, s: dict, order_id: int, n: int) -> int:
        return self.conn.execute(text("""
            INSERT INTO seat_holds (session_id, order_id, kind, quantity, expires_at, status, released_at)
            VALUES (:s, :o, 'checkout', :n, now() + interval '15 minutes', 'converted', now()) RETURNING id
        """), {"s": s["id"], "o": order_id, "n": n}).scalar_one()

    def _seats(self, s: dict, n: int, *, product: dict | None = None) -> list[int]:
        """`n` paid online bookings of one seat each (test bookings: no card)."""
        regs = []
        for i in range(n):
            p = product or (self.ap if i % 2 else self.adult)
            who = self._person()
            order = self._order(channel="web", buyer=who, total=p["price_cents"], status="paid",
                                paid=p["price_cents"])
            regs.append(self._register(s, order, p, who, paid=True, hold=self._hold(s, order, 1)))
        return regs

    def _group(self, s: dict) -> None:
        """A manager books two of their staff and pays (booked by someone else)."""
        buyer = self._person()
        p = self.adult
        order = self._order(channel="web", buyer=buyer, total=2 * p["price_cents"], status="paid",
                            paid=2 * p["price_cents"])
        hold = self._hold(s, order, 2)
        for _ in range(2):
            self._register(s, order, p, self._person(), paid=True, hold=hold)

    def _invoice(self, s: dict) -> None:
        """An employer's two seats on an invoice, due in 30 days, not paid yet."""
        buyer = self._person()
        p = self.ap
        order = self._order(channel="invoice", buyer=buyer, total=2 * p["price_cents"], status="awaiting_payment",
                            paid=0, extra={"company": "Test Daycare LLC", "po": "PO-TEST-1",
                                           "due": self.today + timedelta(days=30)})
        for _ in range(2):
            self._register(s, order, p, self._person(), paid=False, hold=None)

    def _refund_waiting(self, registration_id: int) -> None:
        """A $20 refund asked for and waiting for a manager's approval."""
        order = self.conn.execute(text("SELECT order_id FROM registrations WHERE id = :r"),
                                  {"r": registration_id}).scalar_one()
        self.conn.execute(text("""
            INSERT INTO refunds (order_id, registration_id, amount_cents, reason, source, status,
                                 requires_manager_approval, requested_by_user_id, note)
            VALUES (:o, :r, 2000, 'customer_cancel', 'console', 'requested', true, :u,
                    'Test data: a partial refund waiting for approval')
        """), {"o": order, "r": registration_id, "u": self.user_id})

    def _wait(self, s: dict, *, party: int, minutes_ago: int) -> None:
        from .integrations.checkout import _person
        first, last, email = self._person()
        pid = _person(self.conn, first, last, email, "+15555550101")
        self.conn.execute(text("""
            INSERT INTO waitlist_entries (session_id, preferred_product_id, person_id, first_name, last_name, email,
                                          phone_e164, party_size, joined_at, status_changed_at)
            VALUES (:s, :p, :pid, :f, :l, :e, '+15555550101', :n, now() - make_interval(mins => :m),
                    now() - make_interval(mins => :m))
        """), {"s": s["id"], "p": self.adult["product_id"], "pid": pid, "f": first, "l": last, "e": email,
               "n": party, "m": minutes_ago})


# --- Removing -------------------------------------------------------------------------------

_REMOVE = """
CREATE TEMP TABLE td_s ON COMMIT DROP AS SELECT id FROM sessions WHERE slug LIKE '{p}%';
CREATE TEMP TABLE td_r ON COMMIT DROP AS
  SELECT id FROM registrations WHERE session_id IN (SELECT id FROM td_s)
  UNION SELECT registration_id FROM registration_transfers
         WHERE from_session_id IN (SELECT id FROM td_s) OR to_session_id IN (SELECT id FROM td_s);
CREATE TEMP TABLE td_w ON COMMIT DROP AS SELECT id FROM waitlist_entries WHERE session_id IN (SELECT id FROM td_s);
CREATE TEMP TABLE td_o ON COMMIT DROP AS
  SELECT order_id AS id FROM registrations WHERE id IN (SELECT id FROM td_r)
  UNION SELECT order_id FROM seat_holds WHERE session_id IN (SELECT id FROM td_s) AND order_id IS NOT NULL
  UNION SELECT settlement_order_id FROM registration_transfers
         WHERE registration_id IN (SELECT id FROM td_r) AND settlement_order_id IS NOT NULL
  UNION SELECT order_id FROM waitlist_offers WHERE waitlist_entry_id IN (SELECT id FROM td_w) AND order_id IS NOT NULL;
INSERT INTO td_o SELECT id FROM orders WHERE parent_order_id IN (SELECT id FROM td_o) EXCEPT SELECT id FROM td_o;
INSERT INTO td_r SELECT id FROM registrations WHERE order_id IN (SELECT id FROM td_o) EXCEPT SELECT id FROM td_r;
CREATE TEMP TABLE td_wo ON COMMIT DROP AS
  SELECT id FROM waitlist_offers WHERE waitlist_entry_id IN (SELECT id FROM td_w)
                                    OR offered_session_id IN (SELECT id FROM td_s);
CREATE TEMP TABLE td_h ON COMMIT DROP AS
  SELECT id FROM seat_holds WHERE session_id IN (SELECT id FROM td_s) OR order_id IN (SELECT id FROM td_o)
  UNION SELECT seat_hold_id FROM waitlist_offers WHERE id IN (SELECT id FROM td_wo) AND seat_hold_id IS NOT NULL;
CREATE TEMP TABLE td_a ON COMMIT DROP AS SELECT id FROM assignments WHERE session_id IN (SELECT id FROM td_s);
CREATE TEMP TABLE td_rf ON COMMIT DROP AS
  SELECT id FROM refunds WHERE order_id IN (SELECT id FROM td_o) OR registration_id IN (SELECT id FROM td_r);

UPDATE outbox_jobs SET status = 'cancelled'
 WHERE status NOT IN ('succeeded', 'cancelled')
   AND ((entity_type = 'registration' AND entity_id IN (SELECT id FROM td_r))
     OR (entity_type = 'order' AND entity_id IN (SELECT id FROM td_o))
     OR (entity_type = 'waitlist_entry' AND entity_id IN (SELECT id FROM td_w))
     OR (entity_type = 'refund' AND entity_id IN (SELECT id FROM td_rf)));
DELETE FROM action_tokens WHERE registration_id IN (SELECT id FROM td_r) OR order_id IN (SELECT id FROM td_o)
                             OR waitlist_offer_id IN (SELECT id FROM td_wo);
DELETE FROM attendance_marks WHERE registration_id IN (SELECT id FROM td_r)
                                OR marked_by_assignment_id IN (SELECT id FROM td_a);
DELETE FROM booking_answers WHERE registration_id IN (SELECT id FROM td_r) OR order_id IN (SELECT id FROM td_o);
DELETE FROM notes WHERE registration_id IN (SELECT id FROM td_r) OR order_id IN (SELECT id FROM td_o)
                     OR session_id IN (SELECT id FROM td_s);
DELETE FROM order_addons WHERE registration_id IN (SELECT id FROM td_r) OR order_id IN (SELECT id FROM td_o);
UPDATE registration_transfers SET settlement_refund_id = NULL WHERE settlement_refund_id IN (SELECT id FROM td_rf);
DELETE FROM refunds WHERE id IN (SELECT id FROM td_rf);
DELETE FROM registration_transfers WHERE registration_id IN (SELECT id FROM td_r)
                                      OR from_session_id IN (SELECT id FROM td_s) OR to_session_id IN (SELECT id FROM td_s);
DELETE FROM payments WHERE order_id IN (SELECT id FROM td_o);
UPDATE waitlist_entries SET sms_consent_id = NULL WHERE id IN (SELECT id FROM td_w);
DELETE FROM consent_records WHERE order_id IN (SELECT id FROM td_o) OR waitlist_entry_id IN (SELECT id FROM td_w);
UPDATE registrations SET rebooked_from_registration_id = NULL WHERE rebooked_from_registration_id IN (SELECT id FROM td_r);
DELETE FROM registrations WHERE id IN (SELECT id FROM td_r);
DELETE FROM checkout_sessions WHERE order_id IN (SELECT id FROM td_o) OR seat_hold_id IN (SELECT id FROM td_h);
DELETE FROM waitlist_offers WHERE id IN (SELECT id FROM td_wo);
DELETE FROM seat_holds WHERE id IN (SELECT id FROM td_h);
DELETE FROM waitlist_entries WHERE id IN (SELECT id FROM td_w);
UPDATE people SET first_order_id = NULL WHERE first_order_id IN (SELECT id FROM td_o);
DELETE FROM orders WHERE id IN (SELECT id FROM td_o);
DELETE FROM pay_records WHERE session_id IN (SELECT id FROM td_s) OR assignment_id IN (SELECT id FROM td_a);
DELETE FROM assignments WHERE id IN (SELECT id FROM td_a);
DELETE FROM seat_overrides WHERE session_id IN (SELECT id FROM td_s);
DELETE FROM class_offers WHERE session_id IN (SELECT id FROM td_s);
DELETE FROM external_seat_reservations WHERE session_id IN (SELECT id FROM td_s);
DELETE FROM url_redirects WHERE session_id IN (SELECT id FROM td_s);
UPDATE sessions SET overflow_of_session_id = NULL WHERE overflow_of_session_id IN (SELECT id FROM td_s);
DELETE FROM sessions WHERE id IN (SELECT id FROM td_s);
"""


def _remove(conn: Connection) -> dict:
    """Delete every test class and what hangs off it. If something the database won't let go of is
    in the way, the classes are taken off sale and renamed out of the set instead."""
    count = conn.execute(text("SELECT count(*) FROM sessions WHERE slug LIKE :p AND slug NOT LIKE :r"),
                         {"p": PREFIX + "%", "r": RETIRED + "%"}).scalar_one()
    if not count:
        return {"classes": 0, "how": "none"}
    try:
        with conn.begin_nested():
            with conn.connection.dbapi_connection.cursor() as cur:
                cur.execute(_REMOVE.format(p=PREFIX))
        return {"classes": count, "how": "deleted"}
    except DBAPIError:
        pass
    conn.execute(text("""
        UPDATE registrations SET status = 'cancelled', cancelled_at = now(), cancel_reason_code = 'admin'
         WHERE status IN ('pending', 'awaiting_payment', 'confirmed', 'displaced')
           AND session_id IN (SELECT id FROM sessions WHERE slug LIKE :p AND slug NOT LIKE :r)
    """), {"p": PREFIX + "%", "r": RETIRED + "%"})
    conn.execute(text("""
        UPDATE waitlist_entries SET status = 'removed', status_changed_at = now()
         WHERE status IN ('waiting', 'offered', 'offer_expired')
           AND session_id IN (SELECT id FROM sessions WHERE slug LIKE :p AND slug NOT LIKE :r)
    """), {"p": PREFIX + "%", "r": RETIRED + "%"})
    conn.execute(text("""
        UPDATE sessions SET status = 'cancelled', cancelled_at = COALESCE(cancelled_at, now()),
               is_published = false, slug = :retired || substr(slug, length(:p) + 1)
         WHERE slug LIKE :pp AND slug NOT LIKE :r
    """), {"p": PREFIX, "pp": PREFIX + "%", "retired": RETIRED, "r": RETIRED + "%"})
    return {"classes": count, "how": "taken_off_sale"}
