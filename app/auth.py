"""Accounts: create, sign in, and work out who someone is.

There is one sign-in for everyone. Nobody picks a role: after sign-in the
account's roles (user_roles) decide what the person sees.

  * New accounts get the 'user' role.
  * Paying for a seat adds 'student' (a database trigger does it; guests who
    booked before creating an account are recognised at sign-up).
  * 'instructor' and staff roles are granted by an admin only. Choosing
    "I'm an instructor" at sign-up is recorded (signup_intent) but grants nothing.

Passwords are stored only as salted scrypt hashes (werkzeug). Google and
Microsoft sign-in are in app/oauth.py.

"View as" (impersonation): someone with 'user.impersonate' (admins) can open
the app as another account to see what they see. It is read-only: while
viewing as someone, every change (any POST but "Return to my account" and
sign-out) is refused. It ends by itself after VIEW_AS_MINUTES, can't target
an admin or a switched-off account, and its start and end are in the action log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Callable

from flask import Flask, abort, current_app, g, redirect, request, session, url_for
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from . import db

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# Roles whose dashboard is the booking console (roles.dashboard_key).
CONSOLE_DASHBOARDS = ("manager", "customer_service")


@dataclass
class CurrentUser:
    id: int
    email: str
    display_name: str
    signup_intent: str | None
    email_verified: bool = False       # confirmed by Google / Microsoft (password sign-up doesn't confirm it)
    roles: list[dict] = field(default_factory=list)   # [{code, name, is_staff, grants_all, dashboard_key, scope_metro_id}], main role first
    permissions: set[str] = field(default_factory=set)

    @property
    def first_name(self) -> str:
        return self.display_name.split(" ", 1)[0]

    @property
    def main_role(self) -> dict:
        """The role that shapes what they see first (lowest sort_order)."""
        return self.roles[0] if self.roles else {"code": "user", "name": "User", "is_staff": False}

    @property
    def is_staff(self) -> bool:
        return any(r["is_staff"] for r in self.roles)

    def has_role(self, code: str) -> bool:
        return any(r["code"] == code for r in self.roles)

    @property
    def is_admin(self) -> bool:
        """Holds a role that grants every permission."""
        return any(r.get("grants_all") for r in self.roles)

    @property
    def area(self) -> str:
        """Which dashboard area this account opens, decided by role data:
        'console' (the booking console) for customer service and managers,
        'admin' for admins and other staff roles, 'instructor' for a role whose
        dashboard is the instructor one, else 'student' (users and students)."""
        if self.is_staff:
            if not self.is_admin and any(r.get("dashboard_key") in CONSOLE_DASHBOARDS for r in self.roles):
                return "console"
            return "admin"
        if any(r.get("dashboard_key") == "instructor" for r in self.roles):
            return "instructor"
        return "student"

    def has_permission(self, permission: str) -> bool:
        """Holds the permission in at least one city area (use can() for an exact area)."""
        return permission in self.permissions

    def can(self, permission: str, metro_id: int | None = None) -> bool:
        """Exact check in the database (honours city-area limits)."""
        return bool(db.fetch_value("SELECT user_has_permission(:uid, :perm, :metro)",
                                   uid=self.id, perm=permission, metro=metro_id))


def load_user(user_id: int) -> CurrentUser | None:
    account = db.fetch_one("""
        SELECT id, email, display_name, signup_intent, email_verified_at
          FROM user_accounts WHERE id = :id AND status = 'active'
    """, id=user_id)
    if not account:
        return None
    roles = db.fetch_all("""
        SELECT r.code, r.name, r.is_staff, r.grants_all, r.dashboard_key, ur.scope_metro_id
          FROM user_roles ur JOIN roles r ON r.code = ur.role_code
         WHERE ur.user_account_id = :id AND ur.revoked_at IS NULL
         ORDER BY r.sort_order
    """, id=user_id)
    perms = {r["permission_code"] for r in db.fetch_all(
        "SELECT DISTINCT permission_code FROM user_effective_permissions WHERE user_account_id = :id", id=user_id)}
    return CurrentUser(id=account["id"], email=account["email"], display_name=account["display_name"],
                       signup_intent=account["signup_intent"],
                       email_verified=account["email_verified_at"] is not None, roles=roles, permissions=perms)


def current_user() -> CurrentUser | None:
    if "current_user" not in g:
        uid = session.get("user_id")
        user = load_user(uid) if uid else None
        if user and user.email_verified and not session.get("verified"):
            # Signed in before the email was confirmed (by Google or Microsoft): that
            # sign-in may have been by someone else who typed this email, so it ends.
            session.clear()
            user = None
        g.current_user = user
    return g.current_user


def _start_session(user_id: int) -> None:
    csrf = session.get("_csrf")
    session.clear()                      # new session id on sign-in
    session["user_id"] = user_id
    session["verified"] = bool(db.fetch_value(
        "SELECT email_verified_at IS NOT NULL FROM user_accounts WHERE id = :id", id=user_id))
    if csrf:
        session["_csrf"] = csrf
    g.pop("current_user", None)


def sign_out() -> None:
    session.clear()
    g.pop("current_user", None)


# --- Creating an account ----------------------------------------------------------

class SignUpError(ValueError):
    """A problem with the sign-up form, worded for the person filling it in."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message


