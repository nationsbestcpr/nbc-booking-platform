"""Airtable -> engine sync: courses, locations, then the class schedule.

Runs every few minutes from the worker, from "Sync now" on the Integrations
page, and for one class from the Airtable button (/admin/sync?record=rec…).

What it does (docs/source-of-truth.md, "Airtable"):
  * Course Master   -> products (one per course row, plus its companion price,
                       e.g. A&P + Adult), each in a course family (seat pool).
  * Master Locations-> cities (metros), sites, a room, private site details.
                       New cities start on Enrollware: nothing sells until an
                       admin switches the city on the Locations page.
  * Class Schedule  -> classes (sessions) with their prices. A class is on sale
                       when its Status includes "Enrollware"; "Cancelled" in
                       Status cancels it. Two rows at the same site, course
                       family and start time are one class (shared seats).

Each class row is saved in its own transaction, so one bad row becomes one
problem on the dashboard and the rest still sync. The database has the last
word: it refuses overlapping classes in one room or a seat limit below the
seats already sold, and those refusals become problems too.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from .. import db
from . import airtable, runs
from .http import IntegrationError

SOURCE = "airtable"


class RowProblem(Exception):
    def __init__(self, code: str, message: str, severity: str = "warning") -> None:
        super().__init__(message)
        self.code, self.severity = code, severity


# --- value helpers -----------------------------------------------------------------

def _text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, list):
        parts = [_text(x) for x in v]
        v = ", ".join(p for p in parts if p)
    elif isinstance(v, dict):
        v = v.get("name") or v.get("email") or v.get("text") or ""
    s = " ".join(str(v).split())
    return s or None


def _is_record_ids(v) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, str) and x.startswith("rec") for x in v)


def _number(v) -> float | None:
    if isinstance(v, list):
        v = v[0] if v else None
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    cleaned = re.sub(r"[^0-9.\-]", "", str(v))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _cents(v) -> int | None:
    n = _number(v)
    return None if n is None else int(round(n * 100))


def _truthy(v) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"yes", "true", "active", "1", "y", "checked"}


def slugify(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-") or "x"


def _hash(fields: dict) -> bytes:
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).digest()


def _statuses(v) -> list[str]:
    if v is None:
        return []
    items = v if isinstance(v, list) else re.split(r"[,;]", str(v))
    return [" ".join(str(x).split()) for x in items if str(x).strip()]


# --- time zones and addresses ------------------------------------------------------

_TZ_WORDS = [
    ("arizona", "America/Phoenix"), ("hawaii", "Pacific/Honolulu"), ("alaska", "America/Anchorage"),
    ("eastern", "America/New_York"), ("central", "America/Chicago"), ("mountain", "America/Denver"),
    ("pacific", "America/Los_Angeles"),
]
_TZ_ABBR = {"et": "America/New_York", "est": "America/New_York", "edt": "America/New_York",
            "ct": "America/Chicago", "cst": "America/Chicago", "cdt": "America/Chicago",
            "mt": "America/Denver", "mst": "America/Denver", "mdt": "America/Denver",
            "pt": "America/Los_Angeles", "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles"}
_STATE_TZ = {
    **{s: "America/New_York" for s in "CT DC DE FL GA IN KY MA MD ME MI NC NH NJ NY OH PA RI SC VA VT WV".split()},
    **{s: "America/Chicago" for s in "AL AR IA IL KS LA MN MO MS ND NE OK SD TN TX WI".split()},
    **{s: "America/Denver" for s in "CO ID MT NM UT WY".split()},
    "AZ": "America/Phoenix", "CA": "America/Los_Angeles", "NV": "America/Los_Angeles",
    "OR": "America/Los_Angeles", "WA": "America/Los_Angeles", "AK": "America/Anchorage", "HI": "Pacific/Honolulu",
}
_STATES = set(_STATE_TZ)


def time_zone(value: str | None, state: str | None) -> str | None:
    raw = (value or "").strip()
    if raw:
        try:
            ZoneInfo(raw)
            if "/" in raw:
                return raw
        except (ZoneInfoNotFoundError, ValueError):
            pass
        low = raw.lower()
        if low in _TZ_ABBR:
            return _TZ_ABBR[low]
        for word, zone in _TZ_WORDS:
            if word in low:
                return zone
    return _STATE_TZ.get((state or "").upper())


def parse_address(full: str | None) -> dict:
    """'123 Main St, Suite 4, Richmond, VA 23233' -> parts. Unknown parts are None."""
    out = {"line1": None, "line2": None, "city": None, "state": None, "zip": None}
    if not full:
        return out
    parts = [p.strip() for p in re.split(r",|\n", full) if p.strip()]
    if parts and parts[-1].upper() in {"USA", "US", "UNITED STATES"}:
        parts.pop()
    if parts:
        m = re.fullmatch(r"([A-Za-z]{2})\.?\s*(\d{5}(?:-\d{4})?)?", parts[-1])
        if m and m.group(1).upper() in _STATES:
            out["state"], out["zip"] = m.group(1).upper(), m.group(2)
            parts.pop()
        elif re.fullmatch(r"\d{5}(?:-\d{4})?", parts[-1]) and len(parts) >= 2:
            out["zip"] = parts.pop()
            m2 = re.fullmatch(r"(.*?)\s+([A-Za-z]{2})", parts[-1])
            if m2 and m2.group(2).upper() in _STATES:
                out["state"] = m2.group(2).upper()
                parts[-1] = m2.group(1)
    if len(parts) >= 2:
        out["city"] = parts.pop()
    if parts:
        out["line1"] = parts[0]
        out["line2"] = ", ".join(parts[1:]) or None
    return out


_TIME = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m?\.?\s*$|^\s*(\d{1,2}):(\d{2})\s*$", re.I)


def parse_clock(v) -> time | None:
    """'9:00 AM', '9am', '13:30', a datetime, or seconds after midnight."""
    if v is None:
        return None
    if isinstance(v, list):
        v = v[0] if v else None
        if v is None:
            return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        secs = int(v)
        return time(secs // 3600 % 24, secs // 60 % 60)
    s = str(v).strip()
    if "T" in s:
        return None    # a full date-time: handled by the caller
    m = _TIME.match(s)
    if not m:
        return None
    if m.group(4):
        hour, minute = int(m.group(4)), int(m.group(5))
    else:
        hour, minute = int(m.group(1)) % 12, int(m.group(2) or 0)
        if m.group(3).lower() == "p":
            hour += 12
    return time(hour, minute) if hour < 24 and minute < 60 else None


def parse_instant(v) -> datetime | None:
    if isinstance(v, list):
        v = v[0] if v else None
    if not v or not isinstance(v, str) or "T" not in v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_day(v) -> date | None:
    if isinstance(v, list):
        v = v[0] if v else None
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s, fmt).date()
        except ValueError:
            continue
    return None


# --- the run -----------------------------------------------------------------------

def run(trigger: str = "schedule", *, user_id: int | None = None, record_id: str | None = None) -> dict:
    """Sync everything (or one Class Schedule record). Returns the run's stats."""
    if not airtable.configured():
        raise IntegrationError("Airtable isn't set up: add AIRTABLE_TOKEN and AIRTABLE_BASE_ID to the secrets.",
                               retry=False)
    run_id = runs.start("airtable_sync", trigger, user_id=user_id,
                        scope={"record_id": record_id} if record_id else None)
    stats = {"classes_seen": 0, "classes_created": 0, "classes_updated": 0, "classes_unchanged": 0,
             "classes_skipped": 0, "classes_removed": 0, "locations": 0, "products": 0, "problems": 0}
    try:
        mapping = airtable.resolve(airtable.read_schema())
        if not _check_mapping(mapping, run_id):
            runs.finish(run_id, "failed", stats, "Required tables or fields are missing in Airtable. "
                                                 "See the problems list on the Integrations page.")
            stats["status"] = "failed"
            return stats
        products = _sync_courses(mapping["courses"], run_id, stats)
        sites = _sync_locations(mapping["locations"], run_id, stats)
        seen = _sync_schedule(mapping["schedule"], products, sites, run_id, stats, record_id)
        if record_id is None:
            _mark_removed(seen, run_id, stats)
        status = "partial" if stats["problems"] else "succeeded"
        runs.finish(run_id, status, stats)
        stats["status"] = status
        stats["run_id"] = run_id
        return stats
    except IntegrationError as exc:
        runs.finish(run_id, "failed", stats, str(exc))
        raise
    except Exception as exc:
        runs.finish(run_id, "failed", stats, f"{exc.__class__.__name__}: {exc}")
        raise


