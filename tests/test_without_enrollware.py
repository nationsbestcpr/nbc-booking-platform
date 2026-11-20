"""Running without Enrollware (migration 0012): the instructor portal (confirming a class, "can't teach"
notices, availability changes that can't quietly clash with an assigned class, students signing the roster),
the student portal's booking page, calendar file and receipt, the Superadmin's instructor accounts and
Enrollware cutover and import, and the Ops Hub's API for instructors' requests.

Each test makes its own class (tests/test_desk.py's helpers), so the demo classes stay as they were.
"""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app import db
from tests.test_api import api, make_key  # noqa: F401  (fixture)
from tests.test_desk import (leave_the_demo_as_found, new_class, paid_seat, post, q, run, text_of,  # noqa: F401
                             today_class)

PATH = "M10 10 L40 40 L80 30 L120 60 L150 20"


def assign(run, session_id: int, email: str, role: str = "lead") -> int:
    return run("""INSERT INTO assignments (session_id, instructor_id, role, source, rate_cents)
                  SELECT :s, i.id, :r, 'airtable', 9000 FROM instructors i JOIN user_accounts u ON u.id = i.user_account_id
                   WHERE u.email = :e RETURNING id""", s=session_id, e=email, r=role)[0]["id"]


def future_class(run, *, days: int = 3, hour_utc: int = 14) -> dict:
    """A class a few days from now (so it's within the confirm window), at a free time."""
    import random
    start = (datetime.now(timezone.utc) + timedelta(days=days)).replace(hour=hour_utc, minute=random.randint(0, 50),
                                                                         second=0, microsecond=0)
    return new_class(run, seats=3, starts_at=start)


# --- Confirming, and "I can't teach it" ---------------------------------------------------------

def test_the_instructor_confirms_a_class_and_can_say_they_cant_teach_one(client, sign_in, run, q):
    c = future_class(run)
    aid = assign(run, c["id"], "priya.raman@example.test")
    sign_in("priya.raman@example.test")
    home = text_of(client.get("/teach"))
    assert "Please confirm" in home and f"/teach/classes/{c['public_id']}/confirm" in home
    page = f"/teach/classes/{c['public_id']}"
    post(client, page, f"{page}/confirm", {})
    assert q("SELECT confirmed_at FROM assignments WHERE id = :a", a=aid)[0]["confirmed_at"]
    assert "Confirmed" in text_of(client.get(page))
    # Something came up: the notice goes to NBC; the class stays theirs until NBC changes it in Airtable.
    res = post(client, f"{page}/cant-teach", f"{page}/cant-teach", {"reason": "x"})
    assert res.status_code == 400 and "in a few words" in text_of(res)
    post(client, f"{page}/cant-teach", f"{page}/cant-teach", {"reason": "Family emergency"})
    req = q("SELECT * FROM instructor_requests WHERE assignment_id = :a", a=aid)[0]
    assert (req["kind"], req["status"], req["reason"]) == ("cant_teach", "open", "Family emergency")
    a = q("SELECT status, confirmed_at FROM assignments WHERE id = :a", a=aid)[0]
    assert a == {"status": "assigned", "confirmed_at": None}
    assert "You told NBC you can't teach it" in text_of(client.get(page))
    post(client, page, f"/teach/requests/{req['id']}/withdraw", {"back": page})
    assert q("SELECT status FROM instructor_requests WHERE id = :r", r=req["id"])[0]["status"] == "withdrawn"
    # Another instructor can't open the class, and the database refuses a notice for a class not theirs.
    sign_in("marcus.hale@example.test")
    assert client.get(page).status_code == 404
    with pytest.raises(DBAPIError, match="assigned to"):
        run("""INSERT INTO instructor_requests (instructor_id, kind, assignment_id, reason, created_by_user_id)
               SELECT i.id, 'cant_teach', :a, 'Not mine', i.user_account_id FROM instructors i
                 JOIN user_accounts u ON u.id = i.user_account_id WHERE u.email = 'marcus.hale@example.test'""", a=aid)


# --- Availability ---------------------------------------------------------------------------

