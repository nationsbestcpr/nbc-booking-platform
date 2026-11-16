"""Who booked whom, invites, the same-course rule, questions, add-ons, bring a friend, SEO pages.

Uses the fake Airtable / Stripe / GHL from test_integrations (nothing leaves the machine).
"""

from __future__ import annotations

import json
import re
import uuid

import pytest
from sqlalchemy import text

from app import db
from app.integrations import outbox
from tests.conftest import DEMO_PASSWORD, csrf_from
from tests.test_dashboards import text_of, unique_email
from tests.test_integrations import (  # noqa: F401  (fixtures)
    book, completed_event, fake, leave_ledgers_as_found, on_sale, on_sale_product, q, run_sync, session_for,
    signed_in_as, stripe_post)


@pytest.fixture()
def no_stripe(monkeypatch):
    """The test app without a Stripe key: bookings confirm at once (no payment)."""
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)


@pytest.fixture()
def setting(app):
    """Change a business setting for one test."""
    changed = []

    def _set(key, value):
        with app.app_context(), db.transaction() as conn:
            old = conn.execute(text("SELECT value FROM app_settings WHERE key = :k"), {"k": key}).scalar()
            changed.append((key, old))
            conn.execute(text("UPDATE app_settings SET value = CAST(:v AS jsonb) WHERE key = :k"),
                         {"k": key, "v": json.dumps(value)})
    yield _set
    with app.app_context(), db.transaction() as conn:
        for key, old in reversed(changed):
            conn.execute(text("UPDATE app_settings SET value = CAST(:v AS jsonb) WHERE key = :k"),
                         {"k": key, "v": json.dumps(old)})


def run_ghl(app):
    with app.app_context():
        return outbox.run_due(targets=["ghl"])


def order_from(q, location: str) -> dict:
    return q("SELECT * FROM orders WHERE public_id = CAST(:p AS uuid)", p=location.split("order=")[1])[0]


def booking_form(client, s, *, seats=1, attends="yes", others=(), extra=None, product=None):
    path = f"/book/{s['public_id']}?seats={seats}"
    data = {"csrf_token": csrf_from(client, path), "seats": seats,
            "product_id": product or on_sale_product(client, s), "buyer_first_name": "Dana",
            "buyer_last_name": "Buyer", "buyer_attends": attends}
    start = 1 if attends == "yes" else 0
    for i, (first, email) in enumerate(others, start=start):
        data.update({f"a{i}_first": first, f"a{i}_last": "Person", f"a{i}_email": email})
    data.update(extra or {})
    return client.post(f"/book/{s['public_id']}", data=data)


# --- Booked for someone else: GHL data, tags, invites, dashboards ------------------------------------

