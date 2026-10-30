"""Dashboards: who lands where, who may open what, and every admin action.

Uses the demo accounts (password demo-password) and fresh accounts with
unique emails, so the test database can be reused between runs.
"""

from __future__ import annotations

import html
import uuid

import pytest
from sqlalchemy import text

from app import db
from tests.conftest import csrf_from


def unique_email(tag: str = "dash") -> str:
    return f"{tag}-{uuid.uuid4().hex[:10]}@example.test"


def text_of(res) -> str:
    return html.unescape(res.get_data(as_text=True))


@pytest.fixture()
def query(app):
    def _q(sql: str, **params):
        with app.app_context():
            return db.connection().execute(text(sql), params).mappings().all()
    return _q


@pytest.fixture()
def new_account(app, post_form, client):
    """Create a fresh regular account, sign out, and return (id, email)."""
    def _new(first: str = "Fresh") -> tuple[int, str]:
        email = unique_email()
        post_form("/create-account", {"first_name": first, "last_name": "Person", "email": email,
                                      "password": "long-enough-1", "intent": "student"})
        with client.session_transaction() as sess:
            sess.clear()
        with app.app_context():
            uid = db.connection().execute(text("SELECT id FROM user_accounts WHERE email = :e"), {"e": email}).scalar()
        return uid, email
    return _new


@pytest.fixture()
def post_action(client):
    """POST to an action-only address (no page of its own), with the session's form token."""
    def _post(path: str, data: dict, **kwargs):
        return client.post(path, data={**data, "csrf_token": csrf_from(client, "/account/profile")}, **kwargs)
    return _post


@pytest.fixture()
def as_admin(sign_in):
    sign_in("admin@example.test")


# --- Who lands where -------------------------------------------------------------------

@pytest.mark.parametrize("email, area", [
    ("riley.brooks@example.test", "/my"),
    ("alder1@example.test", "/my"),
    ("marcus.hale@example.test", "/teach"),
    ("cs@example.test", "/admin"),
    ("admin@example.test", "/admin"),
])
def test_dashboard_opens_the_area_for_the_account(client, sign_in, email, area):
    sign_in(email)
    res = client.get("/dashboard")
    assert res.status_code == 302 and res.headers["Location"].endswith(area)


def test_signed_out_people_are_sent_to_sign_in(client):
    for path in ("/dashboard", "/my", "/teach", "/admin", "/admin/users", "/account/profile"):
        res = client.get(path)
        assert res.status_code == 302 and "/sign-in" in res.headers["Location"]


@pytest.mark.parametrize("email, allowed, refused", [
    ("alder1@example.test", ["/my", "/account/profile", "/account/settings"],
     ["/teach", "/admin", "/admin/users", "/admin/roles", "/admin/integrations"]),
    ("marcus.hale@example.test", ["/teach", "/my"], ["/admin", "/admin/users", "/admin/integrations"]),
    ("cs@example.test", ["/admin", "/admin/integrations"], ["/admin/users", "/admin/roles", "/teach"]),
    ("admin@example.test", ["/admin", "/admin/users", "/admin/roles", "/admin/roles/viewer", "/admin/integrations"], []),
])
def test_each_page_needs_its_permission(client, sign_in, email, allowed, refused):
    sign_in(email)
    for path in allowed:
        assert client.get(path).status_code == 200, path
    for path in refused:
        assert client.get(path).status_code == 403, path


def test_tabs_follow_permissions(client, sign_in):
    sign_in("cs@example.test")
    page = client.get("/admin").get_data(as_text=True)
    assert ">Integrations</a>" in page and ">Users</a>" not in page and ">Roles &amp; permissions</a>" not in page
    sign_in("admin@example.test")
    page = client.get("/admin").get_data(as_text=True)
    assert ">Users</a>" in page and ">Roles &amp; permissions</a>" in page and ">Integrations</a>" in page


# --- Student and instructor dashboards -------------------------------------------------

def test_student_dashboard_shows_their_booking(client, sign_in):
    sign_in("alder1@example.test")
    page = text_of(client.get("/my"))
    assert "Upcoming classes" in page and "Henrico (Three Chopt)" in page and "Booked" in page


