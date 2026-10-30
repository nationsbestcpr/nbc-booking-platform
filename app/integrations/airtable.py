"""Reading the NBC Master Base.

Secrets: AIRTABLE_TOKEN (a personal access token with data.records:read and
schema.bases:read on the NBC Master Base only) and AIRTABLE_BASE_ID ('app…').
Staging and production read the same base; neither writes to it here.

Field names live in FIELDS below and nowhere else. Each logical field lists
the names it may have in the base (matched without regard to case or
spaces); the first one the base actually has is used. Before every sync the
base's own list of fields is read (schema.bases:read), so:
  * only fields that exist are requested (Airtable refuses unknown names);
  * a missing required field becomes one clear problem on the dashboard,
    instead of a failed sync with a cryptic error;
  * "Check Airtable fields" on the Integrations page shows the whole mapping.
Table names can be changed with AIRTABLE_TABLE_SCHEDULE / _LOCATIONS /
_COURSES if the base renames them.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Iterator

from .http import IntegrationError, NotConfigured, request

API = "https://api.airtable.com/v0"
MIN_SECONDS_BETWEEN_CALLS = 0.22          # Airtable allows 5 requests per second per base

TABLES = {
    "courses":   ("AIRTABLE_TABLE_COURSES", "Course Master"),
    "locations": ("AIRTABLE_TABLE_LOCATIONS", "Master Locations"),
    "schedule":  ("AIRTABLE_TABLE_SCHEDULE", "Class Schedule"),
}

# logical field -> names it may have in the base, most likely first
FIELDS: dict[str, dict[str, list[str]]] = {
    "courses": {
        "name":            ["Course Name", "Name", "Course", "Course Title"],
        "family":          ["Course Family", "Family", "Course Type"],
        "delivery":        ["Delivery", "Delivery Mode", "Delivery Method", "Format", "Class Format"],
        "short_name":      ["Short Name", "Abbreviation", "Course Abbreviation"],
        "ghl_code":        ["GHL Course Code", "Course Code", "Code"],
        "max_seats":       ["Max Seats", "Max Students", "Seat Cap", "Capacity"],
        "price":           ["Price", "Course Price", "Base Price"],
        "companion_name":  ["Companion Course", "Companion Name", "Companion"],
        "companion_price": ["Companion Price"],
        "group_key":       ["Concurrent Group Key"],
        "duration":        ["Duration", "Class Length", "Length (hours)", "Hours"],
        "active":          ["Active", "Is Active"],
    },
    "locations": {
        "name":         ["Location Name", "Name", "Location", "Site Name", "Venue"],
        "metro":        ["Metro", "Market", "Metro Area", "Region", "Hub"],
        "market_code":  ["Market Code", "Metro Code"],
        "address":      ["Street Address", "Address", "Address Line 1", "Full Address", "Location Address"],
        "address2":     ["Address Line 2", "Suite", "Suite/Room"],
        "city":         ["City"],
        "state":        ["State", "ST"],
        "zip":          ["Zip", "Zip Code", "ZIP", "Postal Code"],
        "time_zone":    ["Time Zone", "Timezone", "TZ"],
        "lat":          ["Latitude", "Lat"],
        "lng":          ["Longitude", "Long", "Lng"],
        "max_students": ["Max Student Count", "Max Students", "Room Capacity", "Capacity"],
        "parking":      ["Parking Notes", "Parking", "Parking Instructions"],
        "directions":   ["Directions", "Public Directions", "Entrance"],
        "access_code":  ["Access Code", "Door Code"],
        "poc_name":     ["POC Name", "POC", "Point of Contact", "Location POC"],
        "poc_phone":    ["POC Phone", "POC Phone Number"],
        "poc_email":    ["POC Email"],
        "emergency":    ["Emergency Contact"],
        "active":       ["Active", "Is Active"],
    },
    "schedule": {
        "status":          ["Status"],
        "start":           ["Start", "Start Date/Time", "Class Start", "Start Date Time", "Start DateTime"],
        "date":            ["Class Date", "Date", "Course Date"],
        "start_time":      ["Start Time", "Class Start Time", "Time"],
        "end_time":        ["End Time", "Class End Time"],
        "end":             ["End", "End Date/Time", "Class End"],
        "duration":        ["Duration", "Class Length"],
        "location":        ["Location", "Master Location", "Master Locations", "Site", "Training Site"],
        "course":          ["Course", "Course Master", "Course Name", "Class Type"],
        "room":            ["Room"],
        "seats":           ["Max Seats", "Seat Count", "Seats", "Max Students"],
        "price":           ["Price", "Class Price"],
        "companion_price": ["Companion Price"],
        "lead_instructor": ["Instructor Assigned", "Instructor", "Lead Instructor", "Assigned Instructor"],
        "co_instructor":   ["Co-Instructor", "Co Instructor", "Helper Instructor"],
        "access_code":     ["Access Code"],
        "notes":           ["Instructor Notes"],
    },
}

# What a table can't be synced without. ("a|b" = either one.)
REQUIRED: dict[str, list[str]] = {
    "courses":   ["name"],
    "locations": ["name", "address"],
    "schedule":  ["status", "location", "course", "start|date"],
}

PUBLISH_STATUS = "enrollware"     # Status includes "Enrollware" (decided Oct 13, 2026)


def configured() -> bool:
    return bool(os.environ.get("AIRTABLE_TOKEN") and os.environ.get("AIRTABLE_BASE_ID"))


def base_id() -> str:
    return os.environ.get("AIRTABLE_BASE_ID", "").strip()


def table_name(key: str) -> str:
    env, default = TABLES[key]
    return os.environ.get(env, "").strip() or default


_last_call = 0.0


def _get(path: str, params: Any = None) -> dict:
    global _last_call
    token = os.environ.get("AIRTABLE_TOKEN", "").strip()
    if not token or not base_id():
        raise NotConfigured("Airtable: AIRTABLE_TOKEN and AIRTABLE_BASE_ID are not both set.")
    wait = MIN_SECONDS_BETWEEN_CALLS - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()
    return request("GET", f"{API}/{path}", service="Airtable", params=params,
                   headers={"Authorization": f"Bearer {token}"})


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def read_schema() -> dict[str, dict[str, dict]]:
    """{table name: {normalised field name: {"name", "type"}}} for the base."""
    body = _get(f"meta/bases/{base_id()}/tables")
    return {t["name"]: {_norm(f["name"]): {"name": f["name"], "type": f.get("type")} for f in t.get("fields", [])}
            for t in body.get("tables", [])}


def resolve(schema: dict[str, dict[str, dict]]) -> dict[str, dict]:
    """Match FIELDS to the base. Per table key:
    {"table": actual name or None, "fields": {logical: actual name}, "types": {logical: type},
     "missing_required": [...], "missing_optional": [...]}"""
    by_norm = {_norm(name): name for name in schema}
    out: dict[str, dict] = {}
    for key in FIELDS:
        wanted = table_name(key)
        actual = by_norm.get(_norm(wanted))
        fields: dict[str, str] = {}
        types: dict[str, str] = {}
        if actual:
            cols = schema[actual]
            for logical, names in FIELDS[key].items():
                for candidate in names:
                    hit = cols.get(_norm(candidate))
                    if hit:
                        fields[logical], types[logical] = hit["name"], hit["type"]
                        break
        missing_required = [req for req in REQUIRED[key]
                            if actual and not any(alt in fields for alt in req.split("|"))]
        missing_optional = [f for f in FIELDS[key] if actual and f not in fields
                            and not any(f in req.split("|") for req in REQUIRED[key])]
        out[key] = {"table": actual, "wanted": wanted, "fields": fields, "types": types,
                    "missing_required": missing_required, "missing_optional": missing_optional,
                    "known": FIELDS[key]}
    return out


def list_records(table: str, fields: list[str], formula: str | None = None) -> Iterator[dict]:
    """Every record in a table (100 per call), with only the given fields."""
    params: list[tuple[str, str]] = [("pageSize", "100")] + [("fields[]", f) for f in fields]
    if formula:
        params.append(("filterByFormula", formula))
    offset = None
    while True:
        page = _get(f"{base_id()}/{_quote(table)}", params + ([("offset", offset)] if offset else []))
        yield from page.get("records", [])
        offset = page.get("offset")
        if not offset:
            return


def get_record(table: str, record_id: str) -> dict | None:
    if not re.fullmatch(r"rec[A-Za-z0-9]{14}", record_id or ""):
        raise IntegrationError("Airtable: that isn't an Airtable record id.", retry=False)
    try:
        return _get(f"{base_id()}/{_quote(table)}/{record_id}")
    except IntegrationError as exc:
        if exc.status == 404:
            return None
        raise


def _quote(table: str) -> str:
    from urllib.parse import quote
    return quote(table, safe="")


def formula_field(name: str) -> str:
    return "{" + name.replace("}", "\\}") + "}"