def test_booking_for_others_links_everyone_in_ghl_and_invites_them(fake, on_sale, client, q, app, no_stripe):
    buyer = unique_email("employer")
    staff1, staff2 = unique_email("staff"), unique_email("staff")
    signed_in_as(client, buyer)
    page = text_of(client.get(f"/book/{on_sale['public_id']}?seats=2"))
    assert "Every person needs their own email address." in page
    assert "We'll email them an invite to make their own account" in page
    res = booking_form(client, on_sale, seats=2, attends="no", others=[("Sam", staff1), ("Lee", staff2)])
    assert res.status_code == 303
    order = order_from(q, res.headers["Location"])
    done = text_of(client.get(res.headers["Location"]))
    assert "We're emailing Sam, Lee an invite" in done
    with app.app_context():
        from app.integrations import ghl
        ghl.load_custom_fields()
    run_ghl(app)

    regs = q("SELECT * FROM registrations WHERE order_id = :o ORDER BY id", o=order["id"])
    opp = fake.ghl_opps[regs[0]["ghl_opportunity_id"]]
    fields = {f["id"]: f["field_value"] for f in opp["customFields"]}
    assert fields["cf-booked_by"] == "Dana Buyer" and fields["cf-booked_by_email"] == buyer
    assert fields["cf-booking_type"] == "Booked by someone else"

    staff_contact = fake.ghl_contacts[staff1]
    tags = fake.ghl_tags[staff_contact]
    city = q("SELECT slug FROM metros WHERE id = :m", m=on_sale["metro_id"])[0]["slug"]
    assert {"nbc-student", "nbc-booked-by-someone-else", "nbc-course-first-aid-cpr-aed",
            f"nbc-city-{city}", "nbc-send-invite"} <= set(tags)
    assert "nbc-booker" in fake.ghl_tags[fake.ghl_contacts[buyer]]
    values = fake.ghl_contact_values[staff_contact]
    link = values["cf-contact-nbc_invite_link"]
    assert values["cf-contact-nbc_booked_by"] == "Dana Buyer" and "/invite/" in link
    assert q("SELECT count(*) AS n FROM notifications WHERE kind = 'attendee_invite' AND subject_id = :r",
             r=regs[0]["id"])[0]["n"] == 1

    # The employer sees everyone they booked, and that the invites went out.
    my = text_of(client.get("/my"))
    assert "People you've booked" in my and staff1 in my and staff2 in my and "Sent " in my
    assert "For Sam Person" in my

    # Sam opens the invite, makes an account, and sees the class booked for them.
    with client.session_transaction() as sess:
        sess.clear()
    path = "/invite/" + link.rsplit("/invite/", 1)[1]
    invite_page = text_of(client.get(path))
    assert "Dana Buyer Booked You a Seat" in invite_page and staff1 in invite_page
    res = client.post(path, data={"csrf_token": csrf_from(client, path), "first_name": "Sam",
                                  "last_name": "Person", "password": "long-enough-1"})
    assert res.status_code == 302 and res.headers["Location"] == "/my"
    account = q("SELECT * FROM user_accounts WHERE email = :e", e=staff1)[0]
    assert account["email_verified_at"] is not None
    my = text_of(client.get("/my"))
    assert "Booked for you by Dana Buyer" in my and "You're attending" in my

    # A resend goes out again, but not twice in a few minutes.
    signed_in_as(client, buyer)
    with app.app_context(), db.transaction() as conn:          # the first invite went out a while ago
        conn.execute(text("UPDATE notifications SET sent_at = now() - interval '1 hour' "
                          "WHERE kind = 'attendee_invite' AND subject_id = :r"), {"r": regs[1]["id"]})
    res = client.post(f"/my/invites/{regs[1]['public_id']}/resend",
                      data={"csrf_token": csrf_from(client, "/my")})
    assert res.status_code == 302 and "We're sending Lee's invite again" in text_of(client.get("/my"))
    run_ghl(app)
    assert [t for c, t in fake.ghl_tag_events if c == fake.ghl_contacts[staff2] and t == "nbc-send-invite"] == \
        ["nbc-send-invite", "nbc-send-invite"]
    client.post(f"/my/invites/{regs[1]['public_id']}/resend", data={"csrf_token": csrf_from(client, "/my")})
    assert "in the last few minutes" in text_of(client.get("/my"))
    # Someone else can't resend (or see) this booking's invites.
    signed_in_as(client, unique_email("nosy"))
    res = client.post(f"/my/invites/{regs[1]['public_id']}/resend", data={"csrf_token": csrf_from(client, "/my")})
    assert res.status_code == 404


def test_an_invite_for_an_existing_account_or_the_wrong_account(fake, on_sale, client, q, app, no_stripe):
    friend = unique_email("has-account")
    signed_in_as(client, friend)                         # they already have an account (email not confirmed)
    buyer = unique_email("buyer")
    signed_in_as(client, buyer)
    res = booking_form(client, on_sale, seats=1, attends="no", others=[("Kim", friend)])
    reg = q("SELECT r.* FROM registrations r JOIN orders o ON o.id = r.order_id "
            "WHERE o.public_id = CAST(:p AS uuid)", p=res.headers["Location"].split("order=")[1])[0]
    run_ghl(app)
    link = fake.ghl_contact_values[fake.ghl_contacts[friend]]["cf-contact-nbc_invite_link"]
    path = "/invite/" + link.rsplit("/invite/", 1)[1]
    assert "This invite is for someone else" in text_of(client.get(path))       # still signed in as the buyer
    with client.session_transaction() as sess:
        sess.clear()
    assert "Sign in to see your class" in text_of(client.get(path))
    client.post("/sign-in", data={"csrf_token": csrf_from(client, "/sign-in"), "email": friend,
                                  "password": "long-enough-1", "next": path})
    res = client.get(path)
    assert res.status_code == 302 and res.headers["Location"] == "/my"
    assert "Booked for you by Dana Buyer" in text_of(client.get("/my"))
    assert q("SELECT used_at FROM action_tokens WHERE registration_id = :r", r=reg["id"])[0]["used_at"]
    assert client.get("/invite/not-a-real-token").status_code == 404


