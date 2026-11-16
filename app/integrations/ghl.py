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

Tags (Business settings → "GHL tags"; GHL workflows start on them, e.g. the
emails sent from the NBC sub-account's templates):
  every attendee      nbc-student, nbc-course-<course>, nbc-city-<city>
  how they were booked nbc-booked-self, or nbc-booked-by-someone-else (+ nbc-bring-a-friend)
  add-ons on the seat nbc-addon-<code>
  the booker          nbc-booker (when they booked seats for other people)
  invitation          nbc-send-invite: added (after removing it) each time an invite
                      goes out; the workflow emails {{contact.nbc_invite_link}}.
Opportunity fields say who booked the seat (booked_by, booked_by_email,
booking_type), its add-ons, and answers to booking questions mapped to a field.

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


# Opportunity custom fields the engine fills, found by their field key. The
# Community Class layout (Sandbox 2, and the live sub-account after the
# Dec 14 snapshot) already has these; their ids differ per sub-account, so
# they're looked up when the app starts (load_custom_fields) and saved in the
# "GHL custom field ids" business setting.
FIELD_KEYS = {
    "class_id": "opportunity.class_id",
    "course": "opportunity.course",
    "course_date": "opportunity.course_date",
    "course_location": "opportunity.course_location",
    "hours": "opportunity.hours",
    "instructor": "opportunity.instructor",
    "reg_type": "opportunity.reg_type",
    "class_price": "opportunity.class_price",
    "order_total": "opportunity.order_total",
    "delivery": "opportunity.delivery",
    "reg_date": "opportunity.reg_date",
    "booked_by": "opportunity.booked_by",
    "booked_by_email": "opportunity.booked_by_email",
    "booking_type": "opportunity.booking_type",
    "addons": "opportunity.addons",
}
# Contact custom fields (the invitation email reads them).
CONTACT_FIELD_KEYS = {
    "contact_invite_link": "contact.nbc_invite_link",
    "contact_booked_by": "contact.nbc_booked_by",
    # A payment link customer service sends (phone booking, a class change's price difference).
    "contact_payment_link": "contact.nbc_payment_link",
    "contact_payment_amount": "contact.nbc_payment_amount",
    # A waiting-list seat offer: the pay link and when it runs out (class's local time).
    "contact_offer_link": "contact.nbc_offer_link",
    "contact_offer_expires": "contact.nbc_offer_expires",
}
DEFAULT_TAGS = {"student": "nbc-student", "booked_self": "nbc-booked-self",
                "booked_by_other": "nbc-booked-by-someone-else", "booker": "nbc-booker",
                "bring_a_friend": "nbc-bring-a-friend", "send_invite": "nbc-send-invite",
                "payment_link": "nbc-send-payment-link", "class_moved": "nbc-class-moved", "waitlist": "nbc-waitlist",
                "waitlist_offer": "nbc-waitlist-offer", "offer_expired": "nbc-offer-expired",
                "waitlist_no_seat": "nbc-waitlist-no-seat",
                "course_prefix": "nbc-course-", "city_prefix": "nbc-city-", "addon_prefix": "nbc-addon-"}


def load_custom_fields() -> dict:
    """Find the opportunity and contact custom fields by key and save their ids. Needs the
    Private Integration scope 'View Custom Fields' (locations/customFields.readonly).
    Booking questions mapped to an opportunity field are looked up too (saved under the field key)."""
    by_key: dict[str, str] = {}
    for model in ("opportunity", "contact"):
        try:
            body = _call("GET", f"/locations/{location_id()}/customFields", params={"model": model})
        except IntegrationError as exc:
            if exc.status in (401, 403):
                raise IntegrationError("GHL: the token can't read custom fields. In the Private Integration, add the "
                                       "scope “View Custom Fields” (locations/customFields.readonly).",
                                       retry=False) from exc
            raise
        by_key.update({f.get("fieldKey"): f.get("id") for f in body.get("customFields") or [] if f.get("fieldKey")})
    wanted = {**FIELD_KEYS, **CONTACT_FIELD_KEYS}
    with db.engine().connect() as conn:
        for key in conn.execute(text("SELECT DISTINCT ghl_field_key FROM booking_questions "
                                     "WHERE ghl_field_key IS NOT NULL")).scalars():
            wanted[key] = key
    found = {name: by_key[key] for name, key in wanted.items() if by_key.get(key)}
    with db.transaction() as conn:
        raw = conn.execute(text("SELECT value FROM app_settings WHERE key = 'ghl_custom_field_ids' FOR NO KEY UPDATE")).scalar()
        current = raw if isinstance(raw, dict) else {}
        merged = {**current, **found}
        if merged != current:
            conn.execute(text("UPDATE app_settings SET value = CAST(:v AS jsonb), updated_at = now() "
                              "WHERE key = 'ghl_custom_field_ids'"), {"v": json.dumps(merged)})
    return {"found": sorted(found), "missing": sorted(set(wanted) - set(found))}


# Names that make GHL give each field the key the engine looks for
# ("Booked By Email" -> opportunity.booked_by_email). Every field is Single Line text.
FIELD_NAMES = {
    "class_id": "Class ID", "course": "Course", "course_date": "Course Date", "course_location": "Course Location",
    "hours": "Hours", "instructor": "Instructor", "reg_type": "Reg Type", "class_price": "Class Price",
    "order_total": "Order Total", "delivery": "Delivery", "reg_date": "Reg Date", "booked_by": "Booked By",
    "booked_by_email": "Booked By Email", "booking_type": "Booking Type", "addons": "Addons",
    "contact_invite_link": "NBC Invite Link", "contact_booked_by": "NBC Booked By",
    "contact_payment_link": "NBC Payment Link", "contact_payment_amount": "NBC Payment Amount",
    "contact_offer_link": "NBC Offer Link", "contact_offer_expires": "NBC Offer Expires",
}


def create_missing_fields(*, dry_run: bool = False) -> dict:
    """Create, in this GHL sub-account, the custom fields the engine fills that it doesn't have yet.

    Only adds: a field that exists (by key, or by the same name) is left alone, so it is
    safe to run again. Covers the opportunity and contact fields above and any booking
    question copied to a GHL field. Needs the Private Integration scope
    "Edit Custom Fields" (locations/customFields.write). Afterwards the field ids are loaded.
    Returns {created, existing, renamed_by_ghl, skipped, failed}."""
    wanted = [(name, key) for name, key in {**FIELD_KEYS, **CONTACT_FIELD_KEYS}.items()]
    with db.engine().connect() as conn:
        wanted += [(key, key) for key in conn.execute(text(
            "SELECT DISTINCT ghl_field_key FROM booking_questions WHERE ghl_field_key IS NOT NULL")).scalars()]
    have_keys: set[str] = set()
    have_names: dict[str, str] = {}
    for model in ("opportunity", "contact"):
        try:
            body = _call("GET", f"/locations/{location_id()}/customFields", params={"model": model})
        except IntegrationError as exc:
            if exc.status in (401, 403):
                raise IntegrationError("GHL: the token can't read custom fields. In the Private Integration, add the "
                                       "scopes “View Custom Fields” and “Edit Custom Fields”.", retry=False) from exc
            raise
        for f in body.get("customFields") or []:
            have_keys.add(f.get("fieldKey") or "")
            have_names[f"{model}:{(f.get('name') or '').strip().lower()}"] = f.get("fieldKey") or ""
    out: dict[str, list] = {"created": [], "existing": [], "renamed_by_ghl": [], "skipped": [], "failed": []}
    for name, key in wanted:
        model, short = key.split(".", 1)
        if key in have_keys:
            out["existing"].append(key)
            continue
        label = FIELD_NAMES.get(name) or short.replace("_", " ").title()
        clash = have_names.get(f"{model}:{label.lower()}")
        if clash is not None:
            out["skipped"].append(f"{key} (a field named “{label}” already exists with the key {clash or '?'})")
            continue
        if dry_run:
            out["created"].append(f"{key} (would create “{label}”)")
            continue
        try:
            made = _call("POST", f"/locations/{location_id()}/customFields",
                         json_body={"name": label, "dataType": "TEXT", "model": model})
        except IntegrationError as exc:
            if exc.status in (401, 403):
                raise IntegrationError("GHL: the token can't create custom fields. In the Private Integration, add "
                                       "the scope “Edit Custom Fields” (locations/customFields.write), then try "
                                       "again.", retry=False) from exc
            out["failed"].append(f"{key}: {exc}")
            continue
        field = made.get("customField") or made
        got = field.get("fieldKey") or ""
        have_keys.add(got)
        have_names[f"{model}:{label.lower()}"] = got
        out["created"].append(key)
        if got and got != key:
            out["renamed_by_ghl"].append(f"{key} → GHL made {got}")
    if not dry_run and out["created"]:
        load_custom_fields()
    return out


def _field_ids() -> dict:
    with db.engine().connect() as conn:
        raw = conn.execute(text("SELECT value FROM app_settings WHERE key = 'ghl_custom_field_ids'")).scalar()
    return raw if isinstance(raw, dict) else (json.loads(raw) if isinstance(raw, str) and raw else {})


def tags() -> dict:
    """Tag names from Business settings ("GHL tags"), with the defaults for any left out."""
    with db.engine().connect() as conn:
        raw = conn.execute(text("SELECT value FROM app_settings WHERE key = 'ghl_tags'")).scalar()
    return {**DEFAULT_TAGS, **(raw if isinstance(raw, dict) else {})}


def add_tags(contact_id: str, names: list[str]) -> None:
    names = sorted({n.strip().lower() for n in names if n and n.strip()})
    if names:
        _call("POST", f"/contacts/{contact_id}/tags", json_body={"tags": names})


def remove_tags(contact_id: str, names: list[str]) -> None:
    if names:
        _call("DELETE", f"/contacts/{contact_id}/tags", json_body={"tags": names})


def set_contact_fields(contact_id: str, values: dict[str, str]) -> None:
    """values: {setting name: value} for contact fields whose ids are known."""
    ids = _field_ids()
    fields = [{"id": ids[k], "field_value": v} for k, v in values.items() if ids.get(k)]
    if fields:
        _call("PUT", f"/contacts/{contact_id}", json_body={"customFields": fields})


def _slug(text_: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", (text_ or "").lower()).strip("-")


def _custom_fields(reg: dict, reg_type: str | None) -> list[dict]:
    ids = _field_ids()
    from ..filters import local_dt
    start = local_dt(reg["starts_at"], reg["time_zone"])
    hours = (reg["ends_at"] - reg["starts_at"]).total_seconds() / 3600
    reg_type_text = {"new": "New", "existing": "Existing"}.get(reg_type or "", None)
    values = {
        "session_id": str(reg["session_public_id"]),
        "class_id": str(reg["session_public_id"]),
        "registration_id": str(reg["public_id"]),
        "site": reg["site_name"],
        "course_location": f"{reg['site_name']}, {reg['address_line1']}, {reg['site_city']}, "
                           f"{reg['site_state']} {reg['postal_code']}",
        "class_date": start.strftime("%Y-%m-%d"),
        "course_date": start.strftime("%m/%d/%Y"),
        "class_time": start.strftime("%-I:%M %p"),
        "hours": f"{hours:g}",
        "product": reg["product_name"],
        "course": reg["product_name"],
        "delivery": reg["product_name"].split(" – ")[-1] if " – " in reg["product_name"]
                    else reg["delivery_mode"].capitalize(),
        "instructor": reg["airtable_lead_instructor"],
        "reg_type": reg_type_text,
        "class_price": f"{reg['unit_price_cents'] / 100:.2f}",
        "order_total": f"{reg['order_total_cents'] / 100:.2f}",
        "reg_date": reg["confirmed_at"].strftime("%m/%d/%Y") if reg["confirmed_at"] else None,
        "city": reg["metro_name"],
        "booked_by": f"{reg['buyer_first_name']} {reg['buyer_last_name']}",
        "booked_by_email": reg["buyer_email"],
        "booking_type": booking_type(reg),
        "addons": ", ".join(a["name"] for a in reg.get("addons") or []) or None,
        **{a["ghl_field_key"]: a["answer"] for a in reg.get("answers") or [] if a["ghl_field_key"]},
    }
    return [{"id": fid, "field_value": values[k]} for k, fid in (ids or {}).items()
            if fid and values.get(k) is not None]


def booking_type(reg: dict) -> str:
    if reg.get("is_bring_a_friend"):
        return "Bring a friend"
    return "Booked by themselves" if _booked_self(reg) else "Booked by someone else"


def _booked_self(reg: dict) -> bool:
    return (reg["buyer_email"] or "").lower() == (reg["attendee_email"] or "").lower()


def _registration(reg_id: int) -> dict | None:
    with db.engine().connect() as conn:
        reg = conn.execute(text("""
            SELECT r.id, r.public_id, r.status, r.reg_type, r.ghl_opportunity_id, r.ghl_stage_pushed,
                   r.attendee_first_name, r.attendee_last_name, r.attendee_email, r.attendee_phone_e164,
                   r.amount_paid_cents, r.person_id, r.unit_price_cents, r.confirmed_at, r.is_bring_a_friend,
                   r.order_id, o.total_cents AS order_total_cents, o.buyer_first_name, o.buyer_last_name,
                   o.buyer_email, o.buyer_person_id,
                   s.starts_at, s.ends_at, s.time_zone, s.public_id AS session_public_id, s.delivery_mode,
                   s.airtable_lead_instructor,
                   si.name AS site_name, si.city AS site_city, si.address_line1, si.state_code AS site_state,
                   si.postal_code, m.market_code, m.name AS metro_name, m.slug AS metro_slug,
                   cf.slug AS course_slug, cf.name AS course_name,
                   p.ghl_course_code, p.short_name, p.name AS product_name,
                   pe.ghl_contact_id, pe.first_name, pe.last_name, pe.email, pe.phone_e164
              FROM registrations r
              JOIN orders o ON o.id = r.order_id
              JOIN sessions s ON s.id = r.session_id
              JOIN course_families cf ON cf.id = s.course_family_id
              JOIN sites si ON si.id = s.site_id
              JOIN metros m ON m.id = s.metro_id
              JOIN products p ON p.id = r.product_id
              LEFT JOIN people pe ON pe.id = r.person_id
             WHERE r.id = :id
        """), {"id": reg_id}).mappings().first()
        if reg is None:
            return None
        reg = dict(reg)
        reg["addons"] = [dict(a) for a in conn.execute(text("""
            SELECT a.code, oa.name FROM order_addons oa JOIN addons a ON a.id = oa.addon_id
             WHERE oa.order_id = :o AND oa.status = 'paid' AND (oa.registration_id = :r OR oa.registration_id IS NULL)
             ORDER BY oa.id
        """), {"o": reg["order_id"], "r": reg_id}).mappings()]
        reg["answers"] = [dict(a) for a in conn.execute(text("""
            SELECT q.ghl_field_key, ba.answer FROM booking_answers ba JOIN booking_questions q ON q.id = ba.question_id
             WHERE ba.order_id = :o AND (ba.registration_id = :r OR ba.registration_id IS NULL)
        """), {"o": reg["order_id"], "r": reg_id}).mappings()]
    return reg


def seat_tags(reg: dict) -> list[str]:
    t = tags()
    out = [t["student"], t["booked_self"] if _booked_self(reg) else t["booked_by_other"],
           t["course_prefix"] + _slug(reg["course_slug"]), t["city_prefix"] + _slug(reg["metro_slug"])]
    if reg.get("is_bring_a_friend"):
        out.append(t["bring_a_friend"])
    out += [t["addon_prefix"] + _slug(a["code"]) for a in reg.get("addons") or []]
    return out


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


def _create_opportunity(body: dict) -> dict:
    return _call("POST", "/opportunities/", json_body=body)


@handler("ghl", "sync_registration")
def sync_registration(job: dict) -> None:
    reg_id = job["payload"]["registration_id"]
    reg = _registration(reg_id)
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
        if reg["ghl_stage_pushed"] != stage["stage_name"] or job["payload"].get("refresh"):
            _call("PUT", f"/opportunities/{opp_id}", json_body={
                "pipelineId": stage["pipeline_id"], "pipelineStageId": stage["stage_id"], "status": "open",
                "name": name, "customFields": _custom_fields(reg, reg_type)})
    else:
        opp_id = _find_opportunity(contact_id, stage["pipeline_id"], name, reg_id)
        if opp_id:
            _call("PUT", f"/opportunities/{opp_id}", json_body={
                "pipelineId": stage["pipeline_id"], "pipelineStageId": stage["stage_id"], "status": "open"})
        else:
            try:
                res = _create_opportunity({
                "locationId": location_id(), "pipelineId": stage["pipeline_id"],
                "pipelineStageId": stage["stage_id"], "name": name, "status": "open", "contactId": contact_id,
                "monetaryValue": round((reg["amount_paid_cents"] or 0) / 100, 2),
                "source": "NBC booking engine", "customFields": _custom_fields(reg, reg_type)})
            except IntegrationError as exc:
                if exc.status in (400, 422) and "duplicate" in str(exc).lower():
                    raise IntegrationError(
                        "GHL refused a second opportunity for the same contact. In the sub-account turn on "
                        "Settings → Objects → Opportunities → Allow Multiple Opportunities per Contact, then press "
                        "Retry.", status=exc.status, retry=False) from exc
                raise
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
    if state == "booked":
        add_tags(contact_id, seat_tags(reg))


@handler("ghl", "sync_booker")
def sync_booker(job: dict) -> None:
    """Someone booked seats for other people: they become a contact too, tagged as a booker."""
    with db.engine().connect() as conn:
        order = conn.execute(text("""
            SELECT id, buyer_first_name, buyer_last_name, buyer_email, buyer_phone_e164, buyer_person_id
              FROM orders WHERE id = :o
        """), {"o": job["payload"]["order_id"]}).mappings().first()
    if order is None:
        raise IntegrationError(f"GHL: order {job['payload']['order_id']} no longer exists.", retry=False)
    contact_id, _ = upsert_contact(order["buyer_first_name"], order["buyer_last_name"], order["buyer_email"],
                                   order["buyer_phone_e164"])
    add_tags(contact_id, [tags()["booker"]])
    if order["buyer_person_id"]:
        with db.transaction() as conn:
            conn.execute(text("""UPDATE people SET ghl_contact_id = :c, ghl_synced_at = now()
                                  WHERE id = :p AND (ghl_contact_id IS NULL OR ghl_contact_id = :c)
                                    AND NOT EXISTS (SELECT 1 FROM people x WHERE x.ghl_contact_id = :c AND x.id <> :p)"""),
                         {"c": contact_id, "p": order["buyer_person_id"]})


@handler("ghl", "send_invite")
def send_invite(job: dict) -> None:
    """Email an attendee someone else booked an invite to make their own account.

    GHL sends the email (its invitation template) when the send-invite tag is added;
    the link and the booker's name are in the contact fields the template reads."""
    from .. import invites
    reg = _registration(job["payload"]["registration_id"])
    if reg is None or reg["status"] not in ("confirmed", "awaiting_payment") or _booked_self(reg):
        return
    ids = _field_ids()
    if not ids.get("contact_invite_link"):
        raise IntegrationError("GHL: no contact field for the invite link. Create a contact custom field with the key "
                               "contact.nbc_invite_link (and contact.nbc_booked_by), then press “Load custom fields” "
                               "and Retry.", retry=False)
    contact_id, _ = upsert_contact(reg["attendee_first_name"], reg["attendee_last_name"], reg["attendee_email"],
                                   reg["attendee_phone_e164"])
    with db.transaction() as conn:
        link = invites.new_link(conn, reg["id"])
    set_contact_fields(contact_id, {"contact_invite_link": link,
                                    "contact_booked_by": f"{reg['buyer_first_name']} {reg['buyer_last_name']}"})
    tag = tags()["send_invite"]
    remove_tags(contact_id, [tag])          # so adding it starts the workflow again on a resend
    add_tags(contact_id, [tag])
    with db.transaction() as conn:          # what was sent, for the booker's dashboard and the console
        conn.execute(text("""
            INSERT INTO notifications (kind, audience, channel, provider, recipient, subject_type, subject_id,
                                       dedupe_key, status, sent_at)
            VALUES ('attendee_invite', 'customer', 'email', 'ghl', :to, 'registration', :r, :k, 'sent', now())
            ON CONFLICT (dedupe_key) DO NOTHING
        """), {"to": reg["attendee_email"], "r": reg["id"], "k": f"invite:{reg['id']}:{link[-16:]}"})


# --- The desk: a class move and payment links (hub 9b) ---------------------------------

def _retag(contact_id: str, tag: str) -> None:
    """Remove then add a tag, so the GHL workflow that starts on it runs again each time."""
    remove_tags(contact_id, [tag])
    add_tags(contact_id, [tag])


@handler("ghl", "move_registration")
def move_registration(job: dict) -> None:
    """A seat moved to another class: the same opportunity gets the new date, place and fields
    (name included), and the attendee's contact is tagged so GHL sends the "class changed" message."""
    sync_registration({**job, "payload": {**job["payload"], "refresh": True}})
    reg = _registration(job["payload"]["registration_id"])
    if reg is None:
        return
    contact_id = reg["ghl_contact_id"] or upsert_contact(
        reg["first_name"] or reg["attendee_first_name"], reg["last_name"] or reg["attendee_last_name"],
        reg["email"] or reg["attendee_email"], reg["phone_e164"] or reg["attendee_phone_e164"])[0]
    _retag(contact_id, tags()["class_moved"])


@handler("ghl", "send_payment_link")
def send_payment_link(job: dict) -> None:
    """A payment link for the buyer (a phone booking, or a class change's price difference): saved on
    their contact and tagged, so a GHL workflow texts and emails it."""
    p = job["payload"]
    with db.engine().connect() as conn:
        order = conn.execute(text("""
            SELECT buyer_first_name, buyer_last_name, buyer_email, buyer_phone_e164, status FROM orders WHERE id = :o
        """), {"o": p["order_id"]}).mappings().first()
    if order is None or order["status"] not in ("open", "awaiting_payment"):
        return                                     # paid, cancelled or lapsed meanwhile: nothing to send
    ids = _field_ids()
    if not ids.get("contact_payment_link"):
        raise IntegrationError("GHL: no contact field for the payment link. Press “Create missing fields in GHL” "
                               "on the Integrations page (it adds NBC Payment Link), then Retry.", retry=False)
    contact_id, _ = upsert_contact(order["buyer_first_name"], order["buyer_last_name"], order["buyer_email"],
                                   order["buyer_phone_e164"])
    set_contact_fields(contact_id, {"contact_payment_link": p["url"],
                                    "contact_payment_amount": f"{p['amount_cents'] / 100:.2f}"})
    _retag(contact_id, tags()["payment_link"])


# --- The waiting list (hub 9c) -----------------------------------------------------------

def _entry(entry_id: int) -> dict | None:
    """A waiting-list entry with the fields an opportunity needs (the same shape as a registration)."""
    with db.engine().connect() as conn:
        row = conn.execute(text("""
            SELECT w.id, w.public_id, w.status, w.first_name, w.last_name, w.email, w.phone_e164, w.party_size,
                   w.ghl_opportunity_id, w.first_name AS attendee_first_name, w.last_name AS attendee_last_name,
                   w.email AS attendee_email, w.first_name AS buyer_first_name, w.last_name AS buyer_last_name,
                   w.email AS buyer_email, false AS is_bring_a_friend, NULL::timestamptz AS confirmed_at,
                   sp.price_cents AS unit_price_cents, 0 AS order_total_cents,
                   s.starts_at, s.ends_at, s.time_zone, s.public_id AS session_public_id, s.delivery_mode,
                   s.airtable_lead_instructor, si.name AS site_name, si.city AS site_city, si.address_line1,
                   si.state_code AS site_state, si.postal_code, m.market_code, m.name AS metro_name,
                   m.slug AS metro_slug, cf.slug AS course_slug, p.ghl_course_code, p.short_name,
                   p.name AS product_name
              FROM waitlist_entries w
              JOIN sessions s ON s.id = w.session_id
              JOIN session_products sp ON sp.session_id = w.session_id AND sp.product_id = w.preferred_product_id
              JOIN products p ON p.id = w.preferred_product_id
              JOIN course_families cf ON cf.id = s.course_family_id
              JOIN sites si ON si.id = s.site_id
              JOIN metros m ON m.id = s.metro_id
             WHERE w.id = :id
        """), {"id": entry_id}).mappings().first()
    return dict(row) if row else None


@handler("ghl", "sync_waitlist_entry")
def sync_waitlist_entry(job: dict) -> None:
    """Someone joined a waiting list: their contact, and an opportunity in the Waitlisted stage
    (it moves to Pending if they get a seat and pay)."""
    entry = _entry(job["payload"]["entry_id"])
    if entry is None or entry["status"] not in ("waiting", "offered", "offer_expired"):
        return
    contact_id, _ = upsert_contact(entry["first_name"], entry["last_name"], entry["email"], entry["phone_e164"])
    if not entry["ghl_opportunity_id"]:
        stage = _stage("waitlisted")
        res = _create_opportunity({
            "locationId": location_id(), "pipelineId": stage["pipeline_id"], "pipelineStageId": stage["stage_id"],
            "name": opportunity_name(entry), "status": "open", "contactId": contact_id, "monetaryValue": 0,
            "source": "NBC booking engine", "customFields": _custom_fields(entry, None)})
        opp_id = (res.get("opportunity") or {}).get("id")
        if opp_id:
            with db.transaction() as conn:
                conn.execute(text("UPDATE waitlist_entries SET ghl_opportunity_id = :o WHERE id = :id"),
                             {"o": opp_id, "id": entry["id"]})
    t = tags()
    add_tags(contact_id, [t["waitlist"], t["course_prefix"] + _slug(entry["course_slug"]),
                          t["city_prefix"] + _slug(entry["metro_slug"])])


@handler("ghl", "waitlist_notice")
def waitlist_notice(job: dict) -> None:
    """A seat offer (link and expiry on the contact, then the offer tag), "Offer expired", or the
    cut-off's "no seat opened" notice: each is a tag a GHL workflow starts on."""
    p = job["payload"]
    entry = _entry(p["entry_id"])
    if entry is None:
        return
    contact_id, _ = upsert_contact(entry["first_name"], entry["last_name"], entry["email"], entry["phone_e164"])
    t = tags()
    if p["kind"] == "offer":
        ids = _field_ids()
        if not ids.get("contact_offer_link"):
            raise IntegrationError("GHL: no contact field for the waiting-list offer link. Press “Create missing "
                                   "fields in GHL” on the Integrations page (it adds NBC Offer Link), then Retry.",
                                   retry=False)
        set_contact_fields(contact_id, {"contact_offer_link": p["url"], "contact_offer_expires": p["expires"]})
        _retag(contact_id, t["waitlist_offer"])
    elif p["kind"] == "expired":
        _retag(contact_id, t["offer_expired"])
    elif p["kind"] == "no_seat":
        _retag(contact_id, t["waitlist_no_seat"])
        if entry["ghl_opportunity_id"]:
            stage = _stage("cancelled")
            _call("PUT", f"/opportunities/{entry['ghl_opportunity_id']}", json_body={
                "pipelineId": stage["pipeline_id"], "pipelineStageId": stage["stage_id"], "status": "open"})