def _check_mapping(mapping: dict, run_id: int) -> bool:
    ok = True
    with db.transaction() as conn:
        for key, m in mapping.items():
            ref = f"table:{m['wanted']}"
            if not m["table"]:
                runs.issue(conn, source=SOURCE, code="MISSING_TABLE", severity="error", run_id=run_id,
                           external_ref=ref, message=f"Airtable has no table called “{m['wanted']}”. "
                           "Rename it back, or set the table name in the secrets (see the README).")
                ok = False
            elif m["missing_required"]:
                names = " or ".join("/".join(f'“{airtable.FIELDS[key][alt][0]}”' for alt in req.split("|"))
                                    for req in m["missing_required"])
                runs.issue(conn, source=SOURCE, code="MISSING_FIELD", severity="error", run_id=run_id,
                           external_ref=ref, message=f"{m['table']}: no field for {names}. Add it in Airtable, "
                           "or tell the developer its real name.",
                           details={"missing": m["missing_required"]})
                ok = False
            else:
                runs.resolve(conn, source=SOURCE, external_ref=ref)
    return ok


def _register(conn: Connection, record: dict, table: str, fields: dict) -> bool:
    """Upsert the airtable_records row. True when the record's fields changed."""
    h = _hash(fields)
    created = record.get("createdTime")
    row = conn.execute(text("""
        INSERT INTO airtable_records (record_id, base_id, table_name, fields_hash, airtable_created_at)
        VALUES (:id, :base, :t, :h, CAST(:c AS timestamptz))
        ON CONFLICT (record_id) DO UPDATE
           SET last_seen_at = now(), source_deleted_at = NULL, table_name = EXCLUDED.table_name,
               last_changed_at = CASE WHEN airtable_records.fields_hash <> EXCLUDED.fields_hash
                                      THEN now() ELSE airtable_records.last_changed_at END,
               fields_hash = EXCLUDED.fields_hash
        RETURNING (xmax = 0) AS inserted, last_changed_at = now() AS changed
    """), {"id": record["id"], "base": airtable.base_id(), "t": table, "h": h, "c": created}).mappings().one()
    return bool(row["inserted"] or row["changed"])


