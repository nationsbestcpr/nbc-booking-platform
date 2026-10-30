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
