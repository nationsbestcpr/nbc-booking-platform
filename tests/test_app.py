from __future__ import annotations

import uuid

import pytest

from app import create_app
from app.config import ConfigError
from tests.conftest import csrf_from


def unique_email(tag: str = "new") -> str:
    return f"{tag}-{uuid.uuid4().hex[:10]}@example.test"


# --- Settings (no database needed) ---------------------------------------------

def test_production_requires_a_secret_key():
    with pytest.raises(ConfigError, match="SECRET_KEY"):
        create_app({"APP_ENV": "production", "DATABASE_URL": "postgresql:///x", "SECRET_KEY": ""})


def test_unknown_environment_is_refused():
    with pytest.raises(ConfigError, match="APP_ENV"):
        create_app({"APP_ENV": "prod", "DATABASE_URL": "postgresql:///x"})


# --- Landing page ------------------------------------------------------------------

def test_health_check(client):
    res = client.get("/healthz")
    assert res.status_code == 200 and res.get_json()["status"] == "ok"


def test_landing_page_lists_classes_on_sale(client):
    html = client.get("/").get_data(as_text=True)
    assert "Find the CPR Class" in html
    assert "Henrico (Three Chopt)" in html
    assert "$108" in html and "$88" in html
    assert "3 of 12 seats left" in html            # Moorefield, nearly full
    assert "Full · join the waitlist" in html        # Henrico Jan 16
    assert "Galleria" not in html                    # Houston is still on Enrollware


def test_public_page_never_shows_private_class_details(app, client):
    from sqlalchemy import text
    from app import db
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("""
            INSERT INTO site_private_details (site_id, access_code, poc_name)
            SELECT id, 'DOOR-4821', 'Pat Contact' FROM sites WHERE slug = 'henrico'
            ON CONFLICT (site_id) DO UPDATE SET access_code = 'DOOR-4821', poc_name = 'Pat Contact'
        """))
    try:
        html = client.get("/").get_data(as_text=True)
        assert "DOOR-4821" not in html and "Pat Contact" not in html
    finally:
        with app.app_context(), db.transaction() as conn:
            conn.execute(text("DELETE FROM site_private_details"))


# --- Creating an account -------------------------------------------------------------

def test_create_account_page_asks_student_or_instructor(client):
    html = client.get("/create-account").get_data(as_text=True)
    assert 'value="student"' in html and 'value="instructor"' in html


def test_forms_without_the_csrf_token_are_refused(client):
    res = client.post("/create-account", data={"first_name": "A", "last_name": "B",
                                               "email": unique_email(), "password": "x" * 12, "intent": "student"})
    assert res.status_code == 400


def test_new_account_starts_as_a_regular_user_and_is_signed_in(client, post_form):
    res = post_form("/create-account", {"first_name": "Casey", "last_name": "New", "email": unique_email(),
                                        "password": "long-enough-1", "intent": "student"})
    assert res.status_code == 302 and res.headers["Location"].endswith("/?welcome=1")
    html = client.get("/?welcome=1").get_data(as_text=True)
    assert "Welcome, Casey!" in html
    assert 'class="role-badge ">User</span>' in html
    assert "Pick a class below to book your first seat." in html


def test_signing_up_as_instructor_grants_nothing_yet(client, post_form):
    post_form("/create-account", {"first_name": "Terry", "last_name": "Teach", "email": unique_email(),
                                  "password": "long-enough-1", "intent": "instructor"})
    html = client.get("/").get_data(as_text=True)
    assert ">User</span>" in html
    assert "confirm your certification" in html


def test_guest_who_booked_is_a_student_on_sign_up(client, post_form):
    # birch1@example.test booked the Jan 16 Henrico class as a guest in the demo data.
    from sqlalchemy import text
    from app import db
    with client.application.app_context(), db.transaction() as conn:
        conn.execute(text("DELETE FROM user_accounts WHERE email = 'birch1@example.test'"))
    post_form("/create-account", {"first_name": "Bo", "last_name": "Birch", "email": "Birch1@Example.test",
                                  "password": "long-enough-1", "intent": "student"})
    html = client.get("/").get_data(as_text=True)
    assert ">Student</span>" in html
    assert "Your next class" in html


def test_duplicate_email_is_explained(post_form):
    res = post_form("/create-account", {"first_name": "R", "last_name": "B", "email": "riley.brooks@example.test",
                                        "password": "long-enough-1", "intent": "student"})
    assert res.status_code == 400
    assert "already an account with this email" in res.get_data(as_text=True)


def test_short_password_is_explained(post_form):
    res = post_form("/create-account", {"first_name": "S", "last_name": "P", "email": unique_email(),
                                        "password": "short", "intent": "student"})
    assert res.status_code == 400 and "at least 10 characters" in res.get_data(as_text=True)


# --- Signing in: the role is worked out automatically -------------------------------

@pytest.mark.parametrize("email, role", [
    ("riley.brooks@example.test", "User"),
    ("alder1@example.test", "Student"),
    ("marcus.hale@example.test", "Instructor"),
    ("cs@example.test", "Customer service"),
    ("admin@example.test", "Admin"),
])
def test_sign_in_detects_the_role(client, sign_in, email, role):
    res = sign_in(email)
    assert res.status_code == 302
    html = client.get("/").get_data(as_text=True)
    assert f">{role}</span>" in html


def test_student_sees_their_upcoming_class(client, sign_in):
    sign_in("alder1@example.test")
    html = client.get("/").get_data(as_text=True)
    assert "Your next class" in html and "Henrico (Three Chopt)" in html


def test_wrong_password_gets_one_generic_message(sign_in):
    for email in ("riley.brooks@example.test", "nobody@example.test"):
        res = sign_in(email, "wrong-password")
        assert res.status_code == 400
        assert "That email and password don&#39;t match" in res.get_data(as_text=True)


def test_repeated_wrong_passwords_lock_the_sign_in(client, post_form, sign_in):
    email = unique_email("lock")
    post_form("/create-account", {"first_name": "L", "last_name": "K", "email": email,
                                  "password": "right-password", "intent": "student"})
    with client.session_transaction() as sess:   # sign out
        sess.clear()
    for _ in range(5):
        sign_in(email, "wrong-password")
    res = sign_in(email, "right-password")
    assert "Too many tries" in res.get_data(as_text=True)


def test_sign_in_never_redirects_off_site(post_form):
    res = post_form("/sign-in", {"email": "riley.brooks@example.test", "password": "demo-password",
                                 "next": "//evil.example/steal"})
    assert res.headers["Location"] == "/"


def test_sign_out(client, sign_in):
    sign_in("riley.brooks@example.test")
    token = csrf_from(client, "/")
    client.post("/sign-out", data={"csrf_token": token})
    assert "Riley Brooks" not in client.get("/").get_data(as_text=True)


def test_webhooks_skip_csrf_and_check_signatures(client, monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    assert client.post("/webhooks/stripe").status_code == 503          # not set up: not accepted
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_x")
    assert client.post("/webhooks/stripe", data=b"{}").status_code == 400   # unsigned
    assert client.post("/webhooks/ghl").status_code == 501
