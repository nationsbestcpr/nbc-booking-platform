"""Instructor area: the classes this instructor teaches, class day, and their certifications.

Needs 'own.classes_view' (the instructor role has it). Only this account's own
instructor record is read; lead and helper instructors see the same page and
can both mark attendance (hub 9d). Class day: the roster, check-in, Complete /
No Show (each mark records who made it), finishing the class, and the Red
Cross class list (app/attendance.py).
"""

from __future__ import annotations

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from .. import areas, attendance, auth, db, instructor_portal as portal

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
    classes, certs, recent, requests_ = [], [], [], []
    window = portal.confirm_days()
    if instructor:
        everything = attendance.recent_for_instructor(instructor["id"])
        classes = [c for c in everything if not c["past"] and c["status"] == "scheduled"]
        recent = [c for c in everything if c["past"]]
        certs = db.fetch_all("""
            SELECT cert_type, expires_on, (expires_on - current_date) AS days_left, verified_at
              FROM instructor_certs WHERE instructor_id = :iid ORDER BY expires_on
        """, iid=instructor["id"])
        requests_ = portal.my_requests(instructor["id"])
    to_confirm = [c for c in classes if c["assignment_status"] == "assigned" and not c["confirmed_at"]
                  and not c["notice"] and c["days_away"] <= window]
    return render_template("teach/home.html", instructor=instructor, classes=classes, certs=certs,
                           recent=recent, to_confirm=to_confirm, requests=requests_, window=window,
                           tabs=areas.tabs_for(user))


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
    students = attendance.roster(s["id"])
    signed = portal.signatures(s["id"])
    for r in students:
        r["signed"] = signed.get(r["id"])
    return render_template("teach/class.html", s=s, can=can, students=students, marks=attendance.MARKS,
                           mine=portal.assignment_for(s), window=portal.confirm_days(),
                           days_away=(s["local_date"] - portal.today_in(s["time_zone"])).days,
                           tabs=areas.tabs_for(auth.current_user()))


@bp.post("/classes/<public_id>/confirm")
@auth.permission_required("own.classes_view")
def class_confirm(public_id: str):
    s, _ = _my_class(public_id)
    try:
        portal.confirm(s)
        flash("Confirmed: NBC knows you'll be there. Thank you!", "ok")
    except portal.PortalError as exc:
        flash(str(exc), "error")
    return redirect(request.form.get("back") or url_for("teach.class_day", public_id=public_id))


@bp.route("/classes/<public_id>/cant-teach", methods=["GET", "POST"])
@auth.permission_required("own.class_release")
def class_cant_teach(public_id: str):
    """Tell NBC you can't teach a class. It stays yours until NBC finds someone and changes it in Airtable."""
    s, _ = _my_class(public_id)
    error = None
    if request.method == "POST":
        try:
            portal.cant_teach(s, request.form.get("reason", ""))
            flash("Sent. NBC will find another instructor and let you know; the class stays on your list until "
                  "they do.", "ok")
            return redirect(url_for("teach.class_day", public_id=public_id))
        except portal.PortalError as exc:
            error = str(exc)
    return render_template("teach/cant_teach.html", s=s, mine=portal.assignment_for(s), error=error,
                           reason=request.form.get("reason", ""), tabs=areas.tabs_for(auth.current_user())), \
        (400 if error else 200)


@bp.post("/requests/<int:request_id>/withdraw")
@auth.permission_required("own.classes_view")
def request_withdraw(request_id: int):
    try:
        portal.withdraw(request_id)
        flash("Withdrawn.", "ok")
    except portal.PortalError as exc:
        flash(str(exc), "error")
    return redirect(request.form.get("back") or url_for("teach.home"))


@bp.route("/availability", methods=["GET", "POST"])
@auth.permission_required("own.availability_manage")
def availability():
    """What Airtable has for this instructor, and a form to ask NBC for a change. A change that rules out a class
    they're assigned to asks, for each one, whether they'll still teach it or can't (then NBC is told)."""
    me = portal.my_instructor()
    if not me:
        return render_template("teach/availability.html", me=None, tabs=areas.tabs_for(auth.current_user()))
    form = {"days": me["available_days"] or [], "times": me["available_times"] or [],
            "classes_per_month": me["classes_per_month"] or "", "notice_needed": me["notice_needed"] or "",
            "locations_to_avoid": me["locations_to_avoid"] or "", "away_from": "", "away_to": "", "note": ""}
    clashing, error, decisions, reasons = [], None, {}, {}
    if request.method == "POST":
        form = {"days": request.form.getlist("days"), "times": request.form.getlist("times"),
                **{k: request.form.get(k, "") for k in ("classes_per_month", "notice_needed", "locations_to_avoid",
                                                        "away_from", "away_to", "note")}}
        decisions = {k[len("class_"):]: v for k, v in request.form.items() if k.startswith("class_")}
        reasons = {k[len("reason_"):]: v for k, v in request.form.items() if k.startswith("reason_")}
        try:
            rid, clashing = portal.request_availability(me["id"], form, decisions, reasons)
            if rid:
                flash("Sent to NBC. Your availability changes once the operations team updates it; you'll see it "
                      "here.", "ok")
                return redirect(url_for("teach.availability"))
            error = (f"This change clashes with {len(clashing)} class{'es' if len(clashing) != 1 else ''} you're "
                     "assigned to. For each one, say whether you'll still teach it, or that you can't (NBC is told "
                     "and finds someone else).")
        except portal.PortalError as exc:
            error = str(exc)
    requests_ = [r for r in portal.my_requests(me["id"]) if r["kind"] == "availability"]
    return render_template("teach/availability.html", me=me, form=form, clashing=clashing, error=error,
                           decisions=decisions, reasons=reasons, requests=requests_, days=portal.DAYS,
                           times=portal.TIMES, notice=portal.NOTICE, tabs=areas.tabs_for(auth.current_user())), \
        (400 if error else 200)


@bp.get("/classes/<public_id>/sign")
@auth.permission_required("own.attendance")
def class_sign_sheet(public_id: str):
    """The sign-in sheet: students tap their name and sign on the instructor's phone or tablet."""
    s, can = _my_class(public_id)
    students = [r for r in attendance.roster(s["id"]) if r["status"] in ("confirmed", "awaiting_payment", "completed")]
    signed = portal.signatures(s["id"])
    return render_template("teach/sign.html", s=s, can=can, students=students, signed=signed,
                           tabs=areas.tabs_for(auth.current_user()))


@bp.post("/classes/<public_id>/sign")
@auth.permission_required("own.attendance")
def class_sign(public_id: str):
    s, _ = _my_class(public_id)
    try:
        portal.sign(s, request.form.get("seat", ""), request.form.get("signed_name", ""),
                    request.form.get("signature", ""), request.remote_addr, request.user_agent.string)
        flash(f"Thank you, {request.form.get('signed_name', '').split(' ')[0]}! You're signed in.", "ok")
    except portal.PortalError as exc:
        flash(str(exc), "error")
    return redirect(url_for("teach.class_sign_sheet", public_id=public_id))


@bp.get("/classes/<public_id>/signed-roster")
@auth.permission_required("own.classes_view")
def class_signed_roster(public_id: str):
    """The signed roster, ready to print or save as PDF."""
    s, can = _my_class(public_id)
    students = attendance.roster(s["id"])
    signed = portal.signatures(s["id"])
    return render_template("teach/signed_roster.html", s=s, students=students, signed=signed)


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
