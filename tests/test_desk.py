"""Desk actions (hub 9b), attendance (9d) and the customer waiting list (9c), on the test site
without Stripe: moving and cancelling seats, phone and invoice bookings, marking Complete / No show,
and a full class's waiting list from joining to paying for an offered seat.

Each test makes its own small class in Richmond (and its own seats), so the demo classes other
tests count on are left as they were.
"""

from __future__ import annotations

import html
import random
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app import db, waitlist
from tests.conftest import csrf_from


def text_of(res) -> str:
    return html.unescape(res.get_data(as_text=True))


@pytest.fixture()
def q(app):
    def _q(sql: str, **params):
        with app.app_context():
            with db.engine().connect() as conn:
                return [dict(r) for r in conn.execute(text(sql), params).mappings()]
    return _q


@pytest.fixture()
def run(app):
    """Run SQL as the app would (one transaction)."""
    def _run(sql: str, **params):
        with app.app_context(), db.transaction() as conn:
            res = conn.execute(text(sql), params)
            return res.mappings().all() if res.returns_rows else None
    return _run


@pytest.fixture(autouse=True)
def leave_the_demo_as_found(app):
    """Seats, classes and queued GHL jobs made by a test are closed again afterwards."""
    with app.app_context(), db.engine().connect() as conn:
        started = conn.execute(text("SELECT now()")).scalar()
    yield
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("""UPDATE waitlist_entries SET status = 'removed', status_changed_at = now()
                              WHERE joined_at >= :t AND status IN ('waiting', 'offered', 'offer_expired')"""),
                     {"t": started})
        conn.execute(text("""UPDATE seat_holds SET status = 'released', released_at = now(), release_reason = 'test'
                              WHERE created_at >= :t AND status = 'active'"""), {"t": started})
        conn.execute(text("""UPDATE registrations SET status = 'cancelled', cancelled_at = now(),
                                    cancel_reason_code = 'admin'
                              WHERE created_at >= :t AND status IN ('confirmed', 'awaiting_payment', 'pending',
                                                                   'displaced', 'completed', 'no_show')"""),
                     {"t": started})
        conn.execute(text("""UPDATE sessions SET status = 'cancelled', cancelled_at = now(), is_published = false
                              WHERE created_at >= :t AND status <> 'cancelled'"""), {"t": started})
        conn.execute(text("DELETE FROM pay_records WHERE created_at >= :t"), {"t": started})
        conn.execute(text("""UPDATE outbox_jobs SET status = 'cancelled' WHERE created_at >= :t
                              AND status NOT IN ('succeeded', 'cancelled')"""), {"t": started})


def new_class(run, *, seats: int = 2, starts_at: datetime | None = None, like: int = 1) -> dict:
    """A Richmond First Aid class like demo class `like`, with `seats` seats, at a time no other class uses."""
    if starts_at is None:
        starts_at = datetime(2029, 1, 1, 14, tzinfo=timezone.utc) + timedelta(days=random.randint(0, 3000),
                                                                             minutes=random.randint(0, 50))
    tag = uuid.uuid4().hex[:8]
    row = run("""
        INSERT INTO sessions (metro_id, site_id, room_id, course_family_id, delivery_mode, starts_at, ends_at,
                              time_zone, slug, status, is_published, one_instructor_max, room_max_student_count,
                              capacity)
        SELECT metro_id, site_id, room_id, course_family_id, delivery_mode, :st, :st + interval '4 hours',
               time_zone, :slug, 'scheduled', true, :n, :n, :n
          FROM sessions WHERE id = :like
        RETURNING id, public_id
    """, st=starts_at, slug=f"test-{tag}", n=seats, like=like)[0]
    run("""INSERT INTO session_products (session_id, product_id, course_family_id, delivery_mode, price_cents)
           SELECT :s, product_id, course_family_id, delivery_mode, price_cents FROM session_products
            WHERE session_id = :like AND is_offered""", s=row["id"], like=like)
    return {"id": row["id"], "public_id": str(row["public_id"])}


def product(q, session_id: int, short: str) -> dict:
    return q("""SELECT p.id, sp.price_cents FROM session_products sp JOIN products p ON p.id = sp.product_id
                 WHERE sp.session_id = :s AND p.short_name = :n""", s=session_id, n=short)[0]