def test_new_user_dashboard_has_no_bookings_yet(client, new_account, sign_in):
    _uid, email = new_account()
    sign_in(email, "long-enough-1")
    assert "No classes booked yet" in text_of(client.get("/my"))


def test_instructor_dashboard_shows_lead_and_helper_classes(client, sign_in):
    sign_in("marcus.hale@example.test")
    page = text_of(client.get("/teach"))
    assert "Helper instructor" in page and "Lead instructor" in page
    assert "Red Cross BLS instructor" in page
    assert "co-instructor" not in page.lower()


# --- Profile and settings --------------------------------------------------------------

def test_profile_name_can_be_changed(client, new_account, sign_in, post_form, post_action):
    _uid, email = new_account("Pat")
    sign_in(email, "long-enough-1")
    res = post_form("/account/profile", {"display_name": "  Pat   Renamed "})
    assert res.status_code == 302
    assert "Your profile is saved." in text_of(client.get("/account/profile"))
    assert "Pat Renamed" in text_of(client.get("/account/profile"))
    res = post_form("/account/profile", {"display_name": "   "})
    assert res.status_code == 400 and "Enter your name." in text_of(res)


def test_password_change_checks_the_current_password(client, new_account, sign_in, post_form, post_action):
    _uid, email = new_account()
    sign_in(email, "long-enough-1")
    res = post_form("/account/settings", {"current_password": "wrong-one-123", "new_password": "brand-new-pass",
                                          "confirm_password": "brand-new-pass"})
    assert res.status_code == 400 and "isn't your current password" in text_of(res)
    res = post_form("/account/settings", {"current_password": "long-enough-1", "new_password": "brand-new-pass",
                                          "confirm_password": "different-pass"})
    assert res.status_code == 400 and "don't match" in text_of(res)
    res = post_form("/account/settings", {"current_password": "long-enough-1", "new_password": "brand-new-pass",
                                          "confirm_password": "brand-new-pass"})
    assert res.status_code == 302
    with client.session_transaction() as sess:
        sess.clear()
    assert sign_in(email, "long-enough-1").status_code == 400
    assert sign_in(email, "brand-new-pass").status_code == 302


# --- Admin: users ----------------------------------------------------------------------

def test_user_search(client, as_admin):
    page = text_of(client.get("/admin/users?q=riley"))
    assert "Riley Brooks" in page and "Marcus Hale" not in page
    page = text_of(client.get("/admin/users?role=instructor"))
    assert "Marcus Hale" in page and "Riley Brooks" not in page


def test_admin_gives_and_removes_a_role_and_it_is_logged(client, new_account, sign_in, post_form, post_action, query):
    uid, _email = new_account("Grace")
    sign_in("admin@example.test")
    res = post_action(f"/admin/users/{uid}/roles", {"role_code": "instructor"} | {"city_id": ""},
                    follow_redirects=True)
    assert "now has the Instructor role" in text_of(res)
    grant = query("SELECT id, granted_by_user_id FROM user_roles WHERE user_account_id = :u AND role_code = 'instructor' "
                  "AND revoked_at IS NULL", u=uid)
    admin_id = query("SELECT id FROM user_accounts WHERE email = 'admin@example.test'")[0]["id"]
    assert grant and grant[0]["granted_by_user_id"] == admin_id
    res = post_action(f"/admin/users/{uid}/roles/{grant[0]['id']}/revoke", {}, follow_redirects=True)
    assert "Removed the Instructor role" in text_of(res)
    log = [r["action"] for r in query("SELECT action FROM audit_log WHERE entity_type = 'user_account' "
                                      "AND entity_id = :u ORDER BY id", u=uid)]
    assert log == ["user.role_grant", "user.role_revoke"]
    page = text_of(client.get(f"/admin/users/{uid}"))
    assert "Gave the Instructor role" in page and "Removed the Instructor role" in page