def test_booking_yourself_is_tagged_and_sends_no_invite(fake, on_sale, client, q, app, no_stripe):
    me = unique_email("self")
    res = book(client, on_sale, seats=1, buyer_email=me)
    run_ghl(app)
    tags = fake.ghl_tags[fake.ghl_contacts[me]]
    assert "nbc-booked-self" in tags and "nbc-send-invite" not in tags and "nbc-booker" not in tags
    order = order_from(q, res.headers["Location"])
    assert not q("SELECT 1 FROM outbox_jobs WHERE operation = 'send_invite' AND entity_id IN "
                 "(SELECT id FROM registrations WHERE order_id = :o)", o=order["id"])


# --- The same course once per 18 months -----------------------------------------------------------

def test_one_email_cant_book_the_same_course_twice_within_18_months(fake, run_sync, q, client, app, sign_in, no_stripe):
    later = fake.add_class(day="2027-03-01")
    first = fake.add_class()
    bls = fake.add_class(course=fake.course_bls, start="1:00 PM", minutes=180)
    run_sync()
    city = q("SELECT id FROM metros WHERE slug = :s", s=fake.metro_slug)[0]
    sign_in("admin@example.test")
    client.post(f"/admin/locations/{city['id']}/booking",
                data={"booking_system": "engine", "csrf_token": csrf_from(client, "/admin/locations")})
    s1, s2, s3 = session_for(q, first), session_for(q, later), session_for(q, bls)
    email = unique_email("renew")
    assert book(client, s1, seats=1, buyer_email=email).status_code == 303
    res = book(client, s2, seats=1, buyer_email=email)
    page = text_of(res)
    assert res.status_code == 400 and f"{email} already has a First Aid / CPR / AED class" in page
    assert "only once every 18 months" in page
    assert book(client, s3, seats=1, buyer_email=email).status_code == 303         # a different course is fine
    # Someone else booking a seat on that email for the same course is refused the same way.
    signed_in_as(client, unique_email("other-buyer"))
    res = booking_form(client, s2, seats=1, attends="no", others=[("Pat", email)])
    assert res.status_code == 400 and "already has a First Aid / CPR / AED class" in text_of(res)


# --- Wording: nothing is held before payment ---------------------------------------------------

def test_sign_in_step_says_no_seat_is_held_yet(fake, on_sale, client):
    res = client.get(f"/book/{on_sale['public_id']}")
    page = text_of(client.get(res.headers["Location"]))
    assert "Nothing is held yet" in page and "Your seat is saved" not in page


# --- Booking questions and add-ons, set up by staff ----------------------------------------------

@pytest.fixture()
def question_and_addon(client, sign_in, q, app):
    """A required 'pick one' question per attendee and a $15 per-seat add-on, made in the admin screens."""
    sign_in("admin@example.test")
    label = f"How did you hear about us {uuid.uuid4().hex[:5]}"
    res = client.post("/admin/booking-questions/new", data={
        "csrf_token": csrf_from(client, "/admin/booking-questions/new"), "label": label, "kind": "choice",
        "choices": "Google\nA friend\nMy employer", "asked_for": "attendee", "is_required": "yes",
        "is_active": "yes", "ghl_field_key": "heard_about_us"})
    assert res.status_code == 302
    qid = q("SELECT id FROM booking_questions WHERE label = :l", l=label)[0]["id"]
    code = f"wallet-card-{uuid.uuid4().hex[:5]}"
    res = client.post("/admin/addons/new", data={
        "csrf_token": csrf_from(client, "/admin/addons/new"), "name": "Laminated wallet card", "price": "15",
        "per": "seat", "show_on_booking_page": "yes", "code": code})
    assert res.status_code == 302
    aid = q("SELECT id FROM addons WHERE code = :c", c=code)[0]["id"]
    with client.session_transaction() as sess:
        sess.clear()
    yield {"qid": qid, "aid": aid, "label": label, "code": code}
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("UPDATE booking_questions SET is_active = false WHERE id = :i"), {"i": qid})
        conn.execute(text("UPDATE addons SET show_on_booking_page = false WHERE id = :i"), {"i": aid})


