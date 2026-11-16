"""Instructor area: the classes this instructor teaches, class day, and their certifications.

Needs 'own.classes_view' (the instructor role has it). Only this account's own
instructor record is read; lead and helper instructors see the same page and
can both mark attendance (hub 9d). Class day: the roster, check-in, Complete /
No Show (each mark records who made it), finishing the class, and the Red
Cross class list (app/attendance.py).
"""

from __future__ import annotations

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from .. import areas, attendance, auth, db

bp = Blueprint("teach", __name__, url_prefix="/teach")


@bp.get("")
@auth.permission_required("own.classes_view")
def home():
    user = auth.current_user()
    instructor = db.fetch_one("""
        SELECT i.id, i.first_name, i.status, hm.name AS hub_city
          FROM instructors i LEFT JOIN metros hm ON hm.id = i.hub_metro_id
         WHERE i.user_account_id = :id
    """, id=user.id)
    classes, certs, recent = [], [], []
    if instructor:
        classes = [c for c in attendance.recent_for_instructor(instructor["id"])
                   if not c["past"] and c["status"] == "scheduled"]
        recent = [c for c in attendance.recent_for_instructor(instructor["id"]) if c["past"]]
        certs = db.fetch_all("""
            SELECT cert_type, expires_on, (expires_on - current_date) AS days_left, verified_at
              FROM instructor_certs WHERE instructor_id = :iid ORDER BY expires_on
        """, iid=instructor["id"])
    return render_template("teach/home.html", instructor=instructor, classes=classes, certs=certs,
                           recent=recent, tabs=areas.tabs_for(user))


def _my_instructor_id() -> int | None:
    return db.fetch_value("SELECT id FROM instructors WHERE user_account_id = :id", id=auth.current_user().id)


@bp.get("/certifications")
@auth.permission_required("own.certs_view")
def certifications():
    iid = _my_instructor_id()
    certs = db.fetch_all("""
        SELECT c.cert_type, c.cert_number, c.issued_on, c.expires_on, (c.expires_on - current_date) AS days_left,
               c.verified_at, v.display_name AS verified_by
          FROM instructor_certs c LEFT JOIN user_accounts v ON v.id = c.verified_by_user_id
         WHERE c.instructor_id = :iid ORDER BY c.expires_on
    """, iid=iid) if iid else []
    return render_template("teach/certifications.html", linked=iid is not None, certs=certs,
                           tabs=areas.tabs_for(auth.current_user()))


@bp.get("/pay")
@auth.permission_required("own.pay_view")
def pay():
    iid = _my_instructor_id()
    rows = db.fetch_all("""
        SELECT p.role, p.amount_cents, p.status, p.exported_at, p.created_at,
               s.starts_at, s.time_zone, cf.name AS course_name, si.name AS site_name
          FROM pay_records p
          JOIN sessions s ON s.id = p.session_id
          JOIN sites si ON si.id = s.site_id
          JOIN course_families cf ON cf.id = s.course_family_id
         WHERE p.instructor_id = :iid
         ORDER BY s.starts_at DESC
    """, iid=iid) if iid else []
    totals = {}
    for r in rows:
        totals[r["status"]] = totals.get(r["status"], 0) + r["amount_cents"]
    return render_template("teach/pay.html", linked=iid is not None, rows=rows, totals=totals,
                           tabs=areas.tabs_for(auth.current_user()))


# --- Class day ---------------------------------------------------------------------------

def _my_class(public_id: str) -> tuple[dict, dict]:
    """The class, if this instructor teaches it (lead or helper)."""
    s = attendance.session_by_public_id(public_id)
    if not s:
        abort(404)
    can = attendance.access(s)
    if can["assignment"] is None:
        abort(404)
    return s, can


@bp.get("/classes/<public_id>")
@auth.permission_required("own.classes_view")
def class_day(public_id: str):
    s, can = _my_class(public_id)
    return render_template("teach/class.html", s=s, can=can, students=attendance.roster(s["id"]),
                           marks=attendance.MARKS, tabs=areas.tabs_for(auth.current_user()))


@bp.post("/classes/<public_id>/attendance")
@auth.permission_required("own.attendance")
def class_mark(public_id: str):
    s, _ = _my_class(public_id)
    try:
        attendance.mark(s, request.form.get("seat", ""), request.form.get("mark", ""))
    except attendance.AttendanceError as exc:
        flash(str(exc), "error")
    return redirect(url_for("teach.class_day", public_id=public_id) + "#roster")


@bp.post("/classes/<public_id>/finish")
@auth.permission_required("own.attendance")
def class_finish(public_id: str):
    s, _ = _my_class(public_id)
    try:
        done = attendance.finish(s)
        flash("Class finished. Thank you!" + (" Your pay record is under Pay." if done["pay_records"] else
                                              " Your pay record is made once the operations team sets the rate."),
              "ok")
    except attendance.AttendanceError as exc:
        flash(str(exc), "error")
    return redirect(url_for("teach.class_day", public_id=public_id))


@bp.get("/classes/<public_id>/class-list.csv")
@auth.permission_required("own.roster_export")
def class_export(public_id: str):
    s, _ = _my_class(public_id)
    try:
        body = attendance.export_csv(s)
    except attendance.AttendanceError:
        abort(403)
    name = f"class-list-{s['local_date'].isoformat()}-{s['course_code'].lower()}.csv"
    return Response(body, mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})
