"""Linking instructor records (from Airtable's Instructors table) to sign-in accounts.

An account becomes an instructor only when an admin gives it the instructor role (never at sign-up). Once it
has the role, it's linked to the instructor record with the same email, so the portal shows that
instructor's classes, certifications and pay. The link is made when the role is given, and again after each
Airtable sync (for an instructor added to Airtable later). An email that two instructor records share was
kept off both records by the sync, so it links to neither.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

_LINK = """
    UPDATE instructors i SET user_account_id = a.id, updated_at = now()
      FROM user_accounts a
     WHERE i.user_account_id IS NULL AND i.email IS NOT NULL AND a.email = i.email
       AND i.source_deleted_at IS NULL
       AND EXISTS (SELECT 1 FROM user_roles ur WHERE ur.user_account_id = a.id AND ur.role_code = 'instructor'
                     AND ur.revoked_at IS NULL)
       AND NOT EXISTS (SELECT 1 FROM instructors o WHERE o.user_account_id = a.id)
"""


def link(conn: Connection, user_id: int | None = None) -> int:
    """Link instructor records to accounts holding the instructor role (one account, or all). Returns how many."""
    if user_id is None:
        return conn.execute(text(_LINK)).rowcount
    return conn.execute(text(_LINK + " AND a.id = :u"), {"u": user_id}).rowcount


def candidates(conn: Connection) -> list[dict]:
    """Accounts that signed up as instructors, or match an instructor record by email, without the role yet."""
    return [dict(r) for r in conn.execute(text("""
        SELECT a.id, a.email, a.display_name, a.signup_intent, a.created_at, a.last_sign_in_at,
               i.id AS instructor_id, i.full_name, i.status AS instructor_status, i.removal_status
          FROM user_accounts a
          LEFT JOIN instructors i ON i.email = a.email AND i.source_deleted_at IS NULL
         WHERE a.status = 'active'
           AND NOT EXISTS (SELECT 1 FROM user_roles ur WHERE ur.user_account_id = a.id
                             AND ur.role_code = 'instructor' AND ur.revoked_at IS NULL)
           AND (a.signup_intent = 'instructor' OR i.id IS NOT NULL)
         ORDER BY (i.id IS NULL), a.created_at DESC
    """)).mappings()]