def paid_seat(run, q, session_id: int, short: str = "Adult only", *, paid: bool = True, channel: str = "web") -> dict:
    """A seat booked online and paid (test booking: no card), like the console tests' `seat`."""
    email = f"desk-{uuid.uuid4().hex[:8]}@example.test"
    p = product(q, session_id, short)
    run("""
        WITH o AS (
            INSERT INTO orders (channel, buyer_first_name, buyer_last_name, buyer_email, buyer_phone_e164, livemode,
                                subtotal_cents, total_cents, status, paid_at, amount_paid_cents, created_by_user_id)
            VALUES (:ch, 'Desk', 'Test', :e, '+18045550100', false, :c, :c, 'paid', now(), :a,
                    CASE WHEN :ch <> 'web' THEN (SELECT id FROM user_accounts WHERE email = 'cs@example.test') END)
            RETURNING id),
        h AS (
            INSERT INTO seat_holds (session_id, order_id, kind, quantity, expires_at, status, released_at)
            SELECT :s, o.id, 'checkout', 1, now() + interval '15 minutes', 'converted', now() FROM o RETURNING id)
        INSERT INTO registrations (order_id, seat_hold_id, session_id, product_id, attendee_first_name,
                                   attendee_last_name, attendee_email, unit_price_cents, status, confirmed_at,
                                   amount_paid_cents)
        SELECT o.id, h.id, :s, :p, 'Desk', 'Test', :e, :c, 'confirmed', now(), :a FROM o, h
    """, e=email, s=session_id, p=p["id"], c=p["price_cents"], a=p["price_cents"] if paid else 0, ch=channel)
    return q("SELECT id, public_id, order_id, attendee_email FROM registrations WHERE attendee_email = :e", e=email)[0]


def post(client, page: str, path: str, data: dict):
    return client.post(path, data={**data, "csrf_token": csrf_from(client, page)})


def move(client, seat, to: dict, to_product: dict, *, reason="customer_request", fee="0", note="", extra=""):
    booking = f"/console/bookings/{seat['public_id']}"
    return post(client, f"{booking}/transfer", f"{booking}/transfer",
                {"target": f"{to['id']}:{to_product['id']}", "reason": reason, "fee": fee, "note": note,
                 "extra_seat": extra})


def jobs(q, operation: str, entity_id: int) -> list[dict]:
    return q("SELECT payload, status FROM outbox_jobs WHERE target = 'ghl' AND operation = :op AND entity_id = :e",
             op=operation, e=entity_id)


# --- Moving a seat --------------------------------------------------------------------------

def test_move_to_another_class_in_one_step(client, sign_in, run, q):
    a, b = new_class(run, seats=3), new_class(run, seats=3)
    seat = paid_seat(run, q, a["id"])
    sign_in("cs@example.test")
    page = text_of(client.get(f"/console/bookings/{seat['public_id']}/transfer"))
    assert "Move the seat" in page and f'value="{b["id"]}:' in page            # the new class is offered
    res = move(client, seat, b, product(q, b["id"], "Adult only"))
    assert res.status_code == 302
    assert "Moved. GHL gets the new date and place" in text_of(client.get(res.headers["Location"]))
    reg = q("SELECT session_id, status FROM registrations WHERE id = :r", r=seat["id"])[0]
    assert (reg["session_id"], reg["status"]) == (b["id"], "confirmed")
    t = q("SELECT * FROM registration_transfers WHERE registration_id = :r", r=seat["id"])[0]
    assert (t["from_session_id"], t["to_session_id"], t["price_difference_cents"], t["fee_cents"], t["via"]) == \
        (a["id"], b["id"], 0, 0, "console")
    assert t["performed_by_user_id"] and t["settlement_order_id"] is None
    assert jobs(q, "move_registration", seat["id"])
    assert q("SELECT 1 FROM audit_log WHERE action = 'booking.transfer' AND entity_id = :r", r=seat["id"])
    seats = {r["session_id"]: r["sold_seats"] for r in
             q("SELECT session_id, sold_seats FROM session_availability WHERE session_id IN (:a, :b)", a=a["id"], b=b["id"])}
    assert seats == {a["id"]: 0, b["id"]: 1}


