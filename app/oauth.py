"""Sign in with Google or Microsoft (OpenID Connect, authorization code flow).

    GET /sign-in/<provider>            -> the provider's sign-in page (account.oauth_start)
    GET /sign-in/<provider>/callback   <- back here with a one-time code (account.oauth_callback)

Secrets (Replit Secrets; a button only shows when its provider is set up):
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
    ENTRA_CLIENT_ID, ENTRA_CLIENT_SECRET   an app registration that accepts any
                                           Microsoft account (work, school or personal)
    ENTRA_TENANT_ID (optional)             NBC's own Microsoft 365 tenant: emails from it are trusted

How it stays safe:
  * state, nonce and a PKCE code verifier are kept in the signed session and
    checked on the way back, so a sign-in can't be started by another site or replayed;
  * the ID token comes straight from the provider's token endpoint over TLS (the
    code is exchanged server-side with the client secret), so its signature
    needn't be checked again (OpenID Connect Core 3.1.3.7); its issuer,
    audience, expiry and nonce are;
  * an account is matched by the provider's own id first. An email is only used
    to find or create an account when the provider vouches for it: Google's
    email_verified; Microsoft personal accounts, NBC's own tenant, or the
    xms_edov claim. Otherwise the person is asked to use another way in;
  * linking to an account whose email was never confirmed removes that
    account's password and signs out its other sessions, so nobody who signed up
    with someone else's email keeps a way in.
Roles are never chosen here: new accounts get `user`, and `student` if they have booked.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

from flask import session, url_for
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from . import auth, db
from .integrations.http import IntegrationError, request as http_request

MS_PERSONAL_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"     # Microsoft's tenant for personal accounts
MS_BASE = "https://login.microsoftonline.com/common/oauth2/v2.0"
SESSION_KEY = "oauth"
MAX_SECONDS = 600                       # a sign-in must come back within 10 minutes

PROVIDERS = {
    "google": {"name": "Google", "id_env": "GOOGLE_CLIENT_ID", "secret_env": "GOOGLE_CLIENT_SECRET",
               "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
               "token": "https://oauth2.googleapis.com/token"},
    "microsoft": {"name": "Microsoft", "id_env": "ENTRA_CLIENT_ID", "secret_env": "ENTRA_CLIENT_SECRET",
                  "authorize": f"{MS_BASE}/authorize", "token": f"{MS_BASE}/token"},
}


@dataclass
class Identity:
    provider: str
    subject: str                 # the provider's id for the person (never their email)
    email: str
    email_trusted: bool
    name: str
    tenant_id: str | None = None


def _secret(name: str) -> str:
    return os.environ.get(name, "").strip().strip("'\"")


def configured(provider: str) -> bool:
    spec = PROVIDERS.get(provider)
    return bool(spec and _secret(spec["id_env"]) and _secret(spec["secret_env"]))


def available() -> list[dict]:
    """[{key, name}] of the providers that are set up, for the sign-in buttons."""
    return [{"key": k, "name": v["name"]} for k, v in PROVIDERS.items() if configured(k)]


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def redirect_uri(provider: str) -> str:
    """The address the provider sends people back to. It must be on the host the
    person is on (the session cookie belongs to it), and registered with the provider."""
    return url_for("account.oauth_callback", provider=provider, _external=True)


def start(provider: str, next_url: str | None) -> str:
    """Remember what to check on the way back; return the provider's sign-in address."""
    spec = PROVIDERS[provider]
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    session[SESSION_KEY] = {"provider": provider, "state": state, "nonce": nonce, "verifier": verifier,
                            "next": next_url if auth.is_safe_next(next_url) else None, "at": int(time.time())}
    params = {"client_id": _secret(spec["id_env"]), "response_type": "code", "redirect_uri": redirect_uri(provider),
              "scope": "openid email profile", "state": state, "nonce": nonce, "prompt": "select_account",
              "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
              "code_challenge_method": "S256"}
    return f"{spec['authorize']}?{urlencode(params)}"


