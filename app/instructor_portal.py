"""The instructor portal's own actions (migration 0012): confirming a class, telling NBC you can't teach one,
asking for an availability change, and students signing the roster on class day.

Airtable keeps who teaches each class and each instructor's availability; the booking app never changes them
by itself. An instructor's "can't teach" notice and availability change are requests the NBC Ops Hub reads
through the API (/api/v1/instructor-requests), applies in Airtable, and closes. An availability change that
would rule out a class the instructor is already assigned to can't be sent until they say, for each such
class, whether they'll still teach it or can't (the database checks this: instructor_requests_rules).
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from . import attendance, audit, auth, db

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
TIMES = ("Morning", "Afternoon", "Evening")
NOTICE = ("Same week", "1 week", "2+ weeks")
WHY = {"away": "you'll be away that day", "day": "you no longer give that day of the week",
       "time": "you no longer give that time of day"}


class PortalError(ValueError):
    """Why an instructor's action can't be done, worded for them."""


def _db_message(exc: DBAPIError) -> str:
    return str(getattr(exc, "orig", exc)).split("\n")[0]


def my_instructor() -> dict | None:
    user = auth.current_user()
    return db.fetch_one("""
        SELECT i.id, i.first_name, i.full_name, i.status, i.available_days, i.available_times, i.classes_per_month,
               i.notice_needed, i.locations_to_avoid, i.availability_notes, i.availability_updated_on,
               i.travel_radius_miles
          FROM instructors i WHERE i.user_account_id = :u
    """, u=user.id)


def confirm_days() -> int:
    v = db.fetch_value("SELECT value FROM app_settings WHERE key = 'confirm_days_before'")
    return int(v) if isinstance(v, (int, float)) else 5


def assignment_for(s: dict) -> dict | None:
    """This instructor's live assignment on the class, with its confirmation and any open notice."""
    return db.fetch_one("""
        SELECT a.id, a.role, a.status, a.confirmed_at, a.instructor_id,
               (SELECT json_build_object('id', r.id, 'reason', r.reason, 'created_at', r.created_at)
                  FROM instructor_requests r WHERE r.assignment_id = a.id AND r.kind = 'cant_teach'
                   AND r.status = 'open' ORDER BY r.id DESC LIMIT 1) AS notice
          FROM assignments a JOIN instructors i ON i.id = a.instructor_id
         WHERE a.session_id = :s AND i.user_account_id = :u AND a.status IN ('assigned', 'completed')
         ORDER BY a.role LIMIT 1
    """, s=s["id"], u=auth.current_user().id)


# --- Confirming a class ----------------------------------------------------------------------

def confirm(s: dict) -> None:
    a = assignment_for(s)
    if not a or a["status"] != "assigned" or s["status"] != "scheduled":
        raise PortalError("You can only confirm a class you're assigned to that is still on.")
    if s["starts_at"] <= datetime.now(s["starts_at"].tzinfo):
        raise PortalError("This class has started.")
    if a["notice"]:
        raise PortalError("You told NBC you can't teach this class. Withdraw that first if you can teach it after all.")
    with db.transaction() as conn:
        conn.execute(text("""UPDATE assignments SET confirmed_at = now(), confirmed_by_user_id = :u
                              WHERE id = :a AND confirmed_at IS NULL"""), {"a": a["id"], "u": auth.current_user().id})
        audit.record(conn, "assignment.confirm", "assignment", a["id"], after={"session_id": s["id"]})


# --- "I can't teach this class" ----------------------------------------------------------------

def cant_teach(s: dict, reason: str) -> int:
    a = assignment_for(s)
    reason = (reason or "").strip()
    if not a or a["status"] != "assigned":
        raise PortalError("You can only send this for a class you're assigned to.")
    if a["notice"]:
        raise PortalError("NBC already has your notice for this class.")
    if len(reason) < 3:
        raise PortalError("Tell NBC why, in a few words (they'll find another instructor).")
    try:
        with db.transaction() as conn:
            rid = conn.execute(text("""
                INSERT INTO instructor_requests (instructor_id, kind, assignment_id, reason, created_by_user_id)
                VALUES (:i, 'cant_teach', :a, :why, :u) RETURNING id
            """), {"i": a["instructor_id"], "a": a["id"], "why": reason[:2000], "u": auth.current_user().id}).scalar_one()
            conn.execute(text("UPDATE assignments SET confirmed_at = NULL, confirmed_by_user_id = NULL WHERE id = :a"),
                         {"a": a["id"]})
            audit.record(conn, "instructor_request.cant_teach", "instructor_request", rid,
                         after={"session_id": s["id"], "reason": reason[:200]})
    except DBAPIError as exc:
        raise PortalError(_db_message(exc)) from None
    return rid


