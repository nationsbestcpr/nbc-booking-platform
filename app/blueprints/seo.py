"""Public pages search engines index (launch plan 7 and 15.7).

    /va/                                    state: the cities with classes
    /richmond-va/                           city hub: courses, next classes, training sites
    /richmond-va/bls/                       city + course: copy, prices, every upcoming date
    /richmond-va/locations/henrico/         training site: public venue details, its classes
    /richmond-va/bls/2026-10-10-henrico/    one class: Event data Google reads, Book button
    /sitemap.xml, /robots.txt

Only cities that book here (metros.booking_system = 'engine') have pages.
Each class page carries Event structured data (schema.org): name, start and
end with the time zone, the site's full address, organizer, and one offer per
course sold (A&P and Adult share one page with two prices), InStock or
SoldOut from live seats. A cancelled class says EventCancelled and a moved one
EventRescheduled with its previous date. A class that has finished
redirects (301) to its city + course page. Every page has BreadcrumbList data.

Only public details are selected: never access codes, on-site contacts or
payment details (those live in the *_private_details tables). Copy approved
by Bre comes from page_content (status 'approved'); until then pages use a
short factual default.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from flask import (Blueprint, Response, abort, current_app, redirect, render_template, request)
from markupsafe import Markup
from werkzeug.routing import BaseConverter

from .. import db
from ..filters import local_dt

bp = Blueprint("seo", __name__)

ORGANIZER = {"@type": "Organization", "name": "Nation's Best CPR", "url": "https://nationsbestcpr.com"}
STATE_NAMES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
               "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
               "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana",
               "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
               "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
               "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
               "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
               "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
               "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
               "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin",
               "WY": "Wyoming"}

# Cities with public pages, and the classes those pages list.
_CITY_OPEN = "m.booking_system = 'engine' AND m.is_active"
_LISTED = f"""s.status = 'scheduled' AND s.is_published AND s.starts_at > now() AND {_CITY_OPEN}"""


class StateConverter(BaseConverter):
    regex = "[a-z]{2}"                                   # va


class MetroConverter(BaseConverter):
    regex = r"[a-z0-9]+(?:-[a-z0-9]+)*-[a-z]{2}"         # richmond-va


class SlugConverter(BaseConverter):
    regex = r"[a-z0-9]+(?:-[a-z0-9]+)*"                  # bls, 2026-10-10-henrico


def init_app(app) -> None:
    """Register the address parts these pages use (before the blueprint's rules are added)."""
    app.url_map.converters.update(state=StateConverter, metro=MetroConverter, slug=SlugConverter)


# --- helpers -------------------------------------------------------------------------

def base_url() -> str:
    configured = current_app.config.get("PUBLIC_BASE_URL", "")
    if configured and "localhost" not in configured:
        return configured.rstrip("/")
    return request.url_root.rstrip("/")


def absolute(path: str) -> str:
    return base_url() + path


def city_path(m: dict) -> str:
    return f"/{m['metro_slug']}/"


def course_path(m_slug: str, c_slug: str) -> str:
    return f"/{m_slug}/{c_slug}/"


def class_path(c: dict) -> str:
    return f"/{c['metro_slug']}/{c['course_slug']}/{c['slug']}/"


def site_path(m_slug: str, site_slug: str) -> str:
    return f"/{m_slug}/locations/{site_slug}/"


def breadcrumbs(items: list[tuple[str, str]]) -> dict:
    return {"@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": name, "item": absolute(path)}
                                for i, (name, path) in enumerate(items)]}


def _iso(dt: datetime, tz: str) -> str:
    return local_dt(dt, tz).isoformat(timespec="minutes")


def copy_blocks(scope: str, **ids) -> dict[str, Markup]:
    """Approved copy (page_content) for one page, as HTML: {block_key: html}."""
    where = {"metro": "metro_id = :metro AND course_family_id IS NULL AND site_id IS NULL",
             "metro_course": "metro_id = :metro AND course_family_id = :course",
             "site": "site_id = :site", "state": "state_code = :state"}[scope]
    rows = db.fetch_all(f"""SELECT block_key, body_markdown FROM page_content
                             WHERE scope = :scope AND status = 'approved' AND {where}""", scope=scope, **ids)
    return {r["block_key"]: simple_markdown(r["body_markdown"]) for r in rows}