def _values(record: dict, m: dict) -> dict:
    f = record.get("fields", {})
    return {logical: f.get(actual) for logical, actual in m["fields"].items()}


def _problem(run_id: int, stats: dict, record_id: str, exc: RowProblem, table: str) -> None:
    stats["problems"] += 1
    with db.transaction() as conn:
        runs.issue(conn, source=SOURCE, code=exc.code, severity=exc.severity, run_id=run_id,
                   external_ref=record_id, message=f"{table} {record_id}: {exc}")


# --- Course Master -> products ------------------------------------------------------

def _family_code(*hints: str | None) -> str | None:
    blob = " ".join(h for h in hints if h).lower()
    if not blob:
        return None
    if "instructor" in blob or re.search(r"\bit\b", blob):
        return "IT"
    if "bls" in blob or "basic life" in blob:
        return "BLS"
    if any(w in blob for w in ("cpr", "first aid", "aed", "fa/", "pediatric", "adult")):
        return "FA_CPR_AED"
    return None


def _short_name(name: str, family: str) -> str:
    low = name.lower()
    if family == "FA_CPR_AED":
        return "A&P" if "pediatric" in low or "a&p" in low else "Adult"
    return {"BLS": "BLS", "IT": "Instructor Training"}.get(family, name[:20])


def _sync_courses(m: dict, run_id: int, stats: dict) -> dict[str, list[dict]]:
    """Returns {course record id: [product rows]} (the main product first)."""
    families = {r["code"]: r for r in db.fetch_all("SELECT id, code FROM course_families")}
    out: dict[str, list[dict]] = {}
    for record in airtable.list_records(m["table"], list(m["fields"].values())):
        v = _values(record, m)
        try:
            name = _text(v.get("name"))
            if not name:
                raise RowProblem("COURSE_NO_NAME", "the course has no name.")
            if _truthy(v.get("active")) is False:
                continue
            family = _family_code(_text(v.get("family")), _text(v.get("group_key")), name)
            if family not in families:
                raise RowProblem("UNKNOWN_COURSE_FAMILY",
                                 f"“{name}”: can't tell if it's First Aid/CPR/AED, BLS or Instructor Training. "
                                 "Fill in its Course Family.")
            price = _cents(v.get("price"))
            if price is None or price < 0:
                raise RowProblem("COURSE_NO_PRICE", f"“{name}” has no price.")
            delivery_hint = (_text(v.get("delivery")) or name).lower()
            delivery = "blended" if "blend" in delivery_hint or "online" in delivery_hint else "classroom"
            short = _text(v.get("short_name")) or _short_name(name, family)
            code = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")[:50]
            ghl_code = _text(v.get("ghl_code")) or re.sub(r"[^A-Z0-9]", "", short.upper())[:12] or family
            with db.transaction() as conn:
                _register(conn, record, m["table"], v)
                fam_id = families[family]["id"]
                seats = _number(v.get("max_seats"))
                if seats and seats >= 1:
                    conn.execute(text("""UPDATE course_families SET one_instructor_max = :n
                                          WHERE id = :f AND one_instructor_max <> :n"""),
                                 {"n": int(seats), "f": fam_id})
                minutes = _number(v.get("duration"))
                if minutes and minutes > 0:
                    minutes = minutes * 60 if minutes <= 24 else minutes       # hours or minutes
                if minutes and minutes >= 1:
                    conn.execute(text("""UPDATE course_families SET default_duration_min = :m
                                          WHERE id = :f AND default_duration_min IS DISTINCT FROM :m"""),
                                 {"m": int(minutes), "f": fam_id})
                rows = [_upsert_product(conn, record["id"], code, name, short, family, fam_id, delivery,
                                        ghl_code, price)]
                companion_price = _cents(v.get("companion_price"))
                if companion_price is not None and companion_price >= 0:
                    c_name = _text(v.get("companion_name")) or (
                        name.replace("Pediatric", "").replace("&", "").replace("  ", " ").strip()
                        if family == "FA_CPR_AED" else f"{name} (companion)")
                    c_short = "Adult" if family == "FA_CPR_AED" else "Companion"
                    rows.append(_upsert_product(conn, None, f"{code}_COMPANION"[:60], c_name, c_short, family,
                                                fam_id, delivery, re.sub(r"[^A-Z0-9]", "", c_short.upper()),
                                                companion_price))
                runs.resolve(conn, source=SOURCE, external_ref=record["id"])
            out[record["id"]] = rows
            stats["products"] += len(rows)
        except RowProblem as exc:
            _problem(run_id, stats, record["id"], exc, m["table"])
        except DBAPIError as exc:
            detail = str(exc.orig).splitlines()[0]
            message = ("its course family or delivery changed, but classes already sell it. Add a new Course Master "
                       "row for the new version instead." if "foreign key" in detail
                       else f"the database refused it ({detail[:200]}).")
            _problem(run_id, stats, record["id"], RowProblem("COURSE_REFUSED", message), m["table"])
    return out


