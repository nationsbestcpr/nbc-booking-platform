"""Superadmin → Cutover and import: leaving Enrollware.

Two steps, in either order:
  * Move every city to the booking app (and new cities from Airtable start here too): the
    'enrollware_retired' setting, and each city's booking_system.
  * Import Enrollware's upcoming registrations from its export (app/enrollware_import.py): first "Check the
    file" (the whole import, rolled back), then "Import". Loading a newer export later adds only what is new.

The uploaded file is kept for an hour between the check and the import (in the instance folder), so it
needn't be chosen again; it holds students' details, so it is deleted after the import and never served.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

from flask import abort, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy import text

from .. import areas, audit, auth, db, enrollware_import
from .admin import bp

MAX_BYTES = 15 * 1024 * 1024
KEEP_SECONDS = 3600


def _folder() -> Path:
    path = Path(current_app.instance_path) / "imports"
    path.mkdir(parents=True, exist_ok=True)
    for old in path.glob("*.upload"):
        if time.time() - old.stat().st_mtime > KEEP_SECONDS:
            old.unlink(missing_ok=True)
    return path


def _stash(name: str, data: bytes) -> str:
    token = secrets.token_urlsafe(18)
    (_folder() / f"{token}.upload").write_bytes(data)
    (_folder() / f"{token}.name").write_text(name[:200])
    return token


def _unstash(token: str) -> tuple[str, bytes] | None:
    if not token or not token.replace("-", "").replace("_", "").isalnum():
        return None
    data, name = _folder() / f"{token}.upload", _folder() / f"{token}.name"
    if not data.exists():
        return None
    return (name.read_text() if name.exists() else "export.xlsx"), data.read_bytes()


def _drop(token: str) -> None:
    for suffix in (".upload", ".name"):
        (_folder() / f"{token}{suffix}").unlink(missing_ok=True)


def _state() -> dict:
    retired = db.fetch_value("SELECT value FROM app_settings WHERE key = 'enrollware_retired'")
    cities = db.fetch_all("""
        SELECT m.id, m.name, m.state_code, m.booking_system, m.cutover_at,
               (SELECT count(*) FROM sessions s WHERE s.metro_id = m.id AND s.status = 'scheduled'
                  AND s.starts_at > now() AND s.is_published) AS upcoming,
               (SELECT coalesce(sum(e.seats), 0) FROM external_seat_reservations e JOIN sessions s ON s.id = e.session_id
                 WHERE s.metro_id = m.id AND s.starts_at > now()) AS enrollware_seats
          FROM metros m WHERE m.is_active ORDER BY m.booking_system DESC, m.name
    """)
    imports = db.fetch_all("""
        SELECT i.id, i.file_name, i.rows_total, i.summary, i.sent_to_ghl, i.created_at, a.display_name AS by_name
          FROM enrollware_imports i JOIN user_accounts a ON a.id = i.imported_by_user_id
         ORDER BY i.created_at DESC LIMIT 20
    """)
    return {"retired": retired is True or str(retired).lower() == "true", "cities": cities, "imports": imports,
            "on_enrollware": [c for c in cities if c["booking_system"] != "engine"]}


@bp.get("/cutover")
@auth.permission_required("system.cutover")
def cutover():
    return render_template("admin/cutover.html", **_state(), report=None, token=None,
                           tabs=areas.tabs_for(auth.current_user()))


@bp.post("/cutover/retire")
@auth.permission_required("system.cutover")
def cutover_retire():
    """Enrollware is gone: every city books here from now on, and so does every new city from Airtable."""
    if request.form.get("confirm") != "yes":
        flash("Tick the box to confirm Enrollware no longer takes bookings.", "error")
        return redirect(url_for("admin.cutover"))
    with db.transaction() as conn:
        moved = conn.execute(text("""
            UPDATE metros SET booking_system = 'engine', cutover_at = coalesce(cutover_at, now())
             WHERE booking_system <> 'engine' RETURNING name
        """)).scalars().all()
        conn.execute(text("UPDATE app_settings SET value = 'true'::jsonb WHERE key = 'enrollware_retired'"))
        audit.record(conn, "enrollware.retire", "app_setting", None,
                     before={"enrollware_retired": False}, after={"enrollware_retired": True, "cities_moved": moved})
    flash(f"Enrollware is retired: {len(moved)} cit{'y' if len(moved) == 1 else 'ies'} moved to the booking app, and "
          "new cities from Airtable start here. Now import Enrollware's upcoming registrations below.", "ok")
    return redirect(url_for("admin.cutover"))


@bp.post("/cutover/import")
@auth.permission_required("system.cutover")
def cutover_import():
    """Check an export (rolled back) or import it. A check keeps the file for an hour so Import needn't re-upload."""
    user = auth.current_user()
    commit = request.form.get("action") == "import"
    add_seats = request.form.get("add_seats") == "1"
    send_to_ghl = request.form.get("send_to_ghl") == "1"
    token = request.form.get("token", "")
    upload = request.files.get("file")
    if upload and upload.filename:
        data = upload.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            flash("That file is over 15 MB. Export fewer months from Enrollware.", "error")
            return redirect(url_for("admin.cutover"))
        name = upload.filename
        token = _stash(name, data)
    else:
        found = _unstash(token)
        if not found:
            flash("Choose Enrollware's registration export first (the check keeps it for an hour).", "error")
            return redirect(url_for("admin.cutover"))
        name, data = found
    try:
        rows = enrollware_import.read_file(name, data)
    except enrollware_import.ImportFileError as exc:
        _drop(token)
        flash(str(exc), "error")
        return redirect(url_for("admin.cutover"))
    report = enrollware_import.run(rows, file_name=name, user_id=user.id, commit=commit, add_seats=add_seats,
                                   send_to_ghl=send_to_ghl)
    if commit:
        _drop(token)
        flash(f"Imported: {report.counts.get('imported', 0)} seat(s) and {report.counts.get('waitlisted', 0)} "
              "waiting-list place(s).", "ok")
        return redirect(url_for("admin.cutover_import_detail", import_id=report.import_id))
    return render_template("admin/cutover.html", **_state(), report=report, token=token, add_seats=add_seats,
                           send_to_ghl=send_to_ghl, tabs=areas.tabs_for(user))


