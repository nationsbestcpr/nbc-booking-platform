"""Booking: seat hold -> Stripe Checkout -> confirmed registrations.

The order of things (docs: launch hub 15.6, decision D2):
  1. Book: one transaction creates the order, a 15-minute seat hold and one
     pending registration per named attendee. The database refuses it if the
     seats aren't there (two buyers can't both get the last seat).
  2. A Stripe Checkout session is opened for that hold (Stripe's minimum
     life is 30 minutes, so the engine expires it itself at 15).
  3. checkout.session.completed (webhook): the registrations become
     confirmed, the hold is used up, the order is paid, and each seat is
     queued for GHL (one contact + one opportunity per attendee).
  4. A hold that lapses unpaid is released by the worker, which also expires
     the Stripe page. If money still arrives for a lapsed hold, the engine
     confirms it when a seat is free; otherwise it refunds automatically and
     lists it under the Integrations page's problems.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from .. import db
from . import runs, stripe_api
from .http import IntegrationError
from .outbox import enqueue, handler

MAX_SEATS_PER_ORDER = 10
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class BookingError(Exception):
    """Shown to the customer as is."""


@dataclass
class Person:
    first_name: str
    last_name: str
    email: str
    phone_e164: str | None = None


def _setting(conn: Connection, key: str, default: int) -> int:
    v = conn.execute(text("SELECT value FROM app_settings WHERE key = :k"), {"k": key}).scalar()
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def normalise_phone(raw: str | None) -> str | None:
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None
    if len(digits) == 10:
        digits = "1" + digits
    return f"+{digits}" if 8 <= len(digits) <= 15 and digits[0] != "0" else ""


def test_checkout() -> bool:
    """Test bookings without payment: only outside production, and only while no
    Stripe key is set. Book confirms the seats at once (no money moves), so the
    whole path (seats, the app's records, GHL contact and opportunity) can be
    tried before Stripe is connected. Adding STRIPE_SECRET_KEY switches it off."""
    return current_app.config["APP_ENV"] != "production" and not stripe_api.secret_key()


def online_booking() -> bool:
    return test_checkout() or stripe_api.check_key_for_environment() is None


def bookable_session(public_id: str) -> dict | None:
    """The class with what a customer may see, and whether it's on sale."""
    s = db.fetch_one("""
        SELECT s.id, s.public_id, s.starts_at, s.ends_at, s.time_zone, s.delivery_mode, s.status, s.is_published,
               cf.name AS course_name, si.name AS site_name, si.address_line1, si.address_line2, si.city AS site_city,
               si.state_code AS site_state, si.postal_code, si.parking_notes_public, si.directions_public,
               m.name AS city_name, m.state_code, m.booking_system, a.seats_left, a.seat_limit,
               (s.status = 'scheduled' AND s.is_published AND m.booking_system = 'engine' AND m.is_active
                AND s.starts_at > now() AND (s.sales_close_at IS NULL OR s.sales_close_at > now())) AS on_sale
          FROM sessions s
          JOIN metros m ON m.id = s.metro_id
          JOIN sites si ON si.id = s.site_id
          JOIN course_families cf ON cf.id = s.course_family_id
          JOIN session_availability a ON a.session_id = s.id
         WHERE s.public_id = CAST(:p AS uuid)
    """, p=public_id) if _is_uuid(public_id) else None
    if s:
        s["offers"] = db.fetch_all("""
            SELECT p.id, p.name, p.short_name, sp.price_cents FROM session_products sp
              JOIN products p ON p.id = sp.product_id
             WHERE sp.session_id = :s AND sp.is_offered AND p.is_active
             ORDER BY p.sort_order, sp.price_cents DESC
        """, s=s["id"])
    return s


def _is_uuid(v: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F-]{36}", v or ""))


def start(*, session_public_id: str, product_id: int, buyer: Person, attendees: list[Person],
          base_url: str, client_ip: str | None = None, user_agent: str | None = None) -> str:
    """Hold the seats and open Stripe Checkout. Returns the Stripe page URL."""
    testing = test_checkout()
    if not testing and stripe_api.check_key_for_environment():
        raise BookingError("Online booking isn't open yet. Call us to book this class.")
    if not attendees or len(attendees) > MAX_SEATS_PER_ORDER:
        raise BookingError(f"Book between 1 and {MAX_SEATS_PER_ORDER} seats at a time.")
    emails = [a.email.lower() for a in attendees]
    if len(set(emails)) != len(emails):
        raise BookingError("Each person attending needs their own email address.")

    s = bookable_session(session_public_id)
    if not s or not s["on_sale"]:
        raise BookingError("This class isn't on sale any more. Pick another date.")
    offer = next((o for o in s["offers"] if o["id"] == product_id), None)
    if not offer:
        raise BookingError("Choose one of the courses offered at this class.")
    quantity = len(attendees)
    if quantity > s["seats_left"]:
        raise BookingError(f"Only {s['seats_left']} seat{'s' if s['seats_left'] != 1 else ''} left in this class."
                           if s["seats_left"] > 0 else "This class just filled up.")
    livemode = False if testing else stripe_api.is_live_key()

    try:
        with db.transaction() as conn:
            hold_minutes = _setting(conn, "checkout_hold_minutes", 15)
            stripe_minutes = max(_setting(conn, "stripe_checkout_expiry_minutes", 30), 30) + 1
            subtotal = offer["price_cents"] * quantity
            order = conn.execute(text("""
                INSERT INTO orders (channel, buyer_first_name, buyer_last_name, buyer_email, buyer_phone_e164,
                                    subtotal_cents, total_cents, livemode, client_ip, client_user_agent)
                VALUES ('web', :fn, :ln, :em, :ph, :sub, :sub, :live, CAST(:ip AS inet), :ua)
                RETURNING id, public_id
            """), {"fn": buyer.first_name, "ln": buyer.last_name, "em": buyer.email, "ph": buyer.phone_e164,
                   "sub": subtotal, "live": livemode, "ip": client_ip, "ua": (user_agent or "")[:500] or None}
            ).mappings().one()
            hold = conn.execute(text("""
                INSERT INTO seat_holds (session_id, order_id, kind, quantity, expires_at)
                VALUES (:s, :o, 'checkout', :q, now() + make_interval(mins => :m))
                RETURNING id, public_id
            """), {"s": s["id"], "o": order["id"], "q": quantity, "m": hold_minutes}).mappings().one()
            for a in attendees:
                conn.execute(text("""
                    INSERT INTO registrations (order_id, seat_hold_id, session_id, product_id, attendee_first_name,
                                               attendee_last_name, attendee_email, attendee_phone_e164, unit_price_cents)
                    VALUES (:o, :h, :s, :p, :fn, :ln, :em, :ph, :price)
                """), {"o": order["id"], "h": hold["id"], "s": s["id"], "p": product_id, "fn": a.first_name,
                       "ln": a.last_name, "em": a.email, "ph": a.phone_e164, "price": offer["price_cents"]})
    except DBAPIError as exc:
        if "is full" in str(exc.orig) or "not on sale" in str(exc.orig) or "not open" in str(exc.orig):
            raise BookingError("Someone just took the last seats in this class. Pick another date.") from exc
        raise

    if testing:
        # Confirmed now, exactly as a Stripe payment would (seats, records, GHL), with no payment row.
        amounts = {"subtotal": subtotal, "discount": 0, "tax": 0, "total": subtotal}
        with db.transaction() as conn:
            found = _lock_order(conn, order["id"], hold["id"], None)
            _confirm(conn, found, {"id": None, "customer": None, "payment_intent": None}, amounts)
        return f"{base_url}/book/done?order={order['public_id']}"

    from ..filters import class_date
    expires_at = int((datetime.now(timezone.utc) + timedelta(minutes=stripe_minutes)).timestamp())
    try:
        cs = stripe_api.create_checkout_session(
            order_public_id=str(order["public_id"]), hold_public_id=str(hold["public_id"]),
            product_name=f"{offer['name']}", description=f"{class_date(s['starts_at'], s['time_zone'])} · "
                                                         f"{s['site_name']}, {s['city_name']}, {s['state_code']}",
            unit_amount_cents=offer["price_cents"], quantity=quantity, customer_email=buyer.email,
            expires_at=expires_at,
            success_url=f"{base_url}/book/done?order={order['public_id']}",
            cancel_url=f"{base_url}/book/cancelled?order={order['public_id']}")
    except IntegrationError as exc:
        with db.transaction() as conn:
            _release(conn, order["id"], hold["id"], "checkout_error", "cancelled")
            runs.issue(conn, source="stripe", code="CHECKOUT_NOT_OPENED", severity="error",
                       external_ref=str(order["public_id"]), entity_type="order", entity_id=order["id"],
                       message=f"A customer couldn't reach the payment page: {exc}")
        raise BookingError("We couldn't open the payment page. Please try again in a minute.") from exc

    with db.transaction() as conn:
        conn.execute(text("""
            INSERT INTO checkout_sessions (order_id, seat_hold_id, stripe_checkout_session_id, url, stripe_expires_at)
            VALUES (:o, :h, :cs, :url, to_timestamp(:exp))
        """), {"o": order["id"], "h": hold["id"], "cs": cs["id"], "url": cs.get("url"), "exp": expires_at})
    return cs["url"]


def _release(conn: Connection, order_id: int, hold_id: int, reason: str, order_status: str = "expired") -> None:
    conn.execute(text("""
        UPDATE seat_holds SET status = CASE WHEN :reason IN ('hold_lapsed', 'checkout_expired') THEN 'expired'
                                            ELSE 'released' END,
               released_at = now(), release_reason = :reason
         WHERE id = :h AND status = 'active'
    """), {"h": hold_id, "reason": reason})
    conn.execute(text("UPDATE registrations SET status = 'expired' WHERE order_id = :o AND status = 'pending'"),
                 {"o": order_id})
    conn.execute(text("""
        UPDATE orders SET status = :st,
               expired_at = CASE WHEN :st = 'expired' THEN now() END,
               cancelled_at = CASE WHEN :st = 'cancelled' THEN now() END
         WHERE id = :o AND status = 'open'
    """), {"o": order_id, "st": order_status})


def _expire_stripe_pages(conn: Connection, order_id: int) -> None:
    for cs_id in conn.execute(text("""
        UPDATE checkout_sessions SET expired_by_engine_at = now()
         WHERE order_id = :o AND status = 'open' AND expired_by_engine_at IS NULL
        RETURNING stripe_checkout_session_id
    """), {"o": order_id}).scalars():
        enqueue(conn, "stripe", "expire_checkout", key=f"expire-{cs_id}", entity_type="order", entity_id=order_id,
                payload={"checkout_session_id": cs_id})


def cancel_by_customer(order_public_id: str) -> None:
    """The customer came back from Stripe without paying: free the seats now."""
    if not _is_uuid(order_public_id):
        return
    with db.transaction() as conn:
        row = conn.execute(text("""
            SELECT o.id, h.id AS hold_id FROM orders o JOIN seat_holds h ON h.order_id = o.id
             WHERE o.public_id = CAST(:p AS uuid) AND o.status = 'open' AND h.status = 'active'
             FOR NO KEY UPDATE OF o
        """), {"p": order_public_id}).mappings().first()
        if row:
            _release(conn, row["id"], row["hold_id"], "customer_left_checkout", "cancelled")
            _expire_stripe_pages(conn, row["id"])


def expire_holds() -> int:
    """Release checkout holds past their time and expire their Stripe pages.

    Locks are taken order -> hold -> registrations, the same order as the
    webhook (_find), so a payment arriving as its hold lapses waits instead of
    deadlocking. An order the webhook is working on is skipped this pass."""
    with db.engine().connect() as c:
        due = c.execute(text("""
            SELECT h.id, h.order_id FROM seat_holds h
             WHERE h.kind IN ('checkout', 'phone_link') AND h.status = 'active' AND h.expires_at <= now()
             ORDER BY h.expires_at LIMIT 200
        """)).mappings().all()
    released = 0
    for r in due:
        with db.transaction() as conn:
            if not conn.execute(text("SELECT 1 FROM orders WHERE id = :o FOR NO KEY UPDATE SKIP LOCKED"),
                                {"o": r["order_id"]}).first():
                continue
            still = conn.execute(text("""
                SELECT 1 FROM seat_holds WHERE id = :h AND status = 'active' AND expires_at <= now()
                   FOR NO KEY UPDATE
            """), {"h": r["id"]}).first()
            if still:
                _release(conn, r["order_id"], r["id"], "hold_lapsed", "expired")
                _expire_stripe_pages(conn, r["order_id"])
                released += 1
    return released


def order_summary(order_public_id: str) -> dict | None:
    if not _is_uuid(order_public_id):
        return None
    order = db.fetch_one("""
        SELECT o.id, o.public_id, o.status, o.buyer_first_name, o.buyer_email, o.total_cents, o.discount_cents,
               (SELECT count(*) FROM registrations r WHERE r.order_id = o.id
                   AND r.status IN ('confirmed', 'completed', 'no_show', 'awaiting_payment')) AS seats_confirmed,
               (NOT o.livemode AND o.status = 'paid' AND o.total_cents > 0
                AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.id)
                AND NOT EXISTS (SELECT 1 FROM checkout_sessions c WHERE c.order_id = o.id)) AS test_booking,
               s.starts_at, s.ends_at, s.time_zone, s.public_id AS session_public_id, cf.name AS course_name,
               si.name AS site_name, si.address_line1, si.city AS site_city, si.state_code AS site_state,
               si.postal_code
          FROM orders o
          JOIN seat_holds h ON h.order_id = o.id
          JOIN sessions s ON s.id = h.session_id
          JOIN course_families cf ON cf.id = s.course_family_id
          JOIN sites si ON si.id = s.site_id
         WHERE o.public_id = CAST(:p AS uuid)
         ORDER BY h.id DESC LIMIT 1
    """, p=order_public_id)
    if order:
        order["attendees"] = db.fetch_all("""
            SELECT r.attendee_first_name, r.attendee_last_name, r.status, p.short_name
              FROM registrations r JOIN products p ON p.id = r.product_id
             WHERE r.order_id = :o ORDER BY r.id
        """, o=order["id"])
    return order


# --- Stripe webhook events -----------------------------------------------------------

def handle_event(event: dict) -> str:
    """Apply one verified Stripe event. Returns 'processed' or 'ignored'."""
    kind = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if obj.get("payment_status") not in ("paid", "no_payment_required"):
            return "ignored"          # a delayed payment method; its success arrives later
        return _paid(obj)
    if kind == "checkout.session.expired":
        return _stripe_expired(obj)
    return "ignored"


def _find(conn: Connection, cs_id: str) -> dict | None:
    """The checkout's order, hold and class, locked in a fixed order: order, hold, checkout."""
    ids = conn.execute(text("""SELECT id, order_id, seat_hold_id FROM checkout_sessions
                                WHERE stripe_checkout_session_id = :cs"""), {"cs": cs_id}).mappings().first()
    if ids is None:
        return None
    return _lock_order(conn, ids["order_id"], ids["seat_hold_id"], ids["id"])


def _lock_order(conn: Connection, order_id: int, hold_id: int, checkout_id: int | None) -> dict:
    order = conn.execute(text("""
        SELECT status AS order_status, livemode, public_id AS order_public_id, buyer_first_name, buyer_last_name,
               buyer_email, buyer_phone_e164
          FROM orders WHERE id = :o FOR NO KEY UPDATE
    """), {"o": order_id}).mappings().one()
    hold = conn.execute(text("SELECT session_id FROM seat_holds WHERE id = :h FOR NO KEY UPDATE"),
                        {"h": hold_id}).mappings().one()
    if checkout_id is not None:
        conn.execute(text("SELECT 1 FROM checkout_sessions WHERE id = :c FOR NO KEY UPDATE"), {"c": checkout_id})
    return {"checkout_id": checkout_id, "order_id": order_id, "seat_hold_id": hold_id,
            **order, "session_id": hold["session_id"]}


class NoSeat(Exception):
    """The payment can't become a seat: the money goes back."""


def _no_seat_reason(exc: Exception) -> str | None:
    """Why a confirmation failed if it means 'no seat for this payment', else None (try again later)."""
    if isinstance(exc, NoSeat):
        return str(exc)
    orig = getattr(exc, "orig", None)
    state, msg = getattr(orig, "sqlstate", None), str(orig)
    if state == "23514" and "is full" in msg:
        return "the class filled up after the seat hold ran out"
    if state == "23505" and "registrations_one_seat_per_person" in msg:
        return "this person already has a seat in this class"
    return None


def _person(conn: Connection, first: str, last: str, email: str, phone: str | None) -> int:
    pid = conn.execute(text("""
        SELECT id FROM people WHERE email = :e AND merged_into_person_id IS NULL ORDER BY id LIMIT 1
    """), {"e": email}).scalar()
    if pid:
        if phone:
            conn.execute(text("UPDATE people SET phone_e164 = COALESCE(phone_e164, :p) WHERE id = :id"),
                         {"p": phone, "id": pid})
        return pid
    return conn.execute(text("""
        INSERT INTO people (first_name, last_name, email, phone_e164) VALUES (:f, :l, :e, :p) RETURNING id
    """), {"f": first, "l": last, "e": email, "p": phone}).scalar_one()


def _amounts(cs: dict) -> dict:
    td = cs.get("total_details") or {}
    total = int(cs.get("amount_total") or 0)
    discount = int(td.get("amount_discount") or 0)
    tax = int(td.get("amount_tax") or 0)
    subtotal = int(cs.get("amount_subtotal") if cs.get("amount_subtotal") is not None else total + discount - tax)
    return {"subtotal": subtotal, "discount": discount, "tax": tax, "total": subtotal - discount + tax}


def _paid(cs: dict) -> str:
    cs_id = cs.get("id", "")
    amounts = _amounts(cs)
    with db.transaction() as conn:
        found = _find(conn, cs_id)
        if not found:
            runs.issue(conn, source="stripe", code="UNKNOWN_CHECKOUT", severity="warning", external_ref=cs_id,
                       message="Stripe reported a payment for a checkout this engine didn't open "
                               "(another app on the same Stripe account, or a different database).")
            return "ignored"
        if found["order_status"] in ("paid", "partially_refunded", "refunded"):
            return "processed"        # a repeat of an event already applied
        if bool(cs.get("livemode")) != found["livemode"]:
            runs.issue(conn, source="stripe", code="LIVEMODE_MISMATCH", severity="error", external_ref=cs_id,
                       message="A Stripe event's test/live mode doesn't match its order. It was not applied.")
            return "ignored"
    paid_states = ("paid", "partially_refunded", "refunded")
    try:
        with db.transaction() as conn:
            found = _find(conn, cs_id)
            if found["order_status"] not in paid_states:     # another delivery may have won the race
                _confirm(conn, found, cs, amounts)
        return "processed"
    except (DBAPIError, NoSeat) as exc:
        reason = _no_seat_reason(exc)
        if reason is None:
            raise           # a passing database problem: the webhook answers 500 and Stripe sends it again
        # The seat was given away after the hold lapsed, the person already has
        # a seat, or the class has started: the money goes back.
        with db.transaction() as conn:
            found = _find(conn, cs_id)
            if found["order_status"] not in paid_states:
                _paid_without_seat(conn, found, cs, amounts, reason)
        return "processed"


def _payment(conn: Connection, found: dict, cs: dict, amounts: dict) -> int | None:
    if amounts["total"] <= 0 or not cs.get("payment_intent"):
        return None          # nothing charged (a 100% code, or a test booking)
    return conn.execute(text("""
        INSERT INTO payments (order_id, method, status, amount_cents, stripe_payment_intent_id, received_at, livemode)
        VALUES (:o, 'card', 'succeeded', :a, :pi, now(), :live)
        ON CONFLICT (stripe_payment_intent_id) DO UPDATE SET status = 'succeeded'
        RETURNING id
    """), {"o": found["order_id"], "a": amounts["total"], "pi": cs.get("payment_intent"),
           "live": found["livemode"]}).scalar_one()


def _confirm(conn: Connection, found: dict, cs: dict, amounts: dict) -> None:
    session = conn.execute(text("SELECT status, starts_at <= now() AS started FROM sessions WHERE id = :s "
                                "FOR NO KEY UPDATE"), {"s": found["session_id"]}).mappings().one()
    if session["started"] or session["status"] != "scheduled":
        raise NoSeat("the class had already started or was cancelled when the payment arrived")
    regs = conn.execute(text("""
        SELECT id, attendee_first_name, attendee_last_name, attendee_email, attendee_phone_e164, unit_price_cents
          FROM registrations WHERE order_id = :o AND status IN ('pending', 'expired') ORDER BY id
    """), {"o": found["order_id"]}).mappings().all()
    n = max(len(regs), 1)
    share, extra = divmod(amounts["discount"], n)
    buyer_id = _person(conn, found["buyer_first_name"], found["buyer_last_name"], found["buyer_email"],
                       found["buyer_phone_e164"])
    for i, r in enumerate(regs):
        disc = min(share + (1 if i < extra else 0), r["unit_price_cents"])
        pid = _person(conn, r["attendee_first_name"], r["attendee_last_name"], r["attendee_email"],
                      r["attendee_phone_e164"])
        conn.execute(text("""
            UPDATE registrations SET status = 'confirmed', confirmed_at = now(), person_id = :p,
                   discount_cents = :d, amount_paid_cents = unit_price_cents - :d
             WHERE id = :id
        """), {"id": r["id"], "p": pid, "d": disc})
        conn.execute(text("UPDATE people SET first_order_id = COALESCE(first_order_id, :o) WHERE id = :p"),
                     {"o": found["order_id"], "p": pid})
        enqueue(conn, "ghl", "sync_registration", key=f"ghl-registration-{r['id']}-booked",
                entity_type="registration", entity_id=r["id"], payload={"registration_id": r["id"]})
    conn.execute(text("""
        UPDATE seat_holds SET status = 'converted', released_at = now(), release_reason = 'paid'
         WHERE id = :h AND status <> 'converted'
    """), {"h": found["seat_hold_id"]})
    conn.execute(text("""
        UPDATE orders SET status = 'paid', paid_at = now(), subtotal_cents = :sub, discount_cents = :disc,
               tax_cents = :tax, total_cents = :tot, amount_paid_cents = :tot, buyer_person_id = :buyer,
               stripe_customer_id = :cust, expired_at = NULL
         WHERE id = :o
    """), {"o": found["order_id"], "sub": amounts["subtotal"], "disc": amounts["discount"], "tax": amounts["tax"],
           "tot": amounts["total"], "buyer": buyer_id, "cust": cs.get("customer")})
    conn.execute(text("UPDATE people SET first_order_id = COALESCE(first_order_id, :o) WHERE id = :p"),
                 {"o": found["order_id"], "p": buyer_id})
    _payment(conn, found, cs, amounts)
    if found["checkout_id"] is not None:
        conn.execute(text("""UPDATE checkout_sessions SET status = 'complete', completed_at = now()
                              WHERE id = :c"""), {"c": found["checkout_id"]})


def _paid_without_seat(conn: Connection, found: dict, cs: dict, amounts: dict, reason: str) -> None:
    _release(conn, found["order_id"], found["seat_hold_id"], "paid_after_seat_gone", "expired")
    conn.execute(text("""
        UPDATE orders SET status = 'paid', paid_at = now(), subtotal_cents = :sub, discount_cents = :disc,
               tax_cents = :tax, total_cents = :tot, amount_paid_cents = :tot
         WHERE id = :o
    """), {"o": found["order_id"], "sub": amounts["subtotal"], "disc": amounts["discount"], "tax": amounts["tax"],
           "tot": amounts["total"]})
    payment_id = _payment(conn, found, cs, amounts)
    conn.execute(text("""UPDATE checkout_sessions SET status = 'complete', completed_at = now()
                          WHERE id = :c"""), {"c": found["checkout_id"]})
    if payment_id:
        refund_id = conn.execute(text("""
            INSERT INTO refunds (order_id, payment_id, amount_cents, reason, source, status, note)
            VALUES (:o, :p, :a, 'late_payment_no_seat', 'engine_auto', 'processing', :note)
            RETURNING id
        """), {"o": found["order_id"], "p": payment_id, "a": amounts["total"], "note": reason[:300]}).scalar_one()
        enqueue(conn, "stripe", "refund", key=f"refund-{refund_id}", entity_type="refund", entity_id=refund_id,
                payload={"refund_id": refund_id})
    runs.issue(conn, source="stripe", code="PAID_AFTER_HOLD_LAPSED", severity="error", external_ref=cs.get("id"),
               entity_type="order", entity_id=found["order_id"],
               message=f"{found['buyer_first_name']} {found['buyer_last_name']} ({found['buyer_email']}) paid, but "
                       f"no seat could be given: {reason}. The payment is being refunded automatically; "
                       "contact them to offer another date.")


def _stripe_expired(cs: dict) -> str:
    with db.transaction() as conn:
        found = _find(conn, cs.get("id", ""))
        if not found:
            return "ignored"
        conn.execute(text("UPDATE checkout_sessions SET status = 'expired' WHERE id = :c AND status = 'open'"),
                     {"c": found["checkout_id"]})
        if found["order_status"] == "open":
            _release(conn, found["order_id"], found["seat_hold_id"], "checkout_expired", "expired")
    return "processed"


@handler("stripe", "refund")
def _refund_job(job: dict) -> None:
    refund_id = job["payload"]["refund_id"]
    with db.engine().connect() as conn:
        r = conn.execute(text("""
            SELECT f.id, f.amount_cents, f.status, f.order_id, p.stripe_payment_intent_id
              FROM refunds f JOIN payments p ON p.id = f.payment_id WHERE f.id = :id
        """), {"id": refund_id}).mappings().first()
    if not r or r["status"] == "succeeded":
        return
    if not r["stripe_payment_intent_id"]:
        raise IntegrationError("Stripe: the payment has no payment id to refund.", retry=False)
    try:
        res = stripe_api.refund(r["stripe_payment_intent_id"], r["amount_cents"], key=f"engine-{refund_id}")
    except IntegrationError as exc:
        if not exc.retry:       # Stripe refused (disputed, already refunded in the dashboard, ...)
            with db.transaction() as conn:
                conn.execute(text("""UPDATE refunds SET status = 'failed', failure_reason = :e, processed_at = now()
                                      WHERE id = :id"""), {"id": refund_id, "e": str(exc)[:500]})
                runs.issue(conn, source="stripe", code="REFUND_FAILED", severity="error",
                           external_ref=f"refund:{refund_id}", entity_type="order", entity_id=r["order_id"],
                           message=f"An automatic refund of {r['amount_cents'] / 100:.2f} USD was refused by Stripe "
                                   f"({exc}). Check the payment in the Stripe Dashboard and refund it by hand.")
        raise
    with db.transaction() as conn:
        done = conn.execute(text("""
            UPDATE refunds SET status = 'succeeded', stripe_refund_id = :sr, processed_at = now()
             WHERE id = :id AND status <> 'succeeded' RETURNING amount_cents
        """), {"id": refund_id, "sr": res.get("id")}).scalar()
        if done:
            conn.execute(text("""
                UPDATE orders SET amount_refunded_cents = amount_refunded_cents + :a,
                       status = CASE WHEN amount_refunded_cents + :a >= amount_paid_cents THEN 'refunded'
                                     ELSE 'partially_refunded' END
                 WHERE id = :o
            """), {"a": done, "o": r["order_id"]})


def record_webhook(event: dict, valid: bool) -> tuple[int | None, str]:
    """Store an inbound event once. Returns (row id, status) or (None, status) for a repeat already done."""
    with db.transaction() as conn:
        row = conn.execute(text("""
            INSERT INTO webhook_events (provider, event_id, event_type, livemode, signature_valid, payload)
            VALUES ('stripe', :id, :t, :live, :valid, CAST(:p AS jsonb))
            ON CONFLICT (provider, event_id) DO UPDATE SET attempts = webhook_events.attempts
            RETURNING id, status
        """), {"id": event.get("id"), "t": event.get("type", "?"), "live": event.get("livemode"), "valid": valid,
               "p": json.dumps(event)}).mappings().one()
    return row["id"], row["status"]


def finish_webhook(row_id: int, status: str, error: str | None = None) -> None:
    with db.transaction() as conn:
        conn.execute(text("""
            UPDATE webhook_events SET status = :s, error = :e, attempts = attempts + 1,
                   processed_at = CASE WHEN :s IN ('processed', 'ignored') THEN now() END
             WHERE id = :id
        """), {"id": row_id, "s": status, "e": error})