def _upsert_product(conn: Connection, record_id: str | None, code: str, name: str, short: str, family: str,
                    fam_id: int, delivery: str, ghl_code: str, price: int) -> dict:
    existing = conn.execute(text("""
        SELECT id FROM products WHERE (CAST(:rid AS text) IS NOT NULL AND airtable_record_id = :rid) OR code = :code
         ORDER BY (airtable_record_id = :rid) DESC NULLS LAST LIMIT 1
    """), {"rid": record_id, "code": code}).scalar()
    params = {"rid": record_id, "code": code, "name": name, "short": short, "fam": fam_id, "del": delivery,
              "ghl": ghl_code, "price": price}
    if existing:
        conn.execute(text("""
            UPDATE products SET name = :name, short_name = :short, course_family_id = :fam, delivery_mode = :del,
                   ghl_course_code = :ghl, base_price_cents = :price, is_active = true,
                   airtable_record_id = COALESCE(:rid, airtable_record_id)
             WHERE id = :id
        """), {**params, "id": existing})
        pid = existing
    else:
        pid = conn.execute(text("""
            INSERT INTO products (course_family_id, code, name, short_name, delivery_mode, ghl_course_code,
                                  base_price_cents, airtable_record_id, sort_order)
            VALUES (:fam, :code, :name, :short, :del, :ghl, :price, :rid,
                    CASE WHEN :short = 'Adult' THEN 2 ELSE 1 END)
            RETURNING id
        """), params).scalar_one()
    return {"id": pid, "family": family, "family_id": fam_id, "delivery": delivery, "price": price, "name": name}


# --- Master Locations -> cities, sites, rooms ---------------------------------------

def _sync_locations(m: dict, run_id: int, stats: dict) -> dict[str, dict]:
    """Returns {location record id: {"site_id", "metro_id", "time_zone", "slug", "room_max"}}."""
    out: dict[str, dict] = {}
    for record in airtable.list_records(m["table"], list(m["fields"].values())):
        v = _values(record, m)
        try:
            site = _location(record, m, v)
            if site:
                out[record["id"]] = site
                stats["locations"] += 1
        except RowProblem as exc:
            _problem(run_id, stats, record["id"], exc, m["table"])
        except DBAPIError as exc:
            _problem(run_id, stats, record["id"], RowProblem("LOCATION_REFUSED", _db_reason(exc)), m["table"])
    return out


def _location(record: dict, m: dict, v: dict) -> dict | None:
    name = _text(v.get("name"))
    if not name:
        raise RowProblem("LOCATION_NO_NAME", "the location has no name.")
    addr = parse_address(_text(v.get("address")))
    line1 = addr["line1"] or _text(v.get("address"))
    city = _text(v.get("city")) or addr["city"]
    state = (_text(v.get("state")) or addr["state"] or "").upper()[:2] or None
    postal = _text(v.get("zip")) or addr["zip"]
    if isinstance(v.get("zip"), (int, float)):
        postal = f"{int(v['zip']):05d}"
    if not (line1 and city and state and postal) or state not in _STATES:
        raise RowProblem("LOCATION_ADDRESS", f"“{name}”: the address needs a street, city, two-letter state "
                                             "and ZIP (e.g. 123 Main St, Richmond, VA 23233).")
    zone = time_zone(_text(v.get("time_zone")), state)
    if not zone:
        raise RowProblem("LOCATION_TIME_ZONE", f"“{name}”: unknown time zone.")
    metro_raw = None if _is_record_ids(v.get("metro")) else _text(v.get("metro"))
    metro_name, metro_state = metro_raw or city, state
    mm = re.fullmatch(r"(.+?),?\s+([A-Za-z]{2})", metro_name or "")
    if mm and mm.group(2).upper() in _STATES:
        metro_name, metro_state = mm.group(1).strip(), mm.group(2).upper()
    active = _truthy(v.get("active"))
    room_max = _number(v.get("max_students"))

    with db.transaction() as conn:
        _register(conn, record, m["table"], v)
        metro_slug = slugify(f"{metro_name}-{metro_state}")
        metro_id = conn.execute(text("SELECT id FROM metros WHERE slug = :s"), {"s": metro_slug}).scalar()
        if metro_id is None:
            market = _text(v.get("market_code")) or re.sub(r"[^A-Z]", "", metro_name.upper())[:12] or metro_slug.upper()
            taken = conn.execute(text("SELECT 1 FROM metros WHERE market_code = :c"), {"c": market}).first()
            if taken:
                market = f"{market}-{metro_state}"
            metro_id = conn.execute(text("""
                INSERT INTO metros (slug, name, state_code, market_code, time_zone)
                VALUES (:s, :n, :st, :mc, :tz) RETURNING id
            """), {"s": metro_slug, "n": metro_name, "st": metro_state, "mc": market, "tz": zone}).scalar_one()

        site = conn.execute(text("SELECT id, metro_id, slug FROM sites WHERE airtable_record_id = :r"),
                            {"r": record["id"]}).mappings().first()
        params = {"metro": metro_id, "name": name, "l1": line1, "l2": _text(v.get("address2")) or addr["line2"],
                  "city": city, "st": state, "zip": postal, "lat": _number(v.get("lat")), "lng": _number(v.get("lng")),
                  "tz": zone, "park": _text(v.get("parking")), "dir": _text(v.get("directions")),
                  "active": active is not False, "rid": record["id"]}
        if params["lat"] is not None and not -90 <= params["lat"] <= 90:
            params["lat"] = None
        if params["lng"] is not None and not -180 <= params["lng"] <= 180:
            params["lng"] = None
        if site and site["metro_id"] != metro_id:
            raise RowProblem("LOCATION_CITY_CHANGED", f"“{name}” moved to another city in Airtable. "
                                                      "Classes are tied to their city; ask the developer to move it.")
        if site:
            site_id, slug = site["id"], site["slug"]
            conn.execute(text("""
                UPDATE sites SET name = :name, address_line1 = :l1, address_line2 = :l2, city = :city,
                       state_code = :st, postal_code = :zip, latitude = :lat, longitude = :lng, time_zone = :tz,
                       parking_notes_public = :park, directions_public = :dir, is_active = :active,
                       source_synced_at = now(), source_deleted_at = NULL
                 WHERE id = :id
            """), {**params, "id": site_id})
        else:
            slug = slugify(name)
            n = 1
            while conn.execute(text("SELECT 1 FROM sites WHERE metro_id = :m AND slug = :s"),
                               {"m": metro_id, "s": slug}).first():
                n += 1
                slug = f"{slugify(name)}-{n}"
            site_id = conn.execute(text("""
                INSERT INTO sites (metro_id, slug, name, address_line1, address_line2, city, state_code, postal_code,
                                   latitude, longitude, time_zone, parking_notes_public, directions_public, is_active,
                                   airtable_record_id, source_synced_at)
                VALUES (:metro, :slug, :name, :l1, :l2, :city, :st, :zip, :lat, :lng, :tz, :park, :dir, :active,
                        :rid, now())
                RETURNING id
            """), {**params, "slug": slug}).scalar_one()

        private = {k: _text(v.get(k)) for k in ("access_code", "poc_name", "poc_phone", "poc_email", "emergency")}
        if private["poc_email"] and "@" not in private["poc_email"]:
            private["poc_email"] = None
        conn.execute(text("""
            INSERT INTO site_private_details (site_id, access_code, poc_name, poc_phone, poc_email, emergency_contact)
            VALUES (:s, :access_code, :poc_name, :poc_phone, :poc_email, :emergency)
            ON CONFLICT (site_id) DO UPDATE SET access_code = EXCLUDED.access_code, poc_name = EXCLUDED.poc_name,
                   poc_phone = EXCLUDED.poc_phone, poc_email = EXCLUDED.poc_email,
                   emergency_contact = EXCLUDED.emergency_contact, updated_at = now()
        """), {"s": site_id, **private})
        room_max_int = int(room_max) if room_max and room_max > 0 else None
        runs.resolve(conn, source=SOURCE, external_ref=record["id"])
    return {"site_id": site_id, "metro_id": metro_id, "time_zone": zone, "slug": slug, "name": name,
            "room_max": room_max_int}


