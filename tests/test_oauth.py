"""Sign in with Google or Microsoft: the checks on the way back, and how accounts are found or made.

The providers are faked at app.integrations.http.transport, the one place
outbound calls go through, so no request leaves the machine.
"""

from __future__ import annotations

import base64
import html
import json
import time
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import text

from app import db
from app.integrations import http

from tests.conftest import csrf_from

CLIENT_ID = "test-client-id"
MS_PERSONAL = "9188040d-6c67-4c5b-b112-36a304b66dad"
OTHER_TENANT = "11111111-2222-3333-4444-555555555555"


def unique_email(tag: str = "sso") -> str:
    return f"{tag}-{uuid.uuid4().hex[:10]}@example.test"


def text_of(res) -> str:
    return html.unescape(res.get_data(as_text=True))


def _b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


class Res:
    def __init__(self, status: int, body: dict):
        self.status_code, self._body, self.headers, self.text = status, body, {}, json.dumps(body)

    def json(self):
        return self._body


@pytest.fixture()
def providers(monkeypatch):
    """Google and Microsoft set up, answering token requests with whatever claims a test sets."""
    for name in ("GOOGLE_CLIENT_ID", "ENTRA_CLIENT_ID"):
        monkeypatch.setenv(name, CLIENT_ID)
    for name in ("GOOGLE_CLIENT_SECRET", "ENTRA_CLIENT_SECRET"):
        monkeypatch.setenv(name, "test-secret")
    monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
    state = {"claims": {}, "posted": []}

    def transport(method, url, **kw):
        state["posted"].append((method, url, kw.get("data")))
        token = f"{_b64({'alg': 'RS256'})}.{_b64(state['claims'])}.sig"
        return Res(200, {"id_token": token, "access_token": "unused"})

    monkeypatch.setattr(http, "transport", transport)
    return state


def go(client, providers, provider: str, claims: dict, *, next_url: str | None = None, tamper: dict | None = None):
    """Start a sign-in, let the 'provider' answer with these claims, return the callback response."""
    start = client.get(f"/sign-in/{provider}" + (f"?next={next_url}" if next_url else ""))
    assert start.status_code == 302
    sent = parse_qs(urlparse(start.headers["Location"]).query)
    base = {"aud": CLIENT_ID, "exp": time.time() + 600, "nonce": sent["nonce"][0]}
    if provider == "google":
        base["iss"] = "https://accounts.google.com"
    providers["claims"] = {**base, **claims, **(tamper or {})}
    return client.get(f"/sign-in/{provider}/callback?state={sent['state'][0]}&code=abc")


def google(email: str, sub: str | None = None, verified: bool = True, name: str = "Gia Google") -> dict:
    return {"sub": sub or str(uuid.uuid4().int)[:21], "email": email, "email_verified": verified, "name": name}


def microsoft(email: str, tid: str = MS_PERSONAL, oid: str | None = None, **extra) -> dict:
    return {"iss": f"https://login.microsoftonline.com/{tid}/v2.0", "tid": tid, "oid": oid or str(uuid.uuid4()),
            "email": email, "name": "Mo Microsoft", **extra}


def account(app, email: str) -> dict | None:
    with app.app_context():
        return db.fetch_one("""
            SELECT a.id, a.display_name, a.email_verified_at,
                   array(SELECT provider FROM user_identities i WHERE i.user_account_id = a.id ORDER BY provider) AS ways,
                   array(SELECT role_code FROM user_roles r WHERE r.user_account_id = a.id AND r.revoked_at IS NULL) AS roles
              FROM user_accounts a WHERE a.email = :e
        """, e=email)


def test_buttons_show_only_when_set_up(client, monkeypatch):
    for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "ENTRA_CLIENT_ID", "ENTRA_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    page = text_of(client.get("/sign-in"))
    assert "Continue with Google" not in page and "Continue with Microsoft" not in page
    assert client.get("/sign-in/google").status_code == 404
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "s")
    page = text_of(client.get("/sign-in"))
    assert "Continue with Google" in page and "Continue with Microsoft" not in page
    assert "Continue with Google" in text_of(client.get("/create-account"))