def create_account(*, first_name: str, last_name: str, email: str, password: str, intent: str) -> int:
    first_name, last_name, email = first_name.strip(), last_name.strip(), email.strip().lower()
    if not first_name:
        raise SignUpError("first_name", "Enter your first name.")
    if not last_name:
        raise SignUpError("last_name", "Enter your last name.")
    if not EMAIL_RE.match(email):
        raise SignUpError("email", "Enter an email address like name@example.com.")
    if len(password) < current_app.config["PASSWORD_MIN_LENGTH"]:
        raise SignUpError("password", f"Use at least {current_app.config['PASSWORD_MIN_LENGTH']} characters.")
    if intent not in {"student", "instructor"}:
        raise SignUpError("intent", "Choose student or instructor.")

    try:
        with db.transaction() as conn:
            uid = conn.execute(text("""
                INSERT INTO user_accounts (email, display_name, status, signup_intent)
                VALUES (:email, :name, 'active', :intent) RETURNING id
            """), {"email": email, "name": f"{first_name} {last_name}", "intent": intent}).scalar_one()
            conn.execute(text("""
                INSERT INTO user_identities (user_account_id, provider, subject, password_hash, password_changed_at)
                VALUES (:uid, 'password', :email, :hash, now())
            """), {"uid": uid, "email": email, "hash": generate_password_hash(password)})
            conn.execute(text("INSERT INTO user_roles (user_account_id, role_code) VALUES (:uid, 'user')"), {"uid": uid})
            # Booked before as a guest? Then they are already a student.
            conn.execute(text("SELECT grant_student_role_if_booked(:uid)"), {"uid": uid})
    except IntegrityError as exc:
        if "user_accounts_email_key" in str(exc.orig) or "user_identities_provider_subject_key" in str(exc.orig):
            raise SignUpError("email", "There's already an account with this email. Sign in instead.") from None
        raise
    _start_session(uid)
    return uid


# --- Signing in -------------------------------------------------------------------

class SignInError(ValueError):
    pass


