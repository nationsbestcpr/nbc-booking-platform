"""Airtable sync, Stripe checkout and webhooks, GHL contacts and opportunities.

Every outside call goes through app.integrations.http.transport, which these
tests replace with FakePlatforms: an in-memory Airtable base, Stripe and GHL
sub-account. Nothing leaves the machine.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import re
import string
import time
from datetime import date, timedelta
from urllib.parse import parse_qsl, unquote

import pytest
from sqlalchemy import text

from app import db
from app.integrations import airtable, airtable_sync, checkout, ghl, http, outbox
from tests.conftest import csrf_from
from tests.test_dashboards import text_of, unique_email

WEBHOOK_SECRET = "whsec_test_secret"


def rec_id() -> str:
    return "rec" + "".join(random.choices(string.ascii_letters + string.digits, k=14))


class Res:
    def __init__(self, status: int, body: dict | None = None):
        self.status_code, self._body, self.headers, self.text = status, body, {}, json.dumps(body or {})

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakePlatforms:
    def __init__(self):
        tag = "".join(random.choices(string.ascii_lowercase, k=6))
        self.site_name = f"Harbor Hall {tag}"
        self.metro = f"Tidewater {tag.capitalize()}"           # a new city per test
        self.metro_slug = f"tidewater-{tag}-va"
        self.market = f"TIDEWATER{tag.upper()}"[:12]
        self.day = (date.today() + timedelta(days=12)).isoformat()
        self.course_fa, self.course_bls, self.loc = rec_id(), rec_id(), rec_id()
        # Field names and shapes as in the real NBC Master Base (Oct 2026).
        self.schema = {
            "Course Master": ["Converted Course Name", "Course Family", "Delivery Type", "Price", "Companion Price",
                              "Max Seats", "Duration in Minutes", "Companion Duration (min)", "Status",
                              "Concurrent Group Key"],
            "Master Locations": ["Abbreviation", "Name", "Address", "Metro Area", "Time Zone", "Max Student Count",
                                 "Access Code", "Parking", "Status", "Per Class Rate"],
            "Class Schedule": ["Calendar", "Status", "Date", "Start Time", "Abbreviation", "Course Type", "Course NAME",
                               "Instructor Assigned", "ASSIGN INSTRUCTOR (Name)",
                               "Duration in Minutes (from Duration (min))", "Start Date/Time"],
        }
        self.virtual_loc = rec_id()
        self.tables = {
            "Course Master": [
                {"id": self.course_fa, "fields": {"Converted Course Name": "ARC Blended r.25", "Course Family":
                 "FA_CPR_AED", "Delivery Type": "Blended", "Price": 108, "Companion Price": 88, "Max Seats": 12,
                 "Duration in Minutes": 240, "Companion Duration (min)": 210, "Status": "Active",
                 "Concurrent Group Key": "FA_CPR_AED_BL"}},
                {"id": self.course_bls, "fields": {"Converted Course Name": "ARC BLS r.25", "Course Family": "BLS",
                 "Delivery Type": "Blended", "Price": 88, "Max Seats": 9, "Duration in Minutes": 180,
                 "Status": "Active"}},
                {"id": rec_id(), "fields": {"Converted Course Name": "AHA BLS", "Status": "Inactive"}},
            ],
            "Master Locations": [
                {"id": self.loc, "fields": {"Abbreviation": f"HH{tag}", "Name": f"Virginia - {self.metro} - {self.site_name}",
                 "Address": "100 Main St, Suite 2, Norfolk, VA 23510", "Metro Area": f"Virginia - {self.metro}",
                 "Time Zone": "Eastern", "Max Student Count": "14", "Access Code": "4321#",
                 "Parking": "Lot behind the hall", "Status": ["Currently Scheduling"], "Per Class Rate": 50}},
                {"id": self.virtual_loc, "fields": {"Abbreviation": "Zoom", "Name": "VIRTUAL - Zoom",
                 "Metro Area": "VIRTUAL", "Status": ["VIRTUAL"]}},
                {"id": rec_id(), "fields": {"Abbreviation": "Old", "Name": "Somewhere unused",
                 "Metro Area": "Texas - Houston", "Status": ["Inactive"]}},
            ],
            "Class Schedule": [],
        }
        self.calls: list[tuple[str, str, dict]] = []
        self.stripe_sessions: dict[str, dict] = {}
        self.stripe_expired: list[str] = []
        self.stripe_refunds: list[dict] = []
        self.ghl_contacts: dict[str, str] = {}
        self.ghl_opps: dict[str, dict] = {}
        self.ghl_fail_next = 0
        self.stripe_refuse_refunds = False
        self.ghl_refuse_second_opportunity = False

    def add_class(self, *, status=("Enrollware",), start="9:00 AM", minutes=240, course=None, day=None,
                  location=None) -> str:
        rid = rec_id()
        fields = {"Calendar": "x", "Status": list(status), "Date": day or self.day, "Abbreviation": [location or self.loc],
                  "Course Type": [course or self.course_fa], "Course NAME": "typed name", "Instructor Assigned": True,
                  "ASSIGN INSTRUCTOR (Name)": "Pat Instructor",
                  # the base's formula reads date + time as UTC; the sync must not trust it
                  "Start Date/Time": f"{day or self.day}T09:00:00.000Z"}
        if start:
            fields["Start Time"] = start
        if minutes:
            fields["Duration in Minutes (from Duration (min))"] = minutes
        self.tables["Class Schedule"].append({"id": rid, "fields": fields})
        return rid

    def record(self, rid: str) -> dict:
        return next(r for rows in self.tables.values() for r in rows if r["id"] == rid)

    # transport(method, url, headers=, params=, json=, data=, timeout=)
    def __call__(self, method, url, headers=None, params=None, json=None, data=None, timeout=None):
        self.calls.append((method, url, {"params": params, "json": json, "data": data, "headers": headers}))
        if url.startswith(airtable.API):
            return self._airtable(url[len(airtable.API) + 1:], params)
        if url.startswith("https://api.stripe.com/v1/"):
            return self._stripe(method, url[len("https://api.stripe.com/v1/"):], dict(data or []))
        if url.startswith(ghl.API):
            return self._ghl(method, url[len(ghl.API):], params or {}, json or {})
        raise AssertionError(f"unexpected call {method} {url}")

    def _airtable(self, path, params):
        if path.startswith("meta/bases/"):
            return Res(200, {"tables": [{"name": t, "fields": [{"name": f, "type": "singleLineText"} for f in fs]}
                                        for t, fs in self.schema.items()]})
        _, table, *rest = path.split("/")
        table = unquote(table)
        if table not in self.tables:
            return Res(404, {"error": "NOT_FOUND"})
        if rest:
            try:
                return Res(200, self.record(rest[0]))
            except StopIteration:
                return Res(404, {"error": "NOT_FOUND"})
        wanted = [v for k, v in params if k == "fields[]"]
        unknown = [f for f in wanted if f not in self.schema[table]]
        if unknown:
            return Res(422, {"error": {"type": "UNKNOWN_FIELD_NAME", "message": f"Unknown field name: {unknown[0]}"}})
        rows = [{"id": r["id"], "createdTime": "2026-10-01T00:00:00.000Z",
                 "fields": {k: v for k, v in r["fields"].items() if k in wanted}} for r in self.tables[table]]
        return Res(200, {"records": rows})

    def _stripe(self, method, path, data):
        if path == "checkout/sessions":
            cs_id = "cs_test_" + "".join(random.choices(string.ascii_letters, k=20))
            self.stripe_sessions[cs_id] = data
            return Res(200, {"id": cs_id, "url": f"https://checkout.stripe.test/{cs_id}"})
        m = re.fullmatch(r"checkout/sessions/(cs_\w+)/expire", path)
        if m:
            self.stripe_expired.append(m.group(1))
            return Res(200, {"id": m.group(1), "status": "expired"})
        if path == "refunds":
            if self.stripe_refuse_refunds:
                return Res(400, {"error": {"type": "invalid_request_error", "code": "charge_already_refunded",
                                           "message": "Charge has already been refunded."}})
            self.stripe_refunds.append(data)
            return Res(200, {"id": "re_" + "".join(random.choices(string.ascii_letters, k=12))})
        raise AssertionError(path)

    def _ghl(self, method, path, params, body):
        if self.ghl_fail_next:
            self.ghl_fail_next -= 1
            return Res(503, {"message": "Service unavailable"})
        if path == "/opportunities/pipelines":
            return Res(200, {"pipelines": [{"id": "pipe-1", "name": "Community Class", "stages": [
                {"id": "st-pending", "name": "Pending"}, {"id": "st-complete", "name": "Complete"},
                {"id": "st-noshow", "name": "No Show"}, {"id": "st-cancel", "name": "Cancelled"},
                {"id": "st-wait", "name": "Waitlisted"}]}]})
        if path == "/contacts/upsert":
            email = body["email"].lower()
            new = email not in self.ghl_contacts
            self.ghl_contacts.setdefault(email, "ct-" + "".join(random.choices(string.ascii_lowercase, k=10)))
            return Res(200 if not new else 201, {"new": new, "contact": {"id": self.ghl_contacts[email]}})
        if path == "/opportunities/search":
            return Res(200, {"opportunities": [{"id": k, **v} for k, v in self.ghl_opps.items()
                                               if v["contactId"] == params.get("contact_id")]})
        if re.fullmatch(r"/locations/[^/]+/customFields", path):
            return Res(200, {"customFields": [{"id": f"cf-{k}", "fieldKey": f"opportunity.{k}", "name": k}
                                              for k in ("class_id", "course", "course_date", "course_location",
                                                        "reg_type", "order_total", "shipping")]})
        if path == "/opportunities/" and method == "POST":
            if self.ghl_refuse_second_opportunity and any(o["contactId"] == body["contactId"]
                                                          for o in self.ghl_opps.values()):
                return Res(400, {"message": "Can not create duplicate opportunity for the contact."})
            oid = "op-" + "".join(random.choices(string.ascii_lowercase, k=10))
            self.ghl_opps[oid] = dict(body)
            return Res(201, {"opportunity": {"id": oid}})
        m = re.fullmatch(r"/opportunities/(op-\w+)", path)
        if m and method == "PUT":
            self.ghl_opps[m.group(1)].update(body)
            return Res(200, {"opportunity": {"id": m.group(1)}})
        raise AssertionError(path)


@pytest.fixture()
def fake(monkeypatch):
    platforms = FakePlatforms()
    monkeypatch.setattr(http, "transport", platforms)
    monkeypatch.setattr(airtable, "MIN_SECONDS_BETWEEN_CALLS", 0)
    for k, v in {"AIRTABLE_TOKEN": "pat-test", "AIRTABLE_BASE_ID": "appTESTBASE000001",
                 "STRIPE_SECRET_KEY": "sk_test_123", "STRIPE_WEBHOOK_SECRET": WEBHOOK_SECRET,
                 "GHL_API_TOKEN": "pit-test", "GHL_LOCATION_ID": "loc-test"}.items():
        monkeypatch.setenv(k, v)
    return platforms


@pytest.fixture(autouse=True)
def leave_ledgers_as_found(app):
    """Runs, problems and queued jobs made by a test don't leak into the dashboard tests."""
    with app.app_context():
        with db.engine().connect() as conn:
            started = conn.execute(text("SELECT now()")).scalar()
    yield
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("UPDATE integration_issues SET resolved_at = now() WHERE first_seen_at >= :t "
                          "AND resolved_at IS NULL"), {"t": started})
        conn.execute(text("UPDATE integration_issues SET run_id = NULL WHERE run_id IN "
                          "(SELECT id FROM integration_runs WHERE started_at >= :t)"), {"t": started})
        conn.execute(text("DELETE FROM integration_runs WHERE started_at >= :t"), {"t": started})
        conn.execute(text("UPDATE outbox_jobs SET status = 'cancelled' WHERE created_at >= :t "
                          "AND status NOT IN ('succeeded', 'cancelled')"), {"t": started})
        conn.execute(text("UPDATE sessions SET is_published = false WHERE created_at >= :t"), {"t": started})


