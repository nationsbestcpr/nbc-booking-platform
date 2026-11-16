"""The test site's test data, used by an admin: load it, try every desk action on it (refunds, moves,
cancelling, phone and invoice bookings, attendance, the waiting list), and remove it again."""

from __future__ import annotations

import html

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


def post(client, page: str, path: str, data: dict):
    return client.post(path, data={**data, "csrf_token": csrf_from(client, page)})


@pytest.fixture()
def loaded(client, sign_in, q):
    """The admin loads the set; it's removed again afterwards, whatever the test did to it."""
    sign_in("admin@example.test")
    res = post(client, "/admin/test-data", "/admin/test-data", {})
    assert res.status_code == 302
    rows = q("SELECT id, public_id, slug FROM sessions WHERE slug LIKE 'nbc-test-%' AND slug NOT LIKE 'nbc-test-retired-%'")
    by = {}
    for r in rows:
        key = r["slug"][len("nbc-test-"):].rsplit("-", 1)[0]
        by[key] = {"id": r["id"], "public_id": str(r["public_id"])}
    yield by
    sign_in("admin@example.test")
    post(client, "/admin/test-data", "/admin/test-data", {"do": "remove"})


def seat(q, session_id: int, *, status="confirmed", channel="web") -> dict:
    return q("""SELECT r.id, r.public_id, r.product_id, r.order_id FROM registrations r JOIN orders o ON o.id = r.order_id
                 WHERE r.session_id = :s AND r.status = :st AND o.channel = :ch ORDER BY r.id LIMIT 1""",
             s=session_id, st=status, ch=channel)[0]


def test_the_set_has_a_class_for_each_thing_to_try(client, loaded, q):
    assert set(loaded) == {"full-waiting", "full-co", "full-nobody", "open", "open-later", "today", "cancelled"}
    avail = {k: q("SELECT * FROM session_availability WHERE session_id = :s", s=v["id"])[0] for k, v in loaded.items()}
    for key in ("full-waiting", "full-co", "full-nobody"):
        assert avail[key]["seats_left"] == 0
    assert avail["full-waiting"]["waitlist_count"] == 3 and avail["full-co"]["waitlist_count"] == 1
    assert q("SELECT location_type FROM sessions WHERE id = :s", s=loaded["full-co"]["id"])[0]["location_type"] == \
        "co_instructor"
    assert avail["open"]["seats_left"] > 0 and avail["open-later"]["seats_left"] >= avail["cancelled"]["sold_seats"]
    today = q("SELECT local_date = (now() AT TIME ZONE time_zone)::date AS today FROM sessions WHERE id = :s",
              s=loaded["today"]["id"])[0]
    assert today["today"]
    assert q("SELECT status FROM sessions WHERE id = :s", s=loaded["cancelled"]["id"])[0]["status"] == "cancelled"
    channels = {r["channel"] for r in q("""SELECT DISTINCT o.channel FROM orders o JOIN registrations r ON r.order_id = o.id
                                            WHERE r.session_id = :s""", s=loaded["open"]["id"])}
    assert channels == {"web", "invoice"}
    assert q("SELECT 1 FROM refunds f JOIN registrations r ON r.id = f.registration_id "
             "WHERE r.session_id = :s AND f.status = 'requested'", s=loaded["open"]["id"])
    assert not q("SELECT 1 FROM outbox_jobs j JOIN registrations r ON j.entity_type = 'registration' "
                 "AND j.entity_id = r.id WHERE r.session_id = :s", s=loaded["open"]["id"])   # nothing sent
    page = text_of(client.get("/admin/test-data"))
    assert "Full, 2 groups waiting" in page and "Running today" in page and "Remove the test data" in page
    assert page.count("Open, with bookings to change") == 1 and "Open, a later date to move to" in page


