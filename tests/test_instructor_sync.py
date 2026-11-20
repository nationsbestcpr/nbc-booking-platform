"""Instructor data from Airtable (migration 0011): the Instructors table, the instructor fields on Master Locations
and Class Schedule, and Instructor Recruiting Needs, synced read only, and served to the Ops Hub through the API."""

from __future__ import annotations

import random
import string
from datetime import date, timedelta

import pytest

from app.integrations import airtable
from tests.test_api import make_key
from tests.test_integrations import fake, leave_ledgers_as_found, q, rec_id, run_sync  # noqa: F401  (fixtures)

pytestmark = pytest.mark.usefixtures("leave_ledgers_as_found")

INSTRUCTOR_FIELDS = [
    "Name", "Hub Key", "Email", "Phone", "City", "State", "ZIP", "Active in Enrollware", "Metros", "HUB Instructor",
    "FA/CPR/AED Instructor r.25 Expiration", "BLS Instructor r.25 Expiration", "Instructor Trainer r.25 Expiration",
    "Additional Certs", "Certs Last Synced", "Available Days", "Times", "Classes per Month", "Max Travel (miles)",
    "Locations to Avoid", "Notice Needed", "Interested in Leading a HUB", "Availability Notes", "Availability Updated",
    "Availability Updated By", "Outreach Status", "Last Contact", "Next Follow-up", "Outreach Notes",
    "Removal Recommended", "Removal Reason", "Remove From", "Removal Status", "Cert Status", "Associate",
    "GHL Days Available", "GHL Contact ID", "Locations - Pre-Approval", "Locations - Taught At", "HUB Lead",
    "HUB Team Members", "Date Approval Link", "Other Enrollware Accounts", "Inactive Locations - Taught At",
    "Instructor Page Type",
]


