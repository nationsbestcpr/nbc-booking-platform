"""Booking a class: pick the course and seats, name each attendee, pay on Stripe.

    GET  /book/<class id>?seats=N   the form (one row per seat)
    POST /book/<class id>           hold the seats, then off to Stripe Checkout
    GET  /book/done?order=…         back from Stripe: booked (or "confirming…"), with "Bring a friend"
    GET  /book/cancelled?order=…    left Stripe without paying: seats freed at once
    GET/POST /book/<class id>/friend?order=…   add a friend's seat to a booking already paid

The form also asks the booking questions and offers the add-ons staff set up
(app/booking_setup.py), and "Bring a friend": one more seat for a friend,
priced with the bring-a-friend discount (Business settings). Seats are held
only when the customer presses Continue to payment, never before.

Booking needs an account: anyone not signed in is sent to sign in (email and
password, Google or Microsoft) or create an account, and comes straight back
to the class they picked. The order is saved to that account
(orders.buyer_user_id), so it shows on their dashboard; the confirmation goes
to the account's email. Only public class details are shown (never access
codes or contacts).
"""

from __future__ import annotations

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for

from .. import auth, booking_setup, db
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


def _extras(s: dict) -> dict:
    """What the form asks and offers for this class: questions, add-ons, bring a friend."""
    baf = booking_setup.bring_a_friend()
    return {"questions": booking_setup.questions_for(s), "addons": booking_setup.addons_for(s),
            "friend": baf, "friend_prices": {o["id"]: booking_setup.friend_price(o["price_cents"])
                                             for o in s["offers"]}}


def _render(s: dict, *, seats: int, form, buyer: dict, error: str | None, product_id, status: int = 200):
    return render_template("public/book.html", s=s, seats=seats, form=form, buyer=buyer, error=error,
                           product_id=product_id, online=checkout.online_booking(),
                           testing=checkout.test_checkout(), **_extras(s)), status


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
    return _render(s, seats=seats, form={}, buyer=_buyer(user) if user else {}, error=None,
                   product_id=request.args.get("product", type=int))


def _attendee(f, i: int, who: str, extras: dict, *, friend: bool = False) -> tuple[checkout.Person | None, str | None]:
    """Seat i's person from the form (a{i}_first, a{i}_last, a{i}_email, answers, add-ons)."""
    first, last = f.get(f"a{i}_first", "").strip(), f.get(f"a{i}_last", "").strip()
    email = f.get(f"a{i}_email", "").strip().lower()
    if not (first and last and checkout.EMAIL.match(email)):
        return None, f"{who}: enter {'your friend' if friend else 'the attendee'}'s first name, last name and email."
    person = checkout.Person(first, last, email, is_friend=friend)
    person.answers, error = booking_setup.read_answers(f, extras["questions"]["attendee"], f"a{i}", who)
    person.addon_ids = booking_setup.read_addons(f, extras["addons"]["seat"], f"a{i}")
    return person, error


@bp.post("/<public_id>")
def submit(public_id: str):
    s = checkout.bookable_session(public_id)
    if not s:
        abort(404)
    user = auth.current_user()
    if user is None:
        return _sign_in_first()
    f = request.form
    extras = _extras(s)
    seats = _seats_arg(f.get("seats"), checkout.MAX_SEATS_PER_ORDER)
    buyer = _buyer(user, f)
    error = None
    attendees: list[checkout.Person] = []
    booking_answers: dict[int, str] = {}
    phone = checkout.normalise_phone(buyer["phone"])
    if not (buyer["first_name"] and buyer["last_name"]):
        error = "Enter your first and last name."
    elif phone == "":
        error = "Enter a valid phone number, or leave it blank."
    else:
        me = checkout.Person(buyer["first_name"], buyer["last_name"], buyer["email"], phone or None)
        for i in range(seats):
            if i == 0 and f.get("buyer_attends", "yes") == "yes":
                me.answers, error = booking_setup.read_answers(f, extras["questions"]["attendee"], "a0", "You")
                me.addon_ids = booking_setup.read_addons(f, extras["addons"]["seat"], "a0")
                attendees.append(me)
            else:
                person, error = _attendee(f, i, f"Seat {i + 1}", extras)
                if person:
                    attendees.append(person)
            if error:
                break
        if not error and f.get("bring_friend") == "yes" and extras["friend"]["enabled"]:
            friend, error = _attendee(f, seats, "Your friend", extras, friend=True)
            if friend:
                attendees.append(friend)
        if not error:
            booking_answers, error = booking_setup.read_answers(f, extras["questions"]["booking"], "b",
                                                                "About this booking")
    product_id = f.get("product_id", type=int)
    if not error:
        try:
            url = checkout.start(session_public_id=public_id, product_id=product_id or 0, buyer=me,
                                 attendees=attendees, base_url=_base_url(), buyer_user_id=user.id,
                                 client_ip=request.remote_addr, user_agent=request.headers.get("User-Agent"),
                                 booking_answers=booking_answers,
                                 booking_addon_ids=booking_setup.read_addons(f, extras["addons"]["booking"], "b"))
            return redirect(url, code=303)
        except checkout.BookingError as exc:
            error = str(exc)
            s = checkout.bookable_session(public_id)
    return _render(s, seats=seats, form=f, buyer=buyer, error=error, product_id=product_id, status=400)