def test_start_uses_state_nonce_and_pkce(client, providers):
    res = client.get("/sign-in/google?next=/my")
    url = urlparse(res.headers["Location"])
    sent = parse_qs(url.query)
    assert url.netloc == "accounts.google.com"
    assert sent["code_challenge_method"] == ["S256"] and sent["state"][0] and sent["nonce"][0]
    assert sent["redirect_uri"] == ["http://localhost/sign-in/google/callback"]
    assert sent["scope"] == ["openid email profile"]


def test_google_makes_a_confirmed_account_and_returns_to_next(client, providers, app):
    email = unique_email()
    res = go(client, providers, "google", google(email), next_url="/my")
    assert res.status_code == 302 and res.headers["Location"] == "/my"
    a = account(app, email)
    assert a["ways"] == ["google"] and a["email_verified_at"] and a["display_name"] == "Gia Google"
    assert a["roles"] == ["user"]                                  # never a role chosen at sign-in
    assert "Gia" in text_of(client.get("/my"))
    method, url, data = providers["posted"][-1]
    assert method == "POST" and url == "https://oauth2.googleapis.com/token"
    assert data["grant_type"] == "authorization_code" and data["code"] == "abc" and data["code_verifier"]
    # Next time, the same Google account signs into the same account (by Google's id, not the email).
    sub = google(email)["sub"]
    with client.session_transaction() as sess:
        sess.clear()
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("UPDATE user_identities SET subject = :s WHERE provider = 'google' "
                          "AND user_account_id = :u"), {"s": sub, "u": a["id"]})
    res = go(client, providers, "google", google("changed-" + email, sub=sub))
    assert res.status_code == 302 and account(app, "changed-" + email) is None


def test_off_site_next_is_ignored(client, providers):
    res = go(client, providers, "google", google(unique_email()), next_url="//evil.example/x")
    assert res.headers["Location"] == "/"


@pytest.mark.parametrize("tamper, why", [
    ({"aud": "someone-elses-app"}, "issued to another app"),
    ({"nonce": "replayed"}, "nonce doesn't match"),
    ({"exp": time.time() - 3600}, "expired"),
    ({"iss": "https://evil.example"}, "wrong issuer"),
])
def test_tokens_that_fail_a_check_are_refused(client, providers, app, tamper, why):
    email = unique_email()
    res = go(client, providers, "google", google(email), tamper=tamper)
    assert res.status_code == 400, why
    assert "couldn't be checked" in text_of(res) and account(app, email) is None


def test_wrong_state_is_refused(client, providers, app):
    client.get("/sign-in/google")
    res = client.get("/sign-in/google/callback?state=not-the-one&code=abc")
    assert res.status_code == 400 and "didn't start here" in text_of(res)
    assert not providers["posted"]                                  # no code was exchanged


def test_cancelling_at_google_is_explained(client, providers):
    client.get("/sign-in/google")
    res = client.get("/sign-in/google/callback?error=access_denied")
    assert res.status_code == 400 and "was cancelled" in text_of(res)


def test_unconfirmed_google_email_never_makes_or_opens_an_account(client, providers, app):
    email = unique_email()
    res = go(client, providers, "google", google(email, verified=False))
    assert res.status_code == 400 and "couldn't confirm the email" in text_of(res) and account(app, email) is None