def test_a_dearer_course_and_a_fee_are_charged_and_a_cheaper_one_is_credited(client, sign_in, run, q):
    a, b = new_class(run, seats=3), new_class(run, seats=3)
    seat = paid_seat(run, q, a["id"], "Adult only")                                      # $88
    sign_in("cs@example.test")
    res = move(client, seat, b, product(q, b["id"], "Adult & Pediatric"), fee="25")     # $108 + $25 fee
    assert "45.00 difference recorded as paid (test booking, no card)" in text_of(client.get(res.headers["Location"]))
    t = q("SELECT * FROM registration_transfers WHERE registration_id = :r", r=seat["id"])[0]
    assert (t["price_difference_cents"], t["fee_cents"]) == (2000, 2500)
    order = q("SELECT channel, status, total_cents, amount_paid_cents, parent_order_id FROM orders WHERE id = :o",
              o=t["settlement_order_id"])[0]
    assert order == {"channel": "transfer_adjustment", "status": "paid", "total_cents": 4500,
                     "amount_paid_cents": 4500, "parent_order_id": seat["order_id"]}
    reg = q("SELECT amount_paid_cents, unit_price_cents FROM registrations WHERE id = :r", r=seat["id"])[0]
    assert reg == {"amount_paid_cents": 8800 + 4500, "unit_price_cents": 10800}
    # Back to the cheaper course: the $20 difference goes back to the card, no manager needed.
    res = move(client, seat, a, product(q, a["id"], "Adult only"))
    assert "20.00 is going back to the card" in text_of(client.get(res.headers["Location"]))
    refund = q("SELECT amount_cents, reason, status, requires_manager_approval FROM refunds WHERE registration_id = :r",
               r=seat["id"])[0]
    assert refund == {"amount_cents": 2000, "reason": "transfer_credit", "status": "succeeded",
                      "requires_manager_approval": False}


def test_a_full_class_needs_a_manager_to_add_a_seat(client, sign_in, run, q):
    a, full = new_class(run, seats=2), new_class(run, seats=1)
    paid_seat(run, q, full["id"])
    seat = paid_seat(run, q, a["id"])
    to = product(q, full["id"], "Adult only")
    sign_in("cs@example.test")
    res = move(client, seat, full, to)
    assert res.status_code == 400 and "has no seat left" in text_of(res)
    res = move(client, seat, full, to, extra="Customer's employer needs this date")
    assert res.status_code == 400 and "Only a manager can add a seat" in text_of(res)
    assert q("SELECT session_id FROM registrations WHERE id = :r", r=seat["id"])[0]["session_id"] == a["id"]
    sign_in("manager@example.test")
    assert "Extra seat over the limit" in text_of(client.get(f"/console/bookings/{seat['public_id']}/transfer"))
    res = move(client, seat, full, to, extra="Customer's employer needs this date")
    assert res.status_code == 302
    assert q("SELECT session_id FROM registrations WHERE id = :r", r=seat["id"])[0]["session_id"] == full["id"]
    assert q("SELECT seats, reason FROM seat_overrides WHERE session_id = :s", s=full["id"])[0] == \
        {"seats": 1, "reason": "Customer's employer needs this date"}


def test_moves_are_refused_for_another_course_or_the_same_class(client, sign_in, run, q):
    a = new_class(run, seats=3)
    seat = paid_seat(run, q, a["id"])
    bls = q("""SELECT s.id, sp.product_id AS pid FROM sessions s JOIN session_products sp ON sp.session_id = s.id
                JOIN course_families cf ON cf.id = s.course_family_id JOIN metros m ON m.id = s.metro_id
               WHERE cf.code = 'BLS' AND m.slug = 'richmond-va' AND s.status = 'scheduled' LIMIT 1""")[0]
    sign_in("cs@example.test")
    res = move(client, seat, {"id": bls["id"]}, {"id": bls["pid"]})
    assert res.status_code == 400 and "same course" in text_of(res)
    res = move(client, seat, a, product(q, a["id"], "Adult only"))
    assert res.status_code == 400 and "already in" in text_of(res)
    res = move(client, seat, a, product(q, a["id"], "Adult only"), reason="admin_correction")
    assert res.status_code == 400 and "Add a note" in text_of(res)


# --- Cancelling ----------------------------------------------------------------------------

def test_cancel_with_a_refund_frees_the_seat(client, sign_in, run, q):
    a = new_class(run, seats=2)
    seat = paid_seat(run, q, a["id"])
    sign_in("cs@example.test")
    booking = f"/console/bookings/{seat['public_id']}"
    res = post(client, f"{booking}/cancel", f"{booking}/cancel", {"reason": "customer_request", "refund": "40",
                                                                 "note": ""})
    assert res.status_code == 400 and "Tick the box" in text_of(res)
    res = post(client, f"{booking}/cancel", f"{booking}/cancel", {"reason": "customer_request", "refund": "40",
                                                                 "note": "", "confirm": "yes"})
    assert res.status_code == 302
    done = text_of(client.get(res.headers["Location"]))
    assert "Cancelled. The seat is free again" in done and "sent to a manager to approve" in done
    reg = q("SELECT status, cancel_reason_code FROM registrations WHERE id = :r", r=seat["id"])[0]
    assert reg == {"status": "cancelled", "cancel_reason_code": "customer_request"}
    assert q("SELECT seats_left FROM session_availability WHERE session_id = :s", s=a["id"])[0]["seats_left"] == 2
    refund = q("SELECT amount_cents, reason FROM refunds WHERE registration_id = :r", r=seat["id"])[0]
    assert refund == {"amount_cents": 4000, "reason": "customer_cancel"}
    assert jobs(q, "sync_registration", seat["id"])
    assert q("SELECT 1 FROM audit_log WHERE action = 'booking.cancel' AND entity_id = :r", r=seat["id"])