def authenticate(email: str, password: str) -> int:
    """Check an email + password and start the session. One message for every
    failure, so the form never reveals which emails have accounts."""
    email = email.strip().lower()
    wrong = SignInError("That email and password don't match. Check them and try again.")
    failure: SignInError | None = None
    # The failure count must be saved even when sign-in is refused, so the
    # transaction commits first and the error is raised after it.
    with db.transaction() as conn:
        row = conn.execute(text("""
            SELECT i.id AS identity_id, i.password_hash, i.failed_attempts, i.locked_until,
                   a.id AS user_id, a.status
              FROM user_identities i JOIN user_accounts a ON a.id = i.user_account_id
             WHERE i.provider = 'password' AND i.subject = :email
             FOR NO KEY UPDATE OF i
        """), {"email": email}).mappings().first()
        now = datetime.now(timezone.utc)
        if row is None:
            generate_password_hash(password)        # same work either way (timing)
            failure = wrong
        elif row["locked_until"] and row["locked_until"] > now:
            failure = SignInError("Too many tries. Wait 15 minutes, then sign in again.")
        elif not check_password_hash(row["password_hash"], password):
            failures = row["failed_attempts"] + 1
            locked = (now + timedelta(minutes=current_app.config["SIGN_IN_LOCK_MINUTES"])
                      if failures >= current_app.config["SIGN_IN_MAX_FAILURES"] else None)
            conn.execute(text("""
                UPDATE user_identities SET failed_attempts = :f, locked_until = :l WHERE id = :id
            """), {"f": 0 if locked else failures, "l": locked, "id": row["identity_id"]})
            failure = wrong
        elif row["status"] != "active":
            failure = SignInError("This account is switched off. Contact info@nationsbestcpr.com.")
        else:
            conn.execute(text("""
                UPDATE user_identities SET failed_attempts = 0, locked_until = NULL,
                       last_used_at = now(), first_used_at = coalesce(first_used_at, now())
                 WHERE id = :id
            """), {"id": row["identity_id"]})
            conn.execute(text("UPDATE user_accounts SET last_sign_in_at = now() WHERE id = :id"),
                         {"id": row["user_id"]})
    if failure:
        raise failure
    _start_session(row["user_id"])
    return row["user_id"]


# --- Guards -----------------------------------------------------------------------