def test_linking_to_an_unconfirmed_password_account_switches_its_password_off(client, providers, app, post_form):
    email = unique_email()
    post_form("/create-account", {"first_name": "Pat", "last_name": "Word", "email": email,
                                  "password": "long-enough-1", "intent": "student"})
    other = app.test_client()                     # the same account, signed in somewhere else
    with client.session_transaction() as mine, other.session_transaction() as theirs:
        theirs.update(dict(mine))
        mine.clear()
    assert "Pat" in text_of(other.get("/my"))
    res = go(client, providers, "google", google(email))
    assert res.status_code == 302
    a = account(app, email)
    assert a["ways"] == ["google"] and a["email_verified_at"] and a["display_name"] == "Pat Word"
    assert "password was switched off" in text_of(client.get("/"))
    assert other.get("/my").status_code == 302                    # that older sign-in has ended
    with client.session_transaction() as sess:
        sess.clear()
    assert post_form("/sign-in", {"email": email, "password": "long-enough-1"}).status_code == 400


def test_confirmed_account_links_without_losing_its_password(client, providers, app, post_form):
    email = unique_email()
    post_form("/create-account", {"first_name": "Val", "last_name": "Id", "email": email,
                                  "password": "long-enough-1", "intent": "student"})
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("UPDATE user_accounts SET email_verified_at = now() WHERE email = :e"), {"e": email})
    with client.session_transaction() as sess:
        sess.clear()
    assert go(client, providers, "microsoft", microsoft(email)).status_code == 302
    assert account(app, email)["ways"] == ["microsoft", "password"]


def test_microsoft_work_accounts_need_a_vouched_email(client, providers, app, monkeypatch):
    email = unique_email("work")
    res = go(client, providers, "microsoft", microsoft(email, tid=OTHER_TENANT))
    assert res.status_code == 400 and "couldn't confirm the email" in text_of(res) and account(app, email) is None
    # The tenant's own "email verified" claim, or NBC's own tenant, are trusted.
    assert go(client, providers, "microsoft", microsoft(email, tid=OTHER_TENANT, xms_edov=True)).status_code == 302
    with client.session_transaction() as sess:
        sess.clear()
    monkeypatch.setenv("ENTRA_TENANT_ID", OTHER_TENANT)
    staff = unique_email("staff")
    assert go(client, providers, "microsoft", microsoft(staff, tid=OTHER_TENANT)).status_code == 302
    assert account(app, staff)["ways"] == ["microsoft"]


def test_microsoft_issuer_must_match_its_tenant(client, providers, app):
    email = unique_email()
    claims = microsoft(email)
    claims["iss"] = f"https://login.microsoftonline.com/{OTHER_TENANT}/v2.0"
    assert go(client, providers, "microsoft", claims).status_code == 400 and account(app, email) is None


def test_a_switched_off_account_cant_sign_in(client, providers, app, post_form):
    email = unique_email()
    post_form("/create-account", {"first_name": "Off", "last_name": "Line", "email": email,
                                  "password": "long-enough-1", "intent": "student"})
    with app.app_context(), db.transaction() as conn:
        conn.execute(text("UPDATE user_accounts SET status = 'suspended' WHERE email = :e"), {"e": email})
    with client.session_transaction() as sess:
        sess.clear()
    res = go(client, providers, "google", google(email))
    assert res.status_code == 400 and "switched off" in text_of(res)


def test_sign_in_page_names_the_class_being_booked(client, app):
    with app.app_context():
        s = db.fetch_one("""SELECT s.public_id, cf.name FROM sessions s JOIN course_families cf
                            ON cf.id = s.course_family_id ORDER BY s.starts_at DESC LIMIT 1""")
    page = text_of(client.get(f"/sign-in?next=/book/{s['public_id']}?seats=2"))
    assert "You're booking" in page and s["name"] in page
    assert f'/create-account?next=/book/{s["public_id"]}' in page        # creating an account comes back too


def test_create_account_returns_to_next(client, post_form):
    res = client.post("/create-account", data={"csrf_token": csrf_from(client, "/create-account"),
                                               "first_name": "Ne", "last_name": "Xt", "email": unique_email(),
                                               "password": "long-enough-1", "intent": "student",
                                               "next": "/my#book-more"})
    assert res.status_code == 302 and res.headers["Location"] == "/my#book-more"