@pytest.fixture()
def q(app):
    def _q(sql: str, **params):
        with app.app_context():
            with db.engine().connect() as conn:
                return [dict(r) for r in conn.execute(text(sql), params).mappings()]
    return _q


@pytest.fixture()
def run_sync(app):
    def _run(**kw):
        with app.app_context():
            return airtable_sync.run("manual", **kw)
    return _run


def session_for(q, record_id: str) -> dict:
    rows = q("""SELECT s.* FROM sessions s JOIN session_airtable_records r ON r.session_id = s.id
                 WHERE r.airtable_record_id = :r""", r=record_id)
    return rows[0] if rows else None


def stripe_post(client, event: dict, secret: str = WEBHOOK_SECRET):
    body = json.dumps(event).encode()
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/stripe", data=body, headers={"Stripe-Signature": f"t={ts},v1={sig}",
                                                                "Content-Type": "application/json"})


def completed_event(cs_id: str, subtotal: int, discount: int = 0, *, livemode: bool = False) -> dict:
    return {"id": "evt_" + "".join(random.choices(string.ascii_letters, k=16)), "type": "checkout.session.completed",
            "livemode": livemode, "data": {"object": {
                "id": cs_id, "object": "checkout.session", "payment_status": "paid", "livemode": livemode,
                "amount_subtotal": subtotal, "amount_total": subtotal - discount,
                "total_details": {"amount_discount": discount, "amount_tax": 0},
                "payment_intent": "pi_" + "".join(random.choices(string.ascii_letters, k=16)), "customer": None}}}


