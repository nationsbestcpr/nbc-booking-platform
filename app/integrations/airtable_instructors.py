"""Airtable -> engine: instructor data (migration 0011). Read only: nothing is written to Airtable.

Part of every full Airtable sync (app/integrations/airtable_sync.py):

  * Instructors table      -> instructors (contact, address, availability, outreach, removal, HUB, page type),
                              their r.25 certifications (instructor_certs), city areas (instructor_metros),
                              HUB teams (instructor_hub_members) and locations (instructor_locations).
  * Master Locations       -> location_instructor_settings: assignment type, primary and HUB instructors,
                              instructor notes and extra pay, for every location (not only those with classes).
  * Class Schedule         -> session_instructor_details: the ASSIGN INSTRUCTOR name matched to an
                              instructor, Instructor Assigned, availability status and performance tier.
  * Instructor Recruiting Needs -> recruiting_needs and the no-instructor cancelled classes behind each.

Airtable is the source (decided 11/16); GHL holds no instructor data (11/20), so the Instructors table's GHL
fields are never read. An instructor with no email in Airtable is kept; one whose email another instructor
already has is kept without it, and listed as a problem. Names are matched case- and space-insensitively.
"""

from __future__ import annotations

import re

from sqlalchemy import text

from .. import db
from . import airtable, runs
from .airtable_sync import (SOURCE, RowProblem, _cents, _hash, _is_record_ids, _number, _problem, _register,
                            _text, _truthy, _values, parse_day, parse_metro, slugify)

CERTS = {"cert_fa": "ARC_FA_CPR_AED_INSTRUCTOR_R25", "cert_bls": "ARC_BLS_INSTRUCTOR_R25",
         "cert_it": "ARC_INSTRUCTOR_TRAINER_R25"}
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def usable(m: dict) -> bool:
    return bool(m.get("table")) and not m.get("missing_required")


def name_key(name: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).split())


def _list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [s for s in (_text(x) for x in v) if s]
    s = _text(v)
    return [s] if s else []


def _ids(v) -> list[str]:
    return list(v) if _is_record_ids(v) else []


def _phone(v) -> str | None:
    from .checkout import normalise_phone
    try:
        return normalise_phone(_text(v)) or None
    except Exception:
        return None


def _instructor_ids() -> dict[str, int]:
    with db.engine().connect() as c:
        return dict(c.execute(text("SELECT airtable_record_id, id FROM instructors "
                                   "WHERE airtable_record_id IS NOT NULL")).all())


def _by_name() -> dict[str, int]:
    """Instructor id by normalised name (full name, and first + last); a name two instructors share maps to none."""
    out: dict[str, int | None] = {}
    with db.engine().connect() as c:
        for r in c.execute(text("SELECT id, full_name, first_name, last_name FROM instructors "
                                "WHERE source_deleted_at IS NULL")).mappings():
            for key in {name_key(r["full_name"]), name_key(f"{r['first_name']} {r['last_name']}")} - {""}:
                out[key] = None if key in out and out[key] != r["id"] else r["id"]
    return {k: v for k, v in out.items() if v}


NAME_SEPARATORS = re.compile(r"\s*(?:,|&|/|\+|;|\band\b)\s*", re.I)


def _matcher():
    """A function from a name as typed in Airtable ("ASSIGN INSTRUCTOR", "Primary Instructor") to one
    instructor, or None. The exact name (full, or first + last) first; then, when the text holds several
    names ("A, B"), the first of them that matches; then a name whose words are all in exactly one
    instructor's full name (a middle or second last name left out). Anything ambiguous stays unmatched:
    the typed name is kept anyway."""
    exact = _by_name()
    with db.engine().connect() as c:
        words = [(r["id"], set(name_key(r["full_name"]).split()))
                 for r in c.execute(text("SELECT id, full_name FROM instructors WHERE source_deleted_at IS NULL "
                                         "AND full_name IS NOT NULL")).mappings()]

    def one(name: str) -> int | None:
        key = name_key(name)
        if not key:
            return None
        if key in exact:
            return exact[key]
        wanted = set(key.split())
        if len(wanted) < 2:
            return None
        hits = {iid for iid, have in words if wanted <= have}
        return hits.pop() if len(hits) == 1 else None

    def match(raw: str | None) -> int | None:
        if not raw:
            return None
        hit = one(raw)
        if hit:
            return hit
        for part in NAME_SEPARATORS.split(raw):
            hit = one(part)
            if hit:
                return hit
        return None
    return match


