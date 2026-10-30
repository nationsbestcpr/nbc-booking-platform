"""Landing page: the classes that are on sale, for everyone.

Signed-in people also get a short greeting based on their role, worked out
from their account (nobody chooses a role). Book opens /book/<class> (book.py).

Only data safe for the public is selected here: never access codes, on-site
contacts or payment details (those live in *_private_details tables).
"""

from __future__ import annotations

from flask import Blueprint, render_template, request

from .. import auth, db

bp = Blueprint("public", __name__)


@bp.get("/")
def home():
    city = request.args.get("city", "")
    cities = db.fetch_all("""
        SELECT m.slug, m.name, m.state_code, count(s.id) AS classes
          FROM metros m
          JOIN sessions s ON s.metro_id = m.id
         WHERE m.is_active AND m.booking_system = 'engine'
           AND s.status = 'scheduled' AND s.is_published AND s.starts_at > now()
         GROUP BY m.id ORDER BY m.name
    """)
    classes = db.fetch_all("""
        SELECT s.id, s.public_id, s.slug, s.starts_at, s.ends_at, s.time_zone, s.delivery_mode,
               m.name AS city_name, m.state_code, m.slug AS city_slug,
               si.name AS site_name, cf.name AS course_name, cf.code AS course_code,
               a.seats_left, a.seat_limit,
               (SELECT json_agg(json_build_object('name', p.short_name, 'price', sp.price_cents)
                                ORDER BY sp.price_cents DESC)
                  FROM session_products sp JOIN products p ON p.id = sp.product_id
                 WHERE sp.session_id = s.id AND sp.is_offered) AS offers
          FROM sessions s
          JOIN metros m ON m.id = s.metro_id
          JOIN sites si ON si.id = s.site_id
          JOIN course_families cf ON cf.id = s.course_family_id
          JOIN session_availability a ON a.session_id = s.id
         WHERE s.status = 'scheduled' AND s.is_published AND s.starts_at > now()
           AND m.booking_system = 'engine' AND m.is_active
           AND (:city = '' OR m.slug = :city)
         ORDER BY s.starts_at, si.name
         LIMIT 60
    """, city=city)

    user = auth.current_user()
    my_classes = []
    if user and user.has_role("student"):
        my_classes = db.fetch_all("""
            SELECT s.starts_at, s.ends_at, s.time_zone, cf.name AS course_name, si.name AS site_name
              FROM registrations r
              JOIN orders o ON o.id = r.order_id
              JOIN sessions s ON s.id = r.session_id
              JOIN sites si ON si.id = s.site_id
              JOIN course_families cf ON cf.id = s.course_family_id
             WHERE (r.attendee_email = :email OR o.buyer_email = :email)
               AND r.status IN ('awaiting_payment', 'confirmed') AND s.starts_at > now()
             ORDER BY s.starts_at LIMIT 3
        """, email=user.email)

    return render_template("public/home.html", cities=cities, city=city, classes=classes,
                           my_classes=my_classes, welcome=request.args.get("welcome") == "1")
