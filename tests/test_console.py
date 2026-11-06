"""The booking console (/console): who gets it, finding a booking, its page, notes, city limits.

Demo accounts (password demo-password): cs@ (customer service), manager@,
admin@, and the student booking "Student Alder 1" in Richmond.
"""

from __future__ import annotations

import html
import uuid

import pytest
from sqlalchemy import text

from app import db
from tests.conftest import csrf_from


def text_of(res) -> str:
    return html.unescape(res.get_data(as_text=True))


@pytest.fixture()
def q(app):
    def _q(sql: str, **params):
        with app.app_context():
            return db.connection().execute(text(sql), params).mappings().all()
    return _q


@pytest.fixture()
def alder(q):
    return q("SELECT public_id, id FROM registrations WHERE attendee_email = 'alder1@example.test' "
             "ORDER BY id LIMIT 1")[0]


def test_customer_service_and_managers_land_on_the_desk(client, sign_in):
    for email in ("cs@example.test", "manager@example.test"):
        sign_in(email)
        assert client.get("/dashboard").headers["Location"].endswith("/console")
        page = text_of(client.get("/console"))
        assert "Today at the Desk" in page and "Find a student or booking" in page
        assert "Booking console" in page


def test_admins_keep_their_dashboard_and_get_a_link_to_the_desk(client, sign_in):
    sign_in("admin@example.test")
    assert client.get("/dashboard").headers["Location"].endswith("/admin")
    assert 'href="/console"' in client.get("/admin").get_data(as_text=True)
    assert client.get("/console").status_code == 200


@pytest.mark.parametrize("email", ["alder1@example.test", "marcus.hale@example.test"])
def test_customers_and_instructors_cant_open_the_desk(client, sign_in, email):
    sign_in(email)
    for path in ("/console", "/console/find?q=alder", "/console/new", "/console/approvals"):
        assert client.get(path).status_code == 403, path


def test_find_lists_students_of_the_next_classes(client, sign_in, alder):
    sign_in("cs@example.test")
    page = text_of(client.get("/console/find"))
    assert "Student Alder 1" in page and "First Aid / CPR / AED" in page and "Henrico (Three Chopt)" in page
    assert page.index("Sat, Jan 16, 2027") < page.index("Student Alder 1")       # grouped under its class


def test_find_by_name_email_or_booking_number(client, sign_in, alder):
    sign_in("cs@example.test")
    for term in ("Alder 1", "alder1@example", str(alder["public_id"])[:8]):
        page = text_of(client.get(f"/console/find?q={term}"))
        assert "Student Alder 1" in page and f"/console/bookings/{alder['public_id']}" in page, term
    assert "No students match" in text_of(client.get("/console/find?q=nobody-by-this-name"))


@pytest.mark.parametrize("filters, shown", [
    ("course=FA_CPR_AED&city=richmond-va", True),
    ("course=BLS", False),
    ("city=houston-tx", False),
    ("when=past30", False),
    ("status=cancelled", False),
    ("status=all&when=any&channel=web", True),
    ("from=2027-01-16&to=2027-01-16", True),
    ("from=2027-01-17", False),
])
def test_find_filters(client, sign_in, filters, shown):
    sign_in("cs@example.test")
    assert ("Student Alder 1" in text_of(client.get(f"/console/find?{filters}"))) is shown, filters


def test_booking_page_shows_the_whole_picture(client, sign_in, alder):
    sign_in("cs@example.test")
    page = text_of(client.get(f"/console/bookings/{alder['public_id']}"))
    for part in ("Student Alder 1", "alder1@example.test", "Henrico (Three Chopt)", "Booked",
                 "Payments", "Refunds", "Notes", "GHL and history", "Move to another class",
                 "Cancel the booking", "Refund"):
        assert part in page, part


def test_transfer_and_cancel_are_placeholders_that_change_nothing(client, sign_in, alder, q):
    sign_in("cs@example.test")
    before = q("SELECT status, session_id FROM registrations WHERE id = :i", i=alder["id"])[0]
    for action in ("transfer", "cancel"):
        page = text_of(client.get(f"/console/bookings/{alder['public_id']}/{action}"))
        assert "Coming next" in page and "disabled" in page
    assert client.get(f"/console/bookings/{alder['public_id']}/delete").status_code == 404
    assert q("SELECT status, session_id FROM registrations WHERE id = :i", i=alder["id"])[0] == before


