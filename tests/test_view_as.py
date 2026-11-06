"""'View as' (impersonation) and removing roles, from an account's admin page."""

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
def uid(app):
    def _uid(email: str) -> int:
        with app.app_context():
            return db.connection().execute(text("SELECT id FROM user_accounts WHERE email = :e"),
                                           {"e": email}).scalar_one()
    return _uid


@pytest.fixture()
def q(app):
    def _q(sql: str, **params):
        with app.app_context():
            return db.connection().execute(text(sql), params).mappings().all()
    return _q


def view_as(client, target: int):
    page = f"/admin/users/{target}"
    return client.post(f"{page}/view-as", data={"csrf_token": csrf_from(client, page)})


def stop(client):
    return client.post("/admin/view-as/stop", data={"csrf_token": csrf_from(client, "/")})


def test_admin_sees_the_app_as_a_student_and_comes_back(client, sign_in, uid, q):
    sign_in("admin@example.test")
    student = uid("alder1@example.test")
    assert "View as Alex" in text_of(client.get(f"/admin/users/{student}"))
    res = view_as(client, student)
    assert res.status_code == 302 and res.headers["Location"] == "/dashboard"
    page = text_of(client.get("/my"))
    assert "Viewing as Alex Alder" in page and "Return to my account" in page and "Your Classes" in page
    assert client.get("/admin").status_code == 403                    # their access, not the admin's
    res = stop(client)
    assert res.headers["Location"] == f"/admin/users/{student}"
    assert "Viewing as" not in text_of(client.get("/admin"))
    actions = [r["action"] for r in q("SELECT action FROM audit_log WHERE entity_id = :i AND action LIKE 'user.view_as%' "
                                      "ORDER BY id DESC LIMIT 2", i=student)]
    assert actions == ["user.view_as_stop", "user.view_as_start"]
    assert "Started viewing the app as this account" in text_of(client.get(f"/admin/users/{student}"))


def test_nothing_can_be_changed_while_viewing_as(client, sign_in, uid, q):
    sign_in("admin@example.test")
    student = uid("alder1@example.test")
    view_as(client, student)
    res = client.post("/account/profile", data={"csrf_token": csrf_from(client, "/account/profile"),
                                                "display_name": "Changed By Admin"})
    assert res.status_code == 403 and "Changes are switched off" in text_of(res)
    assert q("SELECT display_name FROM user_accounts WHERE id = :i", i=student)[0]["display_name"] == "Alex Alder"
    stop(client)


def test_staff_view_as_shows_their_dashboard(client, sign_in, uid):
    sign_in("admin@example.test")
    view_as(client, uid("cs@example.test"))
    assert client.get("/dashboard").headers["Location"].endswith("/console")
    assert "Viewing as Demo Customer Service" in text_of(client.get("/console"))
    stop(client)


def test_who_can_be_viewed_as(client, sign_in, uid):
    sign_in("admin@example.test")
    me = uid("admin@example.test")
    assert "View as" not in text_of(client.get(f"/admin/users/{me}"))
    res = view_as(client, me)
    assert res.headers["Location"].endswith(f"/admin/users/{me}")
    assert "your own account" in text_of(client.get(f"/admin/users/{me}"))
    assert "Viewing as" not in text_of(client.get("/admin"))


@pytest.mark.parametrize("email", ["cs@example.test", "manager@example.test", "alder1@example.test"])
def test_only_admins_can_view_as(client, sign_in, uid, email):
    sign_in(email)
    target = uid("riley.brooks@example.test")
    res = client.post(f"/admin/users/{target}/view-as", data={"csrf_token": csrf_from(client, "/account/profile")})
    assert res.status_code == 403


def test_view_as_ends_by_itself_after_an_hour(client, sign_in, uid):
    sign_in("admin@example.test")
    view_as(client, uid("alder1@example.test"))
    with client.session_transaction() as sess:
        sess["view_as_since"] -= 61 * 60
    res = client.get("/my")
    assert res.status_code == 302 and res.headers["Location"] == "/dashboard"
    page = text_of(client.get("/admin"))
    assert "ended after 60 minutes" in page and "Viewing as" not in page


def test_admin_removes_a_role_and_sees_it_in_removed_roles(client, sign_in, post_form, uid, q):
    email = f"role-{uuid.uuid4().hex[:8]}@example.test"
    post_form("/create-account", {"first_name": "Rho", "last_name": "Le", "email": email,
                                  "password": "long-enough-1", "intent": "student"})
    sign_in("admin@example.test")
    target = uid(email)
    page = f"/admin/users/{target}"
    client.post(f"{page}/roles", data={"csrf_token": csrf_from(client, page), "role_code": "customer_service"})
    grant = q("SELECT id FROM user_roles WHERE user_account_id = :u AND role_code = 'customer_service' "
              "AND revoked_at IS NULL", u=target)[0]["id"]
    assert "Remove role" in text_of(client.get(page))
    client.post(f"{page}/roles/{grant}/revoke", data={"csrf_token": csrf_from(client, page)})
    after = text_of(client.get(page))
    assert "Removed roles" in after and "Customer service" in after.split("Removed roles")[1]
    assert not q("SELECT 1 FROM user_roles WHERE id = :g AND revoked_at IS NULL", g=grant)


def test_view_as_straight_from_the_users_list_and_back_to_it(client, sign_in, uid):
    sign_in("admin@example.test")
    listing = "/admin/users?q=example.test"
    page = client.get(listing).get_data(as_text=True)
    student, admin = uid("alder1@example.test"), uid("admin@example.test")
    assert f'action="/admin/users/{student}/view-as"' in page
    assert f'action="/admin/users/{admin}/view-as"' not in page           # not yourself, not another admin
    res = client.post(f"/admin/users/{student}/view-as",
                      data={"csrf_token": csrf_from(client, listing), "back": listing})
    assert res.headers["Location"] == "/dashboard"
    assert "Viewing as Alex Alder" in text_of(client.get("/my"))
    assert stop(client).headers["Location"] == listing                     # back where the admin was
    res = client.post(f"/admin/users/{student}/view-as",
                      data={"csrf_token": csrf_from(client, listing), "back": "https://evil.example/"})
    assert stop(client).headers["Location"] == f"/admin/users/{student}"   # an off-site address is ignored