# --- Instructors ----------------------------------------------------------------------------

def sync_instructors(m: dict, run_id: int, stats: dict) -> None:
    stats.setdefault("instructors", 0)
    records = list(airtable.records(m))
    with db.engine().connect() as c:
        stored = dict(c.execute(text("SELECT record_id, fields_hash FROM airtable_records WHERE table_name = :t"),
                                {"t": m["table"]}).all())
    known = _instructor_ids()
    for record in records:
        v = _values(record, m)
        if record["id"] in known and stored.get(record["id"]) == _hash(v):
            stats["instructors"] += 1                       # unchanged since the last sync
            continue
        try:
            _save_instructor(record, m, v, run_id, stats)
            stats["instructors"] += 1
        except RowProblem as exc:
            _problem(run_id, stats, record["id"], exc, m["table"])
    seen = [r["id"] for r in records]
    ids = {k: v for k, v in _instructor_ids().items() if k in set(seen)}   # links only between current instructors
    with db.transaction() as conn:
        conn.execute(text("""UPDATE instructors SET source_deleted_at = now()
                              WHERE airtable_record_id IS NOT NULL AND NOT airtable_record_id = ANY(:seen)
                                AND source_deleted_at IS NULL"""), {"seen": seen})
        conn.execute(text("UPDATE instructors SET source_deleted_at = NULL WHERE airtable_record_id = ANY(:seen)"),
                     {"seen": seen})
        _links(conn, records, m, ids)
        # A city area that came into the engine after an instructor was saved ("Ohio - Akron" once a class there
        # is on sale): link it now, even if the instructor hasn't changed in Airtable.
        for label in conn.execute(text("SELECT DISTINCT metro_label FROM instructor_metros WHERE metro_id IS NULL")).scalars():
            metro = _metro_id(conn, label)
            if metro:
                conn.execute(text("UPDATE instructor_metros SET metro_id = :m WHERE metro_label = :l AND metro_id IS NULL"),
                             {"m": metro, "l": label})
                conn.execute(text("""UPDATE instructors SET home_metro_id = :m WHERE home_metro_id IS NULL
                                        AND id IN (SELECT instructor_id FROM instructor_metros WHERE metro_label = :l)"""),
                             {"m": metro, "l": label})


def _metro_id(conn, label: str) -> int | None:
    """The engine's city area for an Airtable label ("Virginia - Richmond"), if it has one."""
    name, st = parse_metro(label) or (label, None)
    return conn.execute(text("SELECT id FROM metros WHERE slug = :s"),
                        {"s": slugify(f"{name}-{st}") if st else slugify(name)}).scalar()


