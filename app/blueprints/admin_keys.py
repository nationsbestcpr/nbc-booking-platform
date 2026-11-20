"""Superadmin → API keys: the keys the NBC Ops Hub (and any other tool) use to call /api/v1.

A key is shown once, on the page that makes it, and never stored: only a hash
of its secret is kept. Each key lists what it may do (permissions), and every
call names the staff member it is for, so a key never does more than that
person may. The access list on this page is read from the API's own routes.
"""

from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for

from .. import apikeys, areas, auth, db
from . import api
from .admin import bp

# What the NBC Ops Hub's customer service desk needs (ticked when making a key).
DESK_PERMISSIONS = [
    "class.view", "class.roster_export", "student.view", "student.note", "payment.view",
    "booking.create_phone", "booking.create_invoice", "booking.transfer", "booking.transfer_bulk",
    "booking.cancel", "booking.attendance", "refund.issue", "refund.approve_large", "class.capacity_override",
    "waitlist.view", "waitlist.manage", "instructor.view", "instructor.requests", "teaching.coverage_view",
    "pay.view_all",
]


def _page(new_key: str | None = None, error: str | None = None, form: dict | None = None, code: int = 200):
    perms = db.fetch_all("SELECT code, area, description FROM permissions WHERE NOT own_records_only "
                         "AND code NOT LIKE 'self.%' ORDER BY area, code")
    by_area: dict[str, list[dict]] = {}
    for p in perms:
        by_area.setdefault(p["area"], []).append(p)
    used = {p for e in api.access_list() for p in e["permissions"]}
    ticked = set(form.getlist("permissions")) if form else set(DESK_PERMISSIONS)
    return render_template("admin/api_keys.html", keys=apikeys.listing(), by_area=by_area, ticked=ticked, used=used,
                           access=api.access_list(), new_key=new_key, error=error,
                           name=(form.get("name", "") if form else "NBC Ops Hub"),
                           tabs=areas.tabs_for(auth.current_user())), code


@bp.route("/api-keys", methods=["GET", "POST"])
@auth.permission_required("api_key.manage")
def api_keys():
    if request.method == "GET":
        return _page()
    try:
        _key_id, raw = apikeys.create(request.form.get("name", ""), request.form.getlist("permissions"))
    except apikeys.ApiKeyError as exc:
        return _page(error=str(exc), form=request.form, code=400)
    return _page(new_key=raw)          # shown once, on this page only (never in a flash message or cookie)


@bp.post("/api-keys/<int:key_id>/revoke")
@auth.permission_required("api_key.manage")
def api_keys_revoke(key_id: int):
    try:
        name = apikeys.revoke(key_id)
        flash(f"Switched off “{name}”. Calls with it are refused from now on.", "ok")
    except apikeys.ApiKeyError as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin.api_keys"))
