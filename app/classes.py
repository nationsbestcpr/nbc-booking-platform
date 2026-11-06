"""Finding classes on sale, and the classes an account has booked.

One search for every page that lists classes (the public home page and the
customer dashboard), so they filter, page and sort the same way:

    f = classes.filters_from(request.args)
    found = classes.search(f)
    render_template(..., found=found)      # with the macros in _classes.html

Only data safe for the public is selected (never access codes, on-site
contacts or payment details, which live in *_private_details tables).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

from . import db

PER_PAGE = (5, 10, 25, 50)
DEFAULT_PER = 10
NEAR_MILES = 75
COURSES = [("", "All courses"), ("FA_CPR_AED", "First Aid / CPR / AED"), ("BLS", "Basic Life Support (BLS)"),
           ("IT", "Instructor Training")]
WHEN = [("", "Any date"), ("7", "Next 7 days"), ("30", "Next 30 days"), ("90", "Next 3 months")]

# What a customer may book online: published, scheduled, in the future, in a city booked here.
_ON_SALE = """s.status = 'scheduled' AND s.is_published AND s.starts_at > now()
           AND m.booking_system = 'engine' AND m.is_active"""

# Miles from the visitor (haversine); NULL when either side has no coordinates.
_MILES = """3958.8 * 2 * asin(sqrt(
            power(sin(radians((si.latitude - CAST(:lat AS float)) / 2)), 2)
          + cos(radians(CAST(:lat AS float))) * cos(radians(si.latitude))
          * power(sin(radians((si.longitude - CAST(:lng AS float)) / 2)), 2)))"""


@dataclass
class Filters:
    city: str = ""            # metro slug
    course: str = ""          # course family code
    when: str = ""            # days ahead ("7", "30", "90") or ""
    date: str = ""            # from this local date (YYYY-MM-DD)
    per: int = DEFAULT_PER
    page: int = 1
    lat: float | None = None  # rounded to 2 decimals (about 1 km); never stored
    lng: float | None = None

    @property
    def here(self) -> bool:
        return self.lat is not None and self.lng is not None

    def query(self) -> dict[str, Any]:
        """The filters as address parameters (defaults left out), for links and the pager."""
        q = {"city": self.city, "course": self.course, "when": self.when, "date": self.date,
             "per": self.per if self.per != DEFAULT_PER else None,
             "lat": self.lat if self.here else None, "lng": self.lng if self.here else None}
        return {k: v for k, v in q.items() if v not in (None, "")}


def _coordinate(raw: Any, limit: float) -> float | None:
    try:
        v = round(float(raw), 2)
    except (TypeError, ValueError):
        return None
    return v if -limit <= v <= limit else None


def filters_from(args: Mapping[str, str]) -> Filters:
    """Read and clean the filters from a request's query string (bad values are dropped)."""
    f = Filters(city=(args.get("city") or "")[:80], course=args.get("course") or "",
                when=args.get("when") or "", date=args.get("date") or "")
    if f.course not in dict(COURSES):
        f.course = ""
    if f.when not in dict(WHEN):
        f.when = ""
    try:
        date.fromisoformat(f.date) if f.date else None
    except ValueError:
        f.date = ""
    try:
        f.per = int(args.get("per") or DEFAULT_PER)
    except ValueError:
        f.per = DEFAULT_PER
    if f.per not in PER_PAGE:
        f.per = DEFAULT_PER
    try:
        f.page = max(1, int(args.get("page") or 1))
    except ValueError:
        f.page = 1
    f.lat, f.lng = _coordinate(args.get("lat"), 90), _coordinate(args.get("lng"), 180)
    if not f.here:
        f.lat = f.lng = None
    return f


def cities() -> list[dict]:
    """Cities with at least one class on sale, for the city filter."""
    return db.fetch_all(f"""
        SELECT m.slug, m.name, m.state_code, count(s.id) AS classes
          FROM metros m
          JOIN sessions s ON s.metro_id = m.id
         WHERE {_ON_SALE}
         GROUP BY m.id ORDER BY m.state_code, m.name
    """)


def page_list(page: int, pages: int) -> list[int | None]:
    """1 … 4 5 6 … 20 (None is a gap)."""
    keep = {1, pages, page - 1, page, page + 1} | ({2, 3} if page <= 3 else set()) | \
        ({pages - 1, pages - 2} if page >= pages - 2 else set())
    out: list[int | None] = []
    for n in sorted(k for k in keep if 1 <= k <= pages):
        if out and n - (out[-1] or 0) > 1:
            out.append(None)
        out.append(n)
    return out