def finish(provider: str, args) -> tuple[int, str, str | None]:
    """Check the answer, exchange the code, sign the person in.
    Returns (account id, where to go next, a note for the person or None).
    Raises auth.SignInError with a message for the person."""
    name = PROVIDERS[provider]["name"]
    saved = session.pop(SESSION_KEY, None) or {}
    if args.get("error"):
        cancelled = args.get("error") in ("access_denied", "consent_required", "interaction_required")
        raise auth.SignInError(f"Signing in with {name} was cancelled." if cancelled else
                               f"{name} couldn't sign you in. Please try again.")
    if (saved.get("provider") != provider or not args.get("state")
            or not secrets.compare_digest(str(saved.get("state", "")), str(args.get("state")))
            or time.time() - saved.get("at", 0) > MAX_SECONDS or not args.get("code")):
        raise auth.SignInError(f"That {name} sign-in expired or didn't start here. Please try again.")
    claims = _exchange(provider, args["code"], saved["verifier"])
    who = _identity(provider, claims, saved["nonce"])
    user_id, note = sign_in(who)
    return user_id, saved.get("next") or auth.landing(), note


def _exchange(provider: str, code: str, verifier: str) -> dict:
    spec = PROVIDERS[provider]
    try:
        body = http_request("POST", spec["token"], service=f"{spec['name']} sign-in", attempts=2,
                            data={"grant_type": "authorization_code", "code": code,
                                  "redirect_uri": redirect_uri(provider), "client_id": _secret(spec["id_env"]),
                                  "client_secret": _secret(spec["secret_env"]), "code_verifier": verifier},
                            headers={"Accept": "application/json"})
    except IntegrationError as exc:
        raise auth.SignInError(f"Signing in with {spec['name']} didn't finish. Please try again.") from exc
    token = body.get("id_token") if isinstance(body, dict) else None
    if not token:
        raise auth.SignInError(f"Signing in with {spec['name']} didn't finish. Please try again.")
    return _claims(token)


def _claims(id_token: str) -> dict:
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        raise auth.SignInError("That sign-in answer couldn't be read. Please try again.") from None


def _truthy(v) -> bool:
    return v is True or str(v).lower() == "true" or v == 1


def _identity(provider: str, c: dict, nonce: str) -> Identity:
    """Check the ID token's claims and pull out who this is."""
    spec = PROVIDERS[provider]
    bad = auth.SignInError(f"That {spec['name']} sign-in couldn't be checked. Please try again.")
    aud = c.get("aud")
    if (aud if isinstance(aud, str) else (aud or [None])[0]) != _secret(spec["id_env"]):
        raise bad
    if not isinstance(c.get("exp"), (int, float)) or c["exp"] < time.time() - 60:
        raise bad
    if not c.get("nonce") or not secrets.compare_digest(str(c["nonce"]), nonce):
        raise bad
    name = " ".join(str(c.get("name") or "").split())[:120]
    if provider == "google":
        if c.get("iss") not in ("https://accounts.google.com", "accounts.google.com") or not c.get("sub"):
            raise bad
        email = str(c.get("email") or "").strip().lower()
        return Identity("google", str(c["sub"]).lower(), email, _truthy(c.get("email_verified")), name)
    tid = str(c.get("tid") or "").lower()
    if not re.fullmatch(r"[0-9a-f-]{36}", tid) or c.get("iss") != f"https://login.microsoftonline.com/{tid}/v2.0":
        raise bad
    subject = str(c.get("oid") or c.get("sub") or "")
    if not subject:
        raise bad
    email = str(c.get("email") or "").strip().lower()
    if not email and "@" in str(c.get("preferred_username") or ""):
        email = str(c["preferred_username"]).strip().lower()
    own = _secret("ENTRA_TENANT_ID").lower()
    trusted = tid == MS_PERSONAL_TENANT or (own and tid == own) or _truthy(c.get("xms_edov"))
    return Identity("microsoft", subject, email, bool(trusted), name, tid)


def sign_in(who: Identity) -> tuple[int, str | None]:
    """Find or create the account for this identity and start the session.
    Returns (account id, a note for the person or None)."""
    try:
        return _sign_in(who)
    except IntegrityError:          # the same person finishing twice at once: the first one won
        raise auth.SignInError("Something changed while you were signing in. Please try again.") from None