def _room(conn: Connection, site: dict, room_name: str | None, family_max: int) -> dict:
    """The room a class is in: the named one, or the site's 'Main' room."""
    name = room_name or "Main"
    size = site["room_max"] or family_max
    row = conn.execute(text("""
        INSERT INTO rooms (site_id, name, max_student_count) VALUES (:s, :n, :m)
        ON CONFLICT (site_id, name) DO UPDATE SET is_active = true,
               max_student_count = CASE WHEN :explicit THEN EXCLUDED.max_student_count
                                        ELSE GREATEST(rooms.max_student_count, EXCLUDED.max_student_count) END
        RETURNING id, max_student_count
    """), {"s": site["site_id"], "n": name, "m": size, "explicit": site["room_max"] is not None}).mappings().one()
    return dict(row)


# --- Class Schedule -> classes -----------------------------------------------------

def _sync_schedule(m: dict, products: dict, sites: dict, run_id: int, stats: dict,
                   record_id: str | None) -> set[str]:
    fields = list(m["fields"].values())
    if record_id:
        rec = airtable.get_record(m["table"], record_id)
        records = [rec] if rec else []
        if rec is None:
            _unpublish_record(record_id, "The class was deleted in Airtable.", run_id, stats)
        else:
            # The other rows of the same class are needed to work out its state.
            with db.engine().connect() as c:
                siblings = c.execute(text("""
                    SELECT o.airtable_record_id FROM session_airtable_records me
                      JOIN session_airtable_records o ON o.session_id = me.session_id
                     WHERE me.airtable_record_id = :r AND o.airtable_record_id <> :r
                """), {"r": record_id}).scalars().all()
            records += [r for r in (airtable.get_record(m["table"], sid) for sid in siblings) if r]
    else:
        when = m["fields"].get("date") or m["fields"].get("start")
        formula = (f"OR({airtable.formula_field(when)} = BLANK(), "
                   f"IS_AFTER({airtable.formula_field(when)}, DATEADD(TODAY(), -2, 'days')))") if when else None
        records = airtable.list_records(m["table"], fields, formula)

    seen: set[str] = set()
    groups: dict[tuple, list[ClassRow]] = {}
    for record in records:
        seen.add(record["id"])
        stats["classes_seen"] += 1
        try:
            row = _parse_class(record, _values(record, m), products, sites)
        except RowProblem as exc:
            stats["classes_skipped"] += 1
            _problem(run_id, stats, record["id"], exc, m["table"])
            continue
        if row is None:
            stats["classes_unchanged"] += 1
            continue
        key = ("session", row.existing_id) if row.existing_id else \
            ("new", row.site["site_id"], row.main["family_id"], row.starts)
        groups.setdefault(key, []).append(row)

    for rows in groups.values():
        try:
            result = _apply_class(rows, m)
            stats[f"classes_{result}"] += len(rows)
        except RowProblem as exc:
            stats["classes_skipped"] += len(rows)
            for row in rows:
                _problem(run_id, stats, row.record["id"], exc, m["table"])
        except DBAPIError as exc:
            stats["classes_skipped"] += len(rows)
            for row in rows:
                _problem(run_id, stats, row.record["id"], RowProblem(_db_code(exc), _db_reason(exc)), m["table"])
    return seen


