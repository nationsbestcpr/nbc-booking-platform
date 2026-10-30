"""Admin area: dashboard, Users, Roles & permissions, Integrations.

Open to staff accounts; each tab needs its own permission:
  * Users: 'user.manage' (grant and revoke roles, suspend and reactivate).
  * Roles & permissions: view with 'user.manage' or 'role.manage'; change with 'role.manage'.
  * Integrations: 'system.health_view'.
The admin role holds every permission. The database has the final say on
role grants (see trg_user_roles_guard and trg_keep_an_admin): the views catch
its refusals and explain them. Every change is written to audit_log in the
same transaction.
"""

from __future__ import annotations

import os

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from .. import areas, audit, auth, db

bp = Blueprint("admin", __name__, url_prefix="/admin")


def _refusal_message(exc: DBAPIError) -> str | None:
    """Plain-English reason for a database refusal, or None if it's unexpected."""
    state = getattr(exc.orig, "sqlstate", None)
    detail = str(exc.orig)
    if state == "42501":
        if "granted automatically" in detail:
            return "Student and User are given automatically (a paid seat, a new account), not by hand."
        if "only an admin" in detail:
            return "Only an admin can give or remove the Admin role."
        return "Your account isn't allowed to change roles."
    if state == "23001":
        return "At least one active admin must remain. Make someone else an admin first."
    if state == "23505":
        return "They already have that role."
    return None


def describe(entry: dict, role_names: dict[str, str], account_name: str | None = None) -> str:
    """One action-log row in plain English."""
    after, before = entry.get("after") or {}, entry.get("before") or {}
    who = f" for {account_name}" if account_name else ""
    action = entry["action"]
    if action == "user.role_grant":
        return f"Gave the {role_names.get(after.get('role'), after.get('role'))} role{who}"
    if action == "user.role_revoke":
        return f"Removed the {role_names.get(before.get('role'), before.get('role'))} role{who}"
    if action == "user.suspend":
        return f"Suspended the account{who}"
    if action == "user.reactivate":
        return f"Reactivated the account{who}"
    if action == "settings.change":
        from ..filters import setting_label
        return f"Changed the business setting “{setting_label(after.get('key', ''))}” from {before.get('value')} to {after.get('value')}"
    if action == "role.permissions_change":
        name = role_names.get(after.get("role"), after.get("role"))
        return (f"Changed the {name} role's permissions: "
                f"{len(after.get('added', []))} added, {len(before.get('removed', []))} removed")
    if action == "user.password_reset":
        return f"Reset the password{who}"
    return action.replace("_", " ").replace(".", ": ")


def _role_names() -> dict[str, str]:
    return {r["code"]: r["name"] for r in db.fetch_all("SELECT code, name FROM roles")}


# --- Dashboard -----------------------------------------------------------------------

@bp.get("")
@auth.staff_required
def home():
    user = auth.current_user()
    stats = db.fetch_one("""
        SELECT
          (SELECT count(*) FROM user_accounts WHERE status = 'active') AS accounts,
          (SELECT count(DISTINCT ur.user_account_id) FROM user_roles ur JOIN roles r ON r.code = ur.role_code
            JOIN user_accounts a ON a.id = ur.user_account_id
            WHERE ur.revoked_at IS NULL AND r.is_staff AND a.status = 'active') AS staff,
          (SELECT count(*) FROM instructors WHERE status = 'active') AS instructors,
          (SELECT count(DISTINCT ur.user_account_id) FROM user_roles ur
            WHERE ur.revoked_at IS NULL AND ur.role_code = 'student') AS students,
          (SELECT count(*) FROM sessions s JOIN metros m ON m.id = s.metro_id
            WHERE s.status = 'scheduled' AND s.is_published AND s.starts_at > now()
              AND m.booking_system = 'engine') AS classes_on_sale,
          (SELECT count(*) FROM integration_issues WHERE resolved_at IS NULL) AS open_problems,
          (SELECT count(*) FROM unsynced_items) AS unsynced
    """)
    next_classes = []
    if user.has_permission("class.view"):
        next_classes = db.fetch_all("""
            SELECT s.id, s.starts_at, s.ends_at, s.time_zone, cf.name AS course_name, si.name AS site_name,
                   av.sold_seats, av.seat_limit, av.waitlist_count,
                   EXISTS (SELECT 1 FROM assignments a WHERE a.session_id = s.id AND a.role = 'lead'
                             AND a.status = 'assigned') AS has_lead
              FROM sessions s
              JOIN sites si ON si.id = s.site_id
              JOIN course_families cf ON cf.id = s.course_family_id
              JOIN session_availability av ON av.session_id = s.id
             WHERE s.status = 'scheduled' AND s.starts_at > now()
               AND user_has_permission(:uid, 'class.view', s.metro_id)
             ORDER BY s.starts_at LIMIT 6
        """, uid=user.id)
    recent = []
    if user.has_permission("audit.view"):
        recent = db.fetch_all("""
            SELECT l.occurred_at, l.action, l.entity_type, l.entity_id, l.before, l.after,
                   a.display_name AS actor, t.display_name AS target
              FROM audit_log l
              LEFT JOIN user_accounts a ON a.id = l.user_account_id
              LEFT JOIN user_accounts t ON l.entity_type = 'user_account' AND t.id = l.entity_id
             ORDER BY l.occurred_at DESC LIMIT 8
        """)
        names = _role_names()
        for r in recent:
            r["text"] = describe(r, names, r["target"])
    return render_template("admin/home.html", stats=stats, recent=recent, next_classes=next_classes,
                           tabs=areas.tabs_for(user))