@pytest.fixture()
def base(fake):
    """The fake base with Airtable's Instructors and Instructor Recruiting Needs tables and the instructor fields."""
    tag = "".join(random.choices(string.ascii_lowercase, k=5))
    soon = (date.today() + timedelta(days=30)).isoformat()
    later = (date.today() + timedelta(days=400)).isoformat()
    fake.lead, fake.member, fake.removed, fake.twin = rec_id(), rec_id(), rec_id(), rec_id()
    fake.lead_name = f"Lena Lead{tag}"
    fake.schema["Instructors"] = list(INSTRUCTOR_FIELDS)
    fake.schema["Master Locations"] += ["Instructor Assignment Type", "Primary Instructor", "HUB Primary Instructor",
                                        "HUB Secondary Instructor", "Instructor Training Location", "Instructor Notes",
                                        "Instructor Extra Pay per Class", "Instructor Extra Pay Reason"]
    fake.schema["Class Schedule"] += ["Instructor availability status", "Instructor performance tier"]
    fake.schema["Instructor Recruiting Needs"] = ["Need", "Location", "Need Reason", "Recruiting Status",
                                                  "Recruiting Notes", "No-Instructor Cancelled Classes",
                                                  "Enrollware No-Instructor Cancels (12 mo to Oct 2026)",
                                                  "CLS Lost to No-Instructor Cancels (12 mo)",
                                                  "Profit While Running (last 12 mo)",
                                                  "Students per Class (while running)", "Last Month Held", "Date Added"]
    fake.tables["Instructors"] = [
        {"id": fake.lead, "fields": {
            "Name": fake.lead_name, "Hub Key": f"HUB-{tag}", "Email": f"lena.{tag}@example.test", "Phone": "(757) 555-0101",
            "City": "Norfolk", "State": "VA", "ZIP": "23510", "Active in Enrollware": True,
            "Metros": [f"Virginia - {fake.metro}", "Virginia - Richmond"], "HUB Instructor": True,
            "FA/CPR/AED Instructor r.25 Expiration": soon, "BLS Instructor r.25 Expiration": later,
            "Additional Certs": "Wilderness FA", "Available Days": ["Sat", "Sun"], "Times": ["Morning"],
            "Classes per Month": 6, "Max Travel (miles)": 40, "Notice Needed": "1 week",
            "Interested in Leading a HUB": True, "Outreach Status": "Connected", "Associate": "Yes",
            "Locations - Pre-Approval": [fake.loc], "Locations - Taught At": [fake.loc],
            "HUB Team Members": [fake.member], "Instructor Page Type": "HUB Lead",
            "Other Enrollware Accounts": "lena-old", "GHL Days Available": ["Mon"], "GHL Contact ID": "ghl-1",
            "Cert Status": "Expiring within 60 days"}},
        {"id": fake.member, "fields": {"Name": f"Mo Member{tag}", "Email": f"mo.{tag}@example.test",
                                       "HUB Lead": [fake.lead], "Instructor Page Type": "HUB Team Member"}},
        {"id": fake.removed, "fields": {"Name": f"Rae Removed{tag}", "Removal Status": "Removed",
                                        "Removal Reason": "Moved away", "Remove From": ["Enrollware", "RCLC"]}},
        {"id": fake.twin, "fields": {"Name": f"Tia Twin{tag}", "Email": f"LENA.{tag}@example.test"}},
    ]
    loc = fake.record(fake.loc)["fields"]
    loc.update({"Instructor Assignment Type": ["HUB Metro", "Open Bidding"], "Primary Instructor": fake.lead_name,
                "HUB Primary Instructor": fake.lead_name, "HUB Secondary Instructor": "Somebody Else",
                "Instructor Training Location": "No", "Instructor Notes": "Key at the front desk",
                "Instructor Extra Pay per Class": 25, "Instructor Extra Pay Reason": "Travel pay"})
    fake.need = rec_id()
    fake.tables["Instructor Recruiting Needs"] = [{"id": fake.need, "fields": {
        "Need": "Norfolk weekends", "Location": [fake.loc], "Need Reason": ["No-instructor cancellations"],
        "Recruiting Status": "Recruiting", "Enrollware No-Instructor Cancels (12 mo to Oct 2026)": 4,
        "CLS Lost to No-Instructor Cancels (12 mo)": 1200, "Students per Class (while running)": 7.5,
        "Last Month Held": "Sep 2026", "No-Instructor Cancelled Classes": []}}]
    return fake