def _parent_order(order_public_id: str, s: dict) -> dict | None:
    """The signed-in customer's own paid booking of this class (to add a friend to)."""
    user = auth.current_user()
    if not checkout._is_uuid(order_public_id):
        return None
    return db.fetch_one("""
        SELECT o.id, o.public_id, o.buyer_first_name
          FROM orders o
         WHERE o.public_id = CAST(:p AS uuid) AND o.buyer_user_id = :u AND o.status IN ('paid', 'awaiting_payment')
           AND EXISTS (SELECT 1 FROM registrations r WHERE r.order_id = o.id AND r.session_id = :s
                          AND r.status IN ('confirmed', 'awaiting_payment'))
    """, p=order_public_id, u=user.id, s=s["id"])


@bp.route("/<public_id>/friend", methods=["GET", "POST"])
def friend(public_id: str):
    """Bring a friend after paying: one seat for a friend in the same class, as its own payment."""
    s = checkout.bookable_session(public_id)
    if not s:
        abort(404)
    user = auth.current_user()
    if user is None:
        return redirect(url_for("account.sign_in", next=request.full_path.rstrip("?")))
    order_id = request.values.get("order", "")
    parent = _parent_order(order_id, s)
    if parent is None:
        abort(404)
    extras = _extras(s)
    open_ = s["on_sale"] and s["seats_left"] > 0 and extras["friend"]["enabled"] and checkout.online_booking()
    error = None
    f = request.form if request.method == "POST" else {}
    product_id = f.get("product_id", type=int) if f else request.args.get("product", type=int)
    if request.method == "POST" and open_:
        person, error = _attendee(f, 0, "Your friend", extras, friend=True)
        if not error:
            buyer = _buyer(user)
            me = checkout.Person(buyer["first_name"] or user.display_name, buyer["last_name"] or "-", buyer["email"])
            try:
                url = checkout.start(session_public_id=public_id, product_id=product_id or 0, buyer=me,
                                     attendees=[person], base_url=_base_url(), buyer_user_id=user.id,
                                     client_ip=request.remote_addr, user_agent=request.headers.get("User-Agent"),
                                     parent_order_id=parent["id"])
                return redirect(url, code=303)
            except checkout.BookingError as exc:
                error = str(exc)
                s = checkout.bookable_session(public_id)
    return render_template("public/book_friend.html", s=s, parent=parent, form=f, error=error, open=open_,
                           product_id=product_id, testing=checkout.test_checkout(), **extras), \
        (400 if error else 200)


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
    friend = booking_setup.bring_a_friend()
    s = checkout.bookable_session(str(order["session_public_id"]))
    can_bring_friend = bool(friend["enabled"] and s and s["on_sale"] and s["seats_left"] > 0
                            and order["status"] == "paid" and order["seats_confirmed"] > 0)
    return render_template("public/book_done.html", order=order, friend=friend, can_bring_friend=can_bring_friend,
                           friend_prices={o["id"]: booking_setup.friend_price(o["price_cents"])
                                          for o in (s["offers"] if s else [])}, s=s)


@bp.get("/cancelled")
def cancelled():
    order_id = request.args.get("order", "")
    checkout.cancel_by_customer(order_id)
    order = checkout.order_summary(order_id)
    return render_template("public/book_cancelled.html", order=order)
