"""GHL (HighLevel): one contact per student, one opportunity per seat.

Secrets: GHL_API_TOKEN (a Private Integration token from the sub-account:
Settings -> Private Integrations, with contacts.write, contacts.readonly,
opportunities.write, opportunities.readonly) and GHL_LOCATION_ID (the
sub-account id; Sandbox 2 in staging, the NBC sub-account in production).
Optional GHL_PIPELINE_NAME (default "Community Class").

Every confirmed seat queues a "sync_registration" job (outbox). The job:
  1. upserts the attendee's contact (matched by email in GHL);
  2. creates the opportunity "YYYY-MM-DD | COURSE-DELIVERY | MARKET-City" in
     the Community Class pipeline at the stage for the seat's state (Pending
     for a booked seat), or moves the existing one if the state changed;
  3. saves the contact and opportunity ids on the engine's records.
It is safe to run twice: an opportunity already made for that seat (or one
with the same name on the same contact) is reused, never duplicated.

The engine drives the stage (never drag stages in GHL; the plan's golden
rule). Stage ids differ between Sandbox 2 and live, so they're data
(ghl_stage_map), loaded from GHL by name with "Load pipeline stages" on the
Integrations page or automatically on the first sync.

Incoming GHL webhooks (contact edits, messages, stage drags) need the
Marketplace app from the launch plan (15.5); that's the next step and is not
needed to create contacts and opportunities.
"""

from __future__ import annotations

import json
import os

from sqlalchemy import text

from .. import db
from .http import IntegrationError, NotConfigured, request
from .outbox import handler

API = "https://services.leadconnectorhq.com"
API_VERSION = "2021-07-28"

# engine state -> stage names it may have in the pipeline (first match wins)
STAGE_NAMES = {
    "booked":     ["pending", "booked", "registered", "confirmed"],
    "waitlisted": ["waitlisted", "waitlist", "waiting list"],
    "cancelled":  ["cancelled", "canceled"],
    "completed":  ["complete", "completed"],
    "no_show":    ["no show", "no-show", "noshow"],
    "displaced":  ["pending", "displaced"],
}

STATE_FOR_STATUS = {
    "confirmed": "booked", "awaiting_payment": "booked", "displaced": "displaced",
    "completed": "completed", "no_show": "no_show", "cancelled": "cancelled", "forfeited": "cancelled",
}


def token() -> str:
    return os.environ.get("GHL_API_TOKEN", "").strip()


def location_id() -> str:
    return os.environ.get("GHL_LOCATION_ID", "").strip()


def pipeline_name() -> str:
    return os.environ.get("GHL_PIPELINE_NAME", "").strip() or "Community Class"


def configured() -> bool:
    return bool(token() and location_id())


def _call(method: str, path: str, *, params=None, json_body=None) -> dict:
    if not configured():
        raise NotConfigured("GHL: GHL_API_TOKEN and GHL_LOCATION_ID are not both set.")
    return request(method, f"{API}{path}", service="GHL", params=params, json=json_body,
                   headers={"Authorization": f"Bearer {token()}", "Version": API_VERSION,
                            "Accept": "application/json"})


# --- pipeline stages -------------------------------------------------------------------

def load_stages() -> dict:
    """Read the pipeline from GHL and save which stage id each engine state uses."""
    body = _call("GET", "/opportunities/pipelines", params={"locationId": location_id()})
    pipelines = body.get("pipelines") or []
    wanted = pipeline_name().lower()
    pipeline = next((p for p in pipelines if (p.get("name") or "").strip().lower() == wanted), None) or \
        next((p for p in pipelines if wanted in (p.get("name") or "").lower()), None)
    if not pipeline:
        names = ", ".join(p.get("name", "?") for p in pipelines) or "none"
        raise IntegrationError(f"GHL: no pipeline called “{pipeline_name()}” in this sub-account "
                               f"(found: {names}). Set GHL_PIPELINE_NAME if it has another name.", retry=False)
    stages = {(s.get("name") or "").strip().lower(): s for s in pipeline.get("stages") or []}
    found, missing = {}, []
    for state, names in STAGE_NAMES.items():
        stage = next((stages[n] for n in names if n in stages), None)
        if stage:
            found[state] = stage
        else:
            missing.append(state)
    with db.transaction() as conn:
        for state, stage in found.items():
            conn.execute(text("""
                INSERT INTO ghl_stage_map (internal_state, pipeline_id, stage_id, stage_name)
                VALUES (:st, :p, :s, :n)
                ON CONFLICT (internal_state) DO UPDATE SET pipeline_id = EXCLUDED.pipeline_id,
                       stage_id = EXCLUDED.stage_id, stage_name = EXCLUDED.stage_name, updated_at = now()
            """), {"st": state, "p": pipeline["id"], "s": stage["id"], "n": stage.get("name")})
        if missing:
            conn.execute(text("DELETE FROM ghl_stage_map WHERE internal_state = ANY(:m)"), {"m": missing})
    return {"pipeline": pipeline.get("name"), "found": {k: v.get("name") for k, v in found.items()},
            "missing": missing}