def test_questions_and_add_ons_from_the_admin_screens_reach_stripe_and_ghl(fake, on_sale, client, q, app,
                                                                           question_and_addon):
    qa = question_and_addon
    fake.ghl_extra_opportunity_fields.append("heard_about_us")
    signed_in_as(client, unique_email("qa"))
    page = text_of(client.get(f"/book/{on_sale['public_id']}"))
    assert qa["label"] in page and "Laminated wallet card" in page and "+$15" in page
    res = booking_form(client, on_sale)
    assert res.status_code == 400 and f"You: answer “{qa['label']}”." in text_of(res)
    res = booking_form(client, on_sale, extra={f"q{qa['qid']}_a0": "A friend", f"addon{qa['aid']}_a0": "yes"})
    assert res.status_code == 303
    cs_id = res.headers["Location"].rsplit("/", 1)[1]
    sent = fake.stripe_sessions[cs_id]
    assert sent["line_items[1][price_data][product_data][name]"] == "Laminated wallet card"
    assert sent["line_items[1][price_data][unit_amount]"] == "1500"
    order = q("SELECT o.* FROM orders o JOIN checkout_sessions c ON c.order_id = o.id "
              "WHERE c.stripe_checkout_session_id = :c", c=cs_id)[0]
    assert order["subtotal_cents"] == 10800 + 1500
    stripe_post(client, completed_event(cs_id, 12300, discount=1000))
    addon = q("SELECT * FROM order_addons WHERE order_id = :o", o=order["id"])[0]
    reg = q("SELECT * FROM registrations WHERE order_id = :o", o=order["id"])[0]
    assert addon["status"] == "paid" and addon["registration_id"] == reg["id"]
    assert reg["amount_paid_cents"] + addon["amount_paid_cents"] == 11300          # the code's $10 comes off once
    assert q("SELECT answer FROM booking_answers WHERE registration_id = :r", r=reg["id"])[0]["answer"] == "A friend"
    with app.app_context():
        from app.integrations import ghl
        ghl.load_custom_fields()
    run_ghl(app)
    reg = q("SELECT * FROM registrations WHERE id = :r", r=reg["id"])[0]
    fields = {f["id"]: f["field_value"] for f in fake.ghl_opps[reg["ghl_opportunity_id"]]["customFields"]}
    assert fields["cf-addons"] == "Laminated wallet card" and fields["cf-heard_about_us"] == "A friend"
    assert f"nbc-addon-{qa['code']}" in fake.ghl_tags[fake.ghl_contacts[reg["attendee_email"]]]

    # Turned off / hidden: the booking page no longer asks or offers them.
    with client.session_transaction() as sess:
        sess.clear()
    client.post("/sign-in", data={"csrf_token": csrf_from(client, "/sign-in"), "email": "admin@example.test",
                                  "password": DEMO_PASSWORD})
    client.post(f"/admin/booking-questions/{qa['qid']}/switch",
                data={"csrf_token": csrf_from(client, "/admin/booking-questions")})
    client.post(f"/admin/addons/{qa['aid']}/switch", data={"csrf_token": csrf_from(client, "/admin/addons")})
    assert "Turn on" in text_of(client.get("/admin/booking-questions"))
    page = text_of(client.get(f"/book/{on_sale['public_id']}"))
    assert qa["label"] not in page and "Laminated wallet card" not in page
    log = q("SELECT action FROM audit_log WHERE entity_type IN ('booking_question', 'addon') "
            "AND entity_id IN (:a, :b) ORDER BY id", a=qa["qid"], b=qa["aid"])
    assert {r["action"] for r in log} >= {"booking_question.add", "addon.add", "booking_question.switch", "addon.switch"}


def test_only_admins_set_up_questions_and_add_ons(client, sign_in):
    sign_in("cs@example.test")
    assert client.get("/admin/booking-questions").status_code == 403
    assert client.get("/admin/addons").status_code == 403
    sign_in("admin@example.test")
    page = text_of(client.get("/admin/booking-questions"))
    assert "Booking questions" in page and "Add-ons" in page          # in the sidebar


def test_admin_form_explains_mistakes(client, sign_in):
    sign_in("admin@example.test")
    res = client.post("/admin/booking-questions/new", data={
        "csrf_token": csrf_from(client, "/admin/booking-questions/new"), "label": "Pick", "kind": "choice",
        "choices": "only one", "asked_for": "attendee"})
    assert res.status_code == 400 and "Give at least two choices" in text_of(res)
    res = client.post("/admin/addons/new", data={"csrf_token": csrf_from(client, "/admin/addons/new"),
                                                 "name": "Kit", "price": "ten", "per": "seat"})
    assert res.status_code == 400 and "Enter the price in dollars" in text_of(res)


# --- Bring a friend ------------------------------------------------------------------------------