# --- Users ---------------------------------------------------------------------------

@bp.get("/users")
@auth.permission_required("user.manage")
def users():
    q = request.args.get("q", "").strip()
    role = request.args.get("role", "")
    status = request.args.get("status", "")
    rows = db.fetch_all("""
        SELECT a.id, a.email, a.display_name, a.status, a.last_sign_in_at, a.created_at,
               coalesce(string_agg(r.name, ', ' ORDER BY r.sort_order) FILTER (WHERE r.code IS NOT NULL), '') AS roles
          FROM user_accounts a
          LEFT JOIN user_roles ur ON ur.user_account_id = a.id AND ur.revoked_at IS NULL
          LEFT JOIN roles r ON r.code = ur.role_code
         WHERE (:q = '' OR a.email ILIKE '%' || :q || '%' OR a.display_name ILIKE '%' || :q || '%')
           AND (:status = '' OR a.status = :status)
           AND (:role = '' OR EXISTS (SELECT 1 FROM user_roles x WHERE x.user_account_id = a.id
                                        AND x.role_code = :role AND x.revoked_at IS NULL))
         GROUP BY a.id
         ORDER BY a.display_name
         LIMIT 201
    """, q=q, role=role, status=status)
    roles = db.fetch_all("SELECT code, name FROM roles ORDER BY sort_order")
    return render_template("admin/users.html", rows=rows[:200], more=len(rows) > 200, q=q, role=role,
                           status=status, roles=roles, tabs=areas.tabs_for(auth.current_user()))


def _load_account(uid: int) -> dict:
    account = db.fetch_one("""
        SELECT id, email, display_name, status, signup_intent, created_at, last_sign_in_at
          FROM user_accounts WHERE id = :id
    """, id=uid)
    if not account:
        abort(404)
    return account


@bp.get("/users/<int:uid>")
@auth.permission_required("user.manage")
def users_detail(uid: int):
    account = _load_account(uid)
    grants = db.fetch_all("""
        SELECT ur.id, ur.role_code, r.name, r.is_self_service, r.grants_all, ur.granted_at,
               m.name AS city, g.display_name AS granted_by
          FROM user_roles ur
          JOIN roles r ON r.code = ur.role_code
          LEFT JOIN metros m ON m.id = ur.scope_metro_id
          LEFT JOIN user_accounts g ON g.id = ur.granted_by_user_id
         WHERE ur.user_account_id = :id AND ur.revoked_at IS NULL
         ORDER BY r.sort_order
    """, id=uid)
    held = {g["role_code"] for g in grants}
    grantable = [r for r in db.fetch_all("""
        SELECT code, name, description, is_scopable, grants_all FROM roles
         WHERE NOT is_self_service ORDER BY sort_order
    """) if r["code"] not in held or r["is_scopable"]]
    cities = db.fetch_all("SELECT id, name, state_code FROM metros WHERE is_active ORDER BY name")
    history = db.fetch_all("""
        SELECT l.occurred_at, l.action, l.after, l.before, a.display_name AS actor
          FROM audit_log l LEFT JOIN user_accounts a ON a.id = l.user_account_id
         WHERE l.entity_type = 'user_account' AND l.entity_id = :id
         ORDER BY l.occurred_at DESC LIMIT 20
    """, id=uid)
    names = _role_names()
    for h in history:
        h["text"] = describe(h, names)
    return render_template("admin/user_detail.html", account=account, grants=grants, grantable=grantable,
                           cities=cities, history=history, is_self=uid == auth.current_user().id,
                           tabs=areas.tabs_for(auth.current_user()))


