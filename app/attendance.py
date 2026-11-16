"""Class day (launch hub 9 step 9, 9d): the live roster, check-in, Complete / No Show.

Either instructor on the class (lead or helper) can check students in and mark
them Complete or No Show; each mark records who made it (attendance_marks,
append-only). Office staff with 'booking.attendance' can mark any class in
their cities, at any time. Instructors can mark from class day until
'attendance_instructor_days' after it, and not after the class is finished.

A Complete / No Show mark moves the student's GHL opportunity to that stage
(and "Clear" moves it back to Pending). "Finish the class" closes it once
every student has a result, and creates a pay record for each instructor at
their rate (hub 9d: two pay records when a class with a helper is completed).
The roster export is the class list for the Red Cross Learning Center.

Open (hub 9d): which contact details instructors see on the roster
(Jennifer). Until decided, instructors see names, the course bought and the
status, not emails or phones; the Red Cross export has what RCLC needs.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from . import audit, auth, db

MARKS = {"checked_in": "Checked in", "complete": "Complete", "no_show": "No show", "cleared": "Cleared"}
MARKABLE = ("confirmed", "awaiting_payment", "completed", "no_show")


class AttendanceError(ValueError):
    """Why a mark can't be made, worded for the person marking."""


def session_by_public_id(public_id: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", public_id or ""):
        return None
    return db.fetch_one("""
        SELECT s.id, s.public_id, s.metro_id, s.status, s.starts_at, s.ends_at, s.time_zone, s.local_date,
               s.delivery_mode, s.roster_exported_at, s.airtable_lead_instructor, s.airtable_co_instructor,
               s.cancelled_at, s.cancel_reason, s.course_family_id,
               cf.name AS course_name, cf.code AS course_code, si.name AS site_name, si.address_line1,
               si.city AS site_city, si.state_code AS site_state, m.name AS city_name, m.slug AS city_slug,
               av.sold_seats, av.seat_limit, av.seats_left, av.waitlist_count, av.held_seats
          FROM sessions s
          JOIN course_families cf ON cf.id = s.course_family_id
          JOIN sites si ON si.id = s.site_id
          JOIN metros m ON m.id = s.metro_id
          JOIN session_availability av ON av.session_id = s.id
         WHERE s.public_id = CAST(:p AS uuid)
    """, p=public_id)


def access(s: dict) -> dict:
    """What this person may do on this class's roster."""
    user = auth.current_user()
    office = user.can("booking.attendance", s["metro_id"])
    assignment = db.fetch_one("""
        SELECT a.id, a.role FROM assignments a JOIN instructors i ON i.id = a.instructor_id
         WHERE a.session_id = :s AND i.user_account_id = :u AND a.status IN ('assigned', 'completed')
         ORDER BY a.role LIMIT 1
    """, s=s["id"], u=user.id) if user.has_permission("own.attendance") else None
    today = datetime.now(ZoneInfo(s["time_zone"])).date()
    days = db.fetch_value("SELECT value FROM app_settings WHERE key = 'attendance_instructor_days'")
    days = int(days) if isinstance(days, (int, float)) else 7
    class_day = s["local_date"] <= today
    instructor_window = class_day and today <= s["local_date"] + timedelta(days=days) and s["status"] == "scheduled"
    can_mark = s["status"] != "cancelled" and class_day and (office or (assignment is not None and instructor_window))
    return {"office": office, "assignment": assignment, "class_day": class_day, "can_mark": can_mark,
            "can_finish": can_mark and s["status"] == "scheduled",
            "can_export": user.can("class.roster_export", s["metro_id"]) or
            (assignment is not None and user.has_permission("own.roster_export")),
            "contacts": user.can("student.view", s["metro_id"]), "opens_on": s["local_date"]}


def roster(session_id: int) -> list[dict]:
    return db.fetch_all("""
        SELECT r.id, r.public_id, r.status, r.attendee_first_name, r.attendee_last_name, r.attendee_email,
               r.attendee_phone_e164, r.checked_in_at, r.completed_at, p.short_name AS product_name,
               o.channel, o.status AS order_status,
               (SELECT json_build_object('mark', am.mark, 'at', am.marked_at,
                                         'by', COALESCE(ua.display_name, iu.display_name))
                  FROM attendance_marks am
                  LEFT JOIN user_accounts ua ON ua.id = am.marked_by_user_id
                  LEFT JOIN assignments aa ON aa.id = am.marked_by_assignment_id
                  LEFT JOIN instructors ii ON ii.id = aa.instructor_id
                  LEFT JOIN user_accounts iu ON iu.id = ii.user_account_id
                 WHERE am.registration_id = r.id ORDER BY am.marked_at DESC, am.id DESC LIMIT 1) AS last_mark
          FROM registrations r JOIN products p ON p.id = r.product_id JOIN orders o ON o.id = r.order_id
         WHERE r.session_id = :s AND r.status IN ('confirmed', 'awaiting_payment', 'completed', 'no_show', 'displaced')
         ORDER BY lower(r.attendee_last_name), lower(r.attendee_first_name), r.id
    """, s=session_id)


def mark(s: dict, registration_public_id: str, what: str) -> dict:
    """Record one mark. Returns the student's new status."""
    if what not in MARKS:
        raise AttendanceError("Choose Check in, Complete, No show or Clear.")
    can = access(s)
    if not can["can_mark"]:
        if not can["class_day"]:
            raise AttendanceError("Attendance opens on class day.")
        raise AttendanceError("You can't mark attendance for this class.")
    user = auth.current_user()
    with db.transaction() as conn:
        r = conn.execute(text("""
            SELECT r.id, r.status, o.status AS order_status FROM registrations r JOIN orders o ON o.id = r.order_id
             WHERE r.public_id = CAST(:p AS uuid) AND r.session_id = :s FOR NO KEY UPDATE OF r
        """), {"p": registration_public_id, "s": s["id"]}).mappings().first()
        if r is None:
            raise AttendanceError("That student isn't in this class.")
        if r["status"] not in MARKABLE:
            raise AttendanceError("This seat isn't booked, so it can't be marked.")
        by_assignment = can["assignment"]["id"] if (can["assignment"] and not can["office"]) else None
        conn.execute(text("""
            INSERT INTO attendance_marks (registration_id, mark, marked_by_assignment_id, marked_by_user_id)
            VALUES (:r, :m, :a, :u)
        """), {"r": r["id"], "m": what, "a": by_assignment, "u": None if by_assignment else user.id})
        booked = "awaiting_payment" if r["order_status"] == "awaiting_payment" else "confirmed"
        new = {"checked_in": r["status"], "complete": "completed", "no_show": "no_show", "cleared": booked}[what]
        conn.execute(text("""
            UPDATE registrations SET status = :st,
                   checked_in_at = CASE WHEN :m = 'checked_in' THEN COALESCE(checked_in_at, now())
                                        WHEN :m = 'cleared' THEN NULL ELSE checked_in_at END,
                   completed_at = CASE WHEN :st = 'completed' THEN now() ELSE NULL END,
                   confirmed_at = CASE WHEN :st IN ('completed', 'no_show') THEN COALESCE(confirmed_at, now())
                                       ELSE confirmed_at END
             WHERE id = :r
        """), {"st": new, "m": what, "r": r["id"]})
        if new != r["status"]:
            from .integrations.outbox import enqueue
            enqueue(conn, "ghl", "sync_registration", key=f"ghl-registration-{r['id']}-{what}-{uuid.uuid4().hex[:8]}",
                    entity_type="registration", entity_id=r["id"], payload={"registration_id": r["id"]})
        audit.record(conn, f"attendance.{what}", "registration", r["id"], before={"status": r["status"]},
                     after={"status": new})
    return {"status": new}


def finish(s: dict) -> dict:
    """Close the class once every student has a result; one pay record per instructor at their rate."""
    can = access(s)
    if not can["can_finish"]:
        raise AttendanceError("You can't finish this class.")
    with db.transaction() as conn:
        conn.execute(text("SELECT 1 FROM sessions WHERE id = :s FOR NO KEY UPDATE"), {"s": s["id"]})
        left = conn.execute(text("""
            SELECT count(*) FROM registrations WHERE session_id = :s AND status IN ('confirmed', 'awaiting_payment')
        """), {"s": s["id"]}).scalar()
        if left:
            raise AttendanceError(f"{left} student{'s' if left != 1 else ''} still need{'' if left != 1 else 's'} "
                                  "Complete or No show.")
        conn.execute(text("UPDATE sessions SET status = 'completed' WHERE id = :s"), {"s": s["id"]})
        made, missing = 0, []
        for a in conn.execute(text("""
            UPDATE assignments SET status = 'completed' WHERE session_id = :s AND status = 'assigned'
            RETURNING id, instructor_id, role, rate_cents
        """), {"s": s["id"]}).mappings().all():
            rate = a["rate_cents"]
            if rate is None:
                rate = conn.execute(text("""
                    SELECT rate_cents FROM rate_card WHERE course_family_id = :f AND role = :r AND effective @> :d
                """), {"f": s["course_family_id"], "r": a["role"], "d": s["local_date"]}).scalar()
            if rate is None:
                missing.append(a["role"])
                continue
            conn.execute(text("""
                INSERT INTO pay_records (assignment_id, instructor_id, session_id, role, amount_cents)
                VALUES (:a, :i, :s, :r, :amt) ON CONFLICT (assignment_id) DO NOTHING
            """), {"a": a["id"], "i": a["instructor_id"], "s": s["id"], "r": a["role"], "amt": rate})
            made += 1
        audit.record(conn, "class.finish", "session", s["id"], after={"pay_records": made, "missing_rates": missing})
    return {"pay_records": made, "missing_rates": missing}


def export_csv(s: dict) -> str:
    """The class list for the Red Cross Learning Center: everyone with a result, and what it was."""
    if not access(s)["can_export"]:
        raise AttendanceError("You can't export this class list.")
    rows = db.fetch_all("""
        SELECT r.attendee_first_name, r.attendee_last_name, r.attendee_email, r.attendee_phone_e164, r.status,
               p.name AS product_name
          FROM registrations r JOIN products p ON p.id = r.product_id
         WHERE r.session_id = :s AND r.status IN ('completed', 'no_show', 'confirmed', 'awaiting_payment')
         ORDER BY lower(r.attendee_last_name), lower(r.attendee_first_name)
    """, s=s["id"])
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["First Name", "Last Name", "Email", "Phone", "Course", "Result", "Class Date"])
    result = {"completed": "Successful", "no_show": "No Show", "confirmed": "Not marked",
              "awaiting_payment": "Not marked"}
    for r in rows:
        w.writerow([r["attendee_first_name"], r["attendee_last_name"], r["attendee_email"],
                    r["attendee_phone_e164"] or "", r["product_name"], result[r["status"]],
                    s["local_date"].strftime("%m/%d/%Y")])
    with db.transaction() as conn:
        conn.execute(text("UPDATE sessions SET roster_exported_at = now() WHERE id = :s"), {"s": s["id"]})
        audit.record(conn, "class.roster_export", "session", s["id"], after={"rows": len(rows)})
    return out.getvalue()