def test_student_role_cannot_be_given_by_hand(new_account, sign_in, post_form, post_action):
    uid, _email = new_account()
    sign_in("admin@example.test")
    res = post_action(f"/admin/users/{uid}/roles", {"role_code": "student"}, follow_redirects=True)
    assert "given automatically" in text_of(res)


def test_a_role_without_cities_cannot_be_limited_to_one(new_account, sign_in, post_form, post_action, query):
    uid, _email = new_account()
    sign_in("admin@example.test")
    city = query("SELECT id FROM metros WHERE slug = 'richmond-va'")[0]["id"]
    res = post_action(f"/admin/users/{uid}/roles", {"role_code": "admin", "city_id": str(city)}, follow_redirects=True)
    assert "can't be limited to one city" in text_of(res)


def test_the_last_admin_cannot_remove_their_own_admin_role(client, sign_in, post_form, post_action, query):
    sign_in("admin@example.test")
    grant = query("SELECT ur.id, ur.user_account_id FROM user_roles ur JOIN user_accounts a ON a.id = ur.user_account_id "
                  "WHERE a.email = 'admin@example.test' AND ur.role_code = 'admin' AND ur.revoked_at IS NULL")[0]
    others = query("SELECT count(*) AS n FROM user_roles ur JOIN user_accounts a ON a.id = ur.user_account_id "
                   "WHERE ur.role_code = 'admin' AND ur.revoked_at IS NULL AND a.status = 'active' "
                   "AND a.email <> 'admin@example.test'")[0]["n"]
    if others:
        pytest.skip("another admin exists in this test database")
    res = post_action(f"/admin/users/{grant['user_account_id']}/roles/{grant['id']}/revoke", {}, follow_redirects=True)
    assert "At least one active admin must remain" in text_of(res)


def test_suspend_and_reactivate(client, new_account, sign_in, post_form, post_action):
    uid, email = new_account()
    sign_in("admin@example.test")
    res = post_action(f"/admin/users/{uid}/status", {"action": "suspend"}, follow_redirects=True)
    assert "is suspended" in text_of(res)
    with client.session_transaction() as sess:
        sess.clear()
    assert "switched off" in text_of(sign_in(email, "long-enough-1"))
    sign_in("admin@example.test")
    res = post_action(f"/admin/users/{uid}/status", {"action": "reactivate"}, follow_redirects=True)
    assert "active again" in text_of(res)


def test_admins_cannot_suspend_themselves(sign_in, post_form, post_action, query):
    sign_in("admin@example.test")
    uid = query("SELECT id FROM user_accounts WHERE email = 'admin@example.test'")[0]["id"]
    res = post_action(f"/admin/users/{uid}/status", {"action": "suspend"}, follow_redirects=True)
    assert "can't switch off your own account" in text_of(res)


def test_admin_actions_need_the_csrf_token(client, new_account, sign_in):
    uid, _email = new_account()
    sign_in("admin@example.test")
    assert client.post(f"/admin/users/{uid}/status", data={"action": "suspend"}).status_code == 400


# --- Admin: roles and permissions ------------------------------------------------------

def test_admin_changes_a_roles_permissions(client, sign_in, post_form, post_action, query):
    sign_in("admin@example.test")
    before = sorted(r["permission_code"] for r in query(
        "SELECT permission_code FROM role_permissions WHERE role_code = 'viewer'"))
    try:
        res = post_form("/admin/roles/viewer", {"permissions": before + ["audit.view"]}, follow_redirects=True)
        assert "1 added, 0 removed" in text_of(res)
        assert "audit.view" in {r["permission_code"] for r in query(
            "SELECT permission_code FROM role_permissions WHERE role_code = 'viewer'")}
        sign_in("viewer@example.test")
        page = text_of(client.get("/admin"))       # the new permission works at once
        assert "The action log is visible to roles" not in page and "Changed the Viewer role" in page
    finally:
        sign_in("admin@example.test")
        post_form("/admin/roles/viewer", {"permissions": before})
    assert sorted(r["permission_code"] for r in query(
        "SELECT permission_code FROM role_permissions WHERE role_code = 'viewer'")) == before
    assert query("SELECT count(*) AS n FROM audit_log WHERE action = 'role.permissions_change'")[0]["n"] >= 2


