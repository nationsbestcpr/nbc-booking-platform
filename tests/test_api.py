"""The booking engine's API (/api/v1) for the NBC Ops Hub, its keys, and the trimmed booking app.

A Superadmin makes a key (shown once). Every call names the staff member it's for (X-Acting-User) and
runs with that person's roles, limited to the key's permissions: customer service asks for a refund,
only a manager approves it, and the action log names the person and the key.
"""

from __future__ import annotations

import html
import re
import uuid

import pytest

from app import create_app
from tests.conftest import csrf_from
from tests.test_desk import leave_the_demo_as_found, new_class, paid_seat, product, q, run  # noqa: F401  (fixtures)

pytestmark = pytest.mark.usefixtures("leave_the_demo_as_found")


def text_of(res) -> str:
    return html.unescape(res.get_data(as_text=True))


def make_key(client, sign_in, name: str = "NBC Ops Hub", permissions: list[str] | None = None) -> str:
    """A Superadmin makes a key on Superadmin → API keys and copies it from the page."""
    sign_in("admin@example.test")
    page = client.get("/admin/api-keys")
    ticked = re.findall(r'name="permissions" value="([^"]+)" checked', page.get_data(as_text=True))
    res = client.post("/admin/api-keys", data={"csrf_token": csrf_from(client, "/admin/api-keys"),
                                               "name": f"{name} {uuid.uuid4().hex[:4]}",
                                               "permissions": permissions if permissions is not None else ticked})
    assert res.status_code == 200, text_of(res)
    key = re.search(r"(nbc_[a-z0-9]{8}_[A-Za-z0-9_-]{32,})", res.get_data(as_text=True)).group(1)
    with client.session_transaction() as sess:
        sess.clear()                      # API calls never use a browser session
    return key


@pytest.fixture()
def api(client):
    def _call(method: str, path: str, key: str | None, who: str | None = "cs@example.test", json=None):
        headers = {}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if who:
            headers["X-Acting-User"] = who
        return client.open(f"/api/v1{path}", method=method, headers=headers, json=json)
    return _call


# --- Keys ----------------------------------------------------------------------------------

def test_calls_need_a_live_key_and_a_staff_member(client, sign_in, api):
    assert api("GET", "", None).json["data"]["name"] == "NBC booking engine API"      # no key needed to see what it is
    assert api("GET", "/me", None).status_code == 401
    assert api("GET", "/me", "nbc_abcdefgh_" + "x" * 43).status_code == 401
    key = make_key(client, sign_in)
    me = api("GET", "/me", key).json["data"]
    assert me["acting_user"]["email"] == "cs@example.test" and "refund.issue" in me["key"]["permissions"]
    assert "refund.approve_large" not in me["acting_user"]["permissions"]             # customer service can't approve
    assert api("GET", "/me", key, who=None).status_code == 403
    assert api("GET", "/me", key, who="alder1@example.test").status_code == 403        # a customer, not staff
    assert api("GET", "/me", key, who="nobody@example.test").status_code == 403


def test_the_key_is_shown_once_and_can_be_switched_off(client, sign_in, api, q):
    key = make_key(client, sign_in)
    prefix = key.split("_")[1]
    row = q("SELECT secret_hash, revoked_at FROM api_keys WHERE prefix = :p", p=prefix)[0]
    assert key.split("_", 2)[2].encode() not in bytes(row["secret_hash"])               # only a hash is kept
    sign_in("admin@example.test")
    page = text_of(client.get("/admin/api-keys"))
    assert f"nbc_{prefix}_…" in page and key not in page and "API access list" in page
    assert "/api/v1/bookings/<public_id>/refunds" in page
    kid = q("SELECT id FROM api_keys WHERE prefix = :p", p=prefix)[0]["id"]
    client.post(f"/admin/api-keys/{kid}/revoke", data={"csrf_token": csrf_from(client, "/admin/api-keys")})
    assert api("GET", "/me", key).status_code == 401
    log = q("SELECT action FROM audit_log WHERE entity_type = 'api_key' AND entity_id = :k", k=kid)
    assert {r["action"] for r in log} == {"api_key.create", "api_key.revoke"}


def test_only_the_superadmin_manages_keys(client, sign_in):
    sign_in("manager@example.test")
    assert client.get("/admin/api-keys").status_code == 403