def withdraw(request_id: int) -> None:
    """The instructor takes back an open request (they can teach after all, or the change is no longer needed)."""
    with db.transaction() as conn:
        row = conn.execute(text("""
            UPDATE instructor_requests r SET status = 'withdrawn'
              FROM instructors i
             WHERE r.id = :r AND i.id = r.instructor_id AND i.user_account_id = :u AND r.status = 'open'
            RETURNING r.id, r.kind
        """), {"r": request_id, "u": auth.current_user().id}).mappings().first()
        if row is None:
            raise PortalError("That request is no longer open.")
        if row["kind"] == "availability":     # its "can't teach" notices go with it
            conn.execute(text("""UPDATE instructor_requests SET status = 'withdrawn'
                                  WHERE availability_request_id = :r AND status = 'open'"""), {"r": request_id})
        audit.record(conn, "instructor_request.withdraw", "instructor_request", request_id)


def my_requests(instructor_id: int) -> list[dict]:
    return db.fetch_all("""
        SELECT r.id, r.kind, r.status, r.reason, r.details, r.created_at, r.handled_at, r.handled_note,
               r.availability_request_id, s.public_id AS class_id, s.starts_at, s.time_zone, cf.name AS course_name,
               si.name AS site_name
          FROM instructor_requests r
          LEFT JOIN assignments a ON a.id = r.assignment_id
          LEFT JOIN sessions s ON s.id = a.session_id
          LEFT JOIN course_families cf ON cf.id = s.course_family_id
          LEFT JOIN sites si ON si.id = s.site_id
         WHERE r.instructor_id = :i AND (r.status = 'open' OR r.updated_at > now() - interval '30 days')
         ORDER BY (r.status <> 'open'), r.created_at DESC
    """, i=instructor_id)


# --- Availability ------------------------------------------------------------------------------

def _clean(form: dict) -> dict:
    days = [d for d in DAYS if d in form.get("days", [])]
    times = [t for t in TIMES if t in form.get("times", [])]
    if not days:
        raise PortalError("Tick at least one day you can teach.")
    if not times:
        raise PortalError("Tick at least one time of day you can teach.")
    per_month = (form.get("classes_per_month") or "").strip()
    if per_month and not re.fullmatch(r"\d{1,2}", per_month):
        raise PortalError("Classes per month is a number, like 4.")
    notice = form.get("notice_needed") or ""
    if notice and notice not in NOTICE:
        raise PortalError("Choose how much notice you need.")
    away_from, away_to = form.get("away_from") or "", form.get("away_to") or ""
    try:
        f = date.fromisoformat(away_from) if away_from else None
        t = date.fromisoformat(away_to) if away_to else None
    except ValueError:
        raise PortalError("Enter the dates you're away as dates.") from None
    if t and not f:
        raise PortalError("Enter the first day you're away.")
    if f and t and t < f:
        raise PortalError("The last day away is before the first.")
    return {"available_days": days, "available_times": times,
            "classes_per_month": int(per_month) if per_month else None, "notice_needed": notice or None,
            "locations_to_avoid": (form.get("locations_to_avoid") or "").strip()[:1000] or None,
            "away_from": f.isoformat() if f else None, "away_to": (t or f).isoformat() if f else None,
            "note": (form.get("note") or "").strip()[:2000] or None}


def conflicts(instructor_id: int, change: dict) -> list[dict]:
    """The assigned upcoming classes this change would rule out (the database's own rule)."""
    rows = db.fetch_all("""
        SELECT c.assignment_id, c.why, s.public_id, s.starts_at, s.ends_at, s.time_zone, cf.name AS course_name,
               si.name AS site_name, a.role
          FROM instructor_availability_conflicts(:i, CAST(:d AS text[]), CAST(:t AS text[]),
                                                 CAST(:f AS date), CAST(:to AS date)) c
          JOIN sessions s ON s.id = c.session_id
          JOIN assignments a ON a.id = c.assignment_id
          JOIN course_families cf ON cf.id = s.course_family_id
          JOIN sites si ON si.id = s.site_id
         ORDER BY s.starts_at
    """, i=instructor_id, d=change["available_days"], t=change["available_times"], f=change["away_from"],
                       to=change["away_to"])
    for r in rows:
        r["why_text"] = WHY.get(r["why"], r["why"])
    return rows