def test_an_admin_can_do_every_desk_action(client, loaded, q, app):
    open_, later, nobody = loaded["open"], loaded["open-later"], loaded["full-nobody"]
    # The sidebar and the admin pages lead to the desk.
    home = text_of(client.get("/admin"))
    for label in ("Find a student", "Book by phone or invoice", "Refunds", "Refund approvals", "Test data"):
        assert label in home
    detail = text_of(client.get(f"/admin/classes/{open_['id']}"))
    assert "Class list, attendance and waiting list" in detail and "/console/bookings/" in detail
    # 8. Refunds: approve the one waiting, and give one straight away.
    waiting = q("SELECT f.id FROM refunds f JOIN registrations r ON r.id = f.registration_id "
                "WHERE r.session_id = :s AND f.status = 'requested'", s=open_["id"])[0]["id"]
    post(client, "/console/approvals", f"/console/refunds/{waiting}/approve", {})
    assert q("SELECT status FROM refunds WHERE id = :f", f=waiting)[0]["status"] == "succeeded"
    s1 = seat(q, open_["id"])
    booking = f"/console/bookings/{s1['public_id']}"
    post(client, f"{booking}/refund", f"{booking}/refund", {"amount": "10", "reason": "customer_cancel", "note": ""})
    assert q("SELECT amount_refunded_cents FROM registrations WHERE id = :r", r=s1["id"])[0]["amount_refunded_cents"] == 1000
    # 9. Move: into the full class only with a reason (an admin can add the seat), then cancel one.
    res = post(client, f"{booking}/transfer", f"{booking}/transfer",
               {"target": f"{nobody['id']}:{s1['product_id']}", "reason": "customer_request", "fee": "0"})
    assert res.status_code == 400 and "has no seat left" in text_of(res)
    res = post(client, f"{booking}/transfer", f"{booking}/transfer",
               {"target": f"{nobody['id']}:{s1['product_id']}", "reason": "customer_request", "fee": "0",
                "extra_seat": "Testing a manager's extra seat"})
    assert res.status_code == 302
    assert q("SELECT session_id FROM registrations WHERE id = :r", r=s1["id"])[0]["session_id"] == nobody["id"]
    s2 = seat(q, open_["id"])
    page = f"/console/bookings/{s2['public_id']}/cancel"
    post(client, page, page, {"reason": "customer_request", "refund": "0", "confirm": "yes"})
    assert q("SELECT status FROM registrations WHERE id = :r", r=s2["id"])[0]["status"] == "cancelled"
    # A cancelled class moves together.
    cls = f"/console/classes/{loaded['cancelled']['public_id']}"
    post(client, cls, f"{cls}/move-all", {"to": later["id"], "note": "Test"})
    assert "Moved 3 students" in text_of(client.get(cls))
    # 10. Phone and invoice: record the invoice's cheque; book two seats by phone.
    inv = seat(q, open_["id"], status="awaiting_payment", channel="invoice")
    total = q("SELECT total_cents FROM orders WHERE id = :o", o=inv["order_id"])[0]["total_cents"]
    b = f"/console/bookings/{inv['public_id']}"
    post(client, b, f"{b}/payment", {"amount": f"{total / 100:.2f}", "reference": "Cheque 1"})
    assert q("SELECT status FROM orders WHERE id = :o", o=inv["order_id"])[0]["status"] == "paid"
    new = f"/console/new/{later['public_id']}"
    res = post(client, f"{new}?seats=1", new,
               {"how": "phone", "seats": "1", "product_id": str(s2["product_id"]), "buyer_first": "Pat",
                "buyer_last": "Admin", "buyer_email": "pat.admin.test@example.test", "buyer_phone": "(804) 555-0190",
                "buyer_attends": "yes"})
    assert res.status_code == 302
    # 11. Attendance on today's class, as the office.
    today = loaded["today"]
    cls = f"/console/classes/{today['public_id']}"
    for r in q("SELECT public_id FROM registrations WHERE session_id = :s AND status = 'confirmed'", s=today["id"]):
        post(client, cls, f"{cls}/attendance", {"seat": r["public_id"], "mark": "complete"})
    post(client, cls, f"{cls}/finish", {})
    assert q("SELECT status FROM sessions WHERE id = :s", s=today["id"])[0]["status"] == "completed"
    assert client.get(f"{cls}/class-list.csv").mimetype == "text/csv"
    # 12. The waiting list: a freed seat is offered to the first person; staff remove and re-offer.
    full = loaded["full-waiting"]
    s3 = seat(q, full["id"])
    page = f"/console/bookings/{s3['public_id']}/cancel"
    post(client, page, page, {"reason": "customer_request", "refund": "0", "confirm": "yes"})
    first, group = q("SELECT id, party_size FROM waitlist_entries WHERE session_id = :s ORDER BY joined_at",
                     s=full["id"])
    assert q("SELECT status FROM waitlist_offers WHERE waitlist_entry_id = :e", e=first["id"])[0]["status"] == "queued"
    assert not q("SELECT 1 FROM waitlist_offers WHERE waitlist_entry_id = :e", e=group["id"])     # 2 don't fit in 1
    lists = text_of(client.get("/admin/waitlists"))
    assert "Class list and waiting list" in lists
    cls = f"/console/classes/{full['public_id']}"
    post(client, cls, f"/console/waitlist/{group['id']}/remove", {"why": "Test"})
    assert q("SELECT status FROM waitlist_entries WHERE id = :e", e=group["id"])[0]["status"] == "removed"
    with app.app_context():
        waitlist.tick()


def test_remove_deletes_everything_even_after_it_was_used_and_load_works_again(client, loaded, q, sign_in):
    open_ = loaded["open"]
    s1 = seat(q, open_["id"])
    b = f"/console/bookings/{s1['public_id']}"
    post(client, f"{b}/transfer", f"{b}/transfer", {"target": f"{loaded['open-later']['id']}:{s1['product_id']}",
                                                    "reason": "customer_request", "fee": "15"})
    res = post(client, "/admin/test-data", "/admin/test-data", {"do": "remove"})
    assert "Removed 7 test classes" in text_of(client.get(res.headers["Location"]))
    assert not q("SELECT 1 FROM sessions WHERE slug LIKE 'nbc-test-%'")
    assert not q("SELECT 1 FROM registrations WHERE id = :r", r=s1["id"])
    res = post(client, "/admin/test-data", "/admin/test-data", {})
    assert "Loaded 7 test classes" in text_of(client.get(res.headers["Location"]))


def test_never_on_the_live_site(client, sign_in, app, q):
    sign_in("admin@example.test")
    app.config["APP_ENV"] = "production"
    try:
        page = text_of(client.get("/admin/test-data"))
        assert "This is the live site" in page and "Load test data" not in page
        post(client, "/account/profile", "/admin/test-data", {})
        assert not q("SELECT 1 FROM sessions WHERE slug LIKE 'nbc-test-%'")
        assert ">Test data<" not in text_of(client.get("/admin"))
    finally:
        app.config["APP_ENV"] = "local"