def _db_code(exc: DBAPIError) -> str:
    detail = str(exc.orig)
    if "sessions_no_room_overlap" in detail:
        return "ROOM_OVERLAP"
    if "is full" in detail:
        return "SEATS_BELOW_SOLD"
    return "CLASS_REFUSED"


def _db_reason(exc: DBAPIError) -> str:
    detail = str(exc.orig).splitlines()[0]
    if "sessions_no_room_overlap" in detail:
        return "another class is already in that room at that time. Give one of them a different room or time."
    if "is full" in detail:
        return "the new seat limit is lower than the seats already sold. Keep the old limit or move students first."
    return f"the database refused it ({detail[:200]})."


def _link(v, by_id: dict, by_name: dict) -> dict | None:
    if _is_record_ids(v):
        return by_id.get(v[0])
    name = _text(v)
    return by_name.get(name.lower()) if name else None


@dataclass
class ClassRow:
    """One Class Schedule row, read and checked, not saved yet."""
    record: dict
    values: dict
    statuses: list[str]
    published: bool
    cancelled: bool
    existing_id: int | None
    site: dict
    course_rows: list[dict]
    starts: datetime
    ends: datetime
    one_max: int
    offers: list[tuple[dict, int]]
    lead: str | None
    co: str | None
    room_name: str | None

    @property
    def main(self) -> dict:
        return self.course_rows[0]


def _parse_class(record: dict, v: dict, products: dict, sites: dict) -> ClassRow | None:
    """Read one row. None for a planning row that was never on sale (none of the engine's business)."""
    statuses = _statuses(v.get("status"))
    published = any(s.lower() == airtable.PUBLISH_STATUS for s in statuses)
    cancelled = any("cancel" in s.lower() for s in statuses)
    with db.engine().connect() as c:
        existing_id = c.execute(text("SELECT session_id FROM session_airtable_records WHERE airtable_record_id = :r"),
                                {"r": record["id"]}).scalar()
    if not existing_id and not published:
        return None

    site = _link(v.get("location"), sites, {s["name"].lower(): s for s in sites.values()})
    if not site:
        raise RowProblem("CLASS_UNKNOWN_LOCATION", "its Location isn't one of the synced Master Locations.")
    course_rows = _link(v.get("course"), products, {rows[0]["name"].lower(): rows for rows in products.values()})
    if not course_rows:
        raise RowProblem("CLASS_UNKNOWN_COURSE", "its Course isn't one of the synced Course Master rows.")
    main = course_rows[0]
    zone = ZoneInfo(site["time_zone"])

    starts = parse_instant(v.get("start"))
    day = parse_day(v.get("date")) or (starts.astimezone(zone).date() if starts else None)
    clock = parse_clock(v.get("start_time"))
    if clock is None and parse_instant(v.get("start_time")):
        starts = parse_instant(v.get("start_time"))
    if starts is None:
        if not day or not clock:
            raise RowProblem("CLASS_NO_START", "it needs a date and a start time.")
        starts = datetime.combine(day, clock, zone)
    starts = starts.astimezone(timezone.utc)
    ends = parse_instant(v.get("end")) or parse_instant(v.get("end_time"))
    end_clock = parse_clock(v.get("end_time"))
    if ends is None and end_clock:
        local_start = starts.astimezone(zone)
        ends = datetime.combine(local_start.date(), end_clock, zone)
        if ends <= local_start:
            ends += timedelta(days=1)
    if ends is None:
        dur = _number(v.get("duration"))
        if dur and dur > 0:
            seconds = dur if dur > 24 * 60 else dur * 60 if dur > 24 else dur * 3600   # seconds, minutes or hours
            ends = starts + timedelta(seconds=seconds)
    with db.engine().connect() as c:
        fam = c.execute(text("SELECT one_instructor_max, default_duration_min FROM course_families WHERE id = :f"),
                        {"f": main["family_id"]}).mappings().one()
    if ends is None:
        ends = starts + timedelta(minutes=fam["default_duration_min"] or 240)
    ends = ends.astimezone(timezone.utc)
    if ends <= starts:
        raise RowProblem("CLASS_BAD_TIMES", "its end time is before its start time.")

    seats = _number(v.get("seats"))
    one_max = int(seats) if seats and seats >= 1 else fam["one_instructor_max"]
    price = _cents(v.get("price"))
    companion_price = _cents(v.get("companion_price"))
    offers = [(main, price if price is not None and price >= 0 else main["price"])]
    if len(course_rows) > 1:
        offers.append((course_rows[1], companion_price if companion_price is not None and companion_price >= 0
                       else course_rows[1]["price"]))
    return ClassRow(
        record=record, values=v, statuses=statuses, published=published, cancelled=cancelled,
        existing_id=existing_id, site=site, course_rows=course_rows, starts=starts, ends=ends, one_max=one_max,
        offers=offers,
        lead=None if _is_record_ids(v.get("lead_instructor")) else _text(v.get("lead_instructor")),
        co=None if _is_record_ids(v.get("co_instructor")) else _text(v.get("co_instructor")),
        room_name=None if _is_record_ids(v.get("room")) else _text(v.get("room")))