def simple_markdown(text_: str) -> Markup:
    """Paragraphs, "- " lists, **bold** and [links](https://…) from approved copy. Everything else is escaped."""
    out = []
    for block in re.split(r"\n\s*\n", (text_ or "").strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue

        def inline(s: str) -> str:
            s = html.escape(s)
            s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
            return re.sub(r"\[([^\]]+)\]\((https://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
        if all(ln.startswith("- ") for ln in lines):
            out.append("<ul>" + "".join(f"<li>{inline(ln[2:])}</li>" for ln in lines) + "</ul>")
        elif lines[0].startswith("## "):
            out.append(f"<h3>{inline(lines[0][3:])}</h3>" + (f"<p>{inline(' '.join(lines[1:]))}</p>" if lines[1:] else ""))
        else:
            out.append(f"<p>{inline(' '.join(lines))}</p>")
    return Markup("".join(out))


def _classes(where: str, limit: int = 200, **params) -> list[dict]:
    return db.fetch_all(f"""
        SELECT s.id, s.public_id, s.slug, s.starts_at, s.ends_at, s.time_zone, s.delivery_mode, s.local_date,
               m.slug AS metro_slug, m.name AS city_name, m.state_code, cf.slug AS course_slug, cf.name AS course_name,
               si.slug AS site_slug, si.name AS site_name, si.city AS site_city, a.seats_left,
               (SELECT json_agg(json_build_object('name', p.short_name, 'price', sp.price_cents) ORDER BY sp.price_cents DESC)
                  FROM session_products sp JOIN products p ON p.id = sp.product_id
                 WHERE sp.session_id = s.id AND sp.is_offered AND p.is_active) AS offers
          FROM sessions s
          JOIN metros m ON m.id = s.metro_id
          JOIN sites si ON si.id = s.site_id
          JOIN course_families cf ON cf.id = s.course_family_id
          JOIN session_availability a ON a.session_id = s.id
         WHERE {_LISTED} AND {where}
         ORDER BY s.starts_at, si.name
         LIMIT :limit
    """, limit=limit, **params)


def _metro(slug: str) -> dict | None:
    return db.fetch_one(f"""SELECT m.id, m.slug AS metro_slug, m.name, m.state_code FROM metros m
                             WHERE m.slug = :s AND {_CITY_OPEN}""", s=slug)


def _redirect_or_404():
    """Old addresses kept working (url_redirects), else not found."""
    path = request.path if request.path.endswith("/") else request.path + "/"
    r = db.fetch_one("SELECT to_path, status_code FROM url_redirects WHERE from_path IN (:a, :b)",
                     a=request.path, b=path)
    if r:
        if r["status_code"] == 410:
            abort(410)
        return redirect(r["to_path"], code=r["status_code"])
    abort(404)


def _render(template: str, *, title: str, description: str, canonical: str, ld: list[dict], **ctx):
    return render_template(template, seo_title=title, seo_description=description,
                           canonical=absolute(canonical), ld=ld, **ctx)


# --- pages ---------------------------------------------------------------------------

@bp.get("/<state:state>/")
def state(state: str):
    code = state.upper()
    cities = db.fetch_all(f"""
        SELECT m.slug AS metro_slug, m.name, m.state_code,
               (SELECT count(*) FROM sessions s WHERE s.metro_id = m.id AND s.status = 'scheduled'
                   AND s.is_published AND s.starts_at > now()) AS upcoming
          FROM metros m WHERE m.state_code = :st AND {_CITY_OPEN} ORDER BY m.name
    """, st=code)
    if not cities:
        return _redirect_or_404()
    name = STATE_NAMES.get(code, code)
    return _render("seo/state.html", title=f"CPR, First Aid & BLS Classes in {name}",
                   description=f"Red Cross CPR, First Aid, AED and BLS classes in {name}. Pick your city and book online.",
                   canonical=f"/{state}/", state_name=name, cities=cities, copy=copy_blocks("state", state=code),
                   ld=[breadcrumbs([("Classes", "/"), (name, f"/{state}/")])])


@bp.get("/<metro:metro>/")
def city(metro: str):
    m = _metro(metro)
    if not m:
        return _redirect_or_404()
    upcoming = _classes("m.id = :m", limit=12, m=m["id"])
    courses = db.fetch_all(f"""
        SELECT cf.slug AS course_slug, cf.name, count(s.id) AS upcoming, min(sp.price_cents) AS from_price
          FROM sessions s JOIN metros m ON m.id = s.metro_id JOIN course_families cf ON cf.id = s.course_family_id
          LEFT JOIN session_products sp ON sp.session_id = s.id AND sp.is_offered
         WHERE {_LISTED} AND m.id = :m GROUP BY cf.id ORDER BY cf.name
    """, m=m["id"])
    sites = db.fetch_all("""
        SELECT si.slug, si.name, si.address_line1, si.city, si.state_code, si.postal_code
          FROM sites si WHERE si.metro_id = :m AND si.is_active AND si.source_deleted_at IS NULL ORDER BY si.name
    """, m=m["id"])
    state_name = STATE_NAMES.get(m["state_code"], m["state_code"])
    return _render("seo/city.html", title=f"CPR, First Aid & BLS Classes in {m['name']}, {m['state_code']}",
                   description=f"Red Cross CPR, First Aid, AED and BLS classes in {m['name']}, {m['state_code']}. "
                               "See dates, prices and seats left, and book online.",
                   canonical=city_path(m), m=m, upcoming=upcoming, courses=courses, sites=sites,
                   state_name=state_name, copy=copy_blocks("metro", metro=m["id"]),
                   ld=[breadcrumbs([("Classes", "/"), (state_name, f"/{m['state_code'].lower()}/"),
                                    (m["name"], city_path(m))])])


@bp.get("/<metro:metro>/<slug:course>/")
def city_course(metro: str, course: str):
    m = _metro(metro)
    cf = db.fetch_one("SELECT id, slug, name, default_duration_min FROM course_families WHERE slug = :s", s=course)
    if not m or not cf:
        return _redirect_or_404()
    upcoming = _classes("m.id = :m AND cf.id = :c", m=m["id"], c=cf["id"])
    prices = db.fetch_all(f"""
        SELECT p.short_name, p.name, min(sp.price_cents) AS low, max(sp.price_cents) AS high
          FROM sessions s JOIN metros m ON m.id = s.metro_id
          JOIN session_products sp ON sp.session_id = s.id AND sp.is_offered
          JOIN products p ON p.id = sp.product_id
         WHERE {_LISTED} AND m.id = :m AND s.course_family_id = :c
         GROUP BY p.id ORDER BY max(sp.price_cents) DESC
    """, m=m["id"], c=cf["id"])
    state_name = STATE_NAMES.get(m["state_code"], m["state_code"])
    return _render("seo/city_course.html", title=f"{cf['name']} Classes in {m['name']}, {m['state_code']}",
                   description=f"{cf['name']} classes in {m['name']}, {m['state_code']}: upcoming dates, "
                               "locations, prices and seats left. Book online.",
                   canonical=course_path(m["metro_slug"], cf["slug"]), m=m, cf=cf, upcoming=upcoming, prices=prices,
                   copy=copy_blocks("metro_course", metro=m["id"], course=cf["id"]),
                   ld=[breadcrumbs([("Classes", "/"), (state_name, f"/{m['state_code'].lower()}/"),
                                    (m["name"], city_path(m)), (cf["name"], course_path(m["metro_slug"], cf["slug"]))])])


@bp.get("/<metro:metro>/locations/<slug:site>/")
def location(metro: str, site: str):
    m = _metro(metro)
    si = db.fetch_one("""
        SELECT si.id, si.slug, si.name, si.address_line1, si.address_line2, si.city, si.state_code, si.postal_code,
               si.parking_notes_public, si.directions_public, si.latitude, si.longitude
          FROM sites si WHERE si.metro_id = :m AND si.slug = :s AND si.is_active
    """, m=m["id"], s=site) if m else None
    if not si:
        return _redirect_or_404()
    upcoming = _classes("si.id = :si", si=si["id"])
    state_name = STATE_NAMES.get(m["state_code"], m["state_code"])
    place = {"@context": "https://schema.org", "@type": "Place", "name": si["name"],
             "address": _address(si), "url": absolute(site_path(m["metro_slug"], si["slug"]))}
    if si["latitude"] is not None and si["longitude"] is not None:
        place["geo"] = {"@type": "GeoCoordinates", "latitude": float(si["latitude"]),
                        "longitude": float(si["longitude"])}
    return _render("seo/location.html", title=f"CPR & First Aid Classes at {si['name']}, {m['name']}",
                   description=f"Red Cross classes at {si['name']}, {si['address_line1']}, {si['city']}, "
                               f"{si['state_code']}. Upcoming dates, parking and directions.",
                   canonical=site_path(m["metro_slug"], si["slug"]), m=m, si=si, upcoming=upcoming,
                   copy=copy_blocks("site", site=si["id"]),
                   ld=[place, breadcrumbs([("Classes", "/"), (state_name, f"/{m['state_code'].lower()}/"),
                                           (m["name"], city_path(m)),
                                           (si["name"], site_path(m["metro_slug"], si["slug"]))])])


def _address(si: dict) -> dict:
    street = si["address_line1"] + (f", {si['address_line2']}" if si.get("address_line2") else "")
    return {"@type": "PostalAddress", "streetAddress": street, "addressLocality": si["city"],
            "addressRegion": si["state_code"], "postalCode": si["postal_code"], "addressCountry": "US"}


@bp.get("/<metro:metro>/<slug:course>/<slug:slug>/")
def class_page(metro: str, course: str, slug: str):
    c = db.fetch_one(f"""
        SELECT s.id, s.public_id, s.slug, s.starts_at, s.ends_at, s.time_zone, s.delivery_mode, s.status,
               s.is_published, s.previous_starts_at, s.created_at, s.sales_close_at,
               s.ends_at < now() AS finished,
               m.id AS metro_id, m.slug AS metro_slug, m.name AS city_name, m.state_code,
               cf.id AS course_family_id, cf.slug AS course_slug, cf.name AS course_name, cf.image_url AS course_image,
               si.slug AS site_slug, si.name AS site_name, si.address_line1, si.address_line2, si.city,
               si.state_code AS site_state, si.postal_code, si.parking_notes_public, si.directions_public,
               si.image_url AS site_image, a.seats_left, a.seat_limit
          FROM sessions s
          JOIN metros m ON m.id = s.metro_id
          JOIN course_families cf ON cf.id = s.course_family_id
          JOIN sites si ON si.id = s.site_id
          JOIN session_availability a ON a.session_id = s.id
         WHERE m.slug = :m AND cf.slug = :c AND s.slug = :s AND {_CITY_OPEN}
           AND (s.is_published OR s.status = 'cancelled')
    """, m=metro, c=course, s=slug)
    if not c:
        return _redirect_or_404()
    if c["finished"]:
        return redirect(course_path(c["metro_slug"], c["course_slug"]), code=301)
    offers = db.fetch_all("""
        SELECT p.short_name, p.name, sp.price_cents FROM session_products sp JOIN products p ON p.id = sp.product_id
         WHERE sp.session_id = :s AND sp.is_offered AND p.is_active ORDER BY sp.price_cents DESC
    """, s=c["id"])
    on_sale = (c["status"] == "scheduled" and c["is_published"] and c["seats_left"] > 0
               and (c["sales_close_at"] is None or c["sales_close_at"] > datetime.now(timezone.utc)))
    next_dates = _classes("m.id = :m AND cf.id = :c AND s.id <> :s", limit=4, m=c["metro_id"],
                          c=c["course_family_id"], s=c["id"])
    page = class_path(c)
    status = ("https://schema.org/EventCancelled" if c["status"] == "cancelled"
              else "https://schema.org/EventRescheduled" if c["previous_starts_at"]
              else "https://schema.org/EventScheduled")
    when = local_dt(c["starts_at"], c["time_zone"])
    event = {
        "@context": "https://schema.org", "@type": "Event",
        "name": f"{c['course_name']} class in {c['city_name']}, {c['state_code']}",
        "description": f"American Red Cross {c['course_name']} {c['delivery_mode']} class at {c['site_name']}, "
                       f"{c['city']}, {c['site_state']}, on {when.strftime('%A, %B %-d, %Y')}.",
        "startDate": _iso(c["starts_at"], c["time_zone"]), "endDate": _iso(c["ends_at"], c["time_zone"]),
        "eventStatus": status, "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        "location": {"@type": "Place", "name": c["site_name"], "address": _address(c)},
        "organizer": ORGANIZER,
        "offers": [{"@type": "Offer", "name": o["name"], "price": f"{o['price_cents'] / 100:.2f}",
                    "priceCurrency": "USD", "url": absolute(page),
                    "availability": "https://schema.org/InStock" if on_sale else "https://schema.org/SoldOut",
                    "validFrom": _iso(c["created_at"], c["time_zone"])} for o in offers],
    }
    if c["previous_starts_at"] and c["status"] != "cancelled":
        event["previousStartDate"] = _iso(c["previous_starts_at"], c["time_zone"])
    images = [i for i in (c["site_image"], c["course_image"]) if i]
    if images:
        event["image"] = images
    state_name = STATE_NAMES.get(c["state_code"], c["state_code"])
    return _render("seo/class.html", title=f"{c['course_name']} · {when.strftime('%b %-d, %Y')} · {c['site_name']}",
                   description=f"{c['course_name']} in {c['city_name']}, {c['state_code']} on "
                               f"{when.strftime('%A, %B %-d')} at {c['site_name']}. "
                               + (f"{c['seats_left']} seats left. Book online." if on_sale else "See other dates."),
                   canonical=page, c=c, offers=offers, on_sale=on_sale, next_dates=next_dates,
                   ld=[event, breadcrumbs([("Classes", "/"), (state_name, f"/{c['state_code'].lower()}/"),
                                           (c["city_name"], f"/{c['metro_slug']}/"),
                                           (c["course_name"], course_path(c["metro_slug"], c["course_slug"])),
                                           (when.strftime("%b %-d, %Y"), page)])])


# --- sitemap and robots ---------------------------------------------------------------

@bp.get("/sitemap.xml")
def sitemap():
    """Every public page: states, cities, city + course pages, sites, and classes not yet
    finished (cancelled ones too, until class day, so Google sees EventCancelled).
    Rebuilt on each request from the synced data, so it's current after every Airtable sync."""
    urls: list[tuple[str, datetime | None]] = []
    metros = db.fetch_all(f"SELECT m.id, m.slug AS metro_slug, m.state_code, m.updated_at FROM metros m "
                          f"WHERE {_CITY_OPEN} ORDER BY m.state_code, m.slug")
    for st in sorted({m["state_code"] for m in metros}):
        urls.append((f"/{st.lower()}/", None))
    for m in metros:
        urls.append((city_path(m), m["updated_at"]))
    for r in db.fetch_all(f"""
        SELECT DISTINCT m.slug AS metro_slug, cf.slug AS course_slug FROM sessions s
          JOIN metros m ON m.id = s.metro_id JOIN course_families cf ON cf.id = s.course_family_id
         WHERE {_LISTED} ORDER BY 1, 2"""):
        urls.append((course_path(r["metro_slug"], r["course_slug"]), None))
    for r in db.fetch_all(f"""
        SELECT m.slug AS metro_slug, si.slug, si.updated_at FROM sites si JOIN metros m ON m.id = si.metro_id
         WHERE si.is_active AND {_CITY_OPEN} ORDER BY 1, 2"""):
        urls.append((site_path(r["metro_slug"], r["slug"]), r["updated_at"]))
    for c in db.fetch_all(f"""
        SELECT m.slug AS metro_slug, cf.slug AS course_slug, s.slug, s.updated_at FROM sessions s
          JOIN metros m ON m.id = s.metro_id JOIN course_families cf ON cf.id = s.course_family_id
         WHERE {_CITY_OPEN} AND s.ends_at > now() AND (s.is_published OR s.status = 'cancelled')
         ORDER BY s.starts_at LIMIT 49000"""):
        urls.append((class_path(c), c["updated_at"]))
    body = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path, changed in urls:
        lastmod = f"<lastmod>{changed.astimezone(timezone.utc).strftime('%Y-%m-%d')}</lastmod>" if changed else ""
        body.append(f"<url><loc>{html.escape(absolute(path))}</loc>{lastmod}</url>")
    body.append("</urlset>")
    return Response("\n".join(body), mimetype="application/xml")


@bp.get("/robots.txt")
def robots():
    lines = ["User-agent: *", "Disallow: /admin", "Disallow: /console", "Disallow: /my", "Disallow: /book/",
             "Disallow: /account", "Disallow: /invite/", "Disallow: /dashboard", "Disallow: /teach",
             f"Sitemap: {absolute('/sitemap.xml')}"]
    if current_app.config["APP_ENV"] != "production":
        lines = ["User-agent: *", "Disallow: /"]            # the test site stays out of search results
    return Response("\n".join(lines) + "\n", mimetype="text/plain")