# --- Airtable ---------------------------------------------------------------------------

def test_sync_creates_city_site_products_and_classes(fake, run_sync, q):
    fa = fake.add_class()
    bls = fake.add_class(course=fake.course_bls, start="2:00 PM", minutes=None)
    online = fake.add_class(location=fake.virtual_loc, start="6pm")
    planning = fake.add_class(status=("Scheduled",), start="10:00 AM")
    broken = fake.add_class(start=None)

    stats = run_sync()
    assert stats["status"] == "partial"
    assert stats["classes_created"] == 2 and stats["classes_skipped"] == 1 and stats["problems"] == 1

    city = q("SELECT * FROM metros WHERE slug = :s", s=fake.metro_slug)[0]
    assert city["booking_system"] == "enrollware" and city["time_zone"] == "America/New_York"
    site = q("SELECT * FROM sites WHERE name = :n", n=fake.site_name)[0]
    assert (site["address_line1"], site["address_line2"], site["city"], site["state_code"], site["postal_code"]) == \
        ("100 Main St", "Suite 2", "Norfolk", "VA", "23510")
    assert q("SELECT access_code FROM site_private_details WHERE site_id = :s", s=site["id"])[0]["access_code"] == "4321#"

    s = session_for(q, fa)
    assert s["is_published"] and s["status"] == "scheduled" and s["capacity"] == 12
    assert s["room_max_student_count"] == 14 and s["airtable_lead_instructor"] == "Pat Instructor"
    local = q("SELECT to_char(starts_at AT TIME ZONE 'America/New_York', 'HH24:MI') AS t, "
              "to_char(ends_at AT TIME ZONE 'America/New_York', 'HH24:MI') AS e FROM sessions WHERE id = :i", i=s["id"])[0]
    assert (local["t"], local["e"]) == ("09:00", "13:00")
    prices = {r["short_name"]: r["price_cents"] for r in q("""
        SELECT p.short_name, sp.price_cents FROM session_products sp JOIN products p ON p.id = sp.product_id
         WHERE sp.session_id = :s""", s=s["id"])}
    assert prices == {"A&P": 10800, "Adult": 8800}
    assert session_for(q, bls)["capacity"] == 9
    assert session_for(q, planning) is None and session_for(q, broken) is None and session_for(q, online) is None
    bls_times = q("SELECT ends_at - starts_at AS d FROM sessions WHERE id = :i", i=session_for(q, bls)["id"])[0]
    assert bls_times["d"] == timedelta(minutes=180)                 # the course's own length
    assert not q("SELECT 1 FROM sites WHERE name = 'Somewhere unused'")  # only locations classes use
    issue = q("SELECT * FROM integration_issues WHERE external_ref = :r AND resolved_at IS NULL", r=broken)[0]
    assert issue["code"] == "CLASS_NO_START" and "start time" in issue["message"]
    # Airtable is only ever read.
    assert {c[0] for c in fake.calls if c[1].startswith(airtable.API)} == {"GET"}
    # Rental fees are never requested from Airtable.
    assert all("Per Class Rate" not in str(c[2]["params"]) for c in fake.calls)