def _apply_class(rows: list[ClassRow], m: dict) -> str:
    """Save one class from all of its rows (usually one; A&P + Adult rows are two).

    The rows must agree on site, course family and times. The class is
    cancelled only when every row says Cancelled (a cancelled Adult row just
    stops selling Adult), and on sale when any live row has Enrollware."""
    first = rows[0]
    for other in rows[1:]:
        if (other.site["site_id"], other.main["family_id"], other.main["delivery"], other.starts, other.ends) != \
                (first.site["site_id"], first.main["family_id"], first.main["delivery"], first.starts, first.ends):
            raise RowProblem("CLASS_ROWS_DISAGREE", "the rows for this class (e.g. its A&P and Adult rows) have "
                             "different locations, courses or times. Make them match.")
    live = [r for r in rows if not r.cancelled]
    cancelled = not live
    published = any(r.published for r in live)
    statuses = sorted({s for r in rows for s in r.statuses})
    site, main, starts, ends = first.site, first.main, first.starts, first.ends
    zone = ZoneInfo(site["time_zone"])
    one_max = max(r.one_max for r in rows)
    lead = next((r.lead for r in rows if r.lead), None)
    co = next((r.co for r in rows if r.co), None)
    room_name = next((r.room_name for r in rows if r.room_name), None)
    offered = {product["id"]: (product, cents) for r in live for product, cents in r.offers}
    withdrawn = {product["id"]: product for r in rows if r.cancelled for product, _ in r.offers
                 if product["id"] not in offered}
    existing_id = next((r.existing_id for r in rows if r.existing_id), None)

    with db.transaction() as conn:
        changed = False
        for r in rows:
            changed = _register(conn, r.record, m["table"], r.values) or changed
        room = _room(conn, site, room_name, one_max)
        capacity = min(one_max, room["max_student_count"])
        session = None
        if existing_id:
            session = conn.execute(text("SELECT * FROM sessions WHERE id = :id FOR NO KEY UPDATE"),
                                   {"id": existing_id}).mappings().first()
        if session is None:
            # A row added later for a class that already exists (same site, family, start) joins it.
            session = conn.execute(text("""
                SELECT * FROM sessions WHERE site_id = :site AND course_family_id = :fam AND starts_at = :st
                   AND status <> 'cancelled' ORDER BY id LIMIT 1 FOR NO KEY UPDATE
            """), {"site": site["site_id"], "fam": main["family_id"], "st": starts}).mappings().first()

        if session is None:
            base_slug = f"{starts.astimezone(zone).date().isoformat()}-{site['slug']}"
            slug = base_slug
            if conn.execute(text("SELECT 1 FROM sessions WHERE metro_id = :m AND course_family_id = :f AND slug = :s"),
                            {"m": site["metro_id"], "f": main["family_id"], "s": slug}).first():
                slug = f"{base_slug}-{starts.astimezone(zone):%H%M}"
            sid = conn.execute(text("""
                INSERT INTO sessions (metro_id, site_id, room_id, course_family_id, delivery_mode, starts_at, ends_at,
                                      time_zone, slug, status, is_published, airtable_status, cancelled_at, cancel_reason,
                                      one_instructor_max, room_max_student_count, capacity,
                                      airtable_lead_instructor, airtable_co_instructor, source_synced_at)
                VALUES (:metro, :site, :room, :fam, :del, :st, :en, :tz, :slug,
                        CASE WHEN :cancelled THEN 'cancelled' ELSE 'scheduled' END, :pub, :raw,
                        CASE WHEN :cancelled THEN now() END, CASE WHEN :cancelled THEN 'Cancelled in Airtable' END,
                        :one, :roommax, :cap, :lead, :co, now())
                RETURNING id
            """), {"metro": site["metro_id"], "site": site["site_id"], "room": room["id"], "fam": main["family_id"],
                   "del": main["delivery"], "st": starts, "en": ends, "tz": site["time_zone"], "slug": slug,
                   "cancelled": cancelled, "pub": published, "raw": statuses, "one": one_max,
                   "roommax": room["max_student_count"], "cap": capacity, "lead": lead, "co": co}).scalar_one()
            result = "created"
        else:
            sid = session["id"]
            if session["course_family_id"] != main["family_id"] or session["delivery_mode"] != main["delivery"]:
                raise RowProblem("CLASS_COURSE_CHANGED", "its course changed to a different seat pool. Cancel this "
                                 "class and add a new row for the new course.")
            moved = session["starts_at"] != starts
            # Airtable may undo its own cancellation, never one made in the engine.
            engine_cancelled = session["status"] == "cancelled" and \
                (session["cancel_reason"] or "Cancelled in Airtable") != "Cancelled in Airtable"
            if cancelled or engine_cancelled:
                new_status = "cancelled"
            else:
                new_status = "scheduled" if session["status"] == "cancelled" else session["status"]
            keep_capacity = session["capacity_basis"] == "co_instructor"
            conn.execute(text("""
                UPDATE sessions SET site_id = :site, room_id = :room, starts_at = :st, ends_at = :en, time_zone = :tz,
                       metro_id = :metro,
                       status = :status, is_published = :pub, airtable_status = :raw,
                       cancelled_at = CASE WHEN :status = 'cancelled' THEN COALESCE(cancelled_at, now()) END,
                       cancel_reason = CASE WHEN :status = 'cancelled' THEN COALESCE(cancel_reason, 'Cancelled in Airtable') END,
                       previous_starts_at = CASE WHEN :moved THEN starts_at ELSE previous_starts_at END,
                       rescheduled_at = CASE WHEN :moved THEN now() ELSE rescheduled_at END,
                       one_instructor_max = :one, room_max_student_count = :roommax,
                       capacity = CASE WHEN :keep THEN LEAST(capacity, :roommax) ELSE :cap END,
                       airtable_lead_instructor = :lead, airtable_co_instructor = :co,
                       source_synced_at = now(), source_deleted_at = NULL
                 WHERE id = :id
            """), {"id": sid, "site": site["site_id"], "metro": site["metro_id"], "room": room["id"], "st": starts,
                   "en": ends, "tz": site["time_zone"], "status": new_status,
                   "pub": published and new_status != "cancelled", "raw": statuses, "moved": moved, "one": one_max,
                   "roommax": room["max_student_count"], "cap": capacity, "keep": keep_capacity, "lead": lead, "co": co})
            result = "updated" if changed or moved else "unchanged"
            if new_status == "cancelled" and session["status"] != "cancelled":
                sold = conn.execute(text("""SELECT count(*) FROM registrations WHERE session_id = :id
                                             AND status IN ('confirmed', 'awaiting_payment')"""), {"id": sid}).scalar()
                if sold:
                    runs.issue(conn, source=SOURCE, code="CLASS_CANCELLED_WITH_STUDENTS", severity="error",
                               external_ref=first.record["id"], entity_type="session", entity_id=sid,
                               message=f"A class with {sold} booked student{'s' if sold != 1 else ''} was cancelled "
                                       "in Airtable. Move or refund them.")

        for r in rows:
            conn.execute(text("""
                INSERT INTO session_airtable_records (airtable_record_id, session_id) VALUES (:r, :s)
                ON CONFLICT (airtable_record_id) DO UPDATE SET session_id = EXCLUDED.session_id
            """), {"r": r.record["id"], "s": sid})
        for product, cents in offered.values():
            conn.execute(text("""
                INSERT INTO session_products (session_id, product_id, course_family_id, delivery_mode, price_cents)
                VALUES (:s, :p, :f, :d, :c)
                ON CONFLICT (session_id, product_id) DO UPDATE SET price_cents = EXCLUDED.price_cents, is_offered = true
            """), {"s": sid, "p": product["id"], "f": product["family_id"], "d": product["delivery"], "c": cents})
        for product in withdrawn.values():
            conn.execute(text("UPDATE session_products SET is_offered = false WHERE session_id = :s AND product_id = :p"),
                         {"s": sid, "p": product["id"]})
        access = next((_text(r.values.get("access_code")) for r in rows if _text(r.values.get("access_code"))), None)
        notes = next((_text(r.values.get("notes")) for r in rows if _text(r.values.get("notes"))), None)
        if access or notes:
            conn.execute(text("""
                INSERT INTO session_private_details (session_id, access_code, instructor_notes) VALUES (:s, :a, :n)
                ON CONFLICT (session_id) DO UPDATE SET access_code = EXCLUDED.access_code,
                       instructor_notes = EXCLUDED.instructor_notes, updated_at = now()
            """), {"s": sid, "a": access, "n": notes})
        for r in rows:
            runs.resolve(conn, source=SOURCE, external_ref=r.record["id"])
    return result