@bp.post("/users/<int:uid>/roles")
@auth.permission_required("user.manage")
def users_grant(uid: int):
    account = _load_account(uid)
    role = db.fetch_one("SELECT code, name, is_scopable FROM roles WHERE code = :c",
                        c=request.form.get("role_code", ""))
    city_id = request.form.get("city_id") or None
    if not role:
        flash("Choose a role to give.", "error")
        return redirect(url_for("admin.users_detail", uid=uid))
    if city_id and not role["is_scopable"]:
        flash(f"{role['name']} can't be limited to one city.", "error")
        return redirect(url_for("admin.users_detail", uid=uid))
    try:
        with db.transaction() as conn:
            gid = conn.execute(text("""
                INSERT INTO user_roles (user_account_id, role_code, scope_metro_id, granted_by_user_id)
                VALUES (:uid, :role, :city, :by) RETURNING id
            """), {"uid": uid, "role": role["code"], "city": city_id, "by": auth.current_user().id}).scalar_one()
            audit.record(conn, "user.role_grant", "user_account", uid,
                         after={"role": role["code"], "city_id": city_id, "grant_id": gid})
    except DBAPIError as exc:
        message = _refusal_message(exc)
        if message is None:
            raise
        flash(message, "error")
        return redirect(url_for("admin.users_detail", uid=uid))
    flash(f"{account['display_name']} now has the {role['name']} role.", "ok")
    return redirect(url_for("admin.users_detail", uid=uid))


@bp.post("/users/<int:uid>/roles/<int:grant_id>/revoke")
@auth.permission_required("user.manage")
def users_revoke(uid: int, grant_id: int):
    account = _load_account(uid)
    try:
        with db.transaction() as conn:
            row = conn.execute(text("""
                UPDATE user_roles SET revoked_at = now(), revoked_by_user_id = :by
                 WHERE id = :gid AND user_account_id = :uid AND revoked_at IS NULL
                RETURNING role_code, scope_metro_id
            """), {"gid": grant_id, "uid": uid, "by": auth.current_user().id}).mappings().first()
            if row is None:
                abort(404)
            audit.record(conn, "user.role_revoke", "user_account", uid,
                         before={"role": row["role_code"], "city_id": row["scope_metro_id"], "grant_id": grant_id})
    except DBAPIError as exc:
        message = _refusal_message(exc)
        if message is None:
            raise
        flash(message, "error")
        return redirect(url_for("admin.users_detail", uid=uid))
    name = db.fetch_value("SELECT name FROM roles WHERE code = :c", c=row["role_code"])
    flash(f"Removed the {name} role from {account['display_name']}.", "ok")
    return redirect(url_for("admin.users_detail", uid=uid))


@bp.post("/users/<int:uid>/status")
@auth.permission_required("user.manage")
def users_status(uid: int):
    account = _load_account(uid)
    new = {"suspend": "suspended", "reactivate": "active"}.get(request.form.get("action", ""))
    if new is None:
        abort(400)
    if uid == auth.current_user().id:
        flash("You can't switch off your own account.", "error")
        return redirect(url_for("admin.users_detail", uid=uid))
    try:
        with db.transaction() as conn:
            conn.execute(text("UPDATE user_accounts SET status = :s WHERE id = :id"), {"s": new, "id": uid})
            audit.record(conn, "user.suspend" if new == "suspended" else "user.reactivate", "user_account", uid,
                         before={"status": account["status"]}, after={"status": new})
    except DBAPIError as exc:
        message = _refusal_message(exc)
        if message is None:
            raise
        flash(message, "error")
        return redirect(url_for("admin.users_detail", uid=uid))
    flash(f"{account['display_name']} is {'suspended: they can no longer sign in' if new == 'suspended' else 'active again'}.", "ok")
    return redirect(url_for("admin.users_detail", uid=uid))