def test_every_instructor_field_reaches_the_booking_engine(base, run_sync, q):
    rid = base.add_class()
    base.record(rid)["fields"].update({"ASSIGN INSTRUCTOR (Name)": base.lead_name.upper() + " ",
                                       "Instructor availability status": "Available",
                                       "Instructor performance tier": "Tier 2"})
    base.tables["Instructor Recruiting Needs"][0]["fields"]["No-Instructor Cancelled Classes"] = [rid]
    stats = run_sync()
    assert stats["status"] in ("succeeded", "partial") and stats["instructors"] == 4
    lead = q("SELECT * FROM instructors WHERE airtable_record_id = :r", r=base.lead)[0]
    assert (lead["first_name"], lead["phone_e164"], lead["city"], lead["postal_code"]) == \
        (base.lead_name.split()[0], "+17575550101", "Norfolk", "23510")
    assert lead["available_days"] == ["Sat", "Sun"] and lead["classes_per_month"] == 6 \
        and lead["travel_radius_miles"] == 40 and lead["notice_needed"] == "1 week"
    assert lead["is_hub_instructor"] and lead["interested_in_leading_hub"] and lead["associate"] is True
    assert lead["page_type"] == "HUB Lead" and lead["other_enrollware_accounts"] == "lena-old"
    assert lead["home_metro_id"] == q("SELECT id FROM metros WHERE slug = :s", s=base.metro_slug)[0]["id"]
    certs = {r["cert_type"] for r in q("SELECT cert_type FROM instructor_certs WHERE instructor_id = :i", i=lead["id"])}
    assert certs == {"ARC_FA_CPR_AED_INSTRUCTOR_R25", "ARC_BLS_INSTRUCTOR_R25"}
    assert q("SELECT cert_status FROM instructor_cert_status WHERE instructor_id = :i", i=lead["id"])[0][
        "cert_status"] == "Expiring within 60 days"                     # Airtable's own Cert Status
    assert {r["metro_label"] for r in q("SELECT metro_label FROM instructor_metros WHERE instructor_id = :i",
                                        i=lead["id"])} == {f"Virginia - {base.metro}", "Virginia - Richmond"}
    member = q("SELECT id FROM instructors WHERE airtable_record_id = :r", r=base.member)[0]["id"]
    assert q("SELECT 1 FROM instructor_hub_members WHERE lead_instructor_id = :l AND member_instructor_id = :m",
             l=lead["id"], m=member)
    site = q("SELECT id FROM sites WHERE airtable_record_id = :r", r=base.loc)[0]["id"]
    rel = {r["relation"] for r in q("SELECT relation FROM instructor_locations WHERE instructor_id = :i AND site_id = :s",
                                    i=lead["id"], s=site)}
    assert rel == {"pre_approved", "taught"}
    # Removed in Airtable: kept, inactive. Same email as another instructor: kept without it, and listed.
    removed = q("SELECT status, remove_from, email FROM instructors WHERE airtable_record_id = :r", r=base.removed)[0]
    assert removed == {"status": "inactive", "remove_from": ["Enrollware", "RCLC"], "email": None}
    assert q("SELECT email FROM instructors WHERE airtable_record_id = :r", r=base.twin)[0]["email"] is None
    assert q("""SELECT 1 FROM integration_issues WHERE code = 'INSTRUCTOR_EMAIL_SHARED' AND external_ref = :r
                 AND resolved_at IS NULL""", r=base.twin)
    # No GHL instructor fields are read (11/20: no instructor data in GHL).
    fields_asked = [p for m, u, kw in base.calls if "Instructors" in u for k, p in (kw["params"] or []) if k == "fields[]"]
    assert fields_asked and not {"GHL Contact ID", "GHL Days Available",
                                 base.field_id("Instructors", "GHL Contact ID"),
                                 base.field_id("Instructors", "GHL Days Available")} & set(fields_asked)
    # The location's staffing, and the class's lead matched to the instructor despite case and spaces.
    s = q("SELECT * FROM location_instructor_settings WHERE location_record_id = :r", r=base.loc)[0]
    assert s["site_id"] == site and s["assignment_types"] == ["HUB Metro", "Open Bidding"]
    assert s["hub_primary_instructor_id"] == lead["id"] and s["hub_secondary_instructor_id"] is None
    assert s["extra_pay_cents"] == 2500 and s["extra_pay_reason"] == "Travel pay" and s["is_instructor_training_location"] is False
    assert q("SELECT 1 FROM location_instructor_settings WHERE location_record_id <> :r AND location_name = 'Somewhere unused'",
             r=base.loc)                                              # every location, not only those with classes
    cls = q("""SELECT d.* FROM session_instructor_details d JOIN session_airtable_records r ON r.session_id = d.session_id
                WHERE r.airtable_record_id = :r""", r=rid)[0]
    assert (cls["lead_instructor_id"], cls["instructor_assigned"], cls["availability_status"], cls["performance_tier"]) == \
        (lead["id"], True, "Available", "Tier 2")
    need = q("SELECT * FROM recruiting_needs WHERE airtable_record_id = :r", r=base.need)[0]
    assert (need["enrollware_no_instructor_cancels"], need["cls_lost_cents"], float(need["students_per_class"])) == \
        (4, 120000, 7.5)
    assert q("SELECT session_id FROM recruiting_need_classes WHERE recruiting_need_id = :n", n=need["id"])[0][
        "session_id"] == cls["session_id"]
    # Nothing was written to Airtable.
    assert all(m == "GET" for m, u, _ in base.calls if u.startswith(airtable.API))