def test_sync_follows_changes_in_airtable(fake, run_sync, q):
    fa = fake.add_class()
    other = fake.add_class(start="2:00 PM")
    run_sync()
    assert run_sync()["classes_unchanged"] == 2

    fake.record(fa)["fields"]["Start Time"] = "10:00 AM"
    fake.record(other)["fields"]["Status"] = ["Approved", "Enrollware", "CANCELLED - Weather"]
    stats = run_sync()
    assert stats["classes_updated"] == 2
    moved = session_for(q, fa)
    assert moved["previous_starts_at"] is not None and moved["rescheduled_at"] is not None
    cancelled = session_for(q, other)
    assert cancelled["status"] == "cancelled" and not cancelled["is_published"]

    fake.record(fa)["fields"]["Status"] = ["Approved", "Send POC Cancellation"]   # Enrollware off: off sale, not cancelled
    run_sync()
    assert not session_for(q, fa)["is_published"] and session_for(q, fa)["status"] == "scheduled"
    fake.record(fa)["fields"]["Status"] = ["Approved", "Enrollware"]
    run_sync()
    assert session_for(q, fa)["is_published"]

    fake.tables["Class Schedule"].remove(fake.record(fa))        # row deleted in Airtable
    assert run_sync()["classes_removed"] == 1
    assert not session_for(q, fa)["is_published"]


def test_two_rows_at_one_time_share_one_class(fake, run_sync, q):
    a = fake.add_class()
    b = fake.add_class()          # the same site, course family and start
    run_sync()
    assert session_for(q, a)["id"] == session_for(q, b)["id"]


def test_a_cancelled_companion_row_only_stops_its_course(fake, run_sync, q):
    a = fake.add_class()
    b = fake.add_class()
    fake.record(b)["fields"]["Course Type"] = [fake.course_fa]
    run_sync()
    sid = session_for(q, a)["id"]
    fake.record(b)["fields"]["Status"] = ["Cancelled"]
    run_sync()
    s = session_for(q, a)
    assert s["status"] == "scheduled" and s["is_published"]
    fake.record(a)["fields"]["Status"] = ["Cancelled"]
    run_sync()
    assert session_for(q, a)["status"] == "cancelled" and session_for(q, b)["id"] == sid


def test_rows_of_one_class_that_disagree_are_flagged_not_flipped(fake, run_sync, q):
    a = fake.add_class()
    b = fake.add_class()
    run_sync()
    before = session_for(q, a)["starts_at"]
    fake.record(b)["fields"]["Start Time"] = "10:00 AM"
    stats = run_sync()
    assert stats["problems"] == 2 and session_for(q, a)["starts_at"] == before
    assert q("""SELECT count(*) AS n FROM integration_issues WHERE code = 'CLASS_ROWS_DISAGREE'
                 AND external_ref IN (:a, :b) AND resolved_at IS NULL""", a=a, b=b)[0]["n"] == 2
    fake.record(a)["fields"]["Start Time"] = "10:00 AM"
    run_sync()
    assert session_for(q, a)["starts_at"] != before


def test_missing_required_field_stops_the_sync_with_a_clear_problem(fake, run_sync, q):
    fake.schema["Class Schedule"].remove("Status")
    stats = run_sync()
    assert stats["status"] == "failed"
    issue = q("""SELECT * FROM integration_issues WHERE code = 'MISSING_FIELD' AND external_ref = 'table:Class Schedule'
                  AND resolved_at IS NULL""")[0]
    assert "Status" in issue["message"]
    fake.schema["Class Schedule"].append("Status")
    assert run_sync()["status"] in ("succeeded", "partial")
    assert not q("""SELECT 1 FROM integration_issues WHERE code = 'MISSING_FIELD'
                     AND external_ref = 'table:Class Schedule' AND resolved_at IS NULL""")


def test_value_parsers():
    assert airtable_sync.parse_clock("9:30 am").strftime("%H:%M") == "09:30"
    assert airtable_sync.parse_clock("1 PM").strftime("%H:%M") == "13:00"
    assert airtable_sync.parse_clock("13:15").strftime("%H:%M") == "13:15"
    assert airtable_sync.parse_clock(32400).strftime("%H:%M") == "09:00"
    for raw, want in (("9am", "09:00"), ("4:30p", "16:30"), ("1:00:00 PM", "13:00"), ("15:00:00", "15:00"),
                      ("8a-4p", "08:00"), ("12:30", "12:30"), ("12pm", "12:00"), ("12:15 AM", "00:15")):
        assert airtable_sync.parse_clock(raw).strftime("%H:%M") == want, raw
    for raw in ("NOT Available", "4th of July", "10", "Weeknights", "13pm"):
        assert airtable_sync.parse_clock(raw) is None, raw
    assert airtable_sync.parse_metro("Virginia - Richmond") == ("Richmond", "VA")
    assert airtable_sync.parse_metro("Texas - DFW") == ("DFW", "TX")
    assert airtable_sync.parse_metro("Richmond, VA") == ("Richmond", "VA")
    assert airtable_sync.time_zone("Central", None) == "America/Chicago"
    assert airtable_sync.time_zone("America/Denver", None) == "America/Denver"
    assert airtable_sync.time_zone(None, "TX") == "America/Chicago"
    parts = airtable_sync.parse_address("2000 Ridge Rd, Henrico, VA 23229, USA")
    assert (parts["line1"], parts["city"], parts["state"], parts["zip"]) == ("2000 Ridge Rd", "Henrico", "VA", "23229")


