"""Admin area, the records: classes, bookings, waiting lists, locations,
instructors, the action log and business settings.

Read straight from the schema. Each list asks the database whether the
account may see each row's city (user_has_permission(..., metro_id)), so a
role limited to one city only sees that city. Private class details need
'class.packet_view'; contact details need 'student.view'; amounts need
'payment.view'. Business settings are the only thing changed here
('settings.manage'), and every change is written to the action log.
"""

from __future__ import annotations

import json

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy import text

from .. import areas, audit, auth, db
from .admin import bp


def _ctx() -> dict:
    return {"tabs": areas.tabs_for(auth.current_user())}


def _filters() -> dict:
    return {
        "cities": db.fetch_all("SELECT slug, name, state_code FROM metros ORDER BY name"),
        "courses": db.fetch_all("SELECT code, name FROM course_families ORDER BY name"),
    }


REG_STATUS = {
    "pending": ("wait", "Holding a seat"), "awaiting_payment": ("wait", "Payment due"),
    "confirmed": ("ok", "Booked"), "displaced": ("bad", "Class cancelled: needs a new date"),
    "completed": ("ok", "Completed"), "no_show": ("bad", "No show"), "cancelled": ("off", "Cancelled"),
    "forfeited": ("off", "Fee forfeited"), "expired": ("off", "Hold expired"),
}
WAIT_STATUS = {
    "waiting": ("wait", "Waiting"), "offered": ("ok", "Seat offered"), "offer_expired": ("bad", "Offer expired"),
    "converted": ("ok", "Booked"), "removed": ("off", "Removed"), "closed_no_seat": ("off", "No seat opened"),
}


# --- Classes -------------------------------------------------------------------------

@bp.get("/classes")
@auth.permission_required("class.view")
def classes():
    f = {k: request.args.get(k, d) for k, d in (("city", ""), ("course", ""), ("when", "upcoming"), ("status", ""))}
    rows = db.fetch_all("""
        SELECT s.id, s.starts_at, s.ends_at, s.time_zone, s.status, s.is_published, s.delivery_mode,
               cf.name AS course_name, si.name AS site_name, m.name AS city_name,
               av.sold_seats, av.held_seats, av.external_seats, av.seat_limit, av.seats_left, av.waitlist_count,
               coalesce((SELECT i.first_name || ' ' || i.last_name FROM assignments a
                           JOIN instructors i ON i.id = a.instructor_id
                          WHERE a.session_id = s.id AND a.role = 'lead' AND a.status = 'assigned' LIMIT 1),
                        s.airtable_lead_instructor) AS lead
          FROM sessions s
          JOIN metros m ON m.id = s.metro_id
          JOIN sites si ON si.id = s.site_id
          JOIN course_families cf ON cf.id = s.course_family_id
          JOIN session_availability av ON av.session_id = s.id
         WHERE user_has_permission(:uid, 'class.view', s.metro_id)
           AND (:city = '' OR m.slug = :city)
           AND (:course = '' OR cf.code = :course)
           AND (:when = 'all' OR (:when = 'upcoming' AND s.starts_at > now()) OR (:when = 'past' AND s.starts_at <= now()))
           AND (:status = ''
                OR (:status = 'on_sale' AND s.status = 'scheduled' AND s.is_published)
                OR (:status = 'not_published' AND s.status = 'scheduled' AND NOT s.is_published)
                OR s.status = :status)
         ORDER BY s.starts_at, si.name
         LIMIT 201
    """, uid=auth.current_user().id, **f)
    if f["when"] == "past":
        rows.reverse()
    return render_template("admin/classes.html", rows=rows[:200], more=len(rows) > 200, f=f, **_filters(), **_ctx())