def request_availability(instructor_id: int, form: dict, decisions: dict[str, str], reasons: dict[str, str]
                         ) -> tuple[int | None, list[dict]]:
    """Send the change. Returns (request id, []) when sent, or (None, clashing classes) when the instructor must
    first say, for each clashing class, whether they'll still teach it."""
    change = _clean(form)
    clashing = conflicts(instructor_id, change)
    pending = [c for c in clashing if decisions.get(str(c["assignment_id"])) not in ("keep", "cant_teach")]
    if pending:
        return None, clashing
    for c in clashing:
        key = str(c["assignment_id"])
        if decisions[key] == "cant_teach" and len((reasons.get(key) or "").strip()) < 3:
            raise PortalError(f"Say why you can't teach {c['course_name']} at {c['site_name']}, in a few words.")
    details = {**change, "classes": {str(c["assignment_id"]): decisions[str(c["assignment_id"])] for c in clashing}}
    user = auth.current_user()
    try:
        with db.transaction() as conn:
            rid = conn.execute(text("""
                INSERT INTO instructor_requests (instructor_id, kind, details, created_by_user_id)
                VALUES (:i, 'availability', CAST(:d AS jsonb), :u) RETURNING id
            """), {"i": instructor_id, "d": json.dumps(details), "u": user.id}).scalar_one()
            for c in clashing:
                key = str(c["assignment_id"])
                if decisions[key] != "cant_teach":
                    continue
                if conn.execute(text("""SELECT 1 FROM instructor_requests WHERE assignment_id = :a
                                          AND kind = 'cant_teach' AND status = 'open'"""), {"a": c["assignment_id"]}).first():
                    continue
                conn.execute(text("""
                    INSERT INTO instructor_requests (instructor_id, kind, assignment_id, availability_request_id,
                                                     reason, created_by_user_id)
                    VALUES (:i, 'cant_teach', :a, :r, :why, :u)
                """), {"i": instructor_id, "a": c["assignment_id"], "r": rid, "why": reasons[key].strip()[:2000],
                       "u": user.id})
                conn.execute(text("UPDATE assignments SET confirmed_at = NULL, confirmed_by_user_id = NULL WHERE id = :a"),
                             {"a": c["assignment_id"]})
            audit.record(conn, "instructor_request.availability", "instructor_request", rid,
                         after={"clashing_classes": len(clashing),
                                "cant_teach": sum(1 for c in clashing if decisions[str(c["assignment_id"])] == "cant_teach")})
    except DBAPIError as exc:
        raise PortalError(_db_message(exc)) from None
    return rid, []


# --- Students sign the roster ---------------------------------------------------------------------

PATH = re.compile(r"^M[0-9MLml .,-]+$")


def signatures(session_id: int) -> dict[int, dict]:
    return {r["registration_id"]: r for r in db.fetch_all("""
        SELECT registration_id, signed_name, signature_path, signed_at FROM roster_signatures WHERE session_id = :s
    """, s=session_id)}


def sign(s: dict, registration_public_id: str, signed_name: str, path: str, ip: str | None, agent: str | None) -> None:
    """A student signs the roster on the instructor's device. Signing also checks them in."""
    can = attendance.access(s)
    if not can["class_day"]:
        raise PortalError("Students sign the roster on class day.")
    if not can["can_mark"]:
        raise PortalError("The roster is closed for this class.")
    signed_name = " ".join((signed_name or "").split())
    if len(signed_name) < 2:
        raise PortalError("Type your full name.")
    path = (path or "").strip()
    if not PATH.match(path) or len(path) > 100_000 or path.count("L") + path.count("l") < 3:
        raise PortalError("Sign in the box with your finger or a stylus.")
    by_assignment = can["assignment"]["id"] if (can["assignment"] and not can["office"]) else None
    with db.transaction() as conn:
        reg = conn.execute(text("""
            SELECT id, status, checked_in_at FROM registrations WHERE public_id = CAST(:p AS uuid) AND session_id = :s
        """), {"p": registration_public_id, "s": s["id"]}).mappings().first()
        if reg is None:
            raise PortalError("That student isn't in this class.")
        try:
            with conn.begin_nested():
                conn.execute(text("""
                    INSERT INTO roster_signatures (registration_id, session_id, signed_name, signature_path,
                                                   collected_by_assignment_id, collected_by_user_id, ip, user_agent)
                    VALUES (:r, :s, :n, :p, :a, :u, CAST(:ip AS inet), :ua)
                    ON CONFLICT (registration_id) DO UPDATE SET signed_name = EXCLUDED.signed_name,
                           signature_path = EXCLUDED.signature_path, signed_at = now(),
                           collected_by_assignment_id = EXCLUDED.collected_by_assignment_id,
                           collected_by_user_id = EXCLUDED.collected_by_user_id, ip = EXCLUDED.ip,
                           user_agent = EXCLUDED.user_agent
                """), {"r": reg["id"], "s": s["id"], "n": signed_name[:200], "p": path, "a": by_assignment,
                       "u": None if by_assignment else auth.current_user().id, "ip": ip, "ua": (agent or "")[:300]})
        except DBAPIError as exc:
            raise PortalError(_db_message(exc)) from None
        if reg["checked_in_at"] is None:
            conn.execute(text("""
                INSERT INTO attendance_marks (registration_id, mark, marked_by_assignment_id, marked_by_user_id)
                VALUES (:r, 'checked_in', :a, :u)
            """), {"r": reg["id"], "a": by_assignment, "u": None if by_assignment else auth.current_user().id})
            conn.execute(text("UPDATE registrations SET checked_in_at = now() WHERE id = :r"), {"r": reg["id"]})
        audit.record(conn, "roster.sign", "registration", reg["id"], after={"session_id": s["id"]})


def today_in(tz: str) -> date:
    return datetime.now(ZoneInfo(tz)).date()
