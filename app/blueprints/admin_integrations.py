"""Integrations page actions (on the admin blueprint).

    POST /admin/integrations/airtable/sync     Sync now                 class.sync_now
    GET  /admin/sync?record=rec…               Airtable "Sync now" button  class.sync_now
    GET  /admin/integrations/airtable/fields   Check Airtable fields    system.health_view
    POST /admin/integrations/ghl/stages        Load pipeline stages     settings.manage
    POST /admin/integrations/queue/run         Send waiting items now   system.issue_resolve
    POST /admin/integrations/jobs/<id>/retry   Retry one item           system.issue_resolve
    POST /admin/integrations/issues/<id>/resolve  Mark a problem fixed  system.issue_resolve
    POST /admin/locations/<id>/booking         Switch a city's booking  settings.manage

Every action is written to the action log in the same transaction as its
change (the sync itself keeps its own record in integration_runs).
"""

from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for
from sqlalchemy import text

from .. import areas, audit, auth, db
from ..integrations import airtable_sync, ghl, outbox
from ..integrations.http import IntegrationError
from .admin import bp


def _summary(stats: dict) -> str:
    parts = [f"{stats.get('classes_created', 0)} new", f"{stats.get('classes_updated', 0)} changed",
             f"{stats.get('classes_unchanged', 0)} unchanged"]
    if stats.get("classes_removed"):
        parts.append(f"{stats['classes_removed']} taken off sale")
    if stats.get("problems"):
        parts.append(f"{stats['problems']} problem{'s' if stats['problems'] != 1 else ''} to look at")
    return ", ".join(parts)


@bp.post("/integrations/airtable/sync")
@auth.permission_required("class.sync_now")
def airtable_sync_now():
    user = auth.current_user()
    try:
        stats = airtable_sync.run("button", user_id=user.id)
        with db.transaction() as conn:
            audit.record(conn, "integration.sync_now", "integration_run", stats.get("run_id"),
                         after={"source": "airtable"})
        flash(f"Airtable synced: {_summary(stats)}.", "ok" if stats.get("status") == "succeeded" else "warn")
    except IntegrationError as exc:
        flash(f"The Airtable sync didn't run: {exc}", "error")
    return redirect(url_for("admin.integrations"))


@bp.get("/sync")
@auth.permission_required("class.sync_now")
def airtable_sync_record():
    """Target of the Airtable button field: …/admin/sync?record=recXXXXXXXXXXXXXX"""
    record = request.args.get("record", "").strip()
    user = auth.current_user()
    try:
        stats = airtable_sync.run("button", user_id=user.id, record_id=record)
    except IntegrationError as exc:
        flash(f"That class didn't sync: {exc}", "error")
        return redirect(url_for("admin.integrations"))
    session_id = db.fetch_value("SELECT session_id FROM session_airtable_records WHERE airtable_record_id = :r",
                                r=record)
    with db.transaction() as conn:
        audit.record(conn, "integration.sync_now", "session" if session_id else "integration_run",
                     session_id or stats.get("run_id"), after={"source": "airtable", "record": record})
    if stats.get("problems"):
        flash("Airtable synced this class, but found a problem with it. See the problems below.", "warn")
        return redirect(url_for("admin.integrations"))
    if session_id:
        flash("Synced from Airtable just now.", "ok")
        return redirect(url_for("admin.classes_detail", sid=session_id))
    flash("Synced. This class isn't on sale (its Status doesn't include Enrollware), so there's nothing to show.",
          "ok")
    return redirect(url_for("admin.integrations"))


@bp.get("/integrations/airtable/fields")
@auth.permission_required("system.health_view")
def airtable_fields():
    try:
        mapping = airtable_sync.check_fields()
        error = None
    except IntegrationError as exc:
        mapping, error = {}, str(exc)
    return render_template("admin/airtable_fields.html", mapping=mapping, error=error,
                           tabs=areas.tabs_for(auth.current_user()))


@bp.post("/integrations/ghl/stages")
@auth.permission_required("settings.manage")
def ghl_stages():
    try:
        result = ghl.load_stages()
        with db.transaction() as conn:
            audit.record(conn, "integration.ghl_stages", "ghl_stage_map", None, after=result)
        msg = f"Loaded the {result['pipeline']} pipeline: " + ", ".join(
            f"{k.replace('_', ' ')} → {v}" for k, v in result["found"].items())
        if result["missing"]:
            msg += ". No stage found for: " + ", ".join(m.replace("_", " ") for m in result["missing"])
        flash(msg + ".", "warn" if result["missing"] else "ok")
    except IntegrationError as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin.integrations"))


@bp.post("/integrations/queue/run")
@auth.permission_required("system.issue_resolve")
def queue_run():
    counts = outbox.run_due(limit=50)
    flash(f"Sent {counts['succeeded']}; {counts['failed']} will be tried again; {counts['dead']} need a look."
          if any(counts.values()) else "Nothing was waiting to be sent.", "ok")
    return redirect(url_for("admin.integrations"))


@bp.post("/integrations/jobs/<int:job_id>/retry")
@auth.permission_required("system.issue_resolve")
def job_retry(job_id: int):
    with db.transaction() as conn:
        if outbox.retry(conn, job_id):
            audit.record(conn, "integration.retry", "outbox_job", job_id)
            flash("It will be sent again within a minute.", "ok")
        else:
            flash("That item isn't waiting for a retry any more.", "warn")
    return redirect(url_for("admin.integrations"))


@bp.post("/integrations/issues/<int:issue_id>/resolve")
@auth.permission_required("system.issue_resolve")
def issue_resolve(issue_id: int):
    user = auth.current_user()
    with db.transaction() as conn:
        done = conn.execute(text("""
            UPDATE integration_issues SET resolved_at = now(), resolved_by_user_id = :u
             WHERE id = :id AND resolved_at IS NULL RETURNING code
        """), {"id": issue_id, "u": user.id}).scalar()
        if done:
            audit.record(conn, "integration.issue_resolve", "integration_issue", issue_id, after={"code": done})
    flash("Marked as fixed. If the problem is still there, the next sync will list it again." if done
          else "That problem was already closed.", "ok")
    return redirect(url_for("admin.integrations"))


@bp.post("/locations/<int:metro_id>/booking")
@auth.permission_required("settings.manage")
def city_booking(metro_id: int):
    system = request.form.get("booking_system")
    if system not in ("engine", "enrollware"):
        flash("Choose where this city books.", "error")
        return redirect(url_for("admin.locations"))
    with db.transaction() as conn:
        before = conn.execute(text("SELECT name, booking_system FROM metros WHERE id = :id FOR NO KEY UPDATE"),
                              {"id": metro_id}).mappings().first()
        if not before:
            flash("That city doesn't exist.", "error")
            return redirect(url_for("admin.locations"))
        if before["booking_system"] != system:
            conn.execute(text("""
                UPDATE metros SET booking_system = :s,
                       cutover_at = CASE WHEN :s = 'engine' THEN COALESCE(cutover_at, now()) ELSE cutover_at END
                 WHERE id = :id
            """), {"s": system, "id": metro_id})
            audit.record(conn, "city.booking_system", "metro", metro_id,
                         before={"booking_system": before["booking_system"]},
                         after={"booking_system": system, "city": before["name"]},
                         reason=request.form.get("reason") or None)
    flash(f"{before['name']} now books {'here' if system == 'engine' else 'in Enrollware'}.", "ok")
    return redirect(url_for("admin.locations"))