def login_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            return redirect(url_for("account.sign_in", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def permission_required(permission: str) -> Callable:
    """Require a permission (in any city area; check the exact area inside the view)."""
    def decorator(view: Callable) -> Callable:
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if permission not in current_user().permissions:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def staff_required(view: Callable) -> Callable:
    """Any office role (roles.is_staff)."""
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user().is_staff:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def any_permission_required(*permissions: str) -> Callable:
    def decorator(view: Callable) -> Callable:
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if not any(p in current_user().permissions for p in permissions):
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def change_password(user_id: int, current: str, new: str, confirm: str) -> None:
    """Change a password after checking the current one. Raises SignUpError
    (field, message) so the settings form can show the problem by the field."""
    if len(new) < current_app.config["PASSWORD_MIN_LENGTH"]:
        raise SignUpError("new_password", f"Use at least {current_app.config['PASSWORD_MIN_LENGTH']} characters.")
    if new != confirm:
        raise SignUpError("confirm_password", "The two new passwords don't match.")
    with db.transaction() as conn:
        row = conn.execute(text("""
            SELECT id, password_hash FROM user_identities
             WHERE user_account_id = :uid AND provider = 'password'
             FOR NO KEY UPDATE
        """), {"uid": user_id}).mappings().first()
        if row is None:
            raise SignUpError("current_password", "This account doesn't sign in with a password.")
        if not check_password_hash(row["password_hash"], current):
            raise SignUpError("current_password", "That isn't your current password.")
        if check_password_hash(row["password_hash"], new):
            raise SignUpError("new_password", "Choose a password different from your current one.")
        conn.execute(text("""
            UPDATE user_identities SET password_hash = :h, password_changed_at = now(),
                   failed_attempts = 0, locked_until = NULL
             WHERE id = :id
        """), {"h": generate_password_hash(new), "id": row["id"]})


def has_password(user_id: int) -> bool:
    return bool(db.fetch_value(
        "SELECT EXISTS (SELECT 1 FROM user_identities WHERE user_account_id = :uid AND provider = 'password')",
        uid=user_id))


def is_safe_next(target: str | None) -> bool:
    """A path on this site (never another site, whatever the spelling)."""
    return bool(target and target.startswith("/") and not target.startswith(("//", "/\\"))
                and not any(c in target for c in "\r\n\t\\"))


def landing() -> str:
    """Where someone goes after signing in with nowhere particular to go: staff and
    instructors to their dashboard (the booking console, admin or teaching),
    customers to the classes."""
    user = current_user()
    if user and user.area in ("console", "admin", "instructor"):
        return url_for("me.dashboard")
    return url_for("public.home")


def safe_next(target: str | None) -> str:
    """Only follow redirects within this site; otherwise go where the account belongs."""
    return target if is_safe_next(target) else landing()


# --- View as (impersonation) -----------------------------------------------------------

VIEW_AS_MINUTES = 60
VIEW_AS_ALLOWED_POSTS = ("admin.view_as_stop", "account.sign_out")


class ViewAsError(ValueError):
    """Why this account can't be viewed as, worded for the admin."""


def viewing_as() -> dict | None:
    """{'admin_id', 'admin_name', 'since'} while someone is viewing as another account."""
    if not session.get("view_as_admin_id"):
        return None
    return {"admin_id": session["view_as_admin_id"], "admin_name": session.get("view_as_admin_name", ""),
            "since": session.get("view_as_since", 0)}


def start_view_as(target_id: int, back: str | None = None) -> dict:
    """Switch this browser to the target account (read-only). Returns the target.
    `back` is the admin page to return to afterwards (the Users list or the user's page)."""
    admin = current_user()
    if admin is None or "user.impersonate" not in admin.permissions or viewing_as():
        abort(403)
    if target_id == admin.id:
        raise ViewAsError("That's your own account.")
    target = db.fetch_one("""
        SELECT a.id, a.display_name, a.status, a.email_verified_at,
               EXISTS (SELECT 1 FROM user_roles ur JOIN roles r ON r.code = ur.role_code
                        WHERE ur.user_account_id = a.id AND ur.revoked_at IS NULL AND r.grants_all) AS is_admin
          FROM user_accounts a WHERE a.id = :id
    """, id=target_id)
    if target is None:
        abort(404)
    if target["is_admin"]:
        raise ViewAsError("Admins can't be viewed as: they see everything already.")
    if target["status"] != "active":
        raise ViewAsError("This account is switched off, so there's nothing to view.")
    import time
    csrf = session.get("_csrf")
    session.clear()
    session.update({"user_id": target["id"], "verified": target["email_verified_at"] is not None,
                    "view_as_admin_id": admin.id, "view_as_admin_name": admin.display_name,
                    "view_as_since": int(time.time()), "view_as_back": back if is_safe_next(back) else None})
    if csrf:
        session["_csrf"] = csrf
    g.pop("current_user", None)
    return target


def stop_view_as() -> tuple[int | None, str | None]:
    """Back to the admin's own account. Returns (the account that was being viewed,
    the admin page to go back to)."""
    info = viewing_as()
    if not info:
        return None, None
    viewed, back = session.get("user_id"), session.get("view_as_back")
    _start_session(info["admin_id"])         # signed out instead if the admin was switched off meanwhile
    return viewed, back


def init_app(app: Flask) -> None:
    @app.context_processor
    def inject_user():
        return {"current_user": current_user(), "viewing_as": viewing_as()}

    @app.before_request
    def view_as_is_read_only():
        info = viewing_as()
        if not info:
            return None
        import time
        if time.time() - info["since"] > VIEW_AS_MINUTES * 60:
            from . import audit
            viewed, _back = stop_view_as()
            if current_user():
                with db.transaction() as conn:
                    audit.record(conn, "user.view_as_stop", "user_account", viewed, after={"reason": "timed out"})
            from flask import flash
            flash(f"“View as” ended after {VIEW_AS_MINUTES} minutes; you're back on your own account.", "ok")
            return redirect(url_for("me.dashboard") if current_user() else url_for("public.home"))
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.endpoint not in VIEW_AS_ALLOWED_POSTS:
            from flask import render_template
            return render_template("errors/error.html", code=403, title="Changes are switched off",
                                   message="You're viewing as someone else, so nothing can be changed. "
                                           "Use “Return to my account” at the top of the page first."), 403
        return None