def test_late_notice_is_forfeited_and_a_refund_cant_exceed_what_was_paid(client, sign_in, run, q):
    a = new_class(run, seats=2)
    seat = paid_seat(run, q, a["id"])
    sign_in("cs@example.test")
    page = f"/console/bookings/{seat['public_id']}/cancel"
    res = post(client, page, page, {"reason": "late_notice", "refund": "500", "confirm": "yes"})
    assert res.status_code == 400 and "At most $88.00" in text_of(res)
    post(client, page, page, {"reason": "late_notice", "refund": "0", "confirm": "yes"})
    assert q("SELECT status FROM registrations WHERE id = :r", r=seat["id"])[0]["status"] == "forfeited"
    assert not q("SELECT 1 FROM refunds WHERE registration_id = :r", r=seat["id"])


def test_a_cancelled_class_moves_together(client, sign_in, run, q):
    a, b = new_class(run, seats=3), new_class(run, seats=3)
    seats = [paid_seat(run, q, a["id"]), paid_seat(run, q, a["id"], "Adult & Pediatric")]
    sign_in("cs@example.test")
    page = f"/console/classes/{a['public_id']}"
    post(client, page, f"{page}/move-all", {"to": b["id"]})
    assert "Only a cancelled class" in text_of(client.get(page))
    run("UPDATE sessions SET status = 'cancelled', cancelled_at = now() WHERE id = :s", s=a["id"])
    assert "Move everyone" in text_of(client.get(page))
    post(client, page, f"{page}/move-all", {"to": b["id"], "note": "Room flooded"})
    assert "Moved 2 students" in text_of(client.get(page))
    regs = q("SELECT session_id, status FROM registrations WHERE id IN (:x, :y)", x=seats[0]["id"], y=seats[1]["id"])
    assert {(r["session_id"], r["status"]) for r in regs} == {(b["id"], "confirmed")}
    batch = {r["bulk_batch_id"] for r in q("SELECT bulk_batch_id FROM registration_transfers WHERE to_session_id = :b",
                                           b=b["id"])}
    assert len(batch) == 1 and None not in batch


# --- Booking by phone or invoice ------------------------------------------------------------

def booking_form(how: str, product_id: int, **extra) -> dict:
    tag = uuid.uuid4().hex[:6]
    return {"how": how, "seats": "2", "product_id": str(product_id), "buyer_first": "Pat", "buyer_last": "Caller",
            "buyer_email": f"pat-{tag}@example.test", "buyer_phone": "(804) 555-0142", "buyer_attends": "yes",
            "a1_first": "Sam", "a1_last": "Guest", "a1_email": f"sam-{tag}@example.test", **extra}


def test_phone_booking_on_the_test_site_confirms_at_once(client, sign_in, run, q):
    a = new_class(run, seats=3)
    sign_in("cs@example.test")
    page = f"/console/new/{a['public_id']}?seats=2"
    res = post(client, page, f"/console/new/{a['public_id']}",
               booking_form("phone", product(q, a["id"], "Adult only")["id"]))
    assert res.status_code == 302 and res.headers["Location"].endswith("#money")
    assert "Booked as a test (no payment taken)" in text_of(client.get(res.headers["Location"]))
    regs = q("""SELECT r.status, o.channel, o.created_by_user_id FROM registrations r JOIN orders o ON o.id = r.order_id
                 WHERE r.session_id = :s""", s=a["id"])
    assert len(regs) == 2 and {(r["status"], r["channel"]) for r in regs} == {("confirmed", "phone")}
    assert all(r["created_by_user_id"] for r in regs)