def test_the_admin_role_always_has_everything(client, sign_in, post_form, post_action):
    sign_in("admin@example.test")
    page = text_of(client.get("/admin/roles/admin"))
    assert "always has every permission" in page and 'name="permissions"' not in page
    assert post_form("/admin/roles/admin", {"permissions": ["class.view"]}).status_code == 403


def test_unknown_permissions_are_refused(sign_in, post_form, post_action):
    sign_in("admin@example.test")
    assert post_form("/admin/roles/viewer", {"permissions": ["made.up"]}).status_code == 400


# --- Admin: integrations ---------------------------------------------------------------

def test_integrations_show_status_without_secret_values(client, sign_in, monkeypatch):
    sign_in("admin@example.test")
    for name in ("AIRTABLE_TOKEN", "AIRTABLE_BASE_ID", "STRIPE_SECRET_KEY", "GHL_CLIENT_ID"):
        monkeypatch.delenv(name, raising=False)
    page = text_of(client.get("/admin/integrations"))
    for name in ("Airtable", "Stripe", "GHL", "Microsoft 365 sign-in", "Not set up", "AIRTABLE_TOKEN"):
        assert name in page
    monkeypatch.setenv("AIRTABLE_TOKEN", "patSECRET-value-123")
    monkeypatch.setenv("AIRTABLE_BASE_ID", "appSECRET456")
    page = text_of(client.get("/admin/integrations"))
    assert "patSECRET-value-123" not in page and "appSECRET456" not in page
    airtable = page.split('id="int-airtable"')[1].split("</section>")[0]
    assert "Working" in airtable                      # demo: a successful run, no errors
    assert "Moorefield has no public parking notes" in page


# --- First admin from the shell --------------------------------------------------------

def test_make_admin_command(app, new_account, query):
    uid, email = new_account()
    runner = app.test_cli_runner()
    res = runner.invoke(args=["users", "make-admin", email, "--yes"])
    assert res.exit_code == 0 and "is now an admin" in res.output
    assert query("SELECT 1 FROM user_roles WHERE user_account_id = :u AND role_code = 'admin' AND revoked_at IS NULL", u=uid)
    assert "already an admin" in runner.invoke(args=["users", "make-admin", email, "--yes"]).output
    res = runner.invoke(args=["users", "make-admin", "nobody-here@example.test", "--yes"])
    assert res.exit_code != 0 and "Create it at /create-account first" in res.output
    # Leave the database with one admin again, as the demo data has.
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("""
            UPDATE user_roles SET revoked_at = now(),
                   revoked_by_user_id = (SELECT id FROM user_accounts WHERE email = 'admin@example.test')
             WHERE user_account_id = :u AND role_code = 'admin'
        """), {"u": uid})


def test_header_links_to_the_dashboard(client, sign_in):
    sign_in("riley.brooks@example.test")
    assert 'href="/dashboard"' in client.get("/").get_data(as_text=True)


# --- Sidebar and the records sections --------------------------------------------------

def sidebar(page: str) -> str:
    return page.split('<details class="side"', 1)[1].split("</details>", 1)[0]


def test_sidebar_sections_follow_permissions(client, sign_in):
    sign_in("cs@example.test")
    nav = sidebar(client.get("/admin").get_data(as_text=True))
    for shown in (">Classes</a>", ">Bookings</a>", ">Waiting lists</a>", ">Integrations</a>", ">Password</a>"):
        assert shown in nav
    for hidden in (">Users</a>", ">Instructors</a>", ">Action log</a>", ">Business settings</a>"):
        assert hidden not in nav
    sign_in("marcus.hale@example.test")
    nav = sidebar(client.get("/teach").get_data(as_text=True))
    assert ">Certifications</a>" in nav and ">Pay</a>" in nav and ">Classes</a>" not in nav
    sign_in("alder1@example.test")
    nav = sidebar(client.get("/my").get_data(as_text=True))
    assert ">Dashboard</a>" in nav and ">Profile</a>" in nav and "Teaching" not in nav


