"""Admin → Booking setup: the questions the booking page asks and the add-ons it offers.

    /admin/booking-questions            list, with On/Off
    /admin/booking-questions/new        add one
    /admin/booking-questions/<id>       change one
    /admin/addons                       list, with Shown/Hidden on booking pages
    /admin/addons/new, /admin/addons/<id>

Both are data (booking_questions, addons), so adding or rewording one needs no
release. Nothing is deleted: answers and sales keep pointing at what was asked
or sold, so a question is turned off and an add-on hidden instead. Needs
'booking_setup.manage'; every change is written to the action log.
"""

from __future__ import annotations

import re

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy import text

from .. import areas, audit, auth, booking_setup, db
from .admin import bp

PERMISSION = "booking_setup.manage"


def _ctx(**kw) -> dict:
    return {"tabs": areas.tabs_for(auth.current_user()),
            "courses": db.fetch_all("SELECT id, name FROM course_families ORDER BY name"), **kw}


def _courses_from(form) -> list[int]:
    known = {c["id"] for c in db.fetch_all("SELECT id FROM course_families")}
    out = []
    for raw in form.getlist("courses"):
        try:
            if int(raw) in known:
                out.append(int(raw))
        except ValueError:
            pass
    return sorted(set(out))


def _set_courses(conn, table: str, key: str, item_id: int, courses: list[int]) -> None:
    conn.execute(text(f"DELETE FROM {table} WHERE {key} = :i"), {"i": item_id})
    for c in courses:
        conn.execute(text(f"INSERT INTO {table} ({key}, course_family_id) VALUES (:i, :c)"), {"i": item_id, "c": c})


def _int(raw: str | None, default: int = 0) -> int:
    try:
        return int(raw or default)
    except ValueError:
        return default


# --- Booking questions ----------------------------------------------------------------

@bp.get("/booking-questions")
@auth.permission_required(PERMISSION)
def questions():
    rows = db.fetch_all("""
        SELECT q.*, (SELECT string_agg(cf.name, ', ' ORDER BY cf.name) FROM booking_question_courses c
                       JOIN course_families cf ON cf.id = c.course_family_id WHERE c.question_id = q.id) AS course_names,
               (SELECT count(*) FROM booking_answers a WHERE a.question_id = q.id) AS answers
          FROM booking_questions q
         ORDER BY q.is_active DESC, q.sort_order, q.id
    """)
    return render_template("admin/questions.html", rows=rows, kinds=booking_setup.KINDS,
                           asked=booking_setup.ASKED_FOR, **_ctx())


def _question_form(form) -> tuple[dict, dict]:
    q = {"label": " ".join(form.get("label", "").split())[:300],
         "help_text": form.get("help_text", "").strip()[:500] or None,
         "kind": form.get("kind", ""), "asked_for": form.get("asked_for", "attendee"),
         "is_required": form.get("is_required") == "yes", "is_active": form.get("is_active", "yes") == "yes",
         "delivery_mode": form.get("delivery_mode") or None,
         "ghl_field_key": form.get("ghl_field_key", "").strip().lower() or None,
         "sort_order": _int(form.get("sort_order")),
         "choices": [c.strip() for c in form.get("choices", "").splitlines() if c.strip()][:30]}
    errors = {}
    if not q["label"]:
        errors["label"] = "Write the question."
    if q["kind"] not in booking_setup.KINDS:
        errors["kind"] = "Choose the kind of answer."
    if q["asked_for"] not in booking_setup.ASKED_FOR:
        errors["asked_for"] = "Choose who answers it."
    if q["delivery_mode"] not in (None, "blended", "classroom"):
        q["delivery_mode"] = None
    if q["kind"] == "choice" and len(q["choices"]) < 2:
        errors["choices"] = "Give at least two choices, one per line."
    if q["kind"] != "choice":
        q["choices"] = []
    if q["ghl_field_key"]:
        if not q["ghl_field_key"].startswith("opportunity."):
            q["ghl_field_key"] = "opportunity." + q["ghl_field_key"]
        if not re.fullmatch(r"opportunity\.[a-z0-9_]+", q["ghl_field_key"]):
            errors["ghl_field_key"] = "Use the GHL field key, like opportunity.medical_needs (letters, numbers, _)."
    return q, errors