def test_notes_are_saved_and_logged(client, sign_in, alder, q):
    sign_in("cs@example.test")
    path = f"/console/bookings/{alder['public_id']}"
    body = f"Asked about parking {uuid.uuid4().hex[:6]}"
    res = client.post(f"{path}/notes", data={"csrf_token": csrf_from(client, path), "body": body})
    assert res.status_code == 302 and res.headers["Location"].endswith("#notes")
    assert body in text_of(client.get(path))
    note = q("SELECT id FROM notes WHERE body = :b", b=body)[0]
    assert q("SELECT 1 FROM audit_log WHERE action = 'booking.note_add' AND entity_id = :r "
             "AND after->>'note_id' = :n", r=alder["id"], n=str(note["id"]))
    assert client.post(f"{path}/notes", data={"body": "no token"}).status_code == 400   # CSRF


def test_desk_only_sees_its_own_cities(app, client, sign_in, post_form, alder, q):
    email = f"cs-houston-{uuid.uuid4().hex[:8]}@example.test"
    post_form("/create-account", {"first_name": "Hou", "last_name": "Desk", "email": email,
                                  "password": "long-enough-1", "intent": "student"})
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("""
            INSERT INTO user_roles (user_account_id, role_code, scope_metro_id, granted_by_user_id)
            SELECT a.id, 'customer_service', m.id, (SELECT id FROM user_accounts WHERE email = 'admin@example.test')
              FROM user_accounts a, metros m WHERE a.email = :e AND m.slug = 'houston-tx'
        """), {"e": email})
    sign_in(email, "long-enough-1")
    assert client.get("/dashboard").headers["Location"].endswith("/console")
    assert "Student Alder 1" not in text_of(client.get("/console/find?q=alder1"))
    assert client.get(f"/console/bookings/{alder['public_id']}").status_code == 404


def test_refund_approvals_are_for_managers(client, sign_in):
    sign_in("cs@example.test")
    assert client.get("/console/approvals").status_code == 403
    sign_in("manager@example.test")
    page = text_of(client.get("/console/approvals"))
    assert "need a manager" in page and "decided yet" in page
    assert ">Refund approvals</a>" in client.get("/console").get_data(as_text=True)


def test_phone_booking_starts_from_the_class_search(client, sign_in, q):
    sign_in("cs@example.test")
    page = text_of(client.get("/console/new"))
    assert "Pick the class" in page and "/console/new/" in page and "per page" in page
    sid = q("""SELECT s.public_id FROM sessions s JOIN session_availability a ON a.session_id = s.id
                JOIN metros m ON m.id = s.metro_id
               WHERE s.status = 'scheduled' AND s.is_published AND s.starts_at > now() AND a.seats_left > 0
                 AND m.booking_system = 'engine' ORDER BY s.starts_at LIMIT 1""")[0]["public_id"]
    page = text_of(client.get(f"/console/new/{sid}"))
    assert "Send payment link" in page and "Book and invoice" in page and "Coming next" in page


def test_signing_in_takes_staff_to_their_dashboard_and_customers_to_classes(client, post_form):
    res = post_form("/sign-in", {"email": "cs@example.test", "password": "demo-password"})
    assert res.status_code == 302 and res.headers["Location"] == "/dashboard"
    assert client.get("/dashboard").headers["Location"].endswith("/console")
    with client.session_transaction() as sess:
        sess.clear()
    res = post_form("/sign-in", {"email": "alder1@example.test", "password": "demo-password"})
    assert res.headers["Location"] == "/"
    with client.session_transaction() as sess:
        sess.clear()
    res = post_form("/sign-in", {"email": "cs@example.test", "password": "demo-password", "next": "/console/new"})
    assert res.headers["Location"] == "/console/new"                  # a page they asked for still wins



# --- Refunds ---------------------------------------------------------------------------