def test_an_availability_change_cant_quietly_drop_an_assigned_class(client, sign_in, run, q):
    c = future_class(run, days=10)
    aid = assign(run, c["id"], "priya.raman@example.test")
    weekday = q("SELECT to_char(timezone(time_zone, starts_at), 'Dy') AS d FROM sessions WHERE id = :s",
                s=c["id"])[0]["d"]
    other_days = [d for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun") if d != weekday]
    sign_in("priya.raman@example.test")
    note = f"New job on that day {uuid.uuid4().hex[:6]}"
    form = {"days": other_days, "times": ["Morning", "Afternoon", "Evening"], "classes_per_month": "4",
            "notice_needed": "1 week", "note": note}
    res = post(client, "/teach/availability", "/teach/availability", form)
    page = text_of(res)
    assert res.status_code == 400 and "clashes with" in page and f'name="class_{aid}"' in page
    assert not q("SELECT 1 FROM instructor_requests WHERE details ->> 'note' = :n", n=note)
    # The database keeps the rule too: no change without a word on each clashing class.
    with pytest.raises(DBAPIError, match="clashes with"):
        run("""INSERT INTO instructor_requests (instructor_id, kind, details, created_by_user_id)
               SELECT i.id, 'availability', CAST(:d AS jsonb), i.user_account_id FROM instructors i
                 JOIN user_accounts u ON u.id = i.user_account_id WHERE u.email = 'priya.raman@example.test'""",
            d=f'{{"available_days": {list(other_days)!r}}}'.replace("'", '"'))
    res = post(client, "/teach/availability", "/teach/availability",
               {**form, f"class_{aid}": "cant_teach", f"reason_{aid}": "New job"})
    assert res.status_code == 302
    change = q("SELECT * FROM instructor_requests WHERE details ->> 'note' = :n", n=note)[0]
    assert change["kind"] == "availability" and change["details"]["classes"] == {str(aid): "cant_teach"}
    notice = q("SELECT * FROM instructor_requests WHERE availability_request_id = :r", r=change["id"])[0]
    assert (notice["kind"], notice["assignment_id"], notice["reason"]) == ("cant_teach", aid, "New job")
    assert q("SELECT status FROM assignments WHERE id = :a", a=aid)[0]["status"] == "assigned"   # NBC decides
    assert "With NBC" in text_of(client.get("/teach"))


# --- Students sign the roster ---------------------------------------------------------------

def test_students_sign_the_roster_on_class_day(client, sign_in, today_class, run, q):
    s, (one, two) = today_class, today_class["seats"]
    sign_in("priya.raman@example.test")
    page = f"/teach/classes/{s['public_id']}"
    assert "Open the sign-in sheet" in text_of(client.get(page))
    sheet = text_of(client.get(f"{page}/sign"))
    assert "Tap your name" in sheet and str(one["public_id"]) in sheet
    post(client, f"{page}/sign", f"{page}/sign", {"seat": one["public_id"], "signed_name": "Desk Test",
                                                  "signature": "M1 1"})            # a dot isn't a signature
    assert not q("SELECT 1 FROM roster_signatures WHERE registration_id = :r", r=one["id"])
    post(client, f"{page}/sign", f"{page}/sign", {"seat": one["public_id"], "signed_name": "Desk Test",
                                                  "signature": PATH})
    sig = q("SELECT * FROM roster_signatures WHERE registration_id = :r", r=one["id"])[0]
    assert sig["signed_name"] == "Desk Test" and sig["collected_by_assignment_id"] and sig["session_id"] == s["id"]
    reg = q("SELECT checked_in_at FROM registrations WHERE id = :r", r=one["id"])[0]
    assert reg["checked_in_at"]                                                     # signing checks them in
    assert "Signed" in text_of(client.get(page))
    roster = text_of(client.get(f"{page}/signed-roster"))
    assert f'd="{PATH}"' in roster and "Not signed" in roster                       # the second student
    # The database: only a student booked in this class can sign its roster.
    other = new_class(run, seats=2)
    with pytest.raises(DBAPIError, match="booked in this class"):
        run("""INSERT INTO roster_signatures (registration_id, session_id, signed_name, signature_path, collected_by_user_id)
               VALUES (:r, :s, 'Someone', :p, (SELECT id FROM user_accounts WHERE email = 'admin@example.test'))""",
            r=two["id"], s=other["id"], p=PATH)


def test_the_sign_in_sheet_opens_on_class_day(client, sign_in, run, q):
    c = future_class(run, days=4)
    seat = paid_seat(run, q, c["id"], channel="phone")
    assign(run, c["id"], "priya.raman@example.test")
    sign_in("priya.raman@example.test")
    page = f"/teach/classes/{c['public_id']}"
    assert "opens on class day" in text_of(client.get(f"{page}/sign"))
    post(client, page, f"{page}/sign", {"seat": seat["public_id"], "signed_name": "Desk Test", "signature": PATH})
    assert not q("SELECT 1 FROM roster_signatures WHERE registration_id = :r", r=seat["id"])


# --- The Ops Hub reads and closes instructors' requests ---------------------------------------------

def test_the_ops_hub_sees_and_closes_instructors_requests(client, sign_in, api, run, q):
    c = future_class(run, days=2)
    aid = assign(run, c["id"], "priya.raman@example.test")
    sign_in("priya.raman@example.test")
    page = f"/teach/classes/{c['public_id']}"
    post(client, f"{page}/cant-teach", f"{page}/cant-teach", {"reason": "Car trouble"})
    rid = q("SELECT id FROM instructor_requests WHERE assignment_id = :a AND status = 'open'", a=aid)[0]["id"]
    key = make_key(client, sign_in)
    res = api("GET", "/instructor-requests?kind=cant_teach", key, who="ops@example.test")
    assert res.status_code == 200, res.json
    row = next(r for r in res.json["data"] if r["id"] == rid)
    assert row["reason"] == "Car trouble" and row["class"]["id"] == c["public_id"] and \
        row["instructor"]["name"].startswith("Priya")
    cls = api("GET", f"/classes?from={(datetime.now() + timedelta(days=1)).date()}", key,
              who="ops@example.test").json["data"]
    mine = next(x for x in cls if x["public_id"] == c["public_id"])
    assert mine["instructors"][0]["cant_teach"]["reason"] == "Car trouble"
    assert api("POST", f"/instructor-requests/{rid}/close", key, who="ops@example.test",
               json={"outcome": "dismissed"}).status_code == 400                      # needs a note for the instructor
    res = api("POST", f"/instructor-requests/{rid}/close", key, who="ops@example.test",
              json={"outcome": "done", "note": "Marcus covers it"})
    assert res.status_code == 200 and q("SELECT status, handled_note FROM instructor_requests WHERE id = :r",
                                         r=rid)[0] == {"status": "done", "handled_note": "Marcus covers it"}
    assert api("GET", "/instructor-requests", key, who="cs@example.test").status_code == 403
    sign_in("priya.raman@example.test")
    assert "Marcus covers it" in text_of(client.get("/teach"))


# --- The student portal -------------------------------------------------------------------------

def test_a_student_sees_their_booking_calendar_file_and_receipt(client, sign_in, run, q):
    c = future_class(run, days=20)
    seat = paid_seat(run, q, c["id"], channel="phone")      # keeps the console's "online bookings" list as it was
    run("UPDATE orders SET buyer_user_id = (SELECT id FROM user_accounts WHERE email = 'riley.brooks@example.test') "
        "WHERE id = :o", o=seat["order_id"])
    order = q("SELECT public_id, total_cents FROM orders WHERE id = :o", o=seat["order_id"])[0]
    sign_in("riley.brooks@example.test")
    assert f"/my/bookings/{seat['public_id']}" in text_of(client.get("/my"))
    page = text_of(client.get(f"/my/bookings/{seat['public_id']}"))
    assert "When and where" in page and "Add to my calendar" in page and "Bring a photo ID" in page
    ics = client.get(f"/my/bookings/{seat['public_id']}/class.ics")
    body = ics.get_data(as_text=True)
    assert ics.mimetype == "text/calendar" and "BEGIN:VEVENT" in body and "DTSTART:" in body
    receipt = text_of(client.get(f"/my/orders/{order['public_id']}/receipt"))
    assert "receipt" in receipt.lower() and f"${order['total_cents'] // 100}" in receipt
    sign_in("alder1@example.test")                                                # someone else's booking
    assert client.get(f"/my/bookings/{seat['public_id']}").status_code == 404
    assert client.get(f"/my/orders/{order['public_id']}/receipt").status_code == 404


# --- Superadmin: instructor accounts ---------------------------------------------------------------

def test_the_superadmin_gives_the_instructor_role_and_the_record_links(client, sign_in, run, q):
    tag = uuid.uuid4().hex[:6]
    email = f"new.instructor.{tag}@example.test"
    uid = run("""INSERT INTO user_accounts (email, display_name, status, signup_intent)
                 VALUES (:e, 'New Instructor', 'active', 'instructor') RETURNING id""", e=email)[0]["id"]
    run("INSERT INTO user_roles (user_account_id, role_code) VALUES (:u, 'user')", u=uid)
    iid = run("""INSERT INTO instructors (first_name, last_name, full_name, email) VALUES ('New', :l, :f, :e)
                 RETURNING id""", l=f"Instructor{tag}", f=f"New Instructor{tag}", e=email)[0]["id"]
    sign_in("admin@example.test")
    page = text_of(client.get("/admin/instructor-accounts"))
    assert email in page and "Match" in page
    post(client, "/admin/instructor-accounts", "/admin/instructor-accounts/grant", {"uid": uid})
    assert q("SELECT user_account_id FROM instructors WHERE id = :i", i=iid)[0]["user_account_id"] == uid
    assert q("""SELECT granted_by_user_id FROM user_roles WHERE user_account_id = :u AND role_code = 'instructor'
                 AND revoked_at IS NULL""", u=uid)[0]["granted_by_user_id"]
    assert "Linked" in text_of(client.get(f"/admin/users/{uid}"))
    sign_in("ops@example.test")                                             # not a user manager
    assert client.get("/admin/instructor-accounts").status_code in (302, 403)


# --- Superadmin: Enrollware cutover and import ---------------------------------------------------------

def export_csv(rows: list[dict]) -> bytes:
    cols = ["Reg. Date", "Last Name", "First Name", "Email", "Phone", "Course", "Class ID", "Course Date",
            "Course Location", "Status", "Class Price", "Options", "Options Price", "Order Total", "Balance Due",
            "Labels", "Comments", "Promo Code"]
    out = io.StringIO()
    import csv
    w = csv.DictWriter(out, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in cols})
    return out.getvalue().encode()