# --- Booking, Stripe and GHL --------------------------------------------------------------

@pytest.fixture()
def on_sale(fake, run_sync, q, client, sign_in, app):
    """A synced class in a city switched to booking here (through the Locations page)."""
    rid = fake.add_class()
    run_sync()
    city = q("SELECT id FROM metros WHERE slug = :s", s=fake.metro_slug)[0]
    sign_in("admin@example.test")
    token = csrf_from(client, "/admin/locations")
    res = client.post(f"/admin/locations/{city['id']}/booking", data={"booking_system": "engine", "csrf_token": token})
    assert res.status_code == 302
    with client.session_transaction() as sess:
        sess.clear()
    return session_for(q, rid)


def book(client, s, seats=2, *, buyer_email=None, others=None, product=None):
    path = f"/book/{s['public_id']}?seats={seats}"
    if product is None:
        page = client.get(path)
        product = re.search(r'name="product_id" value="(\d+)" checked', page.get_data(as_text=True)).group(1)
    data = {"csrf_token": csrf_from(client, "/sign-in"), "seats": seats, "product_id": product,
            "buyer_first_name": "Dana", "buyer_last_name": "Buyer", "buyer_email": buyer_email or unique_email("buyer"),
            "buyer_phone": "(757) 555-0100", "buyer_attends": "yes"}
    for i, email in enumerate(others or [unique_email("guest") for _ in range(seats - 1)], start=1):
        data.update({f"a{i}_first": f"Guest{i}", f"a{i}_last": "Person", f"a{i}_email": email})
    return client.post(f"/book/{s['public_id']}", data=data)


def order_of(q, cs_id: str) -> dict:
    return q("""SELECT o.* FROM orders o JOIN checkout_sessions c ON c.order_id = o.id
                 WHERE c.stripe_checkout_session_id = :c""", c=cs_id)[0]


def test_book_pay_and_sync_to_ghl(fake, on_sale, client, q, app):
    s = on_sale
    res = book(client, s, seats=2)
    assert res.status_code == 303 and res.headers["Location"].startswith("https://checkout.stripe.test/")
    cs_id = res.headers["Location"].rsplit("/", 1)[1]
    sent = fake.stripe_sessions[cs_id]
    assert sent["line_items[0][quantity]"] == "2" and sent["line_items[0][price_data][unit_amount]"] == "10800"
    assert sent["allow_promotion_codes"] == "true"
    order = order_of(q, cs_id)
    assert order["status"] == "open" and order["subtotal_cents"] == 21600 and not order["livemode"]
    assert q("SELECT held_seats FROM session_availability WHERE session_id = :s", s=s["id"])[0]["held_seats"] == 2

    assert stripe_post(client, completed_event(cs_id, 21600, discount=1000), secret="wrong").status_code == 400
    event = completed_event(cs_id, 21600, discount=1000)
    res = stripe_post(client, event)
    assert res.status_code == 200 and res.get_json()["result"] == "processed"
    assert stripe_post(client, event).get_json()["duplicate"] is True

    order = order_of(q, cs_id)
    assert order["status"] == "paid" and order["total_cents"] == 20600 and order["discount_cents"] == 1000
    regs = q("SELECT * FROM registrations WHERE order_id = :o ORDER BY id", o=order["id"])
    assert [r["status"] for r in regs] == ["confirmed", "confirmed"]
    assert [r["amount_paid_cents"] for r in regs] == [10300, 10300]
    avail = q("SELECT held_seats, sold_seats FROM session_availability WHERE session_id = :s", s=s["id"])[0]
    assert (avail["held_seats"], avail["sold_seats"]) == (0, 2)
    assert q("SELECT amount_cents FROM payments WHERE order_id = :o", o=order["id"])[0]["amount_cents"] == 20600
    assert "You're Booked" in text_of(client.get(f"/book/done?order={order['public_id']}"))

    with app.app_context():
        assert outbox.run_due(targets=["ghl"])["succeeded"] >= 2
    regs = q("SELECT * FROM registrations WHERE order_id = :o ORDER BY id", o=order["id"])
    opps = [fake.ghl_opps[r["ghl_opportunity_id"]] for r in regs]
    local_day = q("SELECT local_date FROM sessions WHERE id = :s", s=s["id"])[0]["local_date"]
    assert all(o["pipelineStageId"] == "st-pending" and o["pipelineId"] == "pipe-1" for o in opps)
    assert opps[0]["name"] == f"{local_day.isoformat()} | AP-BLENDED | {fake.market}-Norfolk"
    assert opps[0]["monetaryValue"] == 103.0
    assert {r["ghl_stage_pushed"] for r in regs} == {"Pending"} and all(r["reg_type"] == "new" for r in regs)
    assert all(q("SELECT ghl_contact_id FROM people WHERE id = :p", p=r["person_id"])[0]["ghl_contact_id"] for r in regs)

    # Running it again never makes a second opportunity.
    before = len(fake.ghl_opps)
    with app.app_context():
        ghl.sync_registration({"payload": {"registration_id": regs[0]["id"]}})
    assert len(fake.ghl_opps) == before