def test_a_key_never_does_more_than_it_was_given(client, sign_in, api, run, q):
    s = new_class(run)
    seat = paid_seat(run, q, s["id"])
    key = make_key(client, sign_in, "Read only", ["student.view", "class.view"])
    assert api("GET", f"/bookings/{seat['public_id']}", key, who="manager@example.test").status_code == 200
    res = api("POST", f"/bookings/{seat['public_id']}/refunds", key, who="manager@example.test",
              json={"amount_cents": 1000, "reason": "customer_cancel"})
    assert res.status_code == 403 and "refund.issue" in res.json["error"]["message"]


# --- The desk through the API --------------------------------------------------------------

def test_refund_asked_by_customer_service_waits_for_a_manager(client, sign_in, api, run, q):
    s = new_class(run)
    seat = paid_seat(run, q, s["id"])
    key = make_key(client, sign_in)
    preview = api("GET", f"/bookings/{seat['public_id']}/refund-preview", key).json
    assert "customer_cancel" in preview["reasons"]
    res = api("POST", f"/bookings/{seat['public_id']}/refunds", key, json={"amount_cents": 1500,
                                                                          "reason": "customer_cancel"})
    assert res.status_code == 201
    refund = res.json["data"]
    assert refund["status"] == "requested" and refund["requires_manager_approval"]
    waiting = api("GET", "/refunds?status=waiting", key, who="manager@example.test").json["data"]
    assert refund["id"] in {r["id"] for r in waiting}
    assert api("POST", f"/refunds/{refund['id']}/approve", key).status_code == 403          # not a manager
    done = api("POST", f"/refunds/{refund['id']}/approve", key, who="manager@example.test").json["data"]
    assert done["status"] == "succeeded"
    assert q("SELECT amount_refunded_cents FROM registrations WHERE id = :r", r=seat["id"])[0][
        "amount_refunded_cents"] == 1500
    log = q("""SELECT l.action, l.after, a.email FROM audit_log l JOIN user_accounts a ON a.id = l.user_account_id
                WHERE l.entity_type = 'registration' AND l.entity_id = :r AND l.action LIKE 'refund.%'
                ORDER BY l.id""", r=seat["id"])
    assert [(r["action"], r["email"]) for r in log][-1] == ("refund.approve", "manager@example.test")
    assert all(r["after"]["via_api"].startswith("NBC Ops Hub") for r in log)


def test_find_a_student_and_see_the_whole_booking(client, sign_in, api, run, q):
    s = new_class(run)
    seat = paid_seat(run, q, s["id"])
    key = make_key(client, sign_in)
    found = api("GET", f"/students?q={seat['attendee_email']}", key).json
    assert found["students"] == 1 and found["data"][0]["students"][0]["public_id"] == str(seat["public_id"])
    b = api("GET", f"/bookings/{seat['public_id']}", key).json["data"]
    assert b["seat"]["attendee_email"] == seat["attendee_email"] and b["can"]["refund"] and b["can"]["transfer"]
    assert api("POST", f"/bookings/{seat['public_id']}/notes", key, json={"body": "Called back"}).status_code == 201
    assert api("GET", f"/bookings/{seat['public_id']}", key).json["data"]["notes"][0]["body"] == "Called back"
    assert api("GET", f"/bookings/{uuid.uuid4()}", key).status_code == 404


def test_move_and_cancel_a_seat(client, sign_in, api, run, q):
    a, b = new_class(run), new_class(run)
    seat = paid_seat(run, q, a["id"])
    key = make_key(client, sign_in)
    options = api("GET", f"/bookings/{seat['public_id']}/move-options", key).json["data"]
    assert b["public_id"] in {o["class"] for o in options}
    p = product(q, b["id"], "Adult only")
    res = api("POST", f"/bookings/{seat['public_id']}/move", key,
              json={"to_class": b["public_id"], "product_id": p["id"], "reason": "customer_request", "fee_cents": 0})
    assert res.status_code == 200, res.json
    assert q("SELECT session_id FROM registrations WHERE id = :r", r=seat["id"])[0]["session_id"] == b["id"]
    res = api("POST", f"/bookings/{seat['public_id']}/cancel", key,
              json={"reason": "customer_request", "refund_cents": 0})
    assert res.status_code == 200 and res.json["data"]["cancelled"]
    assert q("SELECT status FROM registrations WHERE id = :r", r=seat["id"])[0]["status"] == "cancelled"
    res = api("POST", f"/bookings/{seat['public_id']}/cancel", key, json={"reason": "customer_request"})
    assert res.status_code == 400 and res.json["error"]["message"]                      # already cancelled