@pytest.fixture()
def cutover_state(app):
    """Put the cutover setting and the cities back as they were."""
    with app.app_context(), db.engine().connect() as conn:
        cities = conn.execute(text("SELECT id, booking_system, cutover_at FROM metros")).mappings().all()
    yield
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("UPDATE app_settings SET value = 'false'::jsonb WHERE key = 'enrollware_retired'"))
        for c in cities:
            conn.execute(text("UPDATE metros SET booking_system = :b, cutover_at = :t WHERE id = :i"),
                         {"b": c["booking_system"], "t": c["cutover_at"], "i": c["id"]})


def test_enrollware_registrations_are_checked_then_imported_once(client, sign_in, run, q, cutover_state):
    c = new_class(run, seats=2)
    local = q("SELECT timezone(time_zone, starts_at) AS t FROM sessions WHERE id = :s", s=c["id"])[0]["t"]
    when = local.strftime("%m/%d/%Y %I:%M %p")
    tag = uuid.uuid4().hex[:6]
    base = {"Course": "ARC Adult First Aid/CPR/AED Blended r.25 (online + classroom)-102", "Class ID": f"CLS-{tag}",
            "Course Date": when, "Course Location": "Virginia - Richmond - Our Lady of Lourdes", "Class Price": "88",
            "Options Price": "0", "Reg. Date": "11/10/2026 10:00 AM"}
    rows = [
        {**base, "First Name": "Ann", "Last Name": "Paid", "Email": f"ann.{tag}@example.test", "Phone": "(804) 555-0111",
         "Status": "Pending", "Order Total": "100", "Balance Due": "0", "Options": "LAMCARD (NON REFUNDABLE)",
         "Options Price": "12", "Comments": "Needs a chair"},
        {**base, "First Name": "Bo", "Last Name": "Owes", "Email": f"bo.{tag}@example.test", "Status": "Pending",
         "Order Total": "88", "Balance Due": "88"},
        {**base, "First Name": "Cy", "Last Name": "Late", "Email": f"cy.{tag}@example.test", "Status": "Pending",
         "Order Total": "88", "Balance Due": "0"},                                    # the class is full by now
        {**base, "First Name": "Di", "Last Name": "Done", "Email": f"di.{tag}@example.test", "Status": "Complete",
         "Order Total": "88", "Balance Due": "0"},
        {**base, "First Name": "Ed", "Last Name": "Nowhere", "Email": f"ed.{tag}@example.test", "Status": "Pending",
         "Course Location": "Ohio - Nowhere - Nothing (Nowhere)", "Order Total": "88", "Balance Due": "0"},
    ]
    data = export_csv(rows)
    sign_in("admin@example.test")
    res = client.post("/admin/cutover/import", data={
        "csrf_token": __import__("tests.conftest", fromlist=["csrf_from"]).csrf_from(client, "/admin/cutover"),
        "action": "check", "file": (io.BytesIO(data), "export.csv")}, content_type="multipart/form-data")
    page = text_of(res)
    assert res.status_code == 200 and "Nothing has been imported yet" in page
    assert "Import 2 seat(s)" in page and "No location in the booking app" in page and "full in the booking app" in page
    assert not q("SELECT 1 FROM registrations WHERE attendee_email = :e", e=f"ann.{tag}@example.test")   # rolled back
    token = __import__("re").search(r'name="token" value="([^"]+)"', page).group(1)
    res = post(client, "/admin/cutover", "/admin/cutover/import", {"token": token, "action": "import"})
    assert res.status_code == 302
    ann = q("""SELECT r.status, r.amount_paid_cents, o.status AS order_status, o.total_cents, o.amount_paid_cents AS paid,
                      o.channel FROM registrations r JOIN orders o ON o.id = r.order_id WHERE r.attendee_email = :e""",
            e=f"ann.{tag}@example.test")[0]
    assert ann == {"status": "confirmed", "amount_paid_cents": 8800, "order_status": "paid", "total_cents": 10000,
                   "paid": 10000, "channel": "admin"}
    assert q("""SELECT p.method, p.manual_reference FROM payments p JOIN registrations r ON r.order_id = p.order_id
                 WHERE r.attendee_email = :e""", e=f"ann.{tag}@example.test")[0] == \
        {"method": "manual", "manual_reference": f"Enrollware CLS-{tag}"}
    note = q("""SELECT n.body FROM notes n JOIN registrations r ON r.id = n.registration_id WHERE r.attendee_email = :e""",
             e=f"ann.{tag}@example.test")[0]["body"]
    assert "LAMCARD" in note and "Needs a chair" in note
    bo = q("""SELECT r.status, o.status AS order_status, o.invoice_due_on FROM registrations r
               JOIN orders o ON o.id = r.order_id WHERE r.attendee_email = :e""", e=f"bo.{tag}@example.test")[0]
    assert bo["status"] == "awaiting_payment" and bo["order_status"] == "awaiting_payment" and bo["invoice_due_on"]
    assert not q("SELECT 1 FROM outbox_jobs WHERE entity_type = 'registration' AND entity_id IN "
                 "(SELECT id FROM registrations WHERE attendee_email = :e)", e=f"ann.{tag}@example.test")   # no messages
    imp = q("SELECT id, summary FROM enrollware_imports ORDER BY id DESC LIMIT 1")[0]
    assert imp["summary"]["counts"] == {"imported": 2, "refused": 1, "skipped": 1, "unmatched": 1}
    assert "Seats imported" in text_of(client.get(f"/admin/cutover/imports/{imp['id']}"))
    # A newer export: what's already in comes in once; the full class gets an extra seat when asked.
    res = client.post("/admin/cutover/import", data={
        "csrf_token": __import__("tests.conftest", fromlist=["csrf_from"]).csrf_from(client, "/admin/cutover"),
        "action": "import", "add_seats": "1", "file": (io.BytesIO(data), "export.csv")},
        content_type="multipart/form-data")
    assert res.status_code == 200 or res.status_code == 302
    res = client.post("/admin/cutover/import", data={
        "csrf_token": __import__("tests.conftest", fromlist=["csrf_from"]).csrf_from(client, "/admin/cutover"),
        "action": "check", "file": (io.BytesIO(data), "export.csv")}, content_type="multipart/form-data")
    second = q("SELECT summary FROM enrollware_imports ORDER BY id DESC LIMIT 1")[0]["summary"]
    assert second["counts"]["already_imported"] == 2 and second["counts"]["imported"] == 1 and second["extra_seats"] == 1
    assert q("SELECT count(*) AS n FROM registrations WHERE session_id = :s AND status IN ('confirmed', 'awaiting_payment')",
             s=c["id"])[0]["n"] == 3
    assert q("SELECT reason FROM seat_overrides WHERE session_id = :s", s=c["id"])[0]["reason"].startswith("Enrollware import")


def test_enrollware_is_retired_and_every_city_books_here(client, sign_in, run, q, cutover_state):
    run("UPDATE metros SET booking_system = 'enrollware' WHERE id = (SELECT min(id) FROM metros WHERE slug <> 'richmond-va')")
    sign_in("ops@example.test")
    assert client.get("/admin/cutover").status_code in (302, 403)                  # Superadmin only
    sign_in("admin@example.test")
    assert "Still on Enrollware" in text_of(client.get("/admin/cutover"))
    post(client, "/admin/cutover", "/admin/cutover/retire", {"confirm": "yes"})
    assert not q("SELECT 1 FROM metros WHERE booking_system <> 'engine'")
    assert q("SELECT value FROM app_settings WHERE key = 'enrollware_retired'")[0]["value"] is True
    assert q("SELECT 1 FROM audit_log WHERE action = 'enrollware.retire'")
    assert "Enrollware is retired" in text_of(client.get("/admin/cutover"))