def _stage(state: str) -> dict:
    with db.engine().connect() as conn:
        row = conn.execute(text("SELECT pipeline_id, stage_id, stage_name FROM ghl_stage_map WHERE internal_state = :s"),
                           {"s": state}).mappings().first()
    if row is None:
        load_stages()
        with db.engine().connect() as conn:
            row = conn.execute(text("SELECT pipeline_id, stage_id, stage_name FROM ghl_stage_map "
                                    "WHERE internal_state = :s"), {"s": state}).mappings().first()
    if row is None:
        raise IntegrationError(f"GHL: the {pipeline_name()} pipeline has no stage for “{state}” "
                               f"(expected one named {STAGE_NAMES[state][0].title()}).", retry=False)
    return dict(row)


# --- contacts and opportunities -------------------------------------------------------

def opportunity_name(reg: dict) -> str:
    """'2026-12-10 | AP-BLENDED | RICHMOND-Henrico'"""
    from ..filters import local_dt
    day = local_dt(reg["starts_at"], reg["time_zone"]).strftime("%Y-%m-%d")
    course = (reg["ghl_course_code"] or reg["short_name"] or "COURSE").upper()
    delivery = reg["delivery_mode"].upper()
    course_token = course if "-" in course else f"{course}-{delivery}"
    return f"{day} | {course_token} | {reg['market_code']}-{reg['site_city']}"


def _custom_fields(reg: dict, reg_type: str | None) -> list[dict]:
    with db.engine().connect() as conn:
        raw = conn.execute(text("SELECT value FROM app_settings WHERE key = 'ghl_custom_field_ids'")).scalar()
    ids = raw if isinstance(raw, dict) else (json.loads(raw) if isinstance(raw, str) and raw else {})
    from ..filters import local_dt
    values = {
        "session_id": str(reg["session_public_id"]),
        "registration_id": str(reg["public_id"]),
        "site": reg["site_name"],
        "class_date": local_dt(reg["starts_at"], reg["time_zone"]).strftime("%Y-%m-%d"),
        "class_time": local_dt(reg["starts_at"], reg["time_zone"]).strftime("%-I:%M %p"),
        "product": reg["product_name"],
        "reg_type": {"new": "New", "existing": "Existing"}.get(reg_type or "", None),
        "city": reg["metro_name"],
    }
    return [{"id": fid, "field_value": values[k]} for k, fid in (ids or {}).items()
            if fid and values.get(k) is not None]


def upsert_contact(first: str, last: str, email: str, phone: str | None) -> tuple[str, bool]:
    body = {"locationId": location_id(), "firstName": first, "lastName": last, "email": email,
            "source": "NBC booking engine"}
    if phone:
        body["phone"] = phone
    res = _call("POST", "/contacts/upsert", json_body=body)
    contact = res.get("contact") or {}
    if not contact.get("id"):
        raise IntegrationError("GHL: the contact upsert returned no contact id.")
    return contact["id"], bool(res.get("new"))


def _find_opportunity(contact_id: str, pipeline_id: str, name: str, registration_id: int) -> str | None:
    """An opportunity made for this seat by an earlier try whose answer was lost.
    One that another seat already owns (the same class booked again after a
    cancellation has the same name) is never taken over."""
    res = _call("GET", "/opportunities/search", params={"location_id": location_id(), "contact_id": contact_id,
                                                         "pipeline_id": pipeline_id, "limit": 100})
    candidates = [o.get("id") for o in res.get("opportunities") or [] if (o.get("name") or "").strip() == name]
    if not candidates:
        return None
    with db.engine().connect() as conn:
        owned = set(conn.execute(text("""SELECT ghl_opportunity_id FROM registrations
                                          WHERE ghl_opportunity_id = ANY(:ids) AND id <> :r"""),
                                 {"ids": candidates, "r": registration_id}).scalars())
    return next((c for c in candidates if c not in owned), None)