def test_bring_a_friend_before_and_after_paying(fake, on_sale, client, q, app, setting):
    setting("bring_a_friend_discount_cents", 1000)
    me, pal = unique_email("me"), unique_email("pal")
    signed_in_as(client, me)
    page = re.sub(r"<[^>]+>", "", text_of(client.get(f"/book/{on_sale['public_id']}")))
    assert "Bring a friend" in page and "Their seat is $10 off" in page
    res = booking_form(client, on_sale, extra={"bring_friend": "yes", "a1_first": "Pal", "a1_last": "Friend",
                                               "a1_email": pal})
    assert res.status_code == 303
    cs_id = res.headers["Location"].rsplit("/", 1)[1]
    sent = fake.stripe_sessions[cs_id]
    assert sent["line_items[1][price_data][product_data][name]"].endswith("(bring a friend)")
    assert sent["line_items[1][price_data][unit_amount]"] == "9800"
    stripe_post(client, completed_event(cs_id, 10800 + 9800))
    order = q("SELECT o.* FROM orders o JOIN checkout_sessions c ON c.order_id = o.id "
              "WHERE c.stripe_checkout_session_id = :c", c=cs_id)[0]
    friend = q("SELECT * FROM registrations WHERE order_id = :o AND is_bring_a_friend", o=order["id"])[0]
    assert friend["attendee_email"] == pal and friend["amount_paid_cents"] == 9800
    run_ghl(app)
    assert "nbc-bring-a-friend" in fake.ghl_tags[fake.ghl_contacts[pal]]

    # After paying: the confirmation page offers another friend's seat, as its own booking.
    done = text_of(client.get(f"/book/done?order={order['public_id']}"))
    assert "Bring a friend?" in done and "Add a friend" in done
    path = f"/book/{on_sale['public_id']}/friend?order={order['public_id']}"
    assert "Add a seat for your friend" in text_of(client.get(path))
    pal2 = unique_email("pal2")
    res = client.post(f"/book/{on_sale['public_id']}/friend", data={
        "csrf_token": csrf_from(client, path), "order": str(order["public_id"]),
        "product_id": on_sale_product(client, on_sale), "a0_first": "Second", "a0_last": "Pal", "a0_email": pal2})
    assert res.status_code == 303
    cs2 = res.headers["Location"].rsplit("/", 1)[1]
    second = q("SELECT o.* FROM orders o JOIN checkout_sessions c ON c.order_id = o.id "
               "WHERE c.stripe_checkout_session_id = :c", c=cs2)[0]
    assert second["parent_order_id"] == order["id"] and second["subtotal_cents"] == 9800
    # Not your booking: no friend page.
    signed_in_as(client, unique_email("nosy"))
    assert client.get(path).status_code == 404


# --- Public pages for search engines -----------------------------------------------------------

def test_city_course_and_class_pages_with_event_data(fake, on_sale, client, q, app):
    s = q("""SELECT s.*, m.slug AS metro_slug, cf.slug AS course_slug, si.slug AS site_slug FROM sessions s
              JOIN metros m ON m.id = s.metro_id JOIN course_families cf ON cf.id = s.course_family_id
              JOIN sites si ON si.id = s.site_id WHERE s.id = :i""", i=on_sale["id"])[0]
    city = f"/{s['metro_slug']}/"
    course = f"{city}{s['course_slug']}/"
    page = f"{course}{s['slug']}/"
    assert fake.metro in text_of(client.get(city)) and "Training sites" in text_of(client.get(city))
    assert s["slug"] in client.get(course).get_data(as_text=True)
    assert fake.site_name in text_of(client.get(f"{city}locations/{s['site_slug']}/"))
    assert client.get("/va/").status_code == 200

    html = client.get(page).get_data(as_text=True)
    events = [json.loads(m) for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]
    event = next(e for e in events if e["@type"] == "Event")
    assert event["eventStatus"] == "https://schema.org/EventScheduled"
    assert event["location"]["address"]["postalCode"] == "23510"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T09:00-0[45]:00", event["startDate"])
    assert {o["price"] for o in event["offers"]} == {"108.00", "88.00"}
    assert all(o["availability"] == "https://schema.org/InStock" for o in event["offers"])
    assert any(e["@type"] == "BreadcrumbList" for e in events)
    assert "4321" not in html                                           # never the access code
    assert f'href="/book/{s["public_id"]}"' in html

    sitemap = client.get("/sitemap.xml").get_data(as_text=True)
    assert f"{page}</loc>" in sitemap and f"{course}</loc>" in sitemap
    assert client.get("/robots.txt").get_data(as_text=True).startswith("User-agent: *\nDisallow: /")

    # Cancelled: still up until class day, marked cancelled. Finished: forwards to the course page.
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("UPDATE sessions SET status = 'cancelled', cancelled_at = now(), cancel_reason = 'test' "
                          "WHERE id = :i"), {"i": s["id"]})
    html = client.get(page).get_data(as_text=True)
    assert "https://schema.org/EventCancelled" in html and "This class is cancelled" in html
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("UPDATE sessions SET status = 'scheduled', cancelled_at = NULL, cancel_reason = NULL, "
                          "starts_at = now() - interval '5 hours', ends_at = now() - interval '1 hour' "
                          "WHERE id = :i"), {"i": s["id"]})
    res = client.get(page)
    assert res.status_code == 301 and res.headers["Location"] == course