@pytest.fixture()
def seat(app, q):
    """A fresh paid test seat (no card: test site without Stripe) in a class with room."""
    email = f"refund-{uuid.uuid4().hex[:8]}@example.test"
    with app.app_context(), db.transaction() as conn:
        sp = conn.execute(text("""
            SELECT sp.session_id, sp.product_id, sp.price_cents FROM session_products sp
              JOIN session_availability a ON a.session_id = sp.session_id
              JOIN sessions s ON s.id = sp.session_id JOIN metros m ON m.id = s.metro_id
             WHERE a.seats_left > 0 AND s.starts_at > now() AND m.slug = 'richmond-va' AND sp.is_offered
             ORDER BY a.seats_left DESC, s.starts_at LIMIT 1""")).mappings().one()
        o = conn.execute(text("""INSERT INTO orders (channel, buyer_first_name, buyer_last_name, buyer_email, livemode,
                                    subtotal_cents, total_cents, status, paid_at, amount_paid_cents)
                                 VALUES ('web', 'Ref', 'Und', :e, false, :p, :p, 'paid', now(), :p) RETURNING id"""),
                         {"e": email, "p": sp["price_cents"]}).scalar_one()
        h = conn.execute(text("""INSERT INTO seat_holds (session_id, order_id, kind, quantity, expires_at)
                                 VALUES (:s, :o, 'checkout', 1, now() + interval '15 minutes') RETURNING id"""),
                         {"s": sp["session_id"], "o": o}).scalar_one()
        conn.execute(text("""INSERT INTO registrations (order_id, seat_hold_id, session_id, product_id, attendee_first_name,
                               attendee_last_name, attendee_email, unit_price_cents, status, confirmed_at, amount_paid_cents)
                             VALUES (:o, :h, :s, :p, 'Ref', 'Und', :e, :c, 'confirmed', now(), :c)"""),
                     {"o": o, "h": h, "s": sp["session_id"], "p": sp["product_id"], "e": email, "c": sp["price_cents"]})
        conn.execute(text("UPDATE seat_holds SET status = 'converted', released_at = now() WHERE id = :h"), {"h": h})
    yield q("SELECT id, public_id, amount_paid_cents, attendee_email FROM registrations WHERE attendee_email = :e",
            e=email)[0]
    with app.app_context(), db.transaction() as conn:     # give the seat back, so other tests' seat counts hold
        conn.execute(text("""UPDATE registrations SET status = 'cancelled', cancelled_at = now(),
                                    cancel_reason_code = 'admin' WHERE attendee_email = :e"""), {"e": email})


def ask_refund(client, seat, amount: str, reason: str = "customer_cancel", note: str = ""):
    page = f"/console/bookings/{seat['public_id']}/refund"
    return client.post(page, data={"csrf_token": csrf_from(client, page), "amount": amount, "reason": reason,
                                   "note": note})


def test_customer_service_refund_waits_for_a_manager_then_goes_through(client, sign_in, seat, q):
    sign_in("cs@example.test")
    assert ">Refunds</a>" in client.get("/console").get_data(as_text=True)
    page = text_of(client.get(f"/console/bookings/{seat['public_id']}/refund"))
    assert "goes to a manager to approve first" in page and "Ask for refund" in page
    res = ask_refund(client, seat, "50")
    assert res.status_code == 302
    refund = q("SELECT * FROM refunds WHERE registration_id = :r", r=seat["id"])[0]
    assert refund["status"] == "requested" and refund["requires_manager_approval"] and refund["amount_cents"] == 5000
    assert "Waiting for a manager" in text_of(client.get("/console/refunds"))
    # Customer service can't approve, even by posting directly.
    assert client.post(f"/console/refunds/{refund['id']}/approve",
                       data={"csrf_token": csrf_from(client, "/console")}).status_code == 403
    sign_in("manager@example.test")
    assert "Ref Und" in text_of(client.get("/console/approvals"))
    res = client.post(f"/console/refunds/{refund['id']}/approve",
                      data={"csrf_token": csrf_from(client, "/console/approvals"), "back": "/console/approvals"})
    assert res.headers["Location"] == "/console/approvals"
    done = q("SELECT status, approved_by_user_id FROM refunds WHERE id = :i", i=refund["id"])[0]
    assert done["status"] == "succeeded" and done["approved_by_user_id"]          # test seat: no card to refund
    assert q("SELECT amount_refunded_cents FROM registrations WHERE id = :r", r=seat["id"])[0]["amount_refunded_cents"] == 5000
    actions = {a["action"] for a in q("SELECT action FROM audit_log WHERE entity_type = 'registration' "
                                      "AND entity_id = :r", r=seat["id"])}
    assert {"refund.request", "refund.approve"} <= actions


def test_a_seat_never_gets_back_more_than_it_paid(client, sign_in, seat, q):
    sign_in("cs@example.test")
    ask_refund(client, seat, "60")
    paid = seat["amount_paid_cents"] / 100
    res = ask_refund(client, seat, f"{paid - 59:.2f}")                 # 1 dollar too much with the first one waiting
    assert res.status_code == 400 and "At most" in text_of(res)
    assert ask_refund(client, seat, "abc").status_code == 400
    res = ask_refund(client, seat, "5", reason="goodwill")
    assert res.status_code == 400 and "Add a note" in text_of(res)


