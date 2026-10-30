"""Instructor area: the classes this instructor teaches and their certifications.

Needs 'own.classes_view' (the instructor role has it). Only this account's own
instructor record is read; lead and helper instructors see the same page.
"""

from __future__ import annotations

from flask import Blueprint, render_template

from .. import areas, auth, db

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
    classes, certs = [], []
    if instructor:
        classes = db.fetch_all("""
            SELECT a.role, s.starts_at, s.ends_at, s.time_zone, cf.name AS course_name,
                   si.name AS site_name, m.name AS city_name, av.sold_seats, av.seat_limit, av.waitlist_count
              FROM assignments a
              JOIN sessions s ON s.id = a.session_id
              JOIN sites si ON si.id = s.site_id
              JOIN metros m ON m.id = s.metro_id
              JOIN course_families cf ON cf.id = s.course_family_id
              JOIN session_availability av ON av.session_id = s.id
             WHERE a.instructor_id = :iid AND a.status = 'assigned'
               AND s.status = 'scheduled' AND s.starts_at > now()
             ORDER BY s.starts_at
        """, iid=instructor["id"])
        certs = db.fetch_all("""
            SELECT cert_type, expires_on, (expires_on - current_date) AS days_left, verified_at
              FROM instructor_certs WHERE instructor_id = :iid ORDER BY expires_on
        """, iid=instructor["id"])
    return render_template("teach/home.html", instructor=instructor, classes=classes, certs=certs,
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
