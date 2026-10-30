"""Booking a class: pick the course and seats, name each attendee, pay on Stripe.

    GET  /book/<class id>?seats=N   the form (one row per seat)
    POST /book/<class id>           hold the seats, then off to Stripe Checkout
    GET  /book/done?order=…         back from Stripe: booked (or "confirming…")
    GET  /book/cancelled?order=…    left Stripe without paying: seats freed at once

Guests can book without an account; a signed-in customer's details are filled
in. Only public class details are shown (never access codes or contacts).
"""

from __future__ import annotations

from flask import Blueprint, abort, current_app, redirect, render_template, request

from .. import auth
from ..integrations import checkout

bp = Blueprint("book", __name__, url_prefix="/book")


def _base_url() -> str:
    configured = current_app.config.get("PUBLIC_BASE_URL", "")
    if configured and "localhost" not in configured:
        return configured.rstrip("/")
    return request.url_root.rstrip("/")


def _seats_arg(raw: str | None, seats_left: int) -> int:
    try:
        n = int(raw or 1)
    except ValueError:
        n = 1
    return max(1, min(n, checkout.MAX_SEATS_PER_ORDER, max(seats_left, 1)))


@bp.get("/<public_id>")
def form(public_id: str):
    s = checkout.bookable_session(public_id)
    if not s:
        abort(404)
    seats = _seats_arg(request.args.get("seats"), s["seats_left"])
    user = auth.current_user()
    buyer = {"first_name": user.first_name if user else "", "last_name": "", "email": user.email if user else "",
             "phone": ""}
    if user and user.display_name and " " in user.display_name:
        buyer["first_name"], buyer["last_name"] = user.display_name.split(" ", 1)
    return render_template("public/book.html", s=s, seats=seats, form={}, buyer=buyer, error=None,
                           product_id=request.args.get("product", type=int),
                           online=checkout.stripe_api.check_key_for_environment() is None)


@bp.post("/<public_id>")
def submit(public_id: str):
    s = checkout.bookable_session(public_id)
    if not s:
        abort(404)
    f = request.form
    seats = _seats_arg(f.get("seats"), checkout.MAX_SEATS_PER_ORDER)
    buyer = {"first_name": f.get("buyer_first_name", "").strip(), "last_name": f.get("buyer_last_name", "").strip(),
             "email": f.get("buyer_email", "").strip().lower(), "phone": f.get("buyer_phone", "").strip()}
    error = None
    attendees: list[checkout.Person] = []
    phone = checkout.normalise_phone(buyer["phone"])
    if not (buyer["first_name"] and buyer["last_name"]):
        error = "Enter your first and last name."
    elif not checkout.EMAIL.match(buyer["email"]):
        error = "Enter a valid email address."
    elif phone == "":
        error = "Enter a valid phone number, or leave it blank."
    else:
        me = checkout.Person(buyer["first_name"], buyer["last_name"], buyer["email"], phone or None)
        for i in range(seats):
            if i == 0 and f.get("buyer_attends", "yes") == "yes":
                attendees.append(me)
                continue
            first, last = f.get(f"a{i}_first", "").strip(), f.get(f"a{i}_last", "").strip()
            email = f.get(f"a{i}_email", "").strip().lower()
            if not (first and last and checkout.EMAIL.match(email)):
                error = f"Seat {i + 1}: enter the attendee's first name, last name and email."
                break
            attendees.append(checkout.Person(first, last, email))
    product_id = f.get("product_id", type=int)
    if not error:
        try:
            url = checkout.start(session_public_id=public_id, product_id=product_id or 0, buyer=me,
                                 attendees=attendees, base_url=_base_url(), client_ip=request.remote_addr,
                                 user_agent=request.headers.get("User-Agent"))
            return redirect(url, code=303)
        except checkout.BookingError as exc:
            error = str(exc)
            s = checkout.bookable_session(public_id)
    return render_template("public/book.html", s=s, seats=seats, form=f, buyer=buyer, error=error,
                           product_id=product_id, online=checkout.stripe_api.check_key_for_environment() is None), 400


@bp.get("/done")
def done():
    order = checkout.order_summary(request.args.get("order", ""))
    if not order:
        abort(404)
    return render_template("public/book_done.html", order=order)


@bp.get("/cancelled")
def cancelled():
    order_id = request.args.get("order", "")
    checkout.cancel_by_customer(order_id)
    order = checkout.order_summary(order_id)
    return render_template("public/book_cancelled.html", order=order)