@bp.route("/booking-questions/new", methods=["GET", "POST"], endpoint="questions_new")
@bp.route("/booking-questions/<int:qid>", methods=["GET", "POST"], endpoint="questions_edit")
@auth.permission_required(PERMISSION)
def question_edit(qid: int | None = None):
    current = db.fetch_one("SELECT * FROM booking_questions WHERE id = :i", i=qid) if qid else None
    if qid and not current:
        abort(404)
    chosen = [r["course_family_id"] for r in db.fetch_all(
        "SELECT course_family_id FROM booking_question_courses WHERE question_id = :i", i=qid)] if qid else []
    errors: dict[str, str] = {}
    q = dict(current) if current else {"kind": "short_text", "asked_for": "attendee", "is_active": True,
                                       "is_required": False, "choices": [], "sort_order": 0}
    if request.method == "POST":
        q, errors = _question_form(request.form)
        chosen = _courses_from(request.form)
        if not errors:
            user = auth.current_user()
            params = {**q, "u": user.id}
            with db.transaction() as conn:
                if qid:
                    conn.execute(text("""
                        UPDATE booking_questions SET label = :label, help_text = :help_text, kind = :kind,
                               choices = :choices, is_required = :is_required, asked_for = :asked_for,
                               delivery_mode = :delivery_mode, ghl_field_key = :ghl_field_key,
                               is_active = :is_active, sort_order = :sort_order, updated_by_user_id = :u
                         WHERE id = :id
                    """), {**params, "id": qid})
                else:
                    qid = conn.execute(text("""
                        INSERT INTO booking_questions (label, help_text, kind, choices, is_required, asked_for,
                                                       delivery_mode, ghl_field_key, is_active, sort_order,
                                                       created_by_user_id, updated_by_user_id)
                        VALUES (:label, :help_text, :kind, :choices, :is_required, :asked_for, :delivery_mode,
                                :ghl_field_key, :is_active, :sort_order, :u, :u)
                        RETURNING id
                    """), params).scalar_one()
                _set_courses(conn, "booking_question_courses", "question_id", qid, chosen)
                audit.record(conn, "booking_question.save" if current else "booking_question.add",
                             "booking_question", qid, before=_plain(current), after={**q, "courses": chosen})
            flash(f"Saved: “{q['label']}”. The booking page asks it {'now' if q['is_active'] else 'once you turn it on'}.",
                  "ok")
            return redirect(url_for("admin.questions"))
    return render_template("admin/question_edit.html", q=q, chosen=chosen, errors=errors, editing=bool(current),
                           kinds=booking_setup.KINDS, asked=booking_setup.ASKED_FOR, **_ctx()), \
        (400 if errors else 200)


@bp.post("/booking-questions/<int:qid>/switch")
@auth.permission_required(PERMISSION)
def questions_switch(qid: int):
    with db.transaction() as conn:
        row = conn.execute(text("UPDATE booking_questions SET is_active = NOT is_active, updated_by_user_id = :u "
                                "WHERE id = :i RETURNING label, is_active"),
                           {"i": qid, "u": auth.current_user().id}).mappings().first()
        if not row:
            abort(404)
        audit.record(conn, "booking_question.switch", "booking_question", qid, after={"is_active": row["is_active"]})
    flash(f"“{row['label']}” is {'on: the booking page asks it' if row['is_active'] else 'off: no longer asked'}.", "ok")
    return redirect(url_for("admin.questions"))


# --- Add-ons ----------------------------------------------------------------------------

@bp.get("/addons")
@auth.permission_required(PERMISSION)
def addons():
    rows = db.fetch_all("""
        SELECT a.*, (SELECT string_agg(cf.name, ', ' ORDER BY cf.name) FROM addon_courses c
                       JOIN course_families cf ON cf.id = c.course_family_id WHERE c.addon_id = a.id) AS course_names,
               (SELECT count(*) FROM order_addons o WHERE o.addon_id = a.id AND o.status = 'paid') AS sold
          FROM addons a
         ORDER BY a.show_on_booking_page DESC, a.sort_order, a.name
    """)
    return render_template("admin/addons.html", rows=rows, **_ctx())