def test_invoice_booking_is_confirmed_now_and_paid_later(client, sign_in, run, q):
    a = new_class(run, seats=3)
    sign_in("cs@example.test")
    page = f"/console/new/{a['public_id']}?seats=2"
    path = f"/console/new/{a['public_id']}"
    pid = product(q, a["id"], "Adult & Pediatric")["id"]
    res = post(client, page, path, booking_form("invoice", pid, company="", due_on="2030-01-31"))
    assert res.status_code == 400 and "Enter the company" in text_of(res)
    res = post(client, page, path, booking_form("invoice", pid, company="Henrico Daycare", po_number="PO-778",
                                                due_on="2030-01-31"))
    assert res.status_code == 302
    booking = res.headers["Location"]
    assert "Payment due" in text_of(client.get(booking))
    order = q("""SELECT o.* FROM orders o JOIN registrations r ON r.order_id = o.id WHERE r.session_id = :s LIMIT 1""",
              s=a["id"])[0]
    assert (order["channel"], order["status"], order["buyer_company"], order["po_number"], order["total_cents"]) == \
        ("invoice", "awaiting_payment", "Henrico Daycare", "PO-778", 21600)
    assert str(order["invoice_due_on"]) == "2030-01-31"
    assert {r["status"] for r in q("SELECT status FROM registrations WHERE order_id = :o", o=order["id"])} == \
        {"awaiting_payment"}
    assert q("SELECT sold_seats FROM session_availability WHERE session_id = :s", s=a["id"])[0]["sold_seats"] == 2
    # A cheque comes in: the full amount is recorded with its reference.
    post(client, booking, f"{booking.split('#')[0]}/payment", {"amount": "100", "reference": "Cheque 1042"})
    assert "the full amount" in text_of(client.get(booking)).lower()
    post(client, booking, f"{booking.split('#')[0]}/payment", {"amount": "216", "reference": "Cheque 1042"})
    assert q("SELECT status, amount_paid_cents FROM orders WHERE id = :o", o=order["id"])[0] == \
        {"status": "paid", "amount_paid_cents": 21600}
    assert {r["status"] for r in q("SELECT status FROM registrations WHERE order_id = :o", o=order["id"])} == \
        {"confirmed"}
    assert q("SELECT manual_reference FROM payments WHERE order_id = :o", o=order["id"])[0]["manual_reference"] == "Cheque 1042"


def test_phone_and_invoice_need_the_buyers_details(client, sign_in, run, q):
    a = new_class(run, seats=3)
    sign_in("cs@example.test")
    page, path = f"/console/new/{a['public_id']}?seats=2", f"/console/new/{a['public_id']}"
    pid = product(q, a["id"], "Adult only")["id"]
    res = post(client, page, path, booking_form("phone", pid, buyer_phone=""))
    assert res.status_code == 400 and "valid mobile number" in text_of(res)
    res = post(client, page, path, booking_form("phone", pid, a1_email=""))
    assert res.status_code == 400 and "Seat 2" in text_of(res)
    assert not q("SELECT 1 FROM registrations WHERE session_id = :s", s=a["id"])


# --- Attendance ----------------------------------------------------------------------------

@pytest.fixture()
def today_class(run, q):
    """A class running today in Richmond, taught by Priya Raman (lead) at $90. Its seats were booked by
    phone, so the console's "online bookings, any date" list still starts with the demo classes."""
    a = new_class(run, seats=3)
    a["seats"] = [paid_seat(run, q, a["id"], channel="phone"), paid_seat(run, q, a["id"], channel="phone")]
    start = datetime.now(timezone.utc) - timedelta(hours=1, minutes=random.randint(0, 50))
    run("UPDATE sessions SET starts_at = :st, ends_at = :st + interval '4 hours' WHERE id = :s", st=start, s=a["id"])
    run("""INSERT INTO assignments (session_id, instructor_id, role, source, rate_cents, session_range)
           SELECT :s, i.id, 'lead', 'console', 9000, s.time_range FROM instructors i
             JOIN user_accounts u ON u.id = i.user_account_id, sessions s
            WHERE u.email = 'priya.raman@example.test' AND s.id = :s""", s=a["id"])
    return a


def mark(client, s, seat, what, *, base="/teach/classes"):
    page = f"{base}/{s['public_id']}"
    return post(client, page, f"{page}/attendance", {"seat": seat["public_id"], "mark": what})


def test_the_instructor_marks_finishes_and_gets_a_pay_record(client, sign_in, today_class, q):
    s, (one, two) = today_class, today_class["seats"]
    sign_in("priya.raman@example.test")
    page = text_of(client.get(f"/teach/classes/{s['public_id']}"))
    assert "Test, Desk" in page and "Lead instructor" in page and one["attendee_email"] not in page   # no contacts
    mark(client, s, one, "checked_in")
    mark(client, s, one, "complete")
    page = f"/teach/classes/{s['public_id']}"
    post(client, page, f"{page}/finish", {})
    assert "1 student still needs" in text_of(client.get(page))
    mark(client, s, two, "no_show")
    assert {r["status"] for r in q("SELECT status FROM registrations WHERE id = :r", r=one["id"])} == {"completed"}
    assert q("SELECT status FROM registrations WHERE id = :r", r=two["id"])[0]["status"] == "no_show"
    marks = q("SELECT mark, marked_by_assignment_id, marked_by_user_id FROM attendance_marks WHERE registration_id = :r "
              "ORDER BY id", r=one["id"])
    assert [m["mark"] for m in marks] == ["checked_in", "complete"]
    assert all(m["marked_by_assignment_id"] and m["marked_by_user_id"] is None for m in marks)   # who marked it
    post(client, page, f"{page}/finish", {})
    assert q("SELECT status FROM sessions WHERE id = :s", s=s["id"])[0]["status"] == "completed"
    pay = q("SELECT amount_cents, role FROM pay_records WHERE session_id = :s", s=s["id"])
    assert pay == [{"amount_cents": 9000, "role": "lead"}]
    csv = client.get(f"/teach/classes/{s['public_id']}/class-list.csv")
    body = csv.get_data(as_text=True)
    assert csv.mimetype == "text/csv" and body.splitlines()[0].startswith("First Name,Last Name,Email")
    assert "Successful" in body and "No Show" in body
    assert q("SELECT roster_exported_at FROM sessions WHERE id = :s", s=s["id"])[0]["roster_exported_at"]
    assert jobs(q, "sync_registration", one["id"])                              # GHL stage follows the mark