def _save_instructor(record: dict, m: dict, v: dict, run_id: int, stats: dict) -> None:
    full = _text(v.get("name"))
    if not full:
        raise RowProblem("INSTRUCTOR_NO_NAME", "the instructor has no name.")
    first, _, last = full.partition(" ")
    email = (_text(v.get("email")) or "").lower() or None
    if email and not EMAIL.match(email):
        email = None
    travel = _number(v.get("travel"))
    per_month = _number(v.get("per_month"))
    removal = _text(v.get("removal_status"))
    params = {
        "rid": record["id"], "full": full, "first": first, "last": last.strip(), "email": email,
        "phone": _phone(v.get("phone")), "hub_key": _text(v.get("hub_key")), "city": _text(v.get("city")),
        "state": _text(v.get("state")), "zip": _text(v.get("zip")),
        "ew": _truthy(v.get("active_in_enrollware")), "hub": bool(_truthy(v.get("hub_instructor"))),
        "lead_hub": bool(_truthy(v.get("lead_hub"))), "addl": _text(v.get("additional_certs")),
        "certs_synced": parse_day(v.get("certs_synced")), "days": _list(v.get("days")),
        "times": _list(v.get("times")), "per_month": int(per_month) if per_month is not None and per_month >= 0 else None,
        "travel": int(travel) if travel and travel > 0 else None, "avoid": _text(v.get("avoid")),
        "notice": _text(v.get("notice")), "avail_notes": _text(v.get("avail_notes")),
        "avail_updated": parse_day(v.get("avail_updated")), "avail_by": _text(v.get("avail_by")),
        "outreach": _text(v.get("outreach_status")), "last_contact": parse_day(v.get("last_contact")),
        "follow_up": parse_day(v.get("follow_up")), "outreach_notes": _text(v.get("outreach_notes")),
        "removal_on": parse_day(v.get("removal_recommended")), "removal_reason": _text(v.get("removal_reason")),
        "removal": removal, "remove_from": _list(v.get("remove_from")),
        "associate": _truthy(v.get("associate")), "page_type": _text(v.get("page_type")),
        "approval_url": _text(v.get("approval_link")), "other_ew": _text(v.get("other_enrollware")),
        "inactive": (removal or "").lower() == "removed",
    }
    problem = False
    with db.transaction() as conn:
        _register(conn, record, m["table"], v)
        if email:
            other = conn.execute(text("""SELECT id, airtable_record_id FROM instructors WHERE email = :e"""),
                                 {"e": email}).mappings().first()
            if other and other["airtable_record_id"] is None:
                # An instructor made in the engine (the portal, the demo) with this email: it's this record.
                conn.execute(text("UPDATE instructors SET airtable_record_id = :rid WHERE id = :id"),
                             {"rid": record["id"], "id": other["id"]})
            elif other and other["airtable_record_id"] != record["id"]:
                params["email"] = None
                problem = True
                _problem(run_id, stats, record["id"], RowProblem(
                    "INSTRUCTOR_EMAIL_SHARED", f"“{full}” has the same email as another instructor; it's kept "
                    "without an email until one of them is changed in Airtable."), m["table"])
        iid = conn.execute(text("""
            INSERT INTO instructors (airtable_record_id, full_name, first_name, last_name, email, phone_e164, hub_key,
                   city, state, postal_code, active_in_enrollware, is_hub_instructor, interested_in_leading_hub,
                   additional_certs, certs_last_synced_on, available_days, available_times, classes_per_month,
                   travel_radius_miles, locations_to_avoid, notice_needed, availability_notes, availability_updated_on,
                   availability_updated_by, outreach_status, last_contact_on, next_follow_up_on, outreach_notes,
                   removal_recommended_on, removal_reason, removal_status, remove_from, associate, page_type,
                   date_approval_url, other_enrollware_accounts, status, source_synced_at)
            VALUES (:rid, :full, :first, :last, :email, :phone, :hub_key, :city, :state, :zip, :ew, :hub, :lead_hub,
                   :addl, :certs_synced, :days, :times, :per_month, :travel, :avoid, :notice, :avail_notes,
                   :avail_updated, :avail_by, :outreach, :last_contact, :follow_up, :outreach_notes, :removal_on,
                   :removal_reason, :removal, :remove_from, :associate, :page_type, :approval_url, :other_ew,
                   CASE WHEN :inactive THEN 'inactive' ELSE 'active' END, now())
            ON CONFLICT (airtable_record_id) DO UPDATE SET
                   full_name = EXCLUDED.full_name, first_name = EXCLUDED.first_name, last_name = EXCLUDED.last_name,
                   email = EXCLUDED.email, phone_e164 = EXCLUDED.phone_e164, hub_key = EXCLUDED.hub_key,
                   city = EXCLUDED.city, state = EXCLUDED.state, postal_code = EXCLUDED.postal_code,
                   active_in_enrollware = EXCLUDED.active_in_enrollware, is_hub_instructor = EXCLUDED.is_hub_instructor,
                   interested_in_leading_hub = EXCLUDED.interested_in_leading_hub,
                   additional_certs = EXCLUDED.additional_certs, certs_last_synced_on = EXCLUDED.certs_last_synced_on,
                   available_days = EXCLUDED.available_days, available_times = EXCLUDED.available_times,
                   classes_per_month = EXCLUDED.classes_per_month, travel_radius_miles = EXCLUDED.travel_radius_miles,
                   locations_to_avoid = EXCLUDED.locations_to_avoid, notice_needed = EXCLUDED.notice_needed,
                   availability_notes = EXCLUDED.availability_notes,
                   availability_updated_on = EXCLUDED.availability_updated_on,
                   availability_updated_by = EXCLUDED.availability_updated_by,
                   outreach_status = EXCLUDED.outreach_status, last_contact_on = EXCLUDED.last_contact_on,
                   next_follow_up_on = EXCLUDED.next_follow_up_on, outreach_notes = EXCLUDED.outreach_notes,
                   removal_recommended_on = EXCLUDED.removal_recommended_on, removal_reason = EXCLUDED.removal_reason,
                   removal_status = EXCLUDED.removal_status, remove_from = EXCLUDED.remove_from,
                   associate = EXCLUDED.associate, page_type = EXCLUDED.page_type,
                   date_approval_url = EXCLUDED.date_approval_url,
                   other_enrollware_accounts = EXCLUDED.other_enrollware_accounts,
                   status = CASE WHEN instructors.status = 'suspended' THEN 'suspended' ELSE EXCLUDED.status END,
                   source_synced_at = now(), source_deleted_at = NULL
            RETURNING id
        """), params).scalar_one()
        # The three r.25 certifications: one row each, as Airtable has it (a cleared date removes it).
        for key, cert_type in CERTS.items():
            expires = parse_day(v.get(key))
            if expires is None:
                conn.execute(text("DELETE FROM instructor_certs WHERE instructor_id = :i AND cert_type = :t"),
                             {"i": iid, "t": cert_type})
            elif not conn.execute(text("""UPDATE instructor_certs SET expires_on = :d, updated_at = now()
                                           WHERE instructor_id = :i AND cert_type = :t"""),
                                  {"i": iid, "t": cert_type, "d": expires}).rowcount:
                conn.execute(text("INSERT INTO instructor_certs (instructor_id, cert_type, expires_on) "
                                  "VALUES (:i, :t, :d)"), {"i": iid, "t": cert_type, "d": expires})
        # City areas, with the engine's city area where it has one by that name.
        conn.execute(text("DELETE FROM instructor_metros WHERE instructor_id = :i"), {"i": iid})
        home = None
        for label in _list(v.get("metros")):
            metro = _metro_id(conn, label)
            home = home or metro
            conn.execute(text("""INSERT INTO instructor_metros (instructor_id, metro_label, metro_id)
                                 VALUES (:i, :l, :m) ON CONFLICT DO NOTHING"""), {"i": iid, "l": label, "m": metro})
        if home:
            conn.execute(text("UPDATE instructors SET home_metro_id = coalesce(home_metro_id, :m) WHERE id = :i"),
                         {"m": home, "i": iid})
        if not problem:
            runs.resolve(conn, source=SOURCE, external_ref=record["id"])


