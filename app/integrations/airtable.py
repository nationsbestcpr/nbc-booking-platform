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
        "name":               ["Converted Course Name", "Course Name", "Name", "Course"],
        "family":             ["Course Family", "Family"],
        "delivery":           ["Delivery Type", "Delivery", "Delivery Mode", "Format"],
        "max_seats":          ["Max Seats", "Max Students", "Seat Cap"],
        "price":              ["Price", "Course Price"],
        "companion_price":    ["Companion Price"],
        "duration":           ["Duration in Minutes", "Duration (min)"],
        "companion_duration": ["Companion Duration (min)"],
        "group_key":          ["Concurrent Group Key"],
        "status":             ["Status", "Active"],
    },
    "locations": {
        "name":         ["Name", "Location Name", "Site Name"],
        "metro":        ["Metro Area", "Metro", "Market"],
        "address":      ["Address", "Street Address", "Full Address"],
        "address2":     ["Address Line 2", "Suite"],
        "city":         ["City"],
        "state":        ["State"],
        "zip":          ["Zip", "Zip Code", "Postal Code"],
        "time_zone":    ["Time Zone", "Timezone"],
        "lat":          ["Latitude", "Lat"],
        "lng":          ["Longitude", "Long", "Lng"],
        "max_students": ["Max Student Count", "Max Students"],
        "parking":      ["Parking", "Parking Notes"],
        "access_code":  ["Access Code", "Door Code"],
        "access_info":  ["Access Information", "Access Instructions"],
        "poc_name":     ["POC Name"],
        "poc_phone":    ["POC Phone"],
        "poc_email":    ["POC Email"],
        "emergency":    ["Emergency Contact"],
        "status":       ["Status"],
    },
    "schedule": {
        "status":          ["Status"],
        "date":            ["Date", "Class Date"],
        "start_time":      ["Start Time", "Class Start Time"],
        "duration":        ["Duration in Minutes (from Duration (min))", "Duration in Minutes", "Duration"],
        "location":        ["Abbreviation", "Master Location", "Location"],
        "course":          ["Course Type", "Course Master"],
        "lead_instructor": ["ASSIGN INSTRUCTOR (Name)", "HUB Primary Instructor (LUp)", "Lead Instructor"],
        "co_instructor":   ["Co-Instructor", "Co Instructor", "Helper Instructor"],
        "room":            ["Room"],
        "seats":           ["Max Seats", "Seat Count"],
        "price":           ["Class Price"],
        "companion_price": ["Class Companion Price"],
        "access_code":     ["Access Code"],
        "notes":           ["Instructor Notes"],
    },
}

# What a table can't be synced without. ("a|b" = either one.)
REQUIRED: dict[str, list[str]] = {
    "courses":   ["name"],
    "locations": ["name", "address"],
    "schedule":  ["status", "location", "course", "date", "start_time"],
}

PUBLISH_STATUS = "enrollware"     # Status includes "Enrollware" (decided Oct 13, 2026)
CANCELLED_PREFIX = "cancelled"    # "CANCELLED - Weather", "CANCELLED - NBC", ... (not "Send POC Cancellation")


def configured() -> bool:
    return bool(os.environ.get("AIRTABLE_TOKEN") and os.environ.get("AIRTABLE_BASE_ID"))


def base_id() -> str:
    """The base id, also when the secret holds the base's whole address
    (https://airtable.com/appXXXX/tblYYYY/...) or stray spaces/quotes."""
    raw = os.environ.get("AIRTABLE_BASE_ID", "")
    found = re.search(r"app[A-Za-z0-9]{14}", raw)
    return found.group(0) if found else raw.strip().strip("'\"")


def masked_base_id() -> str:
    """'applTS…39': enough to recognise the base without printing the whole secret value."""
    b = base_id()
    return f"{b[:6]}…{b[-2:]}" if len(b) > 9 else "(not set)"


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
    """{table name: {normalised field name: {"name", "type"}}} for the base.
    The first call of every sync, so access problems are explained here."""
    if not re.fullmatch(r"app[A-Za-z0-9]{14}", base_id()):
        raise IntegrationError("Airtable: AIRTABLE_BASE_ID isn't a base id. It starts with “app” and has 17 "
                               "characters (the NBC Master Base's starts with applTS).", retry=False)
    try:
        body = _get(f"meta/bases/{base_id()}/tables")
    except IntegrationError as exc:
        if exc.status == 401:
            raise IntegrationError("Airtable: the token was refused. Copy AIRTABLE_TOKEN again (it starts with "
                                   "“pat”) or make a new one.", status=401, retry=False) from exc
        if exc.status in (403, 404):
            raise IntegrationError(
                f"Airtable: this token can't open base {masked_base_id()}. On airtable.com/create/tokens, edit the "
                "token: under Scopes add data.records:read and schema.bases:read, and under Access add "
                "“NBC Master Base”. The token must be made by an Airtable account that can open the base.",
                status=exc.status, retry=False) from exc
        raise
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