def test_ghl_outage_is_retried_then_succeeds(fake, on_sale, client, q, app):
    res = book(client, on_sale, seats=1)
    cs_id = res.headers["Location"].rsplit("/", 1)[1]
    stripe_post(client, completed_event(cs_id, 10800))
    fake.ghl_fail_next = 99
    with app.app_context():
        outbox.run_due(targets=["ghl"])
    job = q("""SELECT * FROM outbox_jobs WHERE target = 'ghl' AND entity_id =
                 (SELECT r.id FROM registrations r JOIN checkout_sessions c ON c.order_id = r.order_id
                   WHERE c.stripe_checkout_session_id = :c)""", c=cs_id)[0]
    assert job["status"] == "failed" and "503" in job["last_error"]
    fake.ghl_fail_next = 0
    with app.app_context(), db.transaction() as conn:
        outbox.retry(conn, job["id"]) or conn.execute(text("UPDATE outbox_jobs SET run_after = now() WHERE id = :i"),
                                                       {"i": job["id"]})
    with app.app_context():
        outbox.run_due(targets=["ghl"])
    assert q("SELECT status FROM outbox_jobs WHERE id = :i", i=job["id"])[0]["status"] == "succeeded"


def test_unpaid_hold_expires_and_late_payment_is_refunded(fake, on_sale, client, q, app):
    s = on_sale
    late = book(client, s, seats=1).headers["Location"].rsplit("/", 1)[1]
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("""UPDATE seat_holds SET expires_at = now() - interval '1 minute'
                              WHERE order_id = (SELECT order_id FROM checkout_sessions
                                                 WHERE stripe_checkout_session_id = :c)"""), {"c": late})
    with app.app_context():
        assert checkout.expire_holds() >= 1
        outbox.run_due(targets=["stripe"])
    assert late in fake.stripe_expired
    assert order_of(q, late)["status"] == "expired"

    # Fill the class, then the late payment arrives: no seat, so it's refunded.
    sold = q("SELECT seats_left FROM session_availability WHERE session_id = :s", s=s["id"])[0]["seats_left"]
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("UPDATE sessions SET capacity = capacity - :n WHERE id = :s"), {"n": sold, "s": s["id"]})
    assert stripe_post(client, completed_event(late, 10800)).status_code == 200
    order = order_of(q, late)
    assert q("SELECT count(*) AS n FROM registrations WHERE order_id = :o AND status = 'confirmed'",
             o=order["id"])[0]["n"] == 0
    assert q("SELECT code FROM integration_issues WHERE external_ref = :c", c=late)[0]["code"] == "PAID_AFTER_HOLD_LAPSED"
    with app.app_context():
        outbox.run_due(targets=["stripe"])
    assert fake.stripe_refunds and fake.stripe_refunds[-1]["amount"] == "10800"
    assert order_of(q, late)["status"] == "refunded"
    assert "Was Taken" in text_of(client.get(f"/book/done?order={order['public_id']}"))


def test_payment_after_class_start_is_refunded_and_a_refused_refund_is_flagged(fake, on_sale, client, q, app):
    cs = book(client, on_sale, seats=1).headers["Location"].rsplit("/", 1)[1]
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("""UPDATE sessions SET starts_at = now() - interval '1 hour', ends_at = now() + interval '2 hours'
                              WHERE id = :s"""), {"s": on_sale["id"]})
    stripe_post(client, completed_event(cs, 10800))
    order = order_of(q, cs)
    assert q("SELECT count(*) AS n FROM registrations WHERE order_id = :o AND status = 'confirmed'",
             o=order["id"])[0]["n"] == 0
    fake.stripe_refuse_refunds = True
    with app.app_context():
        outbox.run_due(targets=["stripe"])
    refund = q("SELECT * FROM refunds WHERE order_id = :o", o=order["id"])[0]
    assert refund["status"] == "failed" and "already been refunded" in refund["failure_reason"]
    assert q("SELECT severity FROM integration_issues WHERE external_ref = :r AND resolved_at IS NULL",
             r=f"refund:{refund['id']}")[0]["severity"] == "error"


def test_rebooking_a_class_gets_its_own_opportunity(fake, on_sale, client, q, app):
    email = unique_email("again")
    first = book(client, on_sale, seats=1, buyer_email=email).headers["Location"].rsplit("/", 1)[1]
    stripe_post(client, completed_event(first, 10800))
    with app.app_context():
        outbox.run_due(targets=["ghl"])
    old = q("SELECT r.* FROM registrations r JOIN checkout_sessions c ON c.order_id = r.order_id "
            "WHERE c.stripe_checkout_session_id = :c", c=first)[0]
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("""UPDATE registrations SET status = 'cancelled', cancelled_at = now(),
                                    cancel_reason_code = 'customer_request' WHERE id = :r"""), {"r": old["id"]})
    second = book(client, on_sale, seats=1, buyer_email=email).headers["Location"].rsplit("/", 1)[1]
    stripe_post(client, completed_event(second, 10800))
    with app.app_context():
        outbox.run_due(targets=["ghl"])
    new = q("SELECT r.* FROM registrations r JOIN checkout_sessions c ON c.order_id = r.order_id "
            "WHERE c.stripe_checkout_session_id = :c", c=second)[0]
    assert new["ghl_opportunity_id"] and new["ghl_opportunity_id"] != old["ghl_opportunity_id"]
    assert new["reg_type"] == "existing"