def _links(conn, records: list[dict], m: dict, ids: dict[str, int]) -> None:
    """HUB teams and instructor-location relations, rebuilt from every instructor record."""
    hub: set[tuple[int, int]] = set()
    places: set[tuple[int, str, str]] = set()
    for record in records:
        me = ids.get(record["id"])
        if not me:
            continue
        v = _values(record, m)
        hub.update((ids[lead], me) for lead in _ids(v.get("hub_lead")) if lead in ids and ids[lead] != me)
        hub.update((me, ids[mem]) for mem in _ids(v.get("hub_members")) if mem in ids and ids[mem] != me)
        for key, rel in (("pre_approved", "pre_approved"), ("taught", "taught"), ("taught_inactive", "taught_inactive")):
            places.update((me, loc, rel) for loc in _ids(v.get(key)))
    conn.execute(text("DELETE FROM instructor_hub_members"))
    for lead, member in hub:
        conn.execute(text("INSERT INTO instructor_hub_members VALUES (:l, :m)"), {"l": lead, "m": member})
    conn.execute(text("DELETE FROM instructor_locations"))
    for me, loc, rel in places:
        conn.execute(text("""INSERT INTO instructor_locations (instructor_id, location_record_id, relation, site_id)
                             VALUES (:i, :loc, :rel, (SELECT id FROM sites WHERE airtable_record_id = :loc))"""),
                     {"i": me, "loc": loc, "rel": rel})


# --- Master Locations: how each location is staffed -----------------------------------------