def test_phone_booking_classes_and_waiting_lists(client, sign_in, api, run, q):
    s = new_class(run, seats=3)
    key = make_key(client, sign_in)
    day = q("SELECT local_date FROM sessions WHERE id = :s", s=s["id"])[0]["local_date"].isoformat()
    listed = api("GET", f"/classes?from={day}&to={day}&city=richmond-va&status=scheduled", key).json["data"]
    assert s["public_id"] in {c["public_id"] for c in listed}
    p = product(q, s["id"], "Adult only")
    res = api("POST", f"/classes/{s['public_id']}/bookings", key, json={
        "how": "phone", "product_id": p["id"],
        "buyer": {"first_name": "Pat", "last_name": "Caller", "email": "pat.api@example.test",
                  "phone": "(804) 555-0190"},
        "attendees": [{"buyer": True}]})
    assert res.status_code == 201, res.json
    assert res.json["data"]["seats"][0]["attendee_email"] == "pat.api@example.test"
    detail = api("GET", f"/classes/{s['public_id']}", key).json["data"]
    assert "pat.api@example.test" in {st.get("attendee_email") for st in detail["students"]}
    assert isinstance(api("GET", "/waitlists", key).json["data"], list)
    res = api("POST", f"/classes/{s['public_id']}/bookings", key, json={"how": "phone", "attendees": []})
    assert res.status_code == 400


# --- The trimmed booking app ---------------------------------------------------------------

@pytest.fixture()
def real_app(database_url):
    """The app as deployed: the old desk and admin pages answer only a Superadmin."""
    return create_app({"APP_ENV": "local", "DATABASE_URL": database_url, "TESTING": True})


def _sign_in(c, email):
    c.post("/sign-in", data={"csrf_token": csrf_from(c, "/sign-in"), "email": email, "password": "demo-password"})


def test_staff_are_sent_to_the_ops_hub_and_old_pages_are_superadmin_only(real_app):
    c = real_app.test_client()
    _sign_in(c, "cs@example.test")
    assert c.get("/dashboard").headers["Location"].endswith("/console")
    assert "Your Desk Moved" in text_of(c.get("/console")) and "NBC Ops Hub" in text_of(c.get("/console"))
    for path in ("/console/find", "/console/refunds", "/console/new", "/admin/classes", "/admin/waitlists"):
        assert c.get(path).status_code == 404, path
    c = real_app.test_client()
    _sign_in(c, "admin@example.test")
    page = text_of(c.get("/admin"))
    assert "User Management" in page and "Fallback pages" in page and "Add a person" in page
    for path in ("/console/find", "/console/refunds", "/admin/classes", "/admin/waitlists"):
        assert c.get(path).status_code == 200, path
    c = real_app.test_client()
    _sign_in(c, "marcus.hale@example.test")
    assert c.get("/teach").status_code == 200                                         # the instructor portal stays


def test_the_superadmin_adds_a_person_with_their_role(client, sign_in, q):
    email = f"new.staff.{uuid.uuid4().hex[:6]}@example.test"
    sign_in("cs@example.test")
    assert client.get("/admin/users/new").status_code == 403
    sign_in("admin@example.test")
    page = "/admin/users/new"
    res = client.post(page, data={"csrf_token": csrf_from(client, page), "first_name": "Nia", "last_name": "Staff",
                                  "email": email, "role_code": "customer_service", "city_id": ""})
    assert res.status_code == 302
    roles = {r["role_code"]: r["granted_by"] for r in q("""
        SELECT ur.role_code, g.email AS granted_by FROM user_roles ur JOIN user_accounts a ON a.id = ur.user_account_id
          LEFT JOIN user_accounts g ON g.id = ur.granted_by_user_id WHERE a.email = :e""", e=email)}
    assert roles == {"user": None, "customer_service": "admin@example.test"}
    res = client.post(page, data={"csrf_token": csrf_from(client, page), "first_name": "Nia", "last_name": "Staff",
                                  "email": email, "role_code": "manager"})
    assert res.status_code == 302 and "/admin/users/" in res.headers["Location"]       # already there: open them
    assert "Added the account" in text_of(client.get("/admin"))