def recent_for_instructor(instructor_id: int) -> list[dict]:
    """This instructor's classes from a week ago onwards, with whether attendance is open."""
    rows = db.fetch_all("""
        SELECT a.role, s.public_id, s.starts_at, s.ends_at, s.time_zone, s.local_date, s.status, cf.name AS course_name,
               si.name AS site_name, m.name AS city_name, av.sold_seats, av.seat_limit, av.waitlist_count,
               (SELECT count(*) FROM registrations r WHERE r.session_id = s.id
                   AND r.status IN ('confirmed', 'awaiting_payment')) AS unmarked
          FROM assignments a
          JOIN sessions s ON s.id = a.session_id
          JOIN sites si ON si.id = s.site_id
          JOIN metros m ON m.id = s.metro_id
          JOIN course_families cf ON cf.id = s.course_family_id
          JOIN session_availability av ON av.session_id = s.id
         WHERE a.instructor_id = :iid AND a.status IN ('assigned', 'completed')
           AND s.status <> 'cancelled' AND s.starts_at > now() - interval '8 days'
         ORDER BY s.starts_at
    """, iid=instructor_id)
    for r in rows:
        today = datetime.now(ZoneInfo(r["time_zone"])).date()
        r["today"] = r["local_date"] == today
        r["past"] = r["local_date"] < today
    return rows
