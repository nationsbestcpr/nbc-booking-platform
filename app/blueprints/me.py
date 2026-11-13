"""Every signed-in account: /dashboard, the customer area (/my), Profile and Settings.

/dashboard sends each account to its own area (worked out from its roles).
Profile and Settings are shared by all areas and show that area's tabs.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from sqlalchemy import text

from .. import areas, auth, booking_setup, classes, db, invites
from ..integrations.outbox import enqueue

bp = Blueprint("me", __name__)


@bp.get("/dashboard")
@auth.login_required
def dashboard():
    return redirect(url_for(areas.home_endpoint(auth.current_user())))


@bp.get("/my")
@auth.login_required
def home():
    """The customer dashboard: classes they booked (upcoming and past), waiting
    lists, and the same class search as the home page to book another."""
    user = auth.current_user()
    mine = classes.booked(user.id, user.email, user.email_verified)
    waiting = db.fetch_all("""
        SELECT w.status, w.party_size, s.starts_at, s.time_zone, cf.name AS course_name, si.name AS site_name
          FROM waitlist_entries w
          JOIN sessions s ON s.id = w.session_id
          JOIN sites si ON si.id = s.site_id
          JOIN course_families cf ON cf.id = s.course_family_id
         WHERE w.email = :email AND w.status IN ('waiting', 'offered') AND s.starts_at > now()
         ORDER BY s.starts_at
    """, email=user.email) if user.email_verified else []
    found = classes.search(classes.filters_from(request.args))
    me_email = user.email.lower()
    for r in mine["upcoming"] + mine["past"]:
        r["mine"] = (r["attendee_email"] or "").lower() == me_email
        r["booked_by_other"] = r["mine"] and not r["bought_here"] and (r["buyer_email"] or "").lower() != me_email
    booked_for_others = [r for r in mine["upcoming"] if r["bought_here"] and not r["mine"]]
    invite_status = invites.status_for([r["id"] for r in booked_for_others])
    return render_template("me/home.html", upcoming=mine["upcoming"], past=mine["past"][:10],
                           more_past=len(mine["past"]) > 10, waiting=waiting, found=found,
                           others=booked_for_others, invite_status=invite_status,
                           friend=booking_setup.bring_a_friend(), tabs=areas.tabs_for(user))


@bp.post("/my/invites/<reg_public_id>/resend")
@auth.login_required
def resend_invite(reg_public_id: str):
    """The booker sends an attendee's invite again (at most once every 10 minutes per seat)."""
    user = auth.current_user()
    reg = db.fetch_one("""
        SELECT r.id, r.attendee_first_name, r.attendee_email,
               (SELECT max(n.sent_at) FROM notifications n WHERE n.kind = 'attendee_invite'
                   AND n.subject_type = 'registration' AND n.subject_id = r.id) AS last_sent
          FROM registrations r JOIN orders o ON o.id = r.order_id JOIN sessions s ON s.id = r.session_id
         WHERE r.public_id = CAST(:p AS uuid) AND o.buyer_user_id = :u
           AND r.status IN ('confirmed', 'awaiting_payment') AND s.starts_at > now()
           AND lower(r.attendee_email) <> lower(o.buyer_email)
    """, p=reg_public_id, u=user.id) if classes_is_uuid(reg_public_id) else None
    if reg is None:
        abort(404)
    recent = db.fetch_value("SELECT :t > now() - interval '10 minutes'", t=reg["last_sent"]) if reg["last_sent"] else False
    if recent:
        flash(f"An invite went to {reg['attendee_email']} in the last few minutes. Ask them to check their spam "
              "folder before sending another.", "error")
    else:
        from datetime import datetime, timezone
        with db.transaction() as conn:
            enqueue(conn, "ghl", "send_invite", key=f"ghl-invite-{reg['id']}-{int(datetime.now(timezone.utc).timestamp())}",
                    entity_type="registration", entity_id=reg["id"], payload={"registration_id": reg["id"]})
        flash(f"We're sending {reg['attendee_first_name']}'s invite again to {reg['attendee_email']}.", "ok")
    return redirect(url_for("me.home") + "#people")


def classes_is_uuid(value: str) -> bool:
    import re
    return bool(re.fullmatch(r"[0-9a-fA-F-]{36}", value or ""))


@bp.route("/account/profile", methods=["GET", "POST"])
@auth.login_required
def profile():
    user = auth.current_user()
    errors: dict[str, str] = {}
    name = user.display_name
    if request.method == "POST":
        name = " ".join(request.form.get("display_name", "").split())
        if not name:
            errors["display_name"] = "Enter your name."
        elif len(name) > 120:
            errors["display_name"] = "Keep your name under 120 characters."
        else:
            with db.transaction() as conn:
                conn.execute(text("UPDATE user_accounts SET display_name = :n WHERE id = :id"),
                             {"n": name, "id": user.id})
            flash("Your profile is saved.", "ok")
            return redirect(url_for("me.profile"))
    instructor = db.fetch_one("""
        SELECT i.first_name, i.last_name, i.email, i.phone_e164, i.status,
               hm.name AS home_city, hm.state_code AS home_state
          FROM instructors i LEFT JOIN metros hm ON hm.id = i.home_metro_id
         WHERE i.user_account_id = :id
    """, id=user.id)
    account = db.fetch_one("SELECT created_at, last_sign_in_at FROM user_accounts WHERE id = :id", id=user.id)
    return render_template("me/profile.html", name=name, errors=errors, instructor=instructor,
                           account=account, tabs=areas.tabs_for(user)), (400 if errors else 200)


@bp.route("/account/settings", methods=["GET", "POST"])
@auth.login_required
def settings():
    user = auth.current_user()
    errors: dict[str, str] = {}
    if request.method == "POST":
        try:
            auth.change_password(user.id, request.form.get("current_password", ""),
                                 request.form.get("new_password", ""), request.form.get("confirm_password", ""))
            flash("Your password is changed.", "ok")
            return redirect(url_for("me.settings"))
        except auth.SignUpError as exc:
            errors[exc.field] = exc.message
    return render_template("me/settings.html", errors=errors, has_password=auth.has_password(user.id),
                           tabs=areas.tabs_for(user)), (400 if errors else 200)