@bp.get("/cutover/imports/<int:import_id>")
@auth.permission_required("system.cutover")
def cutover_import_detail(import_id: int):
    imp = db.fetch_one("""
        SELECT i.*, a.display_name AS by_name FROM enrollware_imports i
          JOIN user_accounts a ON a.id = i.imported_by_user_id WHERE i.id = :i
    """, i=import_id)
    if not imp:
        abort(404)
    outcome = request.args.get("outcome", "")
    rows = db.fetch_all("""
        SELECT x.row_number, x.outcome, x.reason, x.enrollware_class_ref, s.public_id AS class_id, s.starts_at,
               s.time_zone, si.name AS site_name, cf.name AS course_name,
               coalesce(r.attendee_first_name || ' ' || r.attendee_last_name, w.first_name || ' ' || w.last_name) AS name
          FROM enrollware_import_rows x
          LEFT JOIN sessions s ON s.id = x.session_id
          LEFT JOIN sites si ON si.id = s.site_id
          LEFT JOIN course_families cf ON cf.id = s.course_family_id
          LEFT JOIN registrations r ON r.id = x.registration_id
          LEFT JOIN waitlist_entries w ON w.id = x.waitlist_entry_id
         WHERE x.import_id = :i AND (:o = '' OR x.outcome = :o)
         ORDER BY x.row_number
    """, i=import_id, o=outcome)
    summary = imp["summary"] if isinstance(imp["summary"], dict) else json.loads(imp["summary"] or "{}")
    return render_template("admin/cutover_import.html", imp=imp, summary=summary, rows=rows, outcome=outcome,
                           tabs=areas.tabs_for(auth.current_user()))