def test_a_second_sync_changes_only_what_changed_and_follows_airtable(base, run_sync, q):
    base.add_class()
    run_sync()
    fa = base.tables["Instructors"][0]["fields"]
    del fa["FA/CPR/AED Instructor r.25 Expiration"]                  # cleared in Airtable
    fa["Classes per Month"] = 2
    base.tables["Instructors"] = [r for r in base.tables["Instructors"] if r["id"] != base.member]   # deleted
    run_sync()
    lead = q("SELECT id, classes_per_month FROM instructors WHERE airtable_record_id = :r", r=base.lead)[0]
    assert lead["classes_per_month"] == 2
    assert {r["cert_type"] for r in q("SELECT cert_type FROM instructor_certs WHERE instructor_id = :i", i=lead["id"])} == \
        {"ARC_BLS_INSTRUCTOR_R25"}
    gone = q("SELECT source_deleted_at FROM instructors WHERE airtable_record_id = :r", r=base.member)[0]
    assert gone["source_deleted_at"] is not None
    assert not q("SELECT 1 FROM instructor_hub_members WHERE lead_instructor_id = :l", l=lead["id"])


def test_two_class_schedule_fields_with_one_name_dont_stop_the_sync(base, run_sync, q):
    """The real Class Schedule has two "Instructor availability status" and two "Instructor performance tier"
    fields. Airtable refuses such a name (422), so fields are asked for by id and the one with a value is used."""
    base.schema["Class Schedule"] += ["Instructor availability status", "Instructor performance tier"]
    rid = base.add_class()
    second = base.field_id("Class Schedule", "Instructor availability status", 1)
    base.record(rid)["fields"][second] = "Needs coverage"          # only the second of the two has a value
    base.record(rid)["fields"]["Instructor performance tier"] = "Tier 1"
    stats = run_sync()
    assert stats["status"] in ("succeeded", "partial")
    cls = q("""SELECT d.availability_status, d.performance_tier FROM session_instructor_details d
                 JOIN session_airtable_records r ON r.session_id = d.session_id WHERE r.airtable_record_id = :r""", r=rid)[0]
    assert (cls["availability_status"], cls["performance_tier"]) == ("Needs coverage", "Tier 1")
    asked = [k for m, u, kw in base.calls if "Class%20Schedule" in u for k, _ in (kw["params"] or [])]
    assert "returnFieldsByFieldId" in asked