@handler("ghl", "sync_registration")
def sync_registration(job: dict) -> None:
    reg_id = job["payload"]["registration_id"]
    with db.engine().connect() as conn:
        reg = conn.execute(text("""
            SELECT r.id, r.public_id, r.status, r.reg_type, r.ghl_opportunity_id, r.ghl_stage_pushed,
                   r.attendee_first_name, r.attendee_last_name, r.attendee_email, r.attendee_phone_e164,
                   r.amount_paid_cents, r.person_id,
                   s.starts_at, s.time_zone, s.public_id AS session_public_id, s.delivery_mode,
                   si.name AS site_name, si.city AS site_city, m.market_code, m.name AS metro_name,
                   p.ghl_course_code, p.short_name, p.name AS product_name,
                   pe.ghl_contact_id, pe.first_name, pe.last_name, pe.email, pe.phone_e164
              FROM registrations r
              JOIN sessions s ON s.id = r.session_id
              JOIN sites si ON si.id = s.site_id
              JOIN metros m ON m.id = s.metro_id
              JOIN products p ON p.id = r.product_id
              LEFT JOIN people pe ON pe.id = r.person_id
             WHERE r.id = :id
        """), {"id": reg_id}).mappings().first()
    if reg is None:
        raise IntegrationError(f"GHL: registration {reg_id} no longer exists.", retry=False)
    state = STATE_FOR_STATUS.get(reg["status"])
    if state is None:
        return              # an unpaid or lapsed seat: nothing to tell GHL
    stage = _stage(state)

    contact_id, is_new = upsert_contact(reg["first_name"] or reg["attendee_first_name"],
                                        reg["last_name"] or reg["attendee_last_name"],
                                        reg["email"] or reg["attendee_email"],
                                        reg["phone_e164"] or reg["attendee_phone_e164"])
    reg_type = reg["reg_type"] or ("new" if is_new else "existing")
    if not reg["reg_type"]:
        # Saved now: on a retry GHL would say the contact isn't new any more.
        with db.transaction() as conn:
            conn.execute(text("UPDATE registrations SET reg_type = COALESCE(reg_type, :rt) WHERE id = :r"),
                         {"rt": reg_type, "r": reg_id})
    name = opportunity_name(reg)
    opp_id = reg["ghl_opportunity_id"]
    if opp_id:
        if reg["ghl_stage_pushed"] != stage["stage_name"]:
            _call("PUT", f"/opportunities/{opp_id}", json_body={
                "pipelineId": stage["pipeline_id"], "pipelineStageId": stage["stage_id"], "status": "open"})
    else:
        opp_id = _find_opportunity(contact_id, stage["pipeline_id"], name, reg_id)
        if opp_id:
            _call("PUT", f"/opportunities/{opp_id}", json_body={
                "pipelineId": stage["pipeline_id"], "pipelineStageId": stage["stage_id"], "status": "open"})
        else:
            res = _call("POST", "/opportunities/", json_body={
                "locationId": location_id(), "pipelineId": stage["pipeline_id"],
                "pipelineStageId": stage["stage_id"], "name": name, "status": "open", "contactId": contact_id,
                "monetaryValue": round((reg["amount_paid_cents"] or 0) / 100, 2),
                "source": "NBC booking engine", "customFields": _custom_fields(reg, reg_type)})
            opp_id = (res.get("opportunity") or {}).get("id")
            if not opp_id:
                raise IntegrationError("GHL: the opportunity was created but no id came back.")

    with db.transaction() as conn:
        if reg["person_id"]:
            owner = conn.execute(text("SELECT id FROM people WHERE ghl_contact_id = :c"), {"c": contact_id}).scalar()
            if owner and owner != reg["person_id"]:
                # GHL matched this email to a contact another engine person already has: use that person.
                conn.execute(text("UPDATE registrations SET person_id = :p WHERE id = :r"),
                             {"p": owner, "r": reg_id})
            else:
                conn.execute(text("UPDATE people SET ghl_contact_id = :c, ghl_synced_at = now() WHERE id = :p"),
                             {"c": contact_id, "p": reg["person_id"]})
        conn.execute(text("""
            UPDATE registrations SET ghl_opportunity_id = :o, ghl_stage_pushed = :st, ghl_synced_at = now(),
                   reg_type = COALESCE(reg_type, :rt)
             WHERE id = :r
        """), {"o": opp_id, "st": stage["stage_name"], "rt": reg_type, "r": reg_id})