def _addon_form(form) -> tuple[dict, dict]:
    a = {"name": " ".join(form.get("name", "").split())[:120],
         "description": form.get("description", "").strip()[:300] or None,
         "per": form.get("per", "seat"), "show_on_booking_page": form.get("show_on_booking_page") == "yes",
         "sort_order": _int(form.get("sort_order"))}
    a["code"] = re.sub(r"[^a-z0-9]+", "-", (form.get("code") or a["name"]).lower()).strip("-")[:60]
    errors = {}
    raw = form.get("price", "").strip().replace(",", "").lstrip("$")
    try:
        a["price_cents"] = round(float(raw) * 100)
        if a["price_cents"] < 0:
            raise ValueError
    except ValueError:
        a["price_cents"] = None
        errors["price"] = "Enter the price in dollars, like 15 or 12.50."
    if not a["name"]:
        errors["name"] = "Give the add-on a name customers will understand."
    if not a["code"]:
        errors["code"] = "Give a short code, like wallet-card."
    if a["per"] not in ("seat", "booking"):
        errors["per"] = "Choose per seat or per booking."
    return a, errors


@bp.route("/addons/new", methods=["GET", "POST"], endpoint="addons_new")
@bp.route("/addons/<int:aid>", methods=["GET", "POST"], endpoint="addons_edit")
@auth.permission_required(PERMISSION)
def addon_edit(aid: int | None = None):
    current = db.fetch_one("SELECT * FROM addons WHERE id = :i", i=aid) if aid else None
    if aid and not current:
        abort(404)
    chosen = [r["course_family_id"] for r in db.fetch_all(
        "SELECT course_family_id FROM addon_courses WHERE addon_id = :i", i=aid)] if aid else []
    errors: dict[str, str] = {}
    a = dict(current) if current else {"per": "seat", "show_on_booking_page": False, "sort_order": 0,
                                       "price_cents": None}
    if request.method == "POST":
        a, errors = _addon_form(request.form)
        chosen = _courses_from(request.form)
        taken = db.fetch_value("SELECT id FROM addons WHERE code = :c AND id IS DISTINCT FROM :i", c=a["code"], i=aid)
        if taken:
            errors["code"] = "Another add-on already uses this code."
        if not errors:
            user = auth.current_user()
            params = {**a, "u": user.id}
            with db.transaction() as conn:
                if aid:
                    conn.execute(text("""
                        UPDATE addons SET code = :code, name = :name, description = :description,
                               price_cents = :price_cents, per = :per, show_on_booking_page = :show_on_booking_page,
                               sort_order = :sort_order, updated_by_user_id = :u
                         WHERE id = :id
                    """), {**params, "id": aid})
                else:
                    aid = conn.execute(text("""
                        INSERT INTO addons (code, name, description, price_cents, per, show_on_booking_page,
                                            sort_order, created_by_user_id, updated_by_user_id)
                        VALUES (:code, :name, :description, :price_cents, :per, :show_on_booking_page, :sort_order,
                                :u, :u)
                        RETURNING id
                    """), params).scalar_one()
                _set_courses(conn, "addon_courses", "addon_id", aid, chosen)
                audit.record(conn, "addon.save" if current else "addon.add", "addon", aid,
                             before=_plain(current), after={**a, "courses": chosen})
            flash(f"Saved: {a['name']}. " + ("Booking pages show it now." if a["show_on_booking_page"]
                                              else "It's hidden from booking pages until you show it."), "ok")
            return redirect(url_for("admin.addons"))
    return render_template("admin/addon_edit.html", a=a, chosen=chosen, errors=errors, editing=bool(current),
                           **_ctx()), (400 if errors else 200)


@bp.post("/addons/<int:aid>/switch")
@auth.permission_required(PERMISSION)
def addons_switch(aid: int):
    with db.transaction() as conn:
        row = conn.execute(text("UPDATE addons SET show_on_booking_page = NOT show_on_booking_page, "
                                "updated_by_user_id = :u WHERE id = :i RETURNING name, show_on_booking_page"),
                           {"i": aid, "u": auth.current_user().id}).mappings().first()
        if not row:
            abort(404)
        audit.record(conn, "addon.switch", "addon", aid, after={"show_on_booking_page": row["show_on_booking_page"]})
    flash(f"{row['name']} is {'shown on booking pages' if row['show_on_booking_page'] else 'hidden from booking pages'}.",
          "ok")
    return redirect(url_for("admin.addons"))


def _plain(row: dict | None) -> dict | None:
    if not row:
        return None
    return {k: v for k, v in row.items() if k not in ("created_at", "updated_at", "created_by_user_id",
                                                     "updated_by_user_id")}
