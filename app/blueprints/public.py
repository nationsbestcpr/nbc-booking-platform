"""Landing page: the classes that are on sale, for everyone.

Signed-in people also get a short greeting based on their role, worked out
from their account (nobody chooses a role). Book opens /book/<class> (book.py).

Only data safe for the public is selected here: never access codes, on-site
contacts or payment details (those live in *_private_details tables).
"""

from __future__ import annotations

from flask import Blueprint, render_template, request, url_for

from .. import auth, db

bp = Blueprint("public", __name__)


PER_PAGE = (5, 10, 25, 50)
NEAR_MILES = 75
COURSES = [("", "All courses"), ("FA_CPR_AED", "First Aid / CPR / AED"), ("BLS", "Basic Life Support (BLS)"),
           ("IT", "Instructor Training")]
WHEN = [("", "Any date"), ("7", "Next 7 days"), ("30", "Next 30 days"), ("90", "Next 3 months")]


def _coordinate(raw: str | None, limit: float) -> float | None:
    try:
        v = round(float(raw), 2)          # about 1 km: enough to find nearby classes, no more
    except (TypeError, ValueError):
        return None
    return v if -limit <= v <= limit else None


@bp.get("/")
def home():
    """Upcoming classes in every city, page by page (1, 2, 3 ... N), soonest first.

    With the visitor's permission the page asks the browser for their location
    and sends it back rounded to two decimals (about 1 km) in the address; then
    only classes within 75 miles are listed, soonest first, with the distance shown. Nothing about the location is saved. Without it,
    or with nothing that close, every upcoming class is listed. Filters: city,
    course, how far ahead, from a date; all in the address so a search can be shared."""
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
    per = request.args.get("per", 10, type=int)
    per = per if per in PER_PAGE else 10
    page = max(1, request.args.get("page", 1, type=int) or 1)
    lat, lng = _coordinate(request.args.get("lat"), 90), _coordinate(request.args.get("lng"), 180)
    here = lat is not None and lng is not None

    cities = db.fetch_all("""
        SELECT m.slug, m.name, m.state_code, count(s.id) AS classes
          FROM metros m
          JOIN sessions s ON s.metro_id = m.id
         WHERE m.is_active AND m.booking_system = 'engine'
           AND s.status = 'scheduled' AND s.is_published AND s.starts_at > now()
         GROUP BY m.id ORDER BY m.state_code, m.name
    """)
    chosen = next((c for c in cities if c["slug"] == f["city"]), None)
    if not chosen:
        f["city"] = ""

    where = """
          FROM sessions s
          JOIN metros m ON m.id = s.metro_id
          JOIN sites si ON si.id = s.site_id
          JOIN course_families cf ON cf.id = s.course_family_id
          JOIN session_availability a ON a.session_id = s.id
         WHERE s.status = 'scheduled' AND s.is_published AND s.starts_at > now()
           AND m.booking_system = 'engine' AND m.is_active
           AND (:city = '' OR m.slug = :city)
           AND (:course = '' OR cf.code = :course)
           AND (:days = 0 OR s.starts_at < now() + make_interval(days => :days))
           AND (CAST(:start AS date) IS NULL OR s.local_date >= CAST(:start AS date))
    """
    # Miles from the visitor (haversine); NULL when either side has no coordinates.
    distance = """3958.8 * 2 * asin(sqrt(
            power(sin(radians((si.latitude - CAST(:lat AS float)) / 2)), 2)
          + cos(radians(CAST(:lat AS float))) * cos(radians(si.latitude))
          * power(sin(radians((si.longitude - CAST(:lng AS float)) / 2)), 2)))"""
    params = {"city": f["city"], "course": f["course"], "days": int(f["when"] or 0),
              "start": start.isoformat() if start else None, "lat": lat, "lng": lng}

    near = False                 # showing only classes near the visitor
    near_none = False            # location given, nothing within NEAR_MILES
    if here and not f["city"]:
        near_total = db.fetch_value(f"SELECT count(*) {where} AND {distance} <= {NEAR_MILES}", **params)
        near, near_none = near_total > 0, near_total == 0
    near_clause = f" AND {distance} <= {NEAR_MILES}" if near else ""
    total = db.fetch_value("SELECT count(*) " + where + near_clause, **params)
    pages = max(1, -(-total // per))
    page = min(page, pages)
    classes = db.fetch_all(f"""
        SELECT s.id, s.public_id, s.slug, s.starts_at, s.ends_at, s.time_zone, s.delivery_mode, s.local_date,
               m.name AS city_name, m.state_code, m.slug AS city_slug,
               si.name AS site_name, si.city AS site_city, cf.name AS course_name, cf.code AS course_code,
               a.seats_left, a.seat_limit,
               {distance if here else "NULL"} AS miles,
               (SELECT json_agg(json_build_object('name', p.short_name, 'price', sp.price_cents)
                                ORDER BY sp.price_cents DESC)
                  FROM session_products sp JOIN products p ON p.id = sp.product_id
                 WHERE sp.session_id = s.id AND sp.is_offered) AS offers
        {where}{near_clause}
         ORDER BY s.starts_at, si.name
         LIMIT :per OFFSET :off
    """, per=per, off=(page - 1) * per, **params)

    def page_list() -> list[int | None]:
        """1 ... 4 5 6 ... 20 (None is a gap)."""
        keep = {1, pages, page - 1, page, page + 1} | ({2, 3} if page <= 3 else set()) | \
            ({pages - 1, pages - 2} if page >= pages - 2 else set())
        out: list[int | None] = []
        for n in sorted(k for k in keep if 1 <= k <= pages):
            if out and n - (out[-1] or 0) > 1:
                out.append(None)
            out.append(n)
        return out

    query = {k: v for k, v in {**f, "per": per if per != 10 else None,
                               "lat": lat if here else None, "lng": lng if here else None}.items() if v}

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

    return render_template("public/home.html", cities=cities, chosen=chosen, f=f, page=page, pages=pages,
                           page_list=page_list(), per=per, per_options=PER_PAGE, query=query, here=here,
                           near=near, near_none=near_none, near_miles=NEAR_MILES,
                           all_cities_url=url_for("public.home", **{k: v for k, v in query.items()
                                                                     if k not in ("lat", "lng")}) + "#classes",
                           classes=classes, total=total, courses=COURSES, whens=WHEN, my_classes=my_classes,
                           welcome=request.args.get("welcome") == "1")
