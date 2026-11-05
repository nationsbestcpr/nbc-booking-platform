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


PAGE_SIZE = 25
COURSES = [("", "All courses"), ("FA_CPR_AED", "First Aid / CPR / AED"), ("BLS", "Basic Life Support (BLS)"),
           ("IT", "Instructor Training")]
WHEN = [("", "Any date"), ("7", "Next 7 days"), ("30", "Next 30 days"), ("90", "Next 3 months")]


@bp.get("/")
def home():
    """Class finder: pick a city (grouped by state), then classes there by day, 25 at a time.
    Filters: course, how far ahead, from a date. All in the address, so a search can be shared."""
    from datetime import date
    f = {"city": request.args.get("city", ""), "course": request.args.get("course", ""),
         "when": request.args.get("when", ""), "date": request.args.get("date", "")}
    if f["course"] not in dict(COURSES):
        f["course"] = ""
    if f["when"] not in dict(WHEN):
        f["when"] = ""
    try:
        start = date.fromisoformat(f["date"]) if f["date"] else None
    except ValueError:
        start, f["date"] = None, ""
    page = max(1, min(request.args.get("page", 1, type=int) or 1, 40))

    cities = db.fetch_all("""
        SELECT m.slug, m.name, m.state_code, count(s.id) AS classes
          FROM metros m
          JOIN sessions s ON s.metro_id = m.id
         WHERE m.is_active AND m.booking_system = 'engine'
           AND s.status = 'scheduled' AND s.is_published AND s.starts_at > now()
         GROUP BY m.id ORDER BY m.state_code, m.name
    """)
    if len(cities) == 1 and not f["city"]:
        f["city"] = cities[0]["slug"]
    states: dict[str, list] = {}
    for c in cities:
        states.setdefault(c["state_code"], []).append(c)
    chosen = next((c for c in cities if c["slug"] == f["city"]), None)
    if f["city"] not in ("", "all") and not chosen:
        f["city"] = ""

    classes, total = [], 0
    if f["city"]:
        where = """
              FROM sessions s
              JOIN metros m ON m.id = s.metro_id
              JOIN sites si ON si.id = s.site_id
              JOIN course_families cf ON cf.id = s.course_family_id
              JOIN session_availability a ON a.session_id = s.id
             WHERE s.status = 'scheduled' AND s.is_published AND s.starts_at > now()
               AND m.booking_system = 'engine' AND m.is_active
               AND (:city = 'all' OR m.slug = :city)
               AND (:course = '' OR cf.code = :course)
               AND (:days = 0 OR s.starts_at < now() + make_interval(days => :days))
               AND (CAST(:start AS date) IS NULL OR s.local_date >= CAST(:start AS date))
        """
        params = {"city": f["city"], "course": f["course"], "days": int(f["when"] or 0),
                  "start": start.isoformat() if start else None}
        total = db.fetch_value("SELECT count(*) " + where, **params)
        classes = db.fetch_all("""
            SELECT s.id, s.public_id, s.slug, s.starts_at, s.ends_at, s.time_zone, s.delivery_mode, s.local_date,
                   m.name AS city_name, m.state_code, m.slug AS city_slug,
                   si.name AS site_name, si.city AS site_city, cf.name AS course_name, cf.code AS course_code,
                   a.seats_left, a.seat_limit,
                   (SELECT json_agg(json_build_object('name', p.short_name, 'price', sp.price_cents)
                                    ORDER BY sp.price_cents DESC)
                      FROM session_products sp JOIN products p ON p.id = sp.product_id
                     WHERE sp.session_id = s.id AND sp.is_offered) AS offers
        """ + where + """
             ORDER BY s.starts_at, si.name
             LIMIT :n
        """, n=PAGE_SIZE * page, **params)

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

    return render_template("public/home.html", cities=cities, states=states, chosen=chosen, f=f, page=page,
                           classes=classes, total=total, courses=COURSES, whens=WHEN, my_classes=my_classes,
                           welcome=request.args.get("welcome") == "1")