def test_late_payment_is_kept_when_a_seat_is_free(fake, on_sale, client, q, app):
    cs = book(client, on_sale, seats=1).headers["Location"].rsplit("/", 1)[1]
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("""UPDATE seat_holds SET expires_at = now() - interval '1 minute'
                              WHERE order_id = (SELECT order_id FROM checkout_sessions
                                                 WHERE stripe_checkout_session_id = :c)"""), {"c": cs})
    with app.app_context():
        checkout.expire_holds()
    stripe_post(client, completed_event(cs, 10800))
    assert order_of(q, cs)["status"] == "paid"


def test_leaving_stripe_frees_the_seats_at_once(fake, on_sale, client, q, app):
    cs = book(client, on_sale, seats=3).headers["Location"].rsplit("/", 1)[1]
    order = order_of(q, cs)
    page = client.get(f"/book/cancelled?order={order['public_id']}")
    assert page.status_code == 200 and "Wasn't Finished" in text_of(page)
    assert order_of(q, cs)["status"] == "cancelled"
    with app.app_context():
        outbox.run_due(targets=["stripe"])
    assert cs in fake.stripe_expired


def test_booking_refuses_more_seats_than_left_and_duplicate_emails(fake, on_sale, client, q):
    same = unique_email("dup")
    res = book(client, on_sale, seats=2, buyer_email=same, others=[same])
    assert res.status_code == 400 and "own email" in text_of(res)
    page = client.get(f"/book/{on_sale['public_id']}").get_data(as_text=True)
    product = re.search(r'name="product_id" value="(\d+)" checked', page).group(1)
    with client.application.app_context(), db.transaction() as conn:
        conn.execute(text("UPDATE sessions SET capacity = 1 WHERE id = :s"), {"s": on_sale["id"]})
    assert book(client, on_sale, seats=1, product=product).status_code == 303
    # Someone else had the form open: their booking is refused, nothing is held.
    res = book(client, on_sale, seats=1, product=product)
    assert res.status_code == 400 and "filled up" in text_of(res), text_of(res)[-1500:]
    assert "Class full" in text_of(client.get(f"/book/{on_sale['public_id']}"))


def test_live_stripe_key_is_refused_outside_production(fake, on_sale, client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_123")
    page = text_of(client.get(f"/book/{on_sale['public_id']}"))
    assert "Online booking opens soon" in page and "Continue to payment" not in page


def test_enrollware_cities_are_not_bookable(fake, run_sync, q, client):
    rid = fake.add_class()
    run_sync()
    assert q("SELECT booking_system FROM metros WHERE slug = :s", s=fake.metro_slug)[0]["booking_system"] == "enrollware"
    s = session_for(q, rid)
    page = text_of(client.get(f"/book/{s['public_id']}"))
    assert "Not on sale" in page and "Continue to payment" not in page


# --- Dashboard ---------------------------------------------------------------------------

def test_integrations_page_shows_status_and_actions(fake, run_sync, client, sign_in, app):
    fake.add_class()
    run_sync()
    sign_in("admin@example.test")
    page = text_of(client.get("/admin/integrations"))
    for words in ("Sync now", "Check Airtable fields", "pipeline stages", "Test", "/webhooks/stripe",
                  "Not synced yet"):
        assert words in page
    fields = text_of(client.get("/admin/integrations/airtable/fields"))
    assert "“Abbreviation”" in fields and "“Course Type”" in fields and "Ready" in fields

    token = csrf_from(client, "/admin/integrations")
    res = client.post("/admin/integrations/ghl/stages", data={"csrf_token": token})
    assert res.status_code == 302
    assert "Pending" in text_of(client.get("/admin/integrations"))
    res = client.post("/admin/integrations/airtable/sync", data={"csrf_token": token}, follow_redirects=True)
    assert "Airtable synced" in text_of(res)


def test_airtable_button_syncs_one_class(fake, run_sync, client, sign_in, q):
    rid = fake.add_class()
    run_sync()
    fake.record(rid)["fields"]["Start Time"] = "11:00 AM"
    client.get("/sign-out")
    assert client.get(f"/admin/sync?record={rid}").status_code == 302      # sign-in first
    sign_in("admin@example.test")
    res = client.get(f"/admin/sync?record={rid}")
    assert res.status_code == 302 and f"/admin/classes/{session_for(q, rid)['id']}" in res.headers["Location"]
    assert session_for(q, rid)["previous_starts_at"] is not None


def test_sync_buttons_need_their_permission(fake, client, sign_in):
    sign_in("cs@example.test")         # customer service: sees status, can't run the sync
    assert client.get("/admin/integrations").status_code == 200
    assert client.get(f"/admin/sync?record={rec_id()}").status_code == 403


def test_ghl_custom_field_ids_are_edited_in_business_settings(fake, on_sale, client, sign_in, q, app):
    sign_in("admin@example.test")
    token = csrf_from(client, "/admin/settings")
    bad = client.post("/admin/settings", data={"csrf_token": token, "key": "ghl_custom_field_ids",
                                               "value": '{"colour": "x"}'}, follow_redirects=True)
    assert "Nothing was changed" in text_of(bad)
    try:
        client.post("/admin/settings", data={"csrf_token": token, "key": "ghl_custom_field_ids",
                                             "value": '{"session_id": "cf-session", "site": "cf-site"}'})
        with client.session_transaction() as sess:
            sess.clear()
        cs = book(client, on_sale, seats=1).headers["Location"].rsplit("/", 1)[1]
        stripe_post(client, completed_event(cs, 10800))
        with app.app_context():
            outbox.run_due(targets=["ghl"])
        opp = list(fake.ghl_opps.values())[-1]
        fields = {f["id"]: f["field_value"] for f in opp["customFields"]}
        assert fields == {"cf-session": str(on_sale["public_id"]), "cf-site": fake.site_name}
    finally:
        with app.app_context(), db.transaction() as conn:
            conn.execute(text("UPDATE app_settings SET value = '{}' WHERE key = 'ghl_custom_field_ids'"))


def test_hold_expiry_skips_an_order_the_webhook_is_confirming(fake, on_sale, client, q, app):
    cs = book(client, on_sale, seats=1).headers["Location"].rsplit("/", 1)[1]
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("""UPDATE seat_holds SET expires_at = now() - interval '1 minute'
                              WHERE order_id = (SELECT order_id FROM checkout_sessions
                                                 WHERE stripe_checkout_session_id = :c)"""), {"c": cs})
    with app.app_context():
        with db.transaction() as webhook:            # the webhook holds the order, mid-confirmation
            assert checkout._find(webhook, cs)["order_status"] == "open"
            assert checkout.expire_holds() == 0         # skipped, not deadlocked
        assert checkout.expire_holds() == 1             # next pass, once the webhook is done


