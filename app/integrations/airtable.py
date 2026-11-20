"""Reading the NBC Master Base.

Secrets: AIRTABLE_TOKEN (a personal access token with data.records:read and
schema.bases:read on the NBC Master Base only) and AIRTABLE_BASE_ID ('app…').
Staging and production read the same base; neither writes to it here.

Field names live in FIELDS below and nowhere else. Each logical field lists
the names it may have in the base (matched without regard to case or
spaces); the first one the base actually has is used. Before every sync the
base's own list of fields is read (schema.bases:read), so:
  * only fields that exist are requested (Airtable refuses unknown names), and by field id, since
    Airtable also refuses a name two fields share (Class Schedule has two "Instructor availability
    status" fields; the one with a value is used);
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
    "instructors": ("AIRTABLE_TABLE_INSTRUCTORS", "Instructors"),
    "recruiting":  ("AIRTABLE_TABLE_RECRUITING", "Instructor Recruiting Needs"),
}
# Tables the classes don't depend on: if one is missing, the sync says so and carries on.
OPTIONAL_TABLES = {"instructors", "recruiting"}

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
        # 0011: how the location is staffed (location_instructor_settings).
        "assignment_types": ["Instructor Assignment Type"],
        "primary_instructor": ["Primary Instructor"],
        "hub_primary":      ["HUB Primary Instructor"],
        "hub_secondary":    ["HUB Secondary Instructor"],
        "it_location":      ["Instructor Training Location"],
        "instructor_notes": ["Instructor Notes"],
        "extra_pay":        ["Instructor Extra Pay per Class"],
        "extra_pay_reason": ["Instructor Extra Pay Reason"],
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
        # 0011: the class's instructor planning fields (session_instructor_details).
        "instructor_assigned": ["Instructor Assigned"],
        "availability_status": ["Instructor availability status"],
        "performance_tier":    ["Instructor performance tier"],
    },
    # 0011: Airtable is the source of instructor data (11/16); GHL holds none (11/20), so its GHL fields aren't read.
    "instructors": {
        "name":               ["Name"],
        "hub_key":            ["Hub Key"],
        "email":              ["Email"],
        "phone":              ["Phone"],
        "city":               ["City"],
        "state":              ["State"],
        "zip":                ["ZIP", "Zip"],
        "active_in_enrollware": ["Active in Enrollware"],
        "metros":             ["Metros"],
        "hub_instructor":     ["HUB Instructor"],
        "cert_fa":            ["FA/CPR/AED Instructor r.25 Expiration"],
        "cert_bls":           ["BLS Instructor r.25 Expiration"],
        "cert_it":            ["Instructor Trainer r.25 Expiration"],
        "additional_certs":   ["Additional Certs"],
        "certs_synced":       ["Certs Last Synced"],
        "days":               ["Available Days"],
        "times":              ["Times"],
        "per_month":          ["Classes per Month"],
        "travel":             ["Max Travel (miles)"],
        "avoid":              ["Locations to Avoid"],
        "notice":             ["Notice Needed"],
        "lead_hub":           ["Interested in Leading a HUB"],
        "avail_notes":        ["Availability Notes"],
        "avail_updated":      ["Availability Updated"],
        "avail_by":           ["Availability Updated By"],
        "outreach_status":    ["Outreach Status"],
        "last_contact":       ["Last Contact"],
        "follow_up":          ["Next Follow-up"],
        "outreach_notes":     ["Outreach Notes"],
        "removal_recommended": ["Removal Recommended"],
        "removal_reason":     ["Removal Reason"],
        "remove_from":        ["Remove From"],
        "removal_status":     ["Removal Status"],
        "associate":          ["Associate"],
        "pre_approved":       ["Locations - Pre-Approval"],
        "taught":             ["Locations - Taught At"],
        "taught_inactive":    ["Inactive Locations - Taught At"],
        "hub_lead":           ["HUB Lead"],
        "hub_members":        ["HUB Team Members"],
        "approval_link":      ["Date Approval Link"],
        "other_enrollware":   ["Other Enrollware Accounts"],
        "page_type":          ["Instructor Page Type"],
    },
    "recruiting": {
        "need":               ["Need"],
        "location":           ["Location"],
        "reasons":            ["Need Reason"],
        "status":             ["Recruiting Status"],
        "notes":              ["Recruiting Notes"],
        "classes":            ["No-Instructor Cancelled Classes"],
        "ew_cancels":         ["Enrollware No-Instructor Cancels (12 mo to Oct 2026)", "Enrollware No-Instructor Cancels"],
        "cls_lost":           ["CLS Lost to No-Instructor Cancels (12 mo)", "CLS Lost to No-Instructor Cancels"],
        "profit":             ["Profit While Running (last 12 mo)", "Profit While Running"],
        "students_per_class": ["Students per Class (while running)", "Students per Class"],
        "last_month":         ["Last Month Held"],
        "date_added":         ["Date Added"],
    },
}

# What a table can't be synced without. ("a|b" = either one.)
REQUIRED: dict[str, list[str]] = {
    "courses":   ["name"],
    "locations": ["name", "address"],
    "schedule":  ["status", "location", "course", "date", "start_time"],
    "instructors": ["name"],
    "recruiting":  [],
}

# A class is on sale when its Status includes one of these (decided Oct 13, 2026: "Enrollware"; from 11/20
# "Booking App" too, so the status can be renamed in Airtable once Enrollware is gone).
PUBLISH_STATUSES = ("enrollware", "booking app")
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
    # Airtable asks for 30 s after a rate-limit answer: wait once, then give up until the next sync.
    return request("GET", f"{API}/{path}", service="Airtable", params=params, attempts=2, rate_limit_wait=30,
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
    out: dict[str, dict[str, dict]] = {}
    for t in body.get("tables", []):
        cols: dict[str, dict] = {}
        for f in t.get("fields", []):
            # Two fields may share a name (Class Schedule has two "Instructor availability status"):
            # keep every id, so both can be read (see records()).
            hit = cols.setdefault(_norm(f["name"]), {"name": f["name"], "type": f.get("type"), "ids": []})
            if f.get("id"):
                hit["ids"].append(f["id"])
        out[t["name"]] = cols
    return out


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
        ids: dict[str, list[str]] = {}
        if actual:
            cols = schema[actual]
            for logical, names in FIELDS[key].items():
                for candidate in names:
                    hit = cols.get(_norm(candidate))
                    if hit:
                        fields[logical], types[logical] = hit["name"], hit["type"]
                        ids[logical] = list(hit.get("ids") or [])
                        break
        missing_required = [req for req in REQUIRED[key]
                            if actual and not any(alt in fields for alt in req.split("|"))]
        missing_optional = [f for f in FIELDS[key] if actual and f not in fields
                            and not any(f in req.split("|") for req in REQUIRED[key])]
        out[key] = {"table": actual, "wanted": wanted, "fields": fields, "types": types, "ids": ids,
                    "shared_names": sorted(fields[lg] for lg, i in ids.items() if len(i) > 1),
                    "missing_required": missing_required, "missing_optional": missing_optional,
                    "known": FIELDS[key]}
    return out


def list_records(table: str, fields: list[str], formula: str | None = None, *,
                 by_id: bool = False) -> Iterator[dict]:
    """Every record in a table (100 per call), with only the given fields (names, or field ids with by_id,
    in which case the answer is keyed by field id too)."""
    params: list[tuple[str, str]] = [("pageSize", "100")] + [("fields[]", f) for f in fields]
    if by_id:
        params.append(("returnFieldsByFieldId", "true"))
    if formula:
        params.append(("filterByFormula", formula))
    offset = None
    while True:
        page = _get(f"{base_id()}/{_quote(table)}", params + ([("offset", offset)] if offset else []))
        yield from page.get("records", [])
        offset = page.get("offset")
        if not offset:
            return


def get_record(table: str, record_id: str, m: dict | None = None) -> dict | None:
    """One record. With a resolved mapping m, its mapped fields keyed by name (as records() gives them)."""
    if not re.fullmatch(r"rec[A-Za-z0-9]{14}", record_id or ""):
        raise IntegrationError("Airtable: that isn't an Airtable record id.", retry=False)
    by_id = m is not None and _has_ids(m)
    try:
        rec = _get(f"{base_id()}/{_quote(table)}/{record_id}",
                   [("returnFieldsByFieldId", "true")] if by_id else None)
    except IntegrationError as exc:
        if exc.status == 404:
            return None
        raise
    return _by_name(rec, m) if by_id and rec else rec


def _has_ids(m: dict) -> bool:
    ids = m.get("ids") or {}
    return bool(m.get("fields")) and all(ids.get(logical) for logical in m["fields"])


def _by_name(record: dict, m: dict) -> dict:
    """A record answered by field id, keyed by field name again. A name two fields share takes the
    first of them that has a value."""
    got = record.get("fields") or {}
    fields: dict[str, Any] = {}
    for logical, name in m["fields"].items():
        for fid in m["ids"][logical]:
            value = got.get(fid)
            if value not in (None, "", [], {}):
                fields[name] = value
                break
    return {**record, "fields": fields}


def records(m: dict, formula: str | None = None) -> Iterator[dict]:
    """Every record of a resolved table (resolve()), with its mapped fields keyed by name.

    Fields are asked for by id whenever the base gives ids: Airtable refuses a field *name* that two
    fields share (422), which would stop the whole sync."""
    names = list(m["fields"].values())
    if not _has_ids(m):
        yield from list_records(m["table"], names, formula)
        return
    wanted = list(dict.fromkeys(fid for logical in m["fields"] for fid in m["ids"][logical]))
    for record in list_records(m["table"], wanted, formula, by_id=True):
        yield _by_name(record, m)


def _quote(table: str) -> str:
    from urllib.parse import quote
    return quote(table, safe="")


def formula_field(name: str) -> str:
    return "{" + name.replace("}", "\\}") + "}"


def on_sale(statuses) -> bool:
    """Does a Class Schedule row's Status put the class on sale?"""
    return any(str(st).strip().lower() in PUBLISH_STATUSES for st in statuses or [])