def test_the_office_marks_any_class_and_others_cant(client, sign_in, today_class, run, q):
    s, (one, _) = today_class, today_class["seats"]
    sign_in("marcus.hale@example.test")                                   # an instructor not on this class
    assert client.get(f"/teach/classes/{s['public_id']}").status_code == 404
    sign_in("manager@example.test")
    res = mark(client, s, one, "complete", base="/console/classes")
    assert res.status_code == 302
    m = q("SELECT marked_by_user_id, marked_by_assignment_id FROM attendance_marks WHERE registration_id = :r", r=one["id"])[0]
    assert m["marked_by_user_id"] and m["marked_by_assignment_id"] is None
    mark(client, s, one, "cleared", base="/console/classes")
    assert q("SELECT status FROM registrations WHERE id = :r", r=one["id"])[0]["status"] == "confirmed"


def test_attendance_opens_on_class_day(client, sign_in, run, q):
    later = new_class(run, seats=2)
    seat = paid_seat(run, q, later["id"])
    sign_in("manager@example.test")
    page = f"/console/classes/{later['public_id']}"
    mark(client, later, seat, "complete", base="/console/classes")
    assert "Attendance opens on class day" in text_of(client.get(page))
    assert q("SELECT status FROM registrations WHERE id = :r", r=seat["id"])[0]["status"] == "confirmed"


# --- The waiting list ----------------------------------------------------------------------

def join(client, s, pid, *, party=1, email=None, phone="(804) 555-0177"):
    email = email or f"wait-{uuid.uuid4().hex[:8]}@example.test"
    res = post(client, "/sign-in", f"/waitlist/join/{s['public_id']}",
               {"wl_first": "Wren", "wl_last": "Waiting", "wl_email": email, "wl_phone": phone,
                "wl_product": str(pid), "wl_party": str(party), "wl_sms": "yes"})
    return res, email


@pytest.fixture()
def full_class(run, q):
    """A full one-instructor class of 2 (so up to 2 people may wait), and its seats."""
    s = new_class(run, seats=2)
    s["seats"] = [paid_seat(run, q, s["id"]), paid_seat(run, q, s["id"])]
    s["pid"] = product(q, s["id"], "Adult only")["id"]
    return s


def tick(app, *, now_is_open: bool = True):
    """One worker pass, with offers due now (the 8 AM – 6 PM window is tested on its own)."""
    with app.app_context():
        waitlist.offer_free_seats()
        if now_is_open:
            with db.transaction() as conn:
                conn.execute(text("UPDATE waitlist_offers SET send_after = now() WHERE status = 'queued'"))
        return waitlist.tick()


def test_join_a_full_class_waiting_list(client, run, q, full_class):
    s = full_class
    page = text_of(client.get(f"/book/{s['public_id']}"))
    assert "Join the waiting list" in page and "2 hours" in page
    res, email = join(client, s, s["pid"])
    assert res.status_code == 303
    joined = text_of(client.get(res.headers["Location"]))
    assert "on the List" in joined and "You have 2 hours to pay" in joined
    e = q("SELECT * FROM waitlist_entries WHERE email = :e", e=email)[0]
    assert (e["status"], e["party_size"], e["phone_e164"]) == ("waiting", 1, "+18045550177")
    assert q("SELECT granted, kind FROM consent_records WHERE waitlist_entry_id = :w", w=e["id"])[0] == \
        {"granted": True, "kind": "sms"}
    assert jobs(q, "sync_waitlist_entry", e["id"])
    res, _ = join(client, s, s["pid"], email=email)
    assert res.status_code == 400 and "already on the waiting list" in text_of(res)
    res, _ = join(client, s, s["pid"], party=2)
    assert res.status_code == 400 and "room for 1 more person" in text_of(res)