def test_typed_names_and_new_city_areas_are_matched(base, run_sync, q):
    """Names typed in Airtable often leave out a middle name or hold two people; a city area can reach the
    engine after the instructor was saved. Ambiguous names stay unmatched (the typed name is kept)."""
    tag = base.lead_name[-5:]
    ana, sam1, sam2, far = rec_id(), rec_id(), rec_id(), rec_id()
    new_city = f"Akron {tag.capitalize()}"
    base.tables["Instructors"] += [
        {"id": ana, "fields": {"Name": f"Ana Maria Ortiz{tag}", "Email": f"ana.{tag}@example.test"}},
        {"id": sam1, "fields": {"Name": f"Sam Q Ray{tag}", "Email": f"sam1.{tag}@example.test"}},
        {"id": sam2, "fields": {"Name": f"Sam Z Ray{tag}", "Email": f"sam2.{tag}@example.test"}},
        {"id": far, "fields": {"Name": f"Fay Far{tag}", "Email": f"fay.{tag}@example.test",
                               "Metros": [f"Ohio - {new_city}"]}}]
    loc = base.record(base.loc)["fields"]
    loc.update({"Primary Instructor": f"Ana Ortiz{tag}", "HUB Primary Instructor": f"Sam Ray{tag}",
                "HUB Secondary Instructor": f"Somebody Else, {base.lead_name}"})
    rid = base.add_class()
    base.record(rid)["fields"]["ASSIGN INSTRUCTOR (Name)"] = f"ana ortiz{tag}"
    run_sync()
    ids = {r["airtable_record_id"]: r["id"] for r in q("SELECT id, airtable_record_id FROM instructors "
                                                         "WHERE airtable_record_id = ANY(:r)", r=[ana, base.lead, far])}
    s = q("SELECT * FROM location_instructor_settings WHERE location_record_id = :r", r=base.loc)[0]
    assert s["primary_instructor_id"] == ids[ana] and s["hub_secondary_instructor_id"] == ids[base.lead]
    assert s["hub_primary_instructor_id"] is None and s["hub_primary_instructor_name"] == f"Sam Ray{tag}"
    assert q("""SELECT d.lead_instructor_id FROM session_instructor_details d JOIN session_airtable_records r
                  ON r.session_id = d.session_id WHERE r.airtable_record_id = :r""", r=rid)[0]["lead_instructor_id"] == ids[ana]
    assert q("SELECT metro_id FROM instructor_metros WHERE instructor_id = :i", i=ids[far])[0]["metro_id"] is None
    # A class goes on sale in that city area: on the next sync the unchanged instructor is linked to it.
    akron = rec_id()
    base.tables["Master Locations"].append({"id": akron, "fields": {
        "Abbreviation": f"AK{tag}", "Name": f"Ohio - {new_city} - Library", "Address": "1 Main St, Akron, Ohio, 44308",
        "Metro Area": f"Ohio - {new_city}", "Time Zone": "Eastern", "Max Student Count": "10", "Parking": "Street",
        "Status": ["Currently Scheduling"]}})
    base.add_class(location=akron)
    run_sync()
    metro = q("SELECT metro_id FROM instructor_metros WHERE instructor_id = :i", i=ids[far])[0]["metro_id"]
    assert metro and q("SELECT home_metro_id FROM instructors WHERE id = :i", i=ids[far])[0]["home_metro_id"] == metro


def test_classes_still_sync_without_an_instructors_table(fake, run_sync, q):
    rid = fake.add_class()
    stats = run_sync()
    assert stats["status"] == "succeeded" and q("""SELECT 1 FROM session_airtable_records WHERE airtable_record_id = :r""", r=rid)
    assert q("""SELECT severity FROM integration_issues WHERE code = 'MISSING_TABLE' AND external_ref = 'table:Instructors'
                 AND resolved_at IS NULL""")[0]["severity"] == "warning"


def test_the_ops_hub_reads_instructors_their_locations_and_recruiting(base, run_sync, client, sign_in, q, app):
    rid = base.add_class()
    base.record(rid)["fields"]["ASSIGN INSTRUCTOR (Name)"] = base.lead_name
    run_sync()
    key = make_key(client, sign_in)
    h = {"Authorization": f"Bearer {key}", "X-Acting-User": "ops@example.test"}   # operations: instructors and pay
    lead = q("SELECT public_id FROM instructors WHERE airtable_record_id = :r", r=base.lead)[0]["public_id"]
    rows = client.get(f"/api/v1/instructors?q={base.lead_name.split()[1]}", headers=h).json["data"]
    assert rows[0]["id"] == str(lead) and rows[0]["cert_status"] == "Expiring within 60 days"
    one = client.get(f"/api/v1/instructors/{lead}", headers=h).json["data"]
    assert one["available_days"] == ["Sat", "Sun"] and {c["cert_type"] for c in one["certifications"]} >= \
        {"ARC_BLS_INSTRUCTOR_R25"} and one["hub_team"][0]["name"].startswith("Mo Member")
    assert {loc["relation"] for loc in one["locations"]} == {"pre_approved", "taught"}
    assert one["metros"][0]["label"].startswith("Virginia")
    locs = client.get("/api/v1/locations", headers=h).json["data"]
    site = next(s for c in locs for s in c["sites"] if s["name"] == base.site_name)
    assert site["instructors"]["assignment_types"] == ["HUB Metro", "Open Bidding"] and \
        site["instructors"]["extra_pay_cents"] == 2500
    cls = client.get(f"/api/v1/classes?city={base.metro_slug}&from={base.day}&to={base.day}", headers=h).json["data"]
    assert cls[0]["lead_instructor"]["name"] == base.lead_name
    needs = client.get("/api/v1/recruiting-needs", headers=h).json["data"]
    assert any(n["need"] == "Norfolk weekends" and n["enrollware_no_instructor_cancels"] == 4 for n in needs)
    # Customer service can't see instructors' extra pay (pay.view_all).
    h["X-Acting-User"] = "cs@example.test"
    locs = client.get("/api/v1/locations", headers=h).json["data"]
    site = next(s for c in locs for s in c["sites"] if s["name"] == base.site_name)
    assert "extra_pay_cents" not in (site["instructors"] or {})