def sync_location_settings(m: dict, records: list[dict], stats: dict) -> None:
    if not records:
        return
    match = _matcher()
    with db.transaction() as conn:
        for record in records:
            v = _values(record, m)
            pay = _cents(v.get("extra_pay"))
            prim, hp, hs = _text(v.get("primary_instructor")), _text(v.get("hub_primary")), _text(v.get("hub_secondary"))
            conn.execute(text("""
                INSERT INTO location_instructor_settings (location_record_id, site_id, location_name, assignment_types,
                       primary_instructor_name, primary_instructor_id, hub_primary_instructor_name,
                       hub_primary_instructor_id, hub_secondary_instructor_name, hub_secondary_instructor_id,
                       is_instructor_training_location, instructor_notes, extra_pay_cents, extra_pay_reason)
                VALUES (:rid, (SELECT id FROM sites WHERE airtable_record_id = :rid), :name, :types, :prim, :prim_id,
                        :hp, :hp_id, :hs, :hs_id, :it, :notes, :pay, :reason)
                ON CONFLICT (location_record_id) DO UPDATE SET site_id = EXCLUDED.site_id,
                       location_name = EXCLUDED.location_name, assignment_types = EXCLUDED.assignment_types,
                       primary_instructor_name = EXCLUDED.primary_instructor_name,
                       primary_instructor_id = EXCLUDED.primary_instructor_id,
                       hub_primary_instructor_name = EXCLUDED.hub_primary_instructor_name,
                       hub_primary_instructor_id = EXCLUDED.hub_primary_instructor_id,
                       hub_secondary_instructor_name = EXCLUDED.hub_secondary_instructor_name,
                       hub_secondary_instructor_id = EXCLUDED.hub_secondary_instructor_id,
                       is_instructor_training_location = EXCLUDED.is_instructor_training_location,
                       instructor_notes = EXCLUDED.instructor_notes, extra_pay_cents = EXCLUDED.extra_pay_cents,
                       extra_pay_reason = EXCLUDED.extra_pay_reason
            """), {"rid": record["id"], "name": _text(v.get("name")), "types": _list(v.get("assignment_types")),
                   "prim": prim, "prim_id": match(prim), "hp": hp, "hp_id": match(hp), "hs": hs, "hs_id": match(hs),
                   "it": _truthy(v.get("it_location")), "notes": _text(v.get("instructor_notes")),
                   "pay": pay if pay is not None and pay >= 0 else None, "reason": _text(v.get("extra_pay_reason"))})
        stats["location_instructor_settings"] = len(records)


# --- Class Schedule: the class's lead and planning fields --------------------------------------

def sync_class_instructors(m: dict, records: list[dict], run_id: int, stats: dict) -> None:
    if not records:
        return
    match = _matcher()
    with db.engine().connect() as c:
        mapped = dict(c.execute(text("SELECT airtable_record_id, session_id FROM session_airtable_records "
                                     "WHERE airtable_record_id = ANY(:ids)"), {"ids": [r["id"] for r in records]}).all())
    unmatched: set[str] = set()
    rows: dict[int, dict] = {}
    for record in records:
        sid = mapped.get(record["id"])
        if not sid:
            continue
        v = _values(record, m)
        lead = None if _is_record_ids(v.get("lead_instructor")) else _text(v.get("lead_instructor"))
        lead_id = match(lead)
        if lead and not lead_id:
            unmatched.add(lead)
        row = rows.setdefault(sid, {"s": sid, "lead": None, "assigned": False, "avail": None, "tier": None})
        row["lead"] = row["lead"] or lead_id
        row["assigned"] = row["assigned"] or bool(_truthy(v.get("instructor_assigned")))
        row["avail"] = row["avail"] or _text(v.get("availability_status"))
        row["tier"] = row["tier"] or _text(v.get("performance_tier"))
    with db.transaction() as conn:
        for row in rows.values():
            conn.execute(text("""
                INSERT INTO session_instructor_details (session_id, lead_instructor_id, instructor_assigned,
                                                        availability_status, performance_tier)
                VALUES (:s, :lead, :assigned, :avail, :tier)
                ON CONFLICT (session_id) DO UPDATE SET lead_instructor_id = EXCLUDED.lead_instructor_id,
                       instructor_assigned = EXCLUDED.instructor_assigned,
                       availability_status = EXCLUDED.availability_status, performance_tier = EXCLUDED.performance_tier
                 WHERE (session_instructor_details.lead_instructor_id, session_instructor_details.instructor_assigned,
                        session_instructor_details.availability_status, session_instructor_details.performance_tier)
                       IS DISTINCT FROM (EXCLUDED.lead_instructor_id, EXCLUDED.instructor_assigned,
                                         EXCLUDED.availability_status, EXCLUDED.performance_tier)
            """), row)
        if unmatched:
            runs.issue(conn, source=SOURCE, code="LEAD_NOT_AN_INSTRUCTOR", severity="warning", run_id=run_id,
                       external_ref="lead-names", message=f"{len(unmatched)} lead instructor name(s) in Class Schedule's "
                       "ASSIGN INSTRUCTOR don't match one instructor in the Instructors table, so those classes show "
                       "the name only.", details={"names": sorted(unmatched)[:50]})
        else:
            runs.resolve(conn, source=SOURCE, external_ref="lead-names")
    stats["classes_lead_matched"] = sum(1 for r in rows.values() if r["lead"])
    stats["lead_names_unmatched"] = len(unmatched)