def test_current_page_is_marked_in_the_sidebar(client, sign_in, query):
    sign_in("admin@example.test")
    sid = query("SELECT id FROM sessions WHERE slug = '2027-01-16-henrico' ORDER BY id LIMIT 1")[0]["id"]
    nav = sidebar(client.get(f"/admin/classes/{sid}").get_data(as_text=True))
    assert 'href="/admin/classes" aria-current="page"' in nav


def test_classes_list_and_detail_come_from_the_schedule(client, sign_in, query):
    sign_in("cs@example.test")
    page = text_of(client.get("/admin/classes"))
    assert "Henrico (Three Chopt)" in page and "Dana Whitfield" in page and "Full" in page
    assert "Galleria" not in text_of(client.get("/admin/classes?city=richmond-va"))
    sid = query("SELECT s.id FROM sessions s JOIN course_families cf ON cf.id = s.course_family_id "
                "WHERE s.slug = '2027-01-16-henrico' AND cf.code = 'FA_CPR_AED'")[0]["id"]
    page = text_of(client.get(f"/admin/classes/{sid}"))
    assert "Student Alder 1" in page and "alder1@example.test" in page     # class list with contact (student.view)
    assert "Jordan Waiter" in page                                         # waiting list (waitlist.view)
    assert "Private class details" not in page                             # needs class.packet_view


def test_private_class_details_need_their_permission(app, client, sign_in, query):
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("""
            INSERT INTO site_private_details (site_id, access_code) SELECT id, 'DOOR-7731' FROM sites WHERE slug = 'henrico'
            ON CONFLICT (site_id) DO UPDATE SET access_code = 'DOOR-7731'
        """))
    try:
        sid = query("SELECT id FROM sessions WHERE slug = '2027-01-16-henrico' ORDER BY id LIMIT 1")[0]["id"]
        sign_in("cs@example.test")
        assert "DOOR-7731" not in text_of(client.get(f"/admin/classes/{sid}"))
        sign_in("manager@example.test")
        assert "DOOR-7731" in text_of(client.get(f"/admin/classes/{sid}"))
    finally:
        with app.app_context(), db.transaction() as conn:
            conn.execute(text("DELETE FROM site_private_details"))


def test_a_role_limited_to_one_city_sees_only_that_city(client, new_account, sign_in, post_action, query):
    uid, email = new_account("Hou")
    houston = query("SELECT id FROM metros WHERE slug = 'houston-tx'")[0]["id"]
    sign_in("admin@example.test")
    post_action(f"/admin/users/{uid}/roles", {"role_code": "customer_service", "city_id": str(houston)})
    sign_in(email, "long-enough-1")
    page = text_of(client.get("/admin/classes?when=all"))
    assert "Henrico (Three Chopt)" not in page
    sid = query("SELECT id FROM sessions WHERE slug = '2027-01-16-henrico' ORDER BY id LIMIT 1")[0]["id"]
    assert client.get(f"/admin/classes/{sid}").status_code == 403
    assert "alder1@example.test" not in text_of(client.get("/admin/bookings"))
    assert "Jordan Waiter" not in text_of(client.get("/admin/waitlists"))


def test_bookings_waitlists_locations_instructors(client, sign_in):
    sign_in("admin@example.test")
    page = text_of(client.get("/admin/bookings?q=alder1"))
    assert "Student Alder 1" in page and "Booked" in page and "$108" in page and "Student Birch 1" not in page
    page = text_of(client.get("/admin/waitlists"))
    assert "Jordan Waiter" in page and "party of 2" in page
    page = text_of(client.get("/admin/locations"))
    assert "Henrico (Three Chopt)" in page and "Still on Enrollware" in page and "Books here since" in page
    page = text_of(client.get("/admin/instructors"))
    assert "Marcus Hale" in page and "as helper" in page


def test_instructor_certifications_and_pay(client, sign_in):
    sign_in("marcus.hale@example.test")
    page = text_of(client.get("/teach/certifications"))
    assert "Red Cross BLS instructor" in page and "Jun 30, 2027" in page
    page = text_of(client.get("/teach/pay"))
    assert "No pay records yet" in page


