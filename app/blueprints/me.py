"""Every signed-in account: /dashboard, the student area, Profile and Settings.

/dashboard sends each account to its own area (worked out from its roles).
Profile and Settings are shared by all areas and show that area's tabs.
"""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from sqlalchemy import text

from .. import areas, auth, db

bp = Blueprint("me", __name__)


@bp.get("/dashboard")
@auth.login_required
def dashboard():
    return redirect(url_for(areas.home_endpoint(auth.current_user())))


@bp.get("/my")
@auth.login_required
def home():
    user = auth.current_user()
    # Bookings are matched by email: as the attendee, or as the buyer of the order.
    rows = db.fetch_all("""
        SELECT r.status, r.attendee_first_name, r.attendee_last_name, r.attendee_email,
               s.starts_at, s.ends_at, s.time_zone, s.status AS class_status,
               cf.name AS course_name, si.name AS site_name, m.name AS city_name,
               (s.starts_at > now() AND s.status = 'scheduled'
                AND r.status IN ('awaiting_payment', 'confirmed', 'displaced')) AS is_upcoming
          FROM registrations r
          JOIN orders o ON o.id = r.order_id
          JOIN sessions s ON s.id = r.session_id
          JOIN sites si ON si.id = s.site_id
          JOIN metros m ON m.id = s.metro_id
          JOIN course_families cf ON cf.id = s.course_family_id
         WHERE (r.attendee_email = :email OR o.buyer_email = :email)
           AND r.status NOT IN ('pending', 'expired')
         ORDER BY s.starts_at
    """, email=user.email)
    upcoming = [r for r in rows if r["is_upcoming"]]
    past = [r for r in rows if not r["is_upcoming"]][-10:][::-1]   # latest first
    waiting = db.fetch_all("""
        SELECT w.status, w.party_size, s.starts_at, s.time_zone, cf.name AS course_name, si.name AS site_name
          FROM waitlist_entries w
          JOIN sessions s ON s.id = w.session_id
          JOIN sites si ON si.id = s.site_id
          JOIN course_families cf ON cf.id = s.course_family_id
         WHERE w.email = :email AND w.status IN ('waiting', 'offered') AND s.starts_at > now()
         ORDER BY s.starts_at
    """, email=user.email)
    return render_template("me/home.html", upcoming=upcoming, past=past, waiting=waiting,
                           tabs=areas.tabs_for(user))


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