# --- Roles & permissions ---------------------------------------------------------------

@bp.get("/roles")
@auth.any_permission_required("user.manage", "role.manage")
def roles():
    rows = db.fetch_all("""
        SELECT r.code, r.name, r.description, r.is_staff, r.is_self_service, r.grants_all,
               (SELECT count(*) FROM user_roles ur JOIN user_accounts a ON a.id = ur.user_account_id
                 WHERE ur.role_code = r.code AND ur.revoked_at IS NULL AND a.status = 'active') AS holders,
               CASE WHEN r.grants_all THEN (SELECT count(*) FROM permissions)
                    ELSE (SELECT count(*) FROM role_permissions rp WHERE rp.role_code = r.code) END AS perms
          FROM roles r ORDER BY r.sort_order
    """)
    return render_template("admin/roles.html", rows=rows, tabs=areas.tabs_for(auth.current_user()))


@bp.route("/roles/<code>", methods=["GET", "POST"])
@auth.any_permission_required("user.manage", "role.manage")
def roles_detail(code: str):
    user = auth.current_user()
    role = db.fetch_one("SELECT * FROM roles WHERE code = :c", c=code)
    if not role:
        abort(404)
    perms = db.fetch_all("""
        SELECT p.code, p.area, p.description, p.own_records_only,
               EXISTS (SELECT 1 FROM role_permissions rp WHERE rp.role_code = :c AND rp.permission_code = p.code) AS granted
          FROM permissions p ORDER BY p.area, p.code
    """, c=code)
    can_edit = user.has_permission("role.manage") and not role["grants_all"]

    if request.method == "POST":
        if not can_edit:
            abort(403)
        valid = {p["code"] for p in perms}
        wanted = set(request.form.getlist("permissions"))
        if not wanted <= valid:
            abort(400)
        current = {p["code"] for p in perms if p["granted"]}
        added, removed = sorted(wanted - current), sorted(current - wanted)
        if added or removed:
            with db.transaction() as conn:
                if removed:
                    conn.execute(text("DELETE FROM role_permissions WHERE role_code = :c AND permission_code = ANY(:p)"),
                                 {"c": code, "p": removed})
                for p in added:
                    conn.execute(text("INSERT INTO role_permissions (role_code, permission_code) VALUES (:c, :p)"),
                                 {"c": code, "p": p})
                audit.record(conn, "role.permissions_change", "role", None,
                             before={"role": code, "removed": removed}, after={"role": code, "added": added})
            flash(f"Saved {role['name']}: {len(added)} added, {len(removed)} removed. "
                  "It applies to everyone with this role from their next page.", "ok")
        else:
            flash("No changes to save.", "ok")
        return redirect(url_for("admin.roles_detail", code=code))

    holders = db.fetch_all("""
        SELECT a.id, a.display_name, a.email, m.name AS city
          FROM user_roles ur JOIN user_accounts a ON a.id = ur.user_account_id
          LEFT JOIN metros m ON m.id = ur.scope_metro_id
         WHERE ur.role_code = :c AND ur.revoked_at IS NULL AND a.status = 'active'
         ORDER BY a.display_name LIMIT 26
    """, c=code)
    by_area: dict[str, list[dict]] = {}
    for p in perms:
        by_area.setdefault(p["area"], []).append(p)
    return render_template("admin/role_detail.html", role=role, by_area=by_area, can_edit=can_edit,
                           holders=holders[:25], more_holders=len(holders) > 25, tabs=areas.tabs_for(user))


# --- Integrations --------------------------------------------------------------------

