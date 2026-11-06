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


def test_find_by_name_email_or_booking_number(client, sign_in, alder):
    sign_in("cs@example.test")
    for term in ("Alder 1", "alder1@example", str(alder["public_id"])[:8]):
        page = text_of(client.get(f"/console/find?q={term}"))
        assert "Student Alder 1" in page and f"/console/bookings/{alder['public_id']}" in page, term
    assert "No bookings match" in text_of(client.get("/console/find?q=nobody-by-this-name"))


def test_booking_page_shows_the_whole_picture(client, sign_in, alder):
    sign_in("cs@example.test")
    page = text_of(client.get(f"/console/bookings/{alder['public_id']}"))
    for part in ("Student Alder 1", "alder1@example.test", "Henrico (Three Chopt)", "Booked",
                 "Payments", "Refunds", "Notes", "GHL and history", "Move to another class",
                 "Cancel the booking", "Refund"):
        assert part in page, part


def test_actions_are_placeholders_that_change_nothing(client, sign_in, alder, q):
    sign_in("cs@example.test")
    before = q("SELECT status, session_id FROM registrations WHERE id = :i", i=alder["id"])[0]
    for action in ("transfer", "cancel", "refund"):
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
    assert "wait here for a manager" in page and "decided yet" in page
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