# --- Instructor Recruiting Needs -----------------------------------------------------------

def sync_recruiting(m: dict, run_id: int, stats: dict) -> None:
    records = list(airtable.records(m))
    with db.transaction() as conn:
        for record in records:
            v = _values(record, m)
            _register(conn, record, m["table"], v)
            n = _number(v.get("ew_cancels"))
            spc = _number(v.get("students_per_class"))
            nid = conn.execute(text("""
                INSERT INTO recruiting_needs (airtable_record_id, need, location_record_ids, need_reasons,
                       recruiting_status, recruiting_notes, enrollware_no_instructor_cancels, cls_lost_cents,
                       profit_while_running_cents, students_per_class, last_month_held, airtable_created_at,
                       source_synced_at)
                VALUES (:rid, :need, :locs, :reasons, :status, :notes, :n, :lost, :profit, :spc, :last,
                        CAST(:added AS timestamptz), now())
                ON CONFLICT (airtable_record_id) DO UPDATE SET need = EXCLUDED.need,
                       location_record_ids = EXCLUDED.location_record_ids, need_reasons = EXCLUDED.need_reasons,
                       recruiting_status = EXCLUDED.recruiting_status, recruiting_notes = EXCLUDED.recruiting_notes,
                       enrollware_no_instructor_cancels = EXCLUDED.enrollware_no_instructor_cancels,
                       cls_lost_cents = EXCLUDED.cls_lost_cents,
                       profit_while_running_cents = EXCLUDED.profit_while_running_cents,
                       students_per_class = EXCLUDED.students_per_class, last_month_held = EXCLUDED.last_month_held,
                       airtable_created_at = EXCLUDED.airtable_created_at, source_synced_at = now(),
                       source_deleted_at = NULL
                RETURNING id
            """), {"rid": record["id"], "need": _text(v.get("need")), "locs": _ids(v.get("location")),
                   "reasons": _list(v.get("reasons")), "status": _text(v.get("status")), "notes": _text(v.get("notes")),
                   "n": int(n) if n is not None else None, "lost": _cents(v.get("cls_lost")),
                   "profit": _cents(v.get("profit")), "spc": spc, "last": _text(v.get("last_month")),
                   "added": _text(v.get("date_added")) or record.get("createdTime")}).scalar_one()
            conn.execute(text("DELETE FROM recruiting_need_classes WHERE recruiting_need_id = :n"), {"n": nid})
            for cls in _ids(v.get("classes")):
                conn.execute(text("""
                    INSERT INTO recruiting_need_classes (recruiting_need_id, class_record_id, session_id)
                    VALUES (:n, :c, (SELECT session_id FROM session_airtable_records WHERE airtable_record_id = :c))
                """), {"n": nid, "c": cls})
        conn.execute(text("""UPDATE recruiting_needs SET source_deleted_at = now()
                              WHERE NOT airtable_record_id = ANY(:seen) AND source_deleted_at IS NULL"""),
                     {"seen": [r["id"] for r in records]})
    stats["recruiting_needs"] = len(records)
