"""Keys other tools (the NBC Ops Hub) use to call the booking engine's API (/api/v1).

A key looks like ``nbc_<8 letters/digits>_<secret>``. The first part (the
prefix) is stored so a key can be named on screen; only a SHA-256 hash of the
secret is kept, and the whole key is shown once, when a Superadmin makes it.

Each key has a list of permissions it may use. Every call also names the staff
member it is for (``X-Acting-User``, their booking-engine email): the call runs
as that person, limited to the key's permissions, so the database's rules on
cities, large refunds and extra seats hold exactly as they do for the person.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text

from . import audit, auth, db

KEY_RE = re.compile(r"^nbc_([a-z0-9]{8})_([A-Za-z0-9_-]{32,})$")
_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


class ApiKeyError(ValueError):
    """A problem making or switching off a key, worded for the Superadmin."""


def _hash(secret: str) -> bytes:
    return hashlib.sha256(secret.encode()).digest()


@dataclass
class Key:
    id: int
    name: str
    prefix: str
    permissions: set[str] = field(default_factory=set)


def create(name: str, permissions: list[str], expires_at: datetime | None = None) -> tuple[int, str]:
    """Make a key for a tool. Returns (key id, the full key: shown once, never stored)."""
    name = name.strip()[:80]
    if not name:
        raise ApiKeyError("Name the tool this key is for, like “NBC Ops Hub”.")
    valid = {p["code"] for p in db.fetch_all("SELECT code FROM permissions")}
    wanted = sorted(set(permissions))
    if not wanted:
        raise ApiKeyError("Tick at least one thing this key may do.")
    if not set(wanted) <= valid:
        raise ApiKeyError("One of the permissions isn't known.")
    prefix = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    secret = secrets.token_urlsafe(32)
    with db.transaction() as conn:
        key_id = conn.execute(text("""
            INSERT INTO api_keys (name, prefix, secret_hash, created_by_user_id, expires_at)
            VALUES (:n, :p, :h, :u, :e) RETURNING id
        """), {"n": name, "p": prefix, "h": _hash(secret), "u": auth.current_user().id, "e": expires_at}).scalar_one()
        conn.execute(text("""
            INSERT INTO api_key_permissions (api_key_id, permission_code) SELECT :k, unnest(CAST(:perms AS text[]))
        """), {"k": key_id, "perms": wanted})
        audit.record(conn, "api_key.create", "api_key", key_id,
                     after={"name": name, "prefix": prefix, "permissions": wanted})
    return key_id, f"nbc_{prefix}_{secret}"


def revoke(key_id: int) -> str:
    """Switch a key off at once. Returns its name."""
    with db.transaction() as conn:
        row = conn.execute(text("""
            UPDATE api_keys SET revoked_at = now(), revoked_by_user_id = :u
             WHERE id = :k AND revoked_at IS NULL RETURNING name, prefix
        """), {"k": key_id, "u": auth.current_user().id}).mappings().first()
        if row is None:
            raise ApiKeyError("That key is already switched off.")
        audit.record(conn, "api_key.revoke", "api_key", key_id, before={"name": row["name"], "prefix": row["prefix"]})
    return row["name"]


def verify(raw: str) -> Key | None:
    """The live key this is, or None (unknown, wrong secret, switched off or expired)."""
    m = KEY_RE.match(raw or "")
    if not m:
        return None
    row = db.fetch_one("""
        SELECT id, name, prefix, secret_hash, last_used_at < now() - interval '1 minute' AS stale,
               last_used_at IS NULL AS never
          FROM api_keys
         WHERE prefix = :p AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())
    """, p=m.group(1))
    if not row or not hmac.compare_digest(bytes(row["secret_hash"]), _hash(m.group(2))):
        return None
    if row["stale"] or row["never"]:
        with db.transaction() as conn:
            conn.execute(text("UPDATE api_keys SET last_used_at = now() WHERE id = :k"), {"k": row["id"]})
    perms = {r["permission_code"] for r in db.fetch_all(
        "SELECT permission_code FROM api_key_permissions WHERE api_key_id = :k", k=row["id"])}
    return Key(id=row["id"], name=row["name"], prefix=row["prefix"], permissions=perms)


class ApiUser(auth.CurrentUser):
    """The staff member a call is for, limited to what the key may do."""

    key: Key

    def can(self, permission: str, metro_id: int | None = None) -> bool:
        return permission in self.key.permissions and super().can(permission, metro_id)


def acting_user(key: Key, email: str) -> ApiUser | None:
    """The active staff account named on the call, with the key's limits; None if there isn't one."""
    uid = db.fetch_value("SELECT id FROM user_accounts WHERE email = :e AND status = 'active'",
                         e=(email or "").strip().lower())
    person = auth.load_user(uid) if uid else None
    if person is None or not person.is_staff:
        return None
    user = ApiUser(id=person.id, email=person.email, display_name=person.display_name,
                   signup_intent=person.signup_intent, email_verified=person.email_verified, roles=person.roles,
                   permissions=person.permissions & key.permissions)
    user.key = key
    return user


def listing() -> list[dict]:
    rows = db.fetch_all("""
        SELECT k.id, k.name, k.prefix, k.created_at, k.expires_at, k.last_used_at, k.revoked_at,
               c.display_name AS created_by, r.display_name AS revoked_by,
               coalesce((SELECT array_agg(p.permission_code ORDER BY p.permission_code)
                           FROM api_key_permissions p WHERE p.api_key_id = k.id), '{}') AS permissions
          FROM api_keys k
          JOIN user_accounts c ON c.id = k.created_by_user_id
          LEFT JOIN user_accounts r ON r.id = k.revoked_by_user_id
         ORDER BY k.revoked_at IS NOT NULL, k.created_at DESC
    """)
    return rows