@bp.get("/classes/<int:sid>")
@auth.permission_required("class.view")
def classes_detail(sid: int):
    user = auth.current_user()
    c = db.fetch_one("""
        SELECT s.*, cf.name AS course_name, si.name AS site_name, si.address_line1, si.address_line2,
               si.city AS site_city, si.state_code, si.postal_code, si.parking_notes_public,
               m.name AS city_name, r.name AS room_name,
               av.sold_seats, av.held_seats, av.external_seats, av.override_seats, av.seat_limit,
               av.seats_left, av.waitlist_count
          FROM sessions s
          JOIN metros m ON m.id = s.metro_id
          JOIN sites si ON si.id = s.site_id
          JOIN rooms r ON r.id = s.room_id
          JOIN course_families cf ON cf.id = s.course_family_id
          JOIN session_availability av ON av.session_id = s.id
         WHERE s.id = :id
    """, id=sid)
    if not c:
        abort(404)
    if not user.can("class.view", c["metro_id"]):
        abort(403)
    products = db.fetch_all("""
        SELECT p.short_name, p.name, sp.price_cents, sp.is_offered
          FROM session_products sp JOIN products p ON p.id = sp.product_id
         WHERE sp.session_id = :id ORDER BY sp.price_cents DESC
    """, id=sid)
    teachers = db.fetch_all("""
        SELECT a.role, a.status, i.first_name, i.last_name
          FROM assignments a JOIN instructors i ON i.id = a.instructor_id
         WHERE a.session_id = :id AND a.status IN ('assigned', 'completed')
         ORDER BY a.role DESC
    """, id=sid)
    roster = db.fetch_all("""
        SELECT r.attendee_first_name, r.attendee_last_name, r.attendee_email, r.status, r.checked_in_at,
               r.amount_paid_cents, p.short_name, o.channel
          FROM registrations r
          JOIN products p ON p.id = r.product_id
          JOIN orders o ON o.id = r.order_id
         WHERE r.session_id = :id AND r.status NOT IN ('expired')
         ORDER BY r.status = 'cancelled', r.attendee_last_name, r.attendee_first_name
    """, id=sid)
    waiting = db.fetch_all("""
        SELECT q.position, q.first_name, q.last_name, q.email, q.party_size, q.status, q.joined_at
          FROM waitlist_queue q WHERE q.session_id = :id ORDER BY q.position
    """, id=sid) if user.can("waitlist.view", c["metro_id"]) else None
    private = db.fetch_one("""
        SELECT coalesce(sp.access_code, d.access_code) AS access_code, d.access_instructions,
               d.poc_name, d.poc_phone, d.poc_email, d.emergency_contact, sp.instructor_notes, sp.poc_override
          FROM sessions s
          LEFT JOIN site_private_details d ON d.site_id = s.site_id
          LEFT JOIN session_private_details sp ON sp.session_id = s.id
         WHERE s.id = :id
    """, id=sid) if user.can("class.packet_view", c["metro_id"]) else None
    return render_template("admin/class_detail.html", c=c, products=products, teachers=teachers, roster=roster,
                           waiting=waiting, private=private, reg_status=REG_STATUS, wait_status=WAIT_STATUS,
                           show_contact=user.can("student.view", c["metro_id"]),
                           show_money=user.can("payment.view", c["metro_id"]), **_ctx())


# --- Bookings ------------------------------------------------------------------------

@bp.get("/bookings")
@auth.permission_required("student.view")
def bookings():
    user = auth.current_user()
    f = {k: request.args.get(k, "") for k in ("q", "status", "city")}
    rows = db.fetch_all("""
        SELECT r.id, r.created_at, r.attendee_first_name, r.attendee_last_name, r.attendee_email, r.status,
               r.amount_paid_cents, r.amount_refunded_cents, o.channel, o.buyer_email,
               s.id AS session_id, s.starts_at, s.time_zone, s.metro_id,
               cf.name AS course_name, si.name AS site_name, m.name AS city_name, p.short_name
          FROM registrations r
          JOIN orders o ON o.id = r.order_id
          JOIN sessions s ON s.id = r.session_id
          JOIN sites si ON si.id = s.site_id
          JOIN metros m ON m.id = s.metro_id
          JOIN course_families cf ON cf.id = s.course_family_id
          JOIN products p ON p.id = r.product_id
         WHERE user_has_permission(:uid, 'student.view', s.metro_id)
           AND (:q = '' OR r.attendee_email ILIKE '%' || :q || '%' OR o.buyer_email ILIKE '%' || :q || '%'
                OR (r.attendee_first_name || ' ' || r.attendee_last_name) ILIKE '%' || :q || '%')
           AND (:status = '' OR r.status = :status)
           AND (:city = '' OR m.slug = :city)
         ORDER BY r.created_at DESC
         LIMIT 201
    """, uid=user.id, **f)
    return render_template("admin/bookings.html", rows=rows[:200], more=len(rows) > 200, f=f,
                           reg_status=REG_STATUS, show_money=user.has_permission("payment.view"),
                           **_filters(), **_ctx())


# --- Waiting lists -------------------------------------------------------------------

@bp.get("/waitlists")
@auth.permission_required("waitlist.view")
def waitlists():
    user = auth.current_user()
    f = {k: request.args.get(k, "") for k in ("city", "status")}
    rows = db.fetch_all("""
        SELECT w.id, w.first_name, w.last_name, w.email, w.party_size, w.status, w.joined_at,
               q.position, s.id AS session_id, s.starts_at, s.time_zone,
               cf.name AS course_name, si.name AS site_name, m.name AS city_name, av.seats_left
          FROM waitlist_entries w
          LEFT JOIN waitlist_queue q ON q.id = w.id
          JOIN sessions s ON s.id = w.session_id
          JOIN sites si ON si.id = s.site_id
          JOIN metros m ON m.id = s.metro_id
          JOIN course_families cf ON cf.id = s.course_family_id
          JOIN session_availability av ON av.session_id = s.id
         WHERE user_has_permission(:uid, 'waitlist.view', s.metro_id)
           AND s.starts_at > now()
           AND (:city = '' OR m.slug = :city)
           AND (:status = '' OR w.status = :status)
         ORDER BY s.starts_at, q.position NULLS LAST, w.joined_at
         LIMIT 300
    """, uid=user.id, **f)
    show_contact = user.has_permission("student.view")
    return render_template("admin/waitlists.html", rows=rows, f=f, wait_status=WAIT_STATUS,
                           show_contact=show_contact, **_filters(), **_ctx())