def _sign_in(who: Identity) -> tuple[int, str | None]:
    name = PROVIDERS[who.provider]["name"]
    note = None
    with db.transaction() as conn:
        row = conn.execute(text("""
            SELECT i.id AS identity_id, a.id AS user_id, a.status, a.email, a.email_verified_at
              FROM user_identities i JOIN user_accounts a ON a.id = i.user_account_id
             WHERE i.provider = :p AND i.subject = :s
             FOR NO KEY UPDATE OF i, a
        """), {"p": who.provider, "s": who.subject}).mappings().first()
        if row:
            if row["status"] != "active":
                raise auth.SignInError("This account is switched off. Contact info@nationsbestcpr.com.")
            conn.execute(text("UPDATE user_identities SET last_used_at = now(), first_used_at = "
                              "coalesce(first_used_at, now()) WHERE id = :id"), {"id": row["identity_id"]})
            if who.email_trusted and who.email == str(row["email"]).lower() and row["email_verified_at"] is None:
                conn.execute(text("UPDATE user_accounts SET email_verified_at = now() WHERE id = :id"),
                             {"id": row["user_id"]})
            user_id = row["user_id"]
        else:
            if not auth.EMAIL_RE.match(who.email or ""):
                raise auth.SignInError(f"Your {name} account didn't share an email address, so we can't set up "
                                       "your account with it. Create an account with your email instead.")
            if not who.email_trusted:
                raise auth.SignInError(f"We couldn't confirm the email address on this {name} account. Sign in "
                                       "with Google, or with your email and password, instead.")
            account = conn.execute(text("""
                SELECT id, status, email_verified_at FROM user_accounts WHERE email = :e FOR NO KEY UPDATE
            """), {"e": who.email}).mappings().first()
            if account:
                if account["status"] != "active":
                    raise auth.SignInError("This account is switched off. Contact info@nationsbestcpr.com.")
                already = conn.execute(text("SELECT 1 FROM user_identities WHERE user_account_id = :u "
                                            "AND provider = :p"), {"u": account["id"], "p": who.provider}).first()
                if already:
                    raise auth.SignInError(f"Your account is linked to a different {name} account. Sign in with "
                                           f"that one, or with your email and password.")
                user_id = account["id"]
                if account["email_verified_at"] is None:
                    # Nobody had proved they own this email. Whoever made the password might not be
                    # them, so it stops working (they can keep signing in with this button).
                    gone = conn.execute(text("DELETE FROM user_identities WHERE user_account_id = :u "
                                             "AND provider = 'password'"), {"u": user_id}).rowcount
                    conn.execute(text("UPDATE user_accounts SET email_verified_at = now() WHERE id = :u"),
                                 {"u": user_id})
                    if gone:
                        note = (f"Your email is now confirmed by {name}. To keep your account safe, its password "
                                f"was switched off: sign in with {name} from now on.")
            else:
                display = who.name or who.email.split("@")[0]
                user_id = conn.execute(text("""
                    INSERT INTO user_accounts (email, display_name, status, email_verified_at)
                    VALUES (:e, :n, 'active', now()) RETURNING id
                """), {"e": who.email, "n": display}).scalar_one()
                conn.execute(text("INSERT INTO user_roles (user_account_id, role_code) VALUES (:u, 'user')"),
                             {"u": user_id})
                conn.execute(text("SELECT grant_student_role_if_booked(:u)"), {"u": user_id})
            conn.execute(text("""
                INSERT INTO user_identities (user_account_id, provider, subject, tenant_id, first_used_at, last_used_at)
                VALUES (:u, :p, :s, CAST(:t AS uuid), now(), now())
            """), {"u": user_id, "p": who.provider, "s": who.subject, "t": who.tenant_id})
        conn.execute(text("UPDATE user_accounts SET last_sign_in_at = now() WHERE id = :id"), {"id": user_id})
    auth._start_session(user_id)
    return user_id, note