def _unpublish_record(record_id: str, why: str, run_id: int, stats: dict) -> None:
    with db.transaction() as conn:
        sid = conn.execute(text("""
            UPDATE sessions s SET is_published = false, source_deleted_at = COALESCE(s.source_deleted_at, now())
              FROM session_airtable_records sar
             WHERE sar.airtable_record_id = :r AND sar.session_id = s.id AND s.is_published
               AND NOT EXISTS (SELECT 1 FROM session_airtable_records o JOIN airtable_records ar
                                 ON ar.record_id = o.airtable_record_id
                                WHERE o.session_id = s.id AND o.airtable_record_id <> :r
                                  AND ar.source_deleted_at IS NULL)
            RETURNING s.id
        """), {"r": record_id}).scalar()
        conn.execute(text("UPDATE airtable_records SET source_deleted_at = now() WHERE record_id = :r"),
                     {"r": record_id})
        if sid:
            stats["classes_removed"] += 1
            sold = conn.execute(text("""SELECT count(*) FROM registrations WHERE session_id = :id
                                         AND status IN ('confirmed', 'awaiting_payment')"""), {"id": sid}).scalar()
            if sold:
                stats["problems"] += 1
                runs.issue(conn, source=SOURCE, code="CLASS_REMOVED_WITH_STUDENTS", severity="error",
                           run_id=run_id, external_ref=record_id, entity_type="session", entity_id=sid,
                           message=f"{why} It has {sold} booked student{'s' if sold != 1 else ''}; it's off sale "
                                   "but not cancelled. Cancel it in Airtable and move or refund them.")


def _mark_removed(seen: set[str], run_id: int, stats: dict) -> None:
    """Upcoming classes whose Airtable row is gone (or left the sync window) come off sale."""
    with db.engine().connect() as c:
        gone = [r for (r,) in c.execute(text("""
            SELECT sar.airtable_record_id FROM session_airtable_records sar
              JOIN sessions s ON s.id = sar.session_id
             WHERE s.starts_at > now() AND s.is_published
        """)) if r not in seen]
    for record_id in gone:
        _unpublish_record(record_id, "The class is no longer in Airtable's schedule.", run_id, stats)


def check_fields() -> dict:
    """For the Integrations page: how the base's fields match up."""
    return airtable.resolve(airtable.read_schema())