def test_open_seats_cant_be_waited_for(client, run, q):
    s = new_class(run, seats=2)
    res, _ = join(client, s, product(q, s["id"], "Adult only")["id"])
    assert res.status_code == 400 and "book one now instead" in text_of(res)


def test_a_freed_seat_is_offered_held_and_paid_for(app, client, sign_in, run, q, full_class):
    s = full_class
    _, email = join(client, s, s["pid"])
    entry = q("SELECT id FROM waitlist_entries WHERE email = :e", e=email)[0]["id"]
    run("UPDATE waitlist_entries SET ghl_opportunity_id = :o WHERE id = :e", o=f"opp-{entry}", e=entry)
    sign_in("cs@example.test")
    booking = f"/console/bookings/{s['seats'][0]['public_id']}"
    post(client, f"{booking}/cancel", f"{booking}/cancel", {"reason": "customer_request", "refund": "0",
                                                            "confirm": "yes"})
    # The freed seat is held for the person waiting, not put back on public sale.
    offer = q("SELECT * FROM waitlist_offers WHERE waitlist_entry_id = :e", e=entry)[0]
    assert offer["status"] == "queued" and offer["trigger"] == "seat_freed"
    assert q("SELECT seats_left FROM session_availability WHERE session_id = :s", s=s["id"])[0]["seats_left"] == 0
    run("UPDATE waitlist_offers SET send_after = now() WHERE id = :o", o=offer["id"])
    with app.app_context():
        assert waitlist.dispatch() == 1
    offer = q("SELECT * FROM waitlist_offers WHERE id = :o", o=offer["id"])[0]
    assert offer["status"] == "sent" and offer["expires_at"] - offer["sent_at"] <= timedelta(hours=2, seconds=5)
    notice = jobs(q, "waitlist_notice", entry)[0]["payload"]
    assert notice["kind"] == "offer" and notice["url"].endswith(f"/waitlist/offer/{offer['public_id']}")
    # The person signs in (or makes an account) and takes the seat (test site: no payment).
    with client.session_transaction() as sess:
        sess.clear()
    page = f"/waitlist/offer/{offer['public_id']}"
    assert "Your seat is held" in text_of(client.get(page))
    assert post(client, page, page, {}).headers["Location"].startswith("/sign-in")
    client.post("/create-account", data={"csrf_token": csrf_from(client, "/create-account"), "first_name": "Wren",
                                         "last_name": "Waiting", "email": email, "password": "long-enough-1",
                                         "intent": "student"})
    res = post(client, page, page, {"buyer_first": "Wren", "buyer_last": "Waiting"})
    assert res.status_code == 303 and "/book/done?order=" in res.headers["Location"]
    reg = q("SELECT status, ghl_opportunity_id, waitlist_entry_id FROM registrations WHERE attendee_email = :e", e=email)[0]
    assert reg == {"status": "confirmed", "ghl_opportunity_id": f"opp-{entry}", "waitlist_entry_id": entry}
    assert q("SELECT status FROM waitlist_entries WHERE id = :e", e=entry)[0]["status"] == "converted"
    assert q("SELECT status FROM waitlist_offers WHERE id = :o", o=offer["id"])[0]["status"] == "accepted"
    assert "You're booked" in text_of(client.get(page))