def test_airtable_assigns_the_lead_and_booking_app_puts_a_class_on_sale(base, run_sync, q, app):
    """With Enrollware gone, "Booking App" puts a class on sale like "Enrollware" did, and ASSIGN INSTRUCTOR gives
    the matched instructor the class in their portal; naming someone else moves it."""
    rid = base.add_class(status=("Booking App",))
    base.record(rid)["fields"]["ASSIGN INSTRUCTOR (Name)"] = base.lead_name
    stats = run_sync()
    assert stats["status"] in ("succeeded", "partial") and stats["assignments_made"] >= 1
    sid = q("SELECT session_id FROM session_airtable_records WHERE airtable_record_id = :r", r=rid)[0]["session_id"]
    assert q("SELECT is_published FROM sessions WHERE id = :s", s=sid)[0]["is_published"]
    lead = q("SELECT id FROM instructors WHERE airtable_record_id = :r", r=base.lead)[0]["id"]
    member = q("SELECT id FROM instructors WHERE airtable_record_id = :r", r=base.member)[0]["id"]
    a = q("SELECT instructor_id, role, source, status FROM assignments WHERE session_id = :s", s=sid)
    assert a == [{"instructor_id": lead, "role": "lead", "source": "airtable", "status": "assigned"}]
    base.record(rid)["fields"]["ASSIGN INSTRUCTOR (Name)"] = base.tables["Instructors"][1]["fields"]["Name"]
    run_sync()
    rows = {r["instructor_id"]: r["status"] for r in q("SELECT instructor_id, status FROM assignments WHERE session_id = :s",
                                                       s=sid)}
    assert rows == {lead: "removed", member: "assigned"}
    formula = next(dict(c[2]["params"])["filterByFormula"] for c in base.calls if "Class%20Schedule" in c[1]
                   and "filterByFormula" in dict(c[2]["params"] or []))
    assert "FIND('Booking App'" in formula


def test_an_instructor_account_links_when_airtable_has_its_email(base, run_sync, q, app):
    """An account given the instructor role before Airtable had the instructor links on the next sync."""
    from sqlalchemy import text as sql
    from app import db
    email = base.tables["Instructors"][1]["fields"]["Email"]
    with app.app_context(), db.transaction() as conn:
        uid = conn.execute(sql("INSERT INTO user_accounts (email, display_name, status) VALUES (:e, 'Mo', 'active') "
                               "RETURNING id"), {"e": email}).scalar_one()
        conn.execute(sql("INSERT INTO user_roles (user_account_id, role_code) VALUES (:u, 'user'), (:u, 'instructor')"),
                     {"u": uid})
    stats = run_sync()
    assert stats["instructor_accounts_linked"] >= 1
    assert q("SELECT user_account_id FROM instructors WHERE airtable_record_id = :r", r=base.member)[0]["user_account_id"] == uid