def test_unknown_pages_and_kept_old_addresses(client, app):
    assert client.get("/nowhere-zz/").status_code == 404
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("INSERT INTO url_redirects (from_path, to_path, reason) VALUES "
                          "('/old-city-va/', '/richmond-va/', 'manual') ON CONFLICT DO NOTHING"))
    res = client.get("/old-city-va/")
    assert res.status_code == 301 and res.headers["Location"] == "/richmond-va/"


# --- Creating the GHL fields a sub-account is missing; labelling the database -------------------

def test_missing_ghl_fields_are_created_once(fake, app, client, sign_in, q):
    fake.ghl_contact_fields = []                                 # a sub-account without the invite fields
    fake.ghl_created_fields.append({"id": "cf-x", "fieldKey": "opportunity.hours_total", "name": "Hours",
                                    "model": "opportunity"})   # a field already named "Hours", other key
    with app.app_context():
        from app.integrations import ghl
        dry = ghl.create_missing_fields(dry_run=True)
        assert "contact.nbc_invite_link (would create “NBC Invite Link”)" in dry["created"]
        assert not fake.ghl_created_fields[1:]                  # a dry run creates nothing
        out = ghl.create_missing_fields()
    made = {f["fieldKey"]: f for f in fake.ghl_created_fields[1:]}
    assert {"contact.nbc_invite_link", "contact.nbc_booked_by", "opportunity.class_price", "opportunity.delivery",
            "opportunity.reg_date", "opportunity.instructor"} <= set(made)
    assert made["contact.nbc_invite_link"]["dataType"] == "TEXT" and made["contact.nbc_invite_link"]["model"] == "contact"
    assert "opportunity.class_id" in out["existing"] and not out["failed"] and not out["renamed_by_ghl"]
    assert any(s.startswith("opportunity.hours (a field named “Hours”") for s in out["skipped"])
    ids = q("SELECT value FROM app_settings WHERE key = 'ghl_custom_field_ids'")[0]["value"]
    assert ids["contact_invite_link"] == made["contact.nbc_invite_link"]["id"]   # ids loaded right after
    with app.app_context():
        again = ghl.create_missing_fields()
    assert again["created"] == []                               # safe to run twice

    # From the Integrations page, and a token without the scope gets a clear message.
    fake.ghl_contact_fields = []
    fake.ghl_created_fields.clear()
    fake.ghl_refuse_field_create = True
    sign_in("admin@example.test")
    res = client.post("/admin/integrations/ghl/create-fields",
                      data={"csrf_token": csrf_from(client, "/admin/integrations")})
    assert res.status_code == 302
    assert "add the scope “Edit Custom Fields”" in text_of(client.get("/admin/integrations"))


def test_db_label_auto_only_fixes_an_unlabelled_test_database(app, setting, q):
    from app.cli import label
    setting("environment", "local")
    app.config["APP_ENV"] = "staging"
    try:
        out = app.test_cli_runner().invoke(label, ["--auto"])
        assert "Database labelled staging (was local)" in out.output
        assert q("SELECT value FROM app_settings WHERE key = 'environment'")[0]["value"] == "staging"
        app.config["APP_ENV"] = "production"                   # never relabels staging -> production
        out = app.test_cli_runner().invoke(label, ["--auto"])
        assert "leaving it" in out.output
        assert q("SELECT value FROM app_settings WHERE key = 'environment'")[0]["value"] == "staging"
    finally:
        app.config["APP_ENV"] = "local"