# --- Locations -----------------------------------------------------------------------

@bp.get("/locations")
@auth.permission_required("class.view")
def locations():
    uid = auth.current_user().id
    cities = db.fetch_all("""
        SELECT m.id, m.name, m.state_code, m.booking_system, m.cutover_at, m.is_hub, m.is_active, m.time_zone
          FROM metros m WHERE user_has_permission(:uid, 'class.view', m.id) ORDER BY m.name
    """, uid=uid)
    sites = db.fetch_all("""
        SELECT si.id, si.metro_id, si.name, si.address_line1, si.address_line2, si.city, si.state_code,
               si.postal_code, si.time_zone, si.is_active, (si.parking_notes_public IS NOT NULL) AS has_parking,
               (SELECT json_agg(json_build_object('name', r.name, 'max', r.max_student_count) ORDER BY r.name)
                  FROM rooms r WHERE r.site_id = si.id AND r.is_active) AS rooms,
               (SELECT count(*) FROM sessions s WHERE s.site_id = si.id AND s.status = 'scheduled'
                   AND s.starts_at > now()) AS upcoming
          FROM sites si WHERE user_has_permission(:uid, 'class.view', si.metro_id) ORDER BY si.name
    """, uid=uid)
    for city in cities:
        city["sites"] = [s for s in sites if s["metro_id"] == city["id"]]
    return render_template("admin/locations.html", cities=cities, **_ctx())


# --- Instructors ---------------------------------------------------------------------

@bp.get("/instructors")
@auth.permission_required("instructor.view")
def instructors():
    user = auth.current_user()
    rows = db.fetch_all("""
        SELECT i.id, i.first_name, i.last_name, i.email, i.phone_e164, i.status, i.performance_tier,
               i.hub_priority, i.user_account_id, hm.name AS home_city, hub.name AS hub_city,
               (SELECT count(*) FROM instructor_certs c WHERE c.instructor_id = i.id AND c.expires_on >= current_date) AS valid_certs,
               (SELECT min(c.expires_on) FROM instructor_certs c WHERE c.instructor_id = i.id AND c.expires_on >= current_date) AS next_expiry,
               (SELECT count(*) FROM assignments a JOIN sessions s ON s.id = a.session_id
                 WHERE a.instructor_id = i.id AND a.status = 'assigned' AND s.starts_at > now()) AS upcoming,
               (SELECT count(*) FROM assignments a JOIN sessions s ON s.id = a.session_id
                 WHERE a.instructor_id = i.id AND a.status = 'assigned' AND a.role = 'co' AND s.starts_at > now()) AS upcoming_helper
          FROM instructors i
          LEFT JOIN metros hm ON hm.id = i.home_metro_id
          LEFT JOIN metros hub ON hub.id = i.hub_metro_id
         WHERE user_has_permission(:uid, 'instructor.view', i.home_metro_id)
            OR user_has_permission(:uid, 'instructor.view', NULL)
         ORDER BY i.last_name, i.first_name
    """, uid=user.id)
    return render_template("admin/instructors.html", rows=rows, can_users=user.has_permission("user.manage"), **_ctx())


# --- Action log ----------------------------------------------------------------------

@bp.get("/log")
@auth.permission_required("audit.view")
def log():
    from .admin import _role_names, describe
    q = request.args.get("q", "").strip()
    area = request.args.get("area", "")
    rows = db.fetch_all("""
        SELECT l.id, l.occurred_at, l.action, l.entity_type, l.entity_id, l.before, l.after, l.reason,
               l.actor_type, l.acting_role, a.display_name AS actor, a.email AS actor_email, t.display_name AS target
          FROM audit_log l
          LEFT JOIN user_accounts a ON a.id = l.user_account_id
          LEFT JOIN user_accounts t ON l.entity_type = 'user_account' AND t.id = l.entity_id
         WHERE (:q = '' OR a.display_name ILIKE '%' || :q || '%' OR a.email ILIKE '%' || :q || '%'
                OR t.display_name ILIKE '%' || :q || '%')
           AND (:area = '' OR l.action LIKE :area || '.%')
         ORDER BY l.occurred_at DESC, l.id DESC
         LIMIT 201
    """, q=q, area=area)
    names = _role_names()
    for r in rows:
        r["text"] = describe(r, names, r["target"])
    areas_ = [a["area"] for a in db.fetch_all(
        "SELECT DISTINCT split_part(action, '.', 1) AS area FROM audit_log ORDER BY 1")]
    return render_template("admin/log.html", rows=rows[:200], more=len(rows) > 200, q=q, area=area,
                           areas=areas_, role_names=names, **_ctx())