def search(f: Filters) -> dict[str, Any]:
    """Classes on sale matching the filters, one page of them, soonest first.

    With a location and no city chosen, only classes within NEAR_MILES are
    listed (with their distance); when there are none that close, every class is.
    Returns {classes, total, page, pages, page_list, cities, chosen, near,
    near_none, f, query} for the macros in _classes.html."""
    all_cities = cities()
    chosen = next((c for c in all_cities if c["slug"] == f.city), None)
    if not chosen:
        f.city = ""
    where = f"""
          FROM sessions s
          JOIN metros m ON m.id = s.metro_id
          JOIN sites si ON si.id = s.site_id
          JOIN course_families cf ON cf.id = s.course_family_id
          JOIN session_availability a ON a.session_id = s.id
         WHERE {_ON_SALE}
           AND (:city = '' OR m.slug = :city)
           AND (:course = '' OR cf.code = :course)
           AND (:days = 0 OR s.starts_at < now() + make_interval(days => :days))
           AND (CAST(:start AS date) IS NULL OR s.local_date >= CAST(:start AS date))
    """
    params = {"city": f.city, "course": f.course, "days": int(f.when or 0), "start": f.date or None,
              "lat": f.lat, "lng": f.lng}
    near = near_none = False
    if f.here and not f.city:
        near_total = db.fetch_value(f"SELECT count(*) {where} AND {_MILES} <= {NEAR_MILES}", **params)
        near, near_none = near_total > 0, near_total == 0
    near_clause = f" AND {_MILES} <= {NEAR_MILES}" if near else ""
    total = db.fetch_value(f"SELECT count(*) {where}{near_clause}", **params)
    pages = max(1, -(-total // f.per))
    f.page = min(f.page, pages)
    rows = db.fetch_all(f"""
        SELECT s.id, s.public_id, s.slug, s.starts_at, s.ends_at, s.time_zone, s.delivery_mode, s.local_date,
               m.name AS city_name, m.state_code, m.slug AS city_slug,
               si.name AS site_name, si.city AS site_city, cf.name AS course_name, cf.code AS course_code,
               a.seats_left, a.seat_limit,
               {_MILES if f.here else "NULL"} AS miles,
               (SELECT json_agg(json_build_object('name', p.short_name, 'price', sp.price_cents)
                                ORDER BY sp.price_cents DESC)
                  FROM session_products sp JOIN products p ON p.id = sp.product_id
                 WHERE sp.session_id = s.id AND sp.is_offered) AS offers
        {where}{near_clause}
         ORDER BY s.starts_at, si.name
         LIMIT :per OFFSET :off
    """, per=f.per, off=(f.page - 1) * f.per, **params)
    return {"classes": rows, "total": total, "page": f.page, "pages": pages,
            "page_list": page_list(f.page, pages), "cities": all_cities, "chosen": chosen,
            "near": near, "near_none": near_none, "near_miles": NEAR_MILES, "f": f, "query": f.query(),
            "courses": COURSES, "whens": WHEN, "per_options": PER_PAGE}


# --- An account's own bookings ------------------------------------------------------

def _mine(user_id: int, email: str, email_verified: bool) -> tuple[str, dict]:
    """Registrations that belong to this account: orders it made, and (once the
    account's email is confirmed, e.g. by Google or Microsoft) seats booked for
    that email by someone else. An unconfirmed email never shows other people's
    bookings, so typing someone's address at sign-up reveals nothing."""
    clause = "(o.buyer_user_id = :uid" + (" OR r.attendee_email = :email OR o.buyer_email = :email)"
                                          if email_verified else ")")
    return clause, {"uid": user_id, "email": email}


def booked(user_id: int, email: str, email_verified: bool) -> dict[str, list[dict]]:
    """{upcoming, past}: every seat this account booked or holds, one row per seat."""
    mine, params = _mine(user_id, email, email_verified)
    rows = db.fetch_all(f"""
        SELECT r.id, r.status, r.attendee_first_name, r.attendee_last_name, r.attendee_email,
               o.public_id AS order_public_id, o.buyer_user_id = :uid AS bought_here,
               s.public_id AS session_public_id, s.starts_at, s.ends_at, s.time_zone, s.status AS class_status,
               s.delivery_mode, cf.name AS course_name, p.short_name AS product_name,
               si.name AS site_name, si.address_line1, si.city AS site_city, si.state_code AS site_state,
               si.postal_code, m.name AS city_name,
               (s.starts_at > now() AND s.status = 'scheduled'
                AND r.status IN ('awaiting_payment', 'confirmed', 'displaced')) AS is_upcoming
          FROM registrations r
          JOIN orders o ON o.id = r.order_id
          JOIN sessions s ON s.id = r.session_id
          JOIN products p ON p.id = r.product_id
          JOIN sites si ON si.id = s.site_id
          JOIN metros m ON m.id = s.metro_id
          JOIN course_families cf ON cf.id = s.course_family_id
         WHERE {mine}
           AND r.status NOT IN ('pending', 'expired')
         ORDER BY s.starts_at, r.id
    """, **params)
    upcoming = [r for r in rows if r["is_upcoming"]]
    past = [r for r in rows if not r["is_upcoming"]][::-1]       # latest first
    return {"upcoming": upcoming, "past": past}


def next_booked(user_id: int, email: str, email_verified: bool, limit: int = 3) -> list[dict]:
    """The next few classes this account is booked on (for the home page greeting)."""
    mine, params = _mine(user_id, email, email_verified)
    return db.fetch_all(f"""
        SELECT DISTINCT ON (s.starts_at, s.id) s.starts_at, s.ends_at, s.time_zone,
               cf.name AS course_name, si.name AS site_name
          FROM registrations r
          JOIN orders o ON o.id = r.order_id
          JOIN sessions s ON s.id = r.session_id
          JOIN sites si ON si.id = s.site_id
          JOIN course_families cf ON cf.id = s.course_family_id
         WHERE {mine}
           AND r.status IN ('awaiting_payment', 'confirmed') AND s.starts_at > now() AND s.status = 'scheduled'
         ORDER BY s.starts_at, s.id LIMIT :limit
    """, limit=limit, **params)
