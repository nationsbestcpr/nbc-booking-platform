"""Booking a class: pick the course and seats, name each attendee, pay on Stripe.

    GET  /book/<class id>?seats=N   the form (one row per seat)
    POST /book/<class id>           hold the seats, then off to Stripe Checkout
    GET  /book/done?order=…         back from Stripe: booked (or "confirming…")
    GET  /book/cancelled?order=…    left Stripe without paying: seats freed at once

Booking needs an account: anyone not signed in is sent to sign in (email and
password, Google or Microsoft) or create an account, and comes straight back
to the class they picked. The order is saved to that account
(orders.buyer_user_id), so it shows on their dashboard; the confirmation goes
to the account's email. Only public class details are shown (never access
codes or contacts).
"""

from __future__ import annotations

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for

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


def _sign_in_first():
    """Not signed in: off to sign in, then straight back to this class (same seats and course)."""
    back = request.full_path.rstrip("?") if request.method == "GET" else \
        url_for("book.form", public_id=request.view_args["public_id"])
    return redirect(url_for("account.sign_in", next=back))


def _buyer(user: auth.CurrentUser, f=None) -> dict:
    first, _, last = (user.display_name or "").partition(" ")
    f = f or {}
    return {"first_name": (f.get("buyer_first_name") or first).strip(),
            "last_name": (f.get("buyer_last_name") or last).strip(),
            "email": user.email.lower(), "phone": (f.get("buyer_phone") or "").strip()}


@bp.get("/<public_id>")
def form(public_id: str):
    s = checkout.bookable_session(public_id)
    if not s:
        abort(404)
    user = auth.current_user()
    online = checkout.online_booking()
    if user is None and s["on_sale"] and s["seats_left"] > 0 and online:
        return _sign_in_first()                  # a full or closed class is shown to anyone
    seats = _seats_arg(request.args.get("seats"), s["seats_left"])
    return render_template("public/book.html", s=s, seats=seats, form={}, error=None,
                           buyer=_buyer(user) if user else {},
                           product_id=request.args.get("product", type=int),
                           online=online, testing=checkout.test_checkout())


@bp.post("/<public_id>")
def submit(public_id: str):
    s = checkout.bookable_session(public_id)
    if not s:
        abort(404)
    user = auth.current_user()
    if user is None:
        return _sign_in_first()
    f = request.form
    seats = _seats_arg(f.get("seats"), checkout.MAX_SEATS_PER_ORDER)
    buyer = _buyer(user, f)
    error = None
    attendees: list[checkout.Person] = []
    phone = checkout.normalise_phone(buyer["phone"])
    if not (buyer["first_name"] and buyer["last_name"]):
        error = "Enter your first and last name."
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
                                 attendees=attendees, base_url=_base_url(), buyer_user_id=user.id,
                                 client_ip=request.remote_addr, user_agent=request.headers.get("User-Agent"))
            return redirect(url, code=303)
        except checkout.BookingError as exc:
            error = str(exc)
            s = checkout.bookable_session(public_id)
    return render_template("public/book.html", s=s, seats=seats, form=f, buyer=buyer, error=error,
                           product_id=product_id, online=checkout.online_booking(),
                           testing=checkout.test_checkout()), 400


@bp.get("/done")
def done():
    order = checkout.order_summary(request.args.get("order", ""))
    if not order:
        abort(404)
    user = auth.current_user()
    if order["buyer_user_id"] and (user is None or user.id != order["buyer_user_id"]):
        if user is None:                     # e.g. the session ended while they were paying
            return redirect(url_for("account.sign_in", next=request.full_path.rstrip("?")))
        abort(404)                           # someone else's booking
    return render_template("public/book_done.html", order=order)


@bp.get("/cancelled")
def cancelled():
    order_id = request.args.get("order", "")
    checkout.cancel_by_customer(order_id)
    order = checkout.order_summary(order_id)
    return render_template("public/book_cancelled.html", order=order)