# What each connection needs and where its activity shows up. Secret values are
# never read here, only whether each named secret is set.
INTEGRATIONS = [
    {"key": "airtable", "name": "Airtable", "phase": "Phase 1 · Airtable sync",
     "what": "Classes, places, courses and prices come in from the NBC Master Base; seat counts go back.",
     "secrets": ["AIRTABLE_TOKEN", "AIRTABLE_BASE_ID"],
     "runs": ["airtable_sync", "airtable_writeback", "enrollment_snapshot"],
     "issue_source": "airtable", "outbox": ["airtable"], "webhook": None, "oauth": None},
    {"key": "stripe", "name": "Stripe", "phase": "Phase 3 · Checkout",
     "what": "Checkout pages, payments and refunds.",
     "secrets": ["STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"],
     "runs": ["stripe_reconcile"], "issue_source": "stripe", "outbox": ["stripe"], "webhook": "stripe", "oauth": None},
    {"key": "ghl", "name": "GHL", "phase": "Phase 4 · GHL sync",
     "what": "One contact and one opportunity per seat; messages and stage changes come back.",
     "secrets": ["GHL_CLIENT_ID", "GHL_CLIENT_SECRET", "TOKEN_ENCRYPTION_KEY"],
     "runs": ["ghl_reconcile"], "issue_source": "ghl", "outbox": ["ghl"], "webhook": "ghl", "oauth": "ghl"},
    {"key": "microsoft", "name": "Microsoft 365 sign-in", "phase": "Phase 4b · Staff dashboards",
     "what": "Staff sign in with their NBC Microsoft accounts.",
     "secrets": ["ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ENTRA_CLIENT_SECRET"],
     "runs": [], "issue_source": None, "outbox": [], "webhook": None, "oauth": None},
]


def _integration_status(spec: dict) -> dict:
    missing = [name for name in spec["secrets"] if not os.environ.get(name)]
    last_run = db.fetch_one("""
        SELECT kind, status, error, started_at, finished_at FROM integration_runs
         WHERE kind = ANY(:k) ORDER BY started_at DESC LIMIT 1
    """, k=spec["runs"]) if spec["runs"] else None
    problems = db.fetch_one("""
        SELECT count(*) AS open, count(*) FILTER (WHERE severity = 'error') AS errors
          FROM integration_issues WHERE source = :s AND resolved_at IS NULL
    """, s=spec["issue_source"]) if spec["issue_source"] else {"open": 0, "errors": 0}
    unsynced = db.fetch_value("SELECT count(*) FROM unsynced_items WHERE target = ANY(:t)",
                              t=spec["outbox"]) if spec["outbox"] else 0
    last_webhook = db.fetch_value("SELECT max(received_at) FROM webhook_events WHERE provider = :p",
                                  p=spec["webhook"]) if spec["webhook"] else None
    connection = db.fetch_one("""
        SELECT account_ref, installed_at, refreshed_at, access_expires_at FROM oauth_connections
         WHERE provider = :p AND revoked_at IS NULL ORDER BY installed_at DESC LIMIT 1
    """, p=spec["oauth"]) if spec["oauth"] else None

    if missing:
        state, label = "off", "Not set up"
    elif problems["errors"] or unsynced or (last_run and last_run["status"] == "failed"):
        state, label = "bad", "Needs attention"
    elif spec["oauth"] and not connection:
        state, label = "wait", "Waiting for the app to be installed"
    elif not (last_run or last_webhook or connection) and (spec["runs"] or spec["webhook"]):
        state, label = "wait", "Set up, no activity yet"
    else:
        state, label = "ok", "Working"
    return {**spec, "missing": missing, "last_run": last_run, "problems": problems, "unsynced": unsynced,
            "last_webhook": last_webhook, "connection": connection, "state": state, "label": label}


@bp.get("/integrations")
@auth.permission_required("system.health_view")
def integrations():
    cards = [_integration_status(s) for s in INTEGRATIONS]
    issues = db.fetch_all("""
        SELECT source, severity, code, message, occurrences, last_seen_at
          FROM integration_issues WHERE resolved_at IS NULL
         ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, last_seen_at DESC
         LIMIT 20
    """)
    runs = db.fetch_all("""
        SELECT kind, trigger, status, error, stats, started_at, finished_at
          FROM integration_runs ORDER BY started_at DESC LIMIT 10
    """)
    return render_template("admin/integrations.html", cards=cards, issues=issues, runs=runs,
                           tabs=areas.tabs_for(auth.current_user()))