def test_an_unpaid_offer_expires_keeps_the_place_and_staff_can_offer_again(app, client, sign_in, run, q, full_class):
    s = full_class
    _, first = join(client, s, s["pid"])
    _, second = join(client, s, s["pid"])
    e1, e2 = (q("SELECT id FROM waitlist_entries WHERE email = :e", e=e)[0]["id"] for e in (first, second))
    run("UPDATE registrations SET status = 'cancelled', cancelled_at = now(), cancel_reason_code = 'admin' "
        "WHERE id = :r", r=s["seats"][0]["id"])
    tick(app)
    o1 = q("SELECT id, status FROM waitlist_offers WHERE waitlist_entry_id = :e", e=e1)[0]
    assert o1["status"] == "sent" and not q("SELECT 1 FROM waitlist_offers WHERE waitlist_entry_id = :e", e=e2)
    # Two hours pass with no payment.
    run("UPDATE waitlist_offers SET expires_at = now() - interval '1 minute' WHERE id = :o", o=o1["id"])
    run("UPDATE seat_holds SET expires_at = now() - interval '1 minute' "
        "WHERE id = (SELECT seat_hold_id FROM waitlist_offers WHERE id = :o)", o=o1["id"])
    tick(app)
    assert q("SELECT status FROM waitlist_offers WHERE id = :o", o=o1["id"])[0]["status"] == "expired"
    assert q("SELECT status FROM waitlist_entries WHERE id = :e", e=e1)[0]["status"] == "offer_expired"
    assert [j["payload"]["kind"] for j in jobs(q, "waitlist_notice", e1)] == ["offer", "expired"]
    assert q("SELECT status FROM waitlist_offers WHERE waitlist_entry_id = :e", e=e2)[0]["status"] == "sent"
    # The next person doesn't pay either; nobody is re-offered automatically.
    run("UPDATE waitlist_offers SET expires_at = now() - interval '1 minute' WHERE waitlist_entry_id = :e", e=e2)
    run("UPDATE seat_holds SET expires_at = now() - interval '1 minute' WHERE id = "
        "(SELECT seat_hold_id FROM waitlist_offers WHERE waitlist_entry_id = :e)", e=e2)
    tick(app)
    assert len(q("SELECT 1 FROM waitlist_offers WHERE waitlist_entry_id = :e", e=e1)) == 1
    # Staff offer it again by hand.
    sign_in("cs@example.test")
    page = f"/console/classes/{s['public_id']}"
    assert "Wren Waiting" in text_of(client.get(page))
    post(client, page, f"/console/waitlist/{e1}/reoffer", {})
    offers = q("SELECT status, trigger FROM waitlist_offers WHERE waitlist_entry_id = :e ORDER BY id", e=e1)
    assert offers[-1]["trigger"] == "manual" and len(offers) == 2
    post(client, page, f"/console/waitlist/{e2}/remove", {"why": "Booked another date"})
    assert q("SELECT status FROM waitlist_entries WHERE id = :e", e=e2)[0]["status"] == "removed"


def test_a_group_is_offered_only_when_it_fits(app, client, run, q):
    s = new_class(run, seats=3)
    seats = [paid_seat(run, q, s["id"]) for _ in range(3)]
    pid = product(q, s["id"], "Adult only")["id"]
    _, group = join(client, s, pid, party=2)
    run("UPDATE registrations SET status = 'cancelled', cancelled_at = now(), cancel_reason_code = 'admin' "
        "WHERE id = :r", r=seats[0]["id"])
    tick(app)
    entry = q("SELECT id, status FROM waitlist_entries WHERE email = :e", e=group)[0]
    assert entry["status"] == "waiting"                                          # 1 seat free, 2 people
    run("UPDATE registrations SET status = 'cancelled', cancelled_at = now(), cancel_reason_code = 'admin' "
        "WHERE id = :r", r=seats[1]["id"])
    tick(app)
    assert q("SELECT seats FROM waitlist_offers WHERE waitlist_entry_id = :e", e=entry["id"])[0]["seats"] == 2


def test_offers_wait_for_the_daytime_window_and_lists_close_at_the_cut_off(app):
    with app.app_context():
        with db.engine().connect() as conn:
            cfg = waitlist._settings(conn)
    tz = "America/New_York"
    night = datetime(2027, 3, 3, 23, 30, tzinfo=timezone.utc)          # 6:30 PM in Richmond
    assert waitlist.send_time(night, tz, cfg) == datetime(2027, 3, 4, 13, 0, tzinfo=timezone.utc)   # 8 AM next day
    noon = datetime(2027, 3, 3, 17, 0, tzinfo=timezone.utc)
    assert waitlist.send_time(noon, tz, cfg) == noon
    sat = datetime(2027, 3, 6, 14, 0, tzinfo=timezone.utc)             # Saturday 9 AM class
    assert waitlist.cutoff(sat, tz, "one_instructor", cfg) == sat - timedelta(hours=24)
    assert waitlist.cutoff(sat, tz, "co_instructor", cfg) == datetime(2027, 3, 5, 13, 0, tzinfo=timezone.utc)
    for day in (7, 8):                                                  # Sunday and Monday classes
        start = datetime(2027, 3, day, 14, 0, tzinfo=timezone.utc)
        assert waitlist.cutoff(start, tz, "co_instructor", cfg) == datetime(2027, 3, 6, 13, 0, tzinfo=timezone.utc)


def test_past_the_cut_off_everyone_waiting_hears_no_seat_opened(app, client, run, q, full_class):
    s = full_class
    _, email = join(client, s, s["pid"])
    entry = q("SELECT id FROM waitlist_entries WHERE email = :e", e=email)[0]["id"]
    run("UPDATE sessions SET starts_at = now() + interval '20 hours', ends_at = now() + interval '24 hours' "
        "WHERE id = :s", s=s["id"])
    tick(app)
    assert q("SELECT status FROM waitlist_entries WHERE id = :e", e=entry)[0]["status"] == "closed_no_seat"
    assert jobs(q, "waitlist_notice", entry)[0]["payload"]["kind"] == "no_seat"