# --- Business settings ------------------------------------------------------------------

def setting(query, key):
    return query("SELECT value FROM app_settings WHERE key = :k", k=key)[0]["value"]


def test_business_settings_are_changed_and_logged(client, sign_in, post_form, query):
    sign_in("admin@example.test")
    before = setting(query, "checkout_hold_minutes")
    try:
        res = post_form("/admin/settings", {"key": "checkout_hold_minutes", "value": str(before + 5)}, follow_redirects=True)
        assert "Saved: Checkout hold minutes." in text_of(res)
        assert setting(query, "checkout_hold_minutes") == before + 5
        assert f"from {before} to {before + 5}" in text_of(client.get("/admin/log?area=settings"))
    finally:
        post_form("/admin/settings", {"key": "checkout_hold_minutes", "value": str(before)})
    assert setting(query, "checkout_hold_minutes") == before


def test_money_settings_are_entered_in_dollars(sign_in, post_form, query):
    sign_in("admin@example.test")
    before = setting(query, "co_instructor_pay_cents")
    try:
        post_form("/admin/settings", {"key": "co_instructor_pay_cents", "value": "$120.50"})
        assert setting(query, "co_instructor_pay_cents") == 12050
    finally:
        post_form("/admin/settings", {"key": "co_instructor_pay_cents", "value": f"{before / 100:.2f}"})
    assert setting(query, "co_instructor_pay_cents") == before


def test_an_open_setting_can_be_set_and_cleared(sign_in, post_form, query):
    sign_in("admin@example.test")
    post_form("/admin/settings", {"key": "large_refund_threshold_cents", "value": "500"})
    assert setting(query, "large_refund_threshold_cents") == 50000
    post_form("/admin/settings", {"key": "large_refund_threshold_cents", "value": ""})
    assert setting(query, "large_refund_threshold_cents") is None


def test_bad_setting_values_are_refused(sign_in, post_form, query):
    sign_in("admin@example.test")
    before = setting(query, "waitlist_offer_hours")
    for bad in ("two", "-1", ""):
        res = post_form("/admin/settings", {"key": "waitlist_offer_hours", "value": bad}, follow_redirects=True)
        assert "Nothing was changed" in text_of(res)
    assert setting(query, "waitlist_offer_hours") == before
    assert post_form("/admin/settings", {"key": "environment", "value": "1"}).status_code == 400
    assert post_form("/admin/settings", {"key": "airtable_writeback_enabled", "value": "yes"}).status_code == 400


def test_business_settings_need_their_permission(client, sign_in):
    sign_in("manager@example.test")
    assert client.get("/admin/settings").status_code == 403


def test_staff_pages_use_plain_words(client, sign_in):
    sign_in("admin@example.test")
    page = text_of(client.get("/admin/settings")).lower()
    assert "co-instructor" not in page and "co instructor" not in page and "metro" not in page
    assert "helper instructor pay" in page


# --- Preset superadmin from the secrets ----------------------------------------------------

def test_preset_superadmin_is_created_and_can_sign_in(app, client, sign_in, post_form, query, monkeypatch):
    email = unique_email("super")
    monkeypatch.setenv("SUPERADMIN_EMAIL", email.upper())
    monkeypatch.setenv("SUPERADMIN_PASSWORD", "preset-admin-pass-1")
    runner = app.test_cli_runner()
    res = runner.invoke(args=["users", "ensure-superadmin"])
    assert res.exit_code == 0 and "created" in res.output and "preset-admin-pass-1" not in res.output
    assert sign_in(email, "preset-admin-pass-1").status_code == 302
    assert client.get("/dashboard").headers["Location"].endswith("/admin")
    assert client.get("/admin/settings").status_code == 200             # every permission
    # Running again (every start) keeps a password changed in the app.
    post_form("/account/settings", {"current_password": "preset-admin-pass-1", "new_password": "changed-in-app-22",
                                     "confirm_password": "changed-in-app-22"})
    res = runner.invoke(args=["users", "ensure-superadmin"])
    assert res.exit_code == 0 and "its own password was kept" in res.output
    assert sign_in(email, "preset-admin-pass-1").status_code == 400
    assert sign_in(email, "changed-in-app-22").status_code == 302
    # If someone removed its Admin role, the next start gives it back.
    uid = query("SELECT id FROM user_accounts WHERE email = :e", e=email)[0]["id"]
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("""UPDATE user_roles SET revoked_at = now(),
                                    revoked_by_user_id = (SELECT id FROM user_accounts WHERE email = 'admin@example.test')
                              WHERE user_account_id = :u AND role_code = 'admin'"""), {"u": uid})
    assert "given the Admin role" in runner.invoke(args=["users", "ensure-superadmin"]).output
    # Leave one admin, as in the demo data.
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("""UPDATE user_roles SET revoked_at = now(),
                                    revoked_by_user_id = (SELECT id FROM user_accounts WHERE email = 'admin@example.test')
                              WHERE user_account_id = :u AND role_code = 'admin' AND revoked_at IS NULL"""), {"u": uid})