def test_worker_start_does_the_setup_by_itself(fake, app, q):
    from app.integrations import worker
    fake.add_class()
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("DELETE FROM ghl_stage_map"))
    with app.app_context():
        done = worker.on_start()
    try:
        assert done["ghl_stages"] == "loaded" and done["airtable"] in ("succeeded", "partial")
        assert done["ghl_fields"].startswith("loaded, none for")          # some keys aren't in this layout
        assert {r["internal_state"] for r in q("SELECT internal_state FROM ghl_stage_map")} >= {"booked", "cancelled"}
        ids = q("SELECT value FROM app_settings WHERE key = 'ghl_custom_field_ids'")[0]["value"]
        assert ids["class_id"] == "cf-class_id" and ids["course_location"] == "cf-course_location"
        assert "shipping" not in ids
    finally:
        with app.app_context(), db.transaction() as conn:
            conn.execute(text("UPDATE app_settings SET value = '{}' WHERE key = 'ghl_custom_field_ids'"))


def test_ghl_refusing_a_second_opportunity_is_explained(fake, on_sale, client, q, app):
    email = unique_email("twice")
    fake.ghl_refuse_second_opportunity = True
    first = book(client, on_sale, seats=1, buyer_email=email).headers["Location"].rsplit("/", 1)[1]
    stripe_post(client, completed_event(first, 10800))
    with app.app_context():
        outbox.run_due(targets=["ghl"])
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("""UPDATE registrations SET status = 'cancelled', cancelled_at = now(),
                                    cancel_reason_code = 'customer_request'
                              WHERE order_id = (SELECT order_id FROM checkout_sessions
                                                 WHERE stripe_checkout_session_id = :c)"""), {"c": first})
    second = book(client, on_sale, seats=1, buyer_email=email).headers["Location"].rsplit("/", 1)[1]
    stripe_post(client, completed_event(second, 10800))
    with app.app_context():
        outbox.run_due(targets=["ghl"])
    job = q("""SELECT j.* FROM outbox_jobs j JOIN registrations r ON r.id = j.entity_id
                JOIN checkout_sessions c ON c.order_id = r.order_id WHERE c.stripe_checkout_session_id = :c""",
            c=second)[0]
    assert job["status"] == "dead" and "Allow Multiple Opportunities per Contact" in job["last_error"]


def test_without_stripe_the_test_app_books_without_payment(fake, on_sale, client, q, app, monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    page = text_of(client.get(f"/book/{on_sale['public_id']}"))
    assert "Test booking." in page and "Book (test, no payment)" in page
    res = book(client, on_sale, seats=2)
    assert res.status_code == 303 and "/book/done?order=" in res.headers["Location"]
    order = q("SELECT * FROM orders WHERE public_id = CAST(:p AS uuid)",
              p=res.headers["Location"].split("order=")[1])[0]
    assert order["status"] == "paid" and not order["livemode"]
    assert not q("SELECT 1 FROM payments WHERE order_id = :o", o=order["id"])
    regs = q("SELECT * FROM registrations WHERE order_id = :o", o=order["id"])
    assert [r["status"] for r in regs] == ["confirmed", "confirmed"]
    assert not fake.stripe_sessions                                    # Stripe never called
    done = text_of(client.get(res.headers["Location"]))
    assert "You're Booked" in done and "no payment taken" in done
    with app.app_context():
        outbox.run_due(targets=["ghl"])
    regs = q("SELECT * FROM registrations WHERE order_id = :o", o=order["id"])
    assert all(r["ghl_opportunity_id"] for r in regs) and len(fake.ghl_opps) == 2


def test_production_never_books_without_payment(fake, on_sale, client, app, monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.setitem(app.config, "APP_ENV", "production")
    page = text_of(client.get(f"/book/{on_sale['public_id']}"))
    assert "Online booking opens soon" in page and "Book (test" not in page