def test_manager_refunds_straight_away_and_can_turn_down(client, sign_in, seat, q):
    sign_in("cs@example.test")
    ask_refund(client, seat, "10")
    sign_in("manager@example.test")
    waiting = q("SELECT id FROM refunds WHERE registration_id = :r AND status = 'requested'", r=seat["id"])[0]["id"]
    client.post(f"/console/refunds/{waiting}/turn-down",
                data={"csrf_token": csrf_from(client, "/console/refunds"), "why": "Outside the 7-day window"})
    turned = q("SELECT status, failure_reason FROM refunds WHERE id = :i", i=waiting)[0]
    assert turned["status"] == "rejected" and turned["failure_reason"] == "Outside the 7-day window"
    res = ask_refund(client, seat, "20")
    assert res.status_code == 302
    mine = q("SELECT status, requires_manager_approval FROM refunds WHERE registration_id = :r ORDER BY id DESC",
             r=seat["id"])[0]
    assert mine["status"] == "succeeded"


def test_threshold_lets_small_refunds_through_without_a_manager(app, client, sign_in, seat, q):
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("UPDATE app_settings SET value = '2500' WHERE key = 'large_refund_threshold_cents'"))
    try:
        sign_in("cs@example.test")
        ask_refund(client, seat, "25")
        ask_refund(client, seat, "25.01")
        small, big = q("SELECT status FROM refunds WHERE registration_id = :r ORDER BY id", r=seat["id"])
        assert small["status"] == "succeeded" and big["status"] == "requested"
    finally:
        with app.app_context(), db.transaction() as conn:
            conn.execute(text("UPDATE app_settings SET value = 'null' WHERE key = 'large_refund_threshold_cents'"))



def test_refund_can_be_started_from_refunds_and_from_find(client, sign_in, seat):
    sign_in("cs@example.test")
    page = text_of(client.get(f"/console/refunds?who={seat['attendee_email']}"))
    assert "Start a refund" in page and f"/console/bookings/{seat['public_id']}/refund" in page
    assert "No paid seats match" in text_of(client.get("/console/refunds?who=nobody-at-all"))
    find = text_of(client.get(f"/console/find?q={seat['attendee_email']}"))
    assert f"/console/bookings/{seat['public_id']}/refund" in find and ">Open</a>" in find


def test_a_crash_shows_staff_the_error_on_the_test_site_only(app, client, sign_in, monkeypatch):
    from app.blueprints import console as console_bp

    def boom(*a, **k):
        raise RuntimeError("refund preview exploded")
    monkeypatch.setattr(console_bp, "_refund_page", boom)
    monkeypatch.setitem(app.config, "PROPAGATE_EXCEPTIONS", False)
    sign_in("cs@example.test")
    with app.app_context():
        reg = db.connection().execute(text("SELECT public_id FROM registrations WHERE status = 'confirmed' "
                                           "AND amount_paid_cents > 0 LIMIT 1")).scalar_one()
    res = client.get(f"/console/bookings/{reg}/refund")
    page = text_of(res)
    assert res.status_code == 500 and "Something went wrong on our side" in page and "reference" in page
    assert "RuntimeError: refund preview exploded" in page               # test site: staff see the cause
    monkeypatch.setitem(app.config, "APP_ENV", "production")
    page = text_of(client.get(f"/console/bookings/{reg}/refund"))
    assert "refund preview exploded" not in page                         # live site: only the reference


def test_db_verify_finds_nothing_missing(app):
    from app import schema_check
    with app.app_context():
        assert schema_check.missing() == []
    want = schema_check.expected()
    assert ("orders", "buyer_user_id") in want["columns"] and "orders_buyer_user_idx" in want["indexes"]
    assert ("orders", "check") not in want["columns"]


def test_db_repair_puts_a_dropped_column_back_and_is_harmless_otherwise(app):
    from pathlib import Path
    sql = (Path(__file__).resolve().parent.parent / "db" / "repair.sql").read_text()
    with app.app_context():
        with db.engine().connect() as conn:          # all in one transaction, rolled back at the end
            cur = conn.connection.dbapi_connection.cursor()
            cur.execute(sql)                          # on a complete database: changes nothing
            cur.execute("ALTER TABLE orders DROP COLUMN buyer_user_id")
            cur.execute(sql)
            cur.execute("""SELECT count(*) FROM information_schema.columns
                            WHERE table_name = 'orders' AND column_name = 'buyer_user_id'""")
            assert cur.fetchone()[0] == 1
            conn.rollback()