# --- Business settings ---------------------------------------------------------------

# Set per database by the deployment, never from the screen: changing them
# would point a test app at live behaviour (or the reverse).
LOCKED_SETTINGS = {"environment", "airtable_writeback_enabled"}
# Settings that may be left empty (a decision not made yet).
NULLABLE_SETTINGS = {"large_refund_threshold_cents"}
# Settings edited as a list of names -> ids (JSON object of text), with the names allowed.
ID_MAP_SETTINGS = {"ghl_custom_field_ids": {"session_id", "registration_id", "site", "class_date", "class_time",
                                            "product", "reg_type", "city", "class_id", "course", "course_date",
                                            "course_location", "hours", "instructor", "class_price", "order_total",
                                            "delivery", "reg_date"}}


def _setting_kind(key: str, value) -> str:
    """'money' (whole cents, edited in dollars), 'number', 'yesno' or 'fixed' (read-only here)."""
    if key in LOCKED_SETTINGS:
        return "fixed"
    if key in ID_MAP_SETTINGS:
        return "idmap"
    if isinstance(value, bool):
        return "yesno"
    if isinstance(value, int) or (value is None and key in NULLABLE_SETTINGS):
        return "money" if key.endswith("_cents") else "number"
    return "fixed"


@bp.route("/settings", methods=["GET", "POST"])
@auth.permission_required("settings.manage")
def settings():
    if request.method == "POST":
        key = request.form.get("key", "")
        row = db.fetch_one("SELECT key, value FROM app_settings WHERE key = :k", k=key)
        if not row:
            abort(404)
        kind = _setting_kind(key, row["value"])
        raw = request.form.get("value", "").strip().replace(",", "").lstrip("$")
        if kind == "fixed":
            abort(400)
        if kind == "idmap":
            try:
                new = json.loads(request.form.get("value", "").strip() or "{}")
                allowed = ID_MAP_SETTINGS[key]
                if not isinstance(new, dict) or any(k not in allowed or not isinstance(v, str) for k, v in new.items()):
                    raise ValueError
                new = {k: v.strip() for k, v in new.items() if v.strip()}
            except ValueError:
                flash("Use the form {\"name\": \"id\", ...} with these names: "
                      + ", ".join(sorted(ID_MAP_SETTINGS[key])) + ". Nothing was changed.", "error")
                return redirect(url_for("admin.settings") + f"#s-{key}")
        elif kind == "yesno":
            new = raw == "yes"
        elif raw == "" and key in NULLABLE_SETTINGS:
            new = None
        else:
            try:
                new = round(float(raw) * 100) if kind == "money" else int(raw)
            except ValueError:
                flash(f"“{raw}” isn't a number. Nothing was changed.", "error")
                return redirect(url_for("admin.settings") + f"#s-{key}")
            if new < 0:
                flash("Use zero or more. Nothing was changed.", "error")
                return redirect(url_for("admin.settings") + f"#s-{key}")
        if new == row["value"]:
            flash("No change to save.", "ok")
            return redirect(url_for("admin.settings") + f"#s-{key}")
        with db.transaction() as conn:
            conn.execute(text("""
                UPDATE app_settings SET value = CAST(:v AS jsonb), updated_by_user_id = :by, updated_at = now()
                 WHERE key = :k
            """), {"v": json.dumps(new), "by": auth.current_user().id, "k": key})
            audit.record(conn, "settings.change", "app_setting", None,
                         before={"key": key, "value": row["value"]}, after={"key": key, "value": new})
        from ..filters import setting_label
        flash(f"Saved: {setting_label(key)}.", "ok")
        return redirect(url_for("admin.settings") + f"#s-{key}")

    rows = db.fetch_all("""
        SELECT s.key, s.value, s.description, s.updated_at, a.display_name AS updated_by
          FROM app_settings s LEFT JOIN user_accounts a ON a.id = s.updated_by_user_id
         ORDER BY s.key
    """)
    for r in rows:
        r["kind"] = _setting_kind(r["key"], r["value"])
        r["json"] = json.dumps(r["value"], indent=1) if r["kind"] in ("fixed", "idmap") else None
        r["names"] = sorted(ID_MAP_SETTINGS.get(r["key"], ()))
    return render_template("admin/settings.html", rows=rows, locked=LOCKED_SETTINGS, **_ctx())