def test_preset_superadmin_reset_for_an_account_made_earlier(app, client, sign_in, post_form, monkeypatch):
    """The email signed up before the secrets were set: the start log says the
    old password was kept, and SUPERADMIN_RESET_PASSWORD=yes switches to the
    secret's password and clears a lockout."""
    email = unique_email("early")
    with client.session_transaction() as sess:
        sess.clear()
    res = post_form("/create-account", {"first_name": "Early", "last_name": "Bird", "email": email,
                                        "password": "my-own-password-1", "intent": "student"})
    assert res.status_code == 302
    # Pasted with quotes and a trailing space: both are ignored.
    monkeypatch.setenv("SUPERADMIN_EMAIL", f" {email} ")
    monkeypatch.setenv("SUPERADMIN_PASSWORD", '"preset-admin-pass-2" ')
    runner = app.test_cli_runner()
    res = runner.invoke(args=["users", "ensure-superadmin"])
    assert res.exit_code == 0 and "its own password was kept" in res.output and "SUPERADMIN_RESET_PASSWORD" in res.output
    for _ in range(app.config["SIGN_IN_MAX_FAILURES"]):
        sign_in(email, "preset-admin-pass-2")                          # wrong until reset: locks it
    assert "locked" in runner.invoke(args=["users", "ensure-superadmin"]).output
    monkeypatch.setenv("SUPERADMIN_RESET_PASSWORD", "yes")
    res = runner.invoke(args=["users", "ensure-superadmin"])
    assert res.exit_code == 0 and "password reset" in res.output and "preset-admin-pass-2" not in res.output
    assert sign_in(email, "preset-admin-pass-2").status_code == 302
    assert client.get("/dashboard").headers["Location"].endswith("/admin")


def test_preset_superadmin_needs_a_long_password(app, monkeypatch):
    monkeypatch.setenv("SUPERADMIN_EMAIL", unique_email("super"))
    monkeypatch.setenv("SUPERADMIN_PASSWORD", "short")
    res = app.test_cli_runner().invoke(args=["users", "ensure-superadmin"])
    assert res.exit_code != 0 and "12+ characters" in res.output


def test_no_preset_superadmin_without_the_secret(app, monkeypatch):
    monkeypatch.delenv("SUPERADMIN_EMAIL", raising=False)
    res = app.test_cli_runner().invoke(args=["users", "ensure-superadmin"])
    assert res.exit_code == 0 and "no preset admin account" in res.output


# --- Sign-in and create account work end to end in the running app ----------------------

def test_create_account_then_sign_out_and_back_in(client, post_form, sign_in):
    email = unique_email("e2e")
    res = post_form("/create-account", {"first_name": "Ena", "last_name": "End", "email": email,
                                        "password": "end-to-end-1", "intent": "student"})
    assert res.status_code == 302
    assert client.get("/dashboard").headers["Location"].endswith("/my")
    client.post("/sign-out", data={"csrf_token": csrf_from(client, "/")})
    assert client.get("/my").status_code == 302                       # signed out
    assert sign_in(email, "end-to-end-1").status_code == 302
    assert "Hi, Ena" in text_of(client.get("/my"))
