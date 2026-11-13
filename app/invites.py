"""Invites for attendees someone else booked (usually an employer's staff).

When a seat is booked for someone other than the buyer, that attendee gets an
email (sent by GHL from its invitation template) with a link:

    /invite/<token>     "Dana Buyer booked you a seat in BLS on Dec 10. Make your account."

Opening the link and creating an account (or signing in with the same email)
proves the attendee reads that inbox, so the account's email counts as
confirmed and their class shows on their dashboard. Only a hash of the token
is stored (action_tokens, purpose 'account_invite'). Each new link for a seat
ends the earlier ones; links last `invite_link_days` (Business settings).
"""

from __future__ import annotations

import hashlib
import secrets

from flask import current_app
from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import db


def _hash(raw: str) -> bytes:
    return hashlib.sha256(raw.encode()).digest()


def base_url() -> str:
    return current_app.config.get("PUBLIC_BASE_URL", "").rstrip("/") or "http://localhost:5000"


def new_link(conn: Connection, registration_id: int) -> str:
    """A fresh invite link for this seat (earlier unused links stop working)."""
    raw = secrets.token_urlsafe(32)
    conn.execute(text("""
        UPDATE action_tokens SET expires_at = now()
         WHERE purpose = 'account_invite' AND registration_id = :r AND expires_at > now()
    """), {"r": registration_id})
    days = conn.execute(text("SELECT value FROM app_settings WHERE key = 'invite_link_days'")).scalar()
    conn.execute(text("""
        INSERT INTO action_tokens (purpose, token_hash, registration_id, expires_at)
        VALUES ('account_invite', :h, :r, now() + make_interval(days => :d))
    """), {"h": _hash(raw), "r": registration_id, "d": int(days) if isinstance(days, int) and days > 0 else 60})
    return f"{base_url()}/invite/{raw}"


def lookup(raw: str) -> dict | None:
    """The invite behind a link, with the seat and who booked it; None if unknown or expired."""
    if not raw or len(raw) > 100:
        return None
    return db.fetch_one("""
        SELECT t.id AS token_id, t.used_at, r.id AS registration_id, r.public_id AS registration_public_id,
               r.attendee_first_name, r.attendee_last_name, r.attendee_email, r.status,
               o.buyer_first_name, o.buyer_last_name, o.buyer_company,
               s.starts_at, s.ends_at, s.time_zone, s.delivery_mode, cf.name AS course_name,
               si.name AS site_name, si.address_line1, si.city AS site_city, si.state_code AS site_state,
               si.postal_code,
               (SELECT id FROM user_accounts a WHERE a.email = r.attendee_email) AS account_id
          FROM action_tokens t
          JOIN registrations r ON r.id = t.registration_id
          JOIN orders o ON o.id = r.order_id
          JOIN sessions s ON s.id = r.session_id
          JOIN course_families cf ON cf.id = s.course_family_id
          JOIN sites si ON si.id = s.site_id
         WHERE t.purpose = 'account_invite' AND t.token_hash = :h AND t.expires_at > now()
    """, h=_hash(raw))


def claim(invite: dict, user_id: int) -> bool:
    """The signed-in account opened its own invite: its email is confirmed. False if the
    account's email isn't the invited one (nothing changes then)."""
    with db.transaction() as conn:
        ok = conn.execute(text("""
            UPDATE user_accounts SET email_verified_at = COALESCE(email_verified_at, now()),
                   person_id = COALESCE(person_id, (SELECT person_id FROM registrations WHERE id = :r))
             WHERE id = :u AND email = :e
            RETURNING id
        """), {"u": user_id, "e": invite["attendee_email"], "r": invite["registration_id"]}).first()
        if ok:
            conn.execute(text("UPDATE action_tokens SET used_at = COALESCE(used_at, now()) WHERE id = :t"),
                         {"t": invite["token_id"]})
            conn.execute(text("SELECT grant_student_role_if_booked(:u)"), {"u": user_id})
    return ok is not None


def status_for(registration_ids: list[int]) -> dict[int, dict]:
    """For the booker's dashboard: {registration id: {sent_at, sending, joined}} per seat booked for someone else."""
    if not registration_ids:
        return {}
    rows = db.fetch_all("""
        SELECT r.id,
               (SELECT max(n.sent_at) FROM notifications n
                 WHERE n.kind = 'attendee_invite' AND n.subject_type = 'registration' AND n.subject_id = r.id) AS sent_at,
               EXISTS (SELECT 1 FROM outbox_jobs j WHERE j.target = 'ghl' AND j.operation = 'send_invite'
                          AND j.entity_id = r.id AND j.status IN ('pending', 'running', 'failed')) AS sending,
               EXISTS (SELECT 1 FROM user_accounts a WHERE a.email = r.attendee_email
                          AND a.email_verified_at IS NOT NULL) AS joined
          FROM registrations r WHERE r.id = ANY(:ids)
    """, ids=registration_ids)
    return {r["id"]: r for r in rows}
