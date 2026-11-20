"""The booking engine's API (/api/v1): how the NBC Ops Hub and other tools read and change bookings.

The booking app itself keeps only user management, the instructor portal and
the student portal. Everything the desk does (find a student, refunds and their
approval, moving and cancelling seats, phone and invoice bookings, attendance,
the waiting list) is here, for the Ops Hub's screens to call. The full list,
with examples, is docs/api.md.

Every call sends two headers:

    Authorization: Bearer nbc_<prefix>_<secret>     the tool's key (Superadmin → API keys)
    X-Acting-User: jane@nationsbestcpr.com          the staff member the call is for

The call runs as that staff member (their roles and cities), limited to the
key's permissions. The database still refuses what their roles don't allow
(large refunds, extra seats), and every change is in the action log under
their name, marked with the key it came through. Bodies are JSON; money is
integer cents; times are ISO 8601 with their offset.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import wraps
from typing import Any, Callable

from flask import Blueprint, Response, abort, g, jsonify, request
from sqlalchemy import text
from werkzeug.exceptions import HTTPException

from .. import apikeys, attendance, audit, auth, db, desk, instructor_portal, refunds, waitlist
from . import console

bp = Blueprint("api", __name__, url_prefix="/api/v1")

VERSION = "1.0"


# --- Plumbing ------------------------------------------------------------------------

def plain(value: Any) -> Any:
    """JSON-ready: ISO times, strings for ids, numbers for decimals."""
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [plain(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (bytes, memoryview)):
        return None
    return value


def ok(data: Any = None, status: int = 200, **extra) -> Response:
    body = {"data": plain(data)} if data is not None else {}
    body.update(plain(extra))
    res = jsonify(body)
    res.status_code = status
    return res


def refuse(status: int, message: str) -> Response:
    res = jsonify({"error": {"status": status, "message": message}})
    res.status_code = status
    return res


@bp.errorhandler(HTTPException)
def _http_error(exc: HTTPException):
    messages = {401: "Send a valid API key: Authorization: Bearer nbc_…",
                403: "The key or the staff member named in X-Acting-User isn't allowed to do this.",
                404: "Not found, or in a city the staff member doesn't cover.",
                405: "That method isn't allowed here."}
    default = exc.description == type(exc).description
    return refuse(exc.code or 500, messages[exc.code] if default and exc.code in messages else exc.description)


@bp.before_request
def _authenticate():
    if request.endpoint == "api.index":
        return None
    raw = request.headers.get("Authorization", "")
    key = apikeys.verify(raw[7:].strip()) if raw.startswith("Bearer ") else None
    if key is None:
        return refuse(401, "Send a valid API key: Authorization: Bearer nbc_…")
    acting = request.headers.get("X-Acting-User", "")
    user = apikeys.acting_user(key, acting)
    if user is None:
        return refuse(403, "X-Acting-User must be the email of an active staff account in the booking engine "
                           "(a Superadmin adds people under Users).")
    g.current_user = user
    return None


def needs(*permissions: str) -> Callable:
    """The key and the person must allow at least one of these (the exact city is checked inside)."""
    def decorator(view: Callable) -> Callable:
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = auth.current_user()
            if not any(p in user.permissions for p in permissions):
                missing = " or ".join(permissions)
                return refuse(403, f"Needs {missing}: the key must allow it and {user.email} must hold it.")
            return view(*args, **kwargs)
        wrapped.permissions = permissions         # read by the access list (docs/api.md, /api/v1)
        return wrapped
    return decorator


def body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def in_city(permission: str, metro_id: int) -> None:
    if not auth.current_user().can(permission, metro_id):
        abort(403)


def cents(value: Any) -> int | None:
    """Integer cents from a JSON body (an int, or a whole-number string)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= 10_000_000:
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{1,8}", value.strip()):
        return int(value.strip())
    return None


def base_url() -> str:
    return console._base_url()


def seat_ref(registration_id: int) -> str:
    return str(db.fetch_value("SELECT public_id FROM registrations WHERE id = :r", r=registration_id))


def class_or_404(public_id: str) -> dict:
    return console._class_or_404(public_id)


def session_id_from(public_id: Any) -> int | None:
    if not isinstance(public_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", public_id):
        return None
    return db.fetch_value("SELECT id FROM sessions WHERE public_id = CAST(:p AS uuid)", p=public_id)


# --- About this key ------------------------------------------------------------------

@bp.get("")
def index():
    """No key needed: what this API is and where its access list is."""
    return ok({"name": "NBC booking engine API", "version": VERSION, "docs": "docs/api.md",
               "auth": ["Authorization: Bearer nbc_<prefix>_<secret>", "X-Acting-User: <staff email>"]})


@bp.get("/me")
def me():
    """The key and the staff member this call acts for, with what they may do."""
    user = auth.current_user()
    return ok({"key": {"name": user.key.name, "prefix": user.key.prefix,
                       "permissions": sorted(user.key.permissions)},
               "acting_user": {"email": user.email, "name": user.display_name,
                               "roles": [{"code": r["code"], "name": r["name"], "city_id": r["scope_metro_id"]}
                                         for r in user.roles],
                               "permissions": sorted(user.permissions)}})


# --- Classes -------------------------------------------------------------------------

@bp.get("/classes")
@needs("class.view")
def classes():
    """Classes in the staff member's cities with live seats (filters: city, course, status, from, to, full)."""
    a = request.args
    rows = db.fetch_all("""
        SELECT s.public_id, s.starts_at, s.ends_at, s.time_zone, s.local_date, s.status, s.is_published,
               s.delivery_mode, s.location_type, cf.code AS course_code, cf.name AS course_name,
               si.name AS site_name, m.slug AS city, m.name AS city_name,
               av.sold_seats, av.held_seats, av.seat_limit, av.seats_left, av.waitlist_count,
               CASE WHEN li.id IS NOT NULL OR s.airtable_lead_instructor IS NOT NULL THEN
                 json_build_object('id', li.public_id, 'name', coalesce(li.full_name, s.airtable_lead_instructor))
               END AS lead_instructor,
               sid.instructor_assigned, sid.availability_status AS instructor_availability_status,
               sid.performance_tier AS instructor_performance_tier,
               (SELECT json_agg(json_build_object(
                         'id', ai.public_id, 'name', coalesce(ai.full_name, ai.first_name || ' ' || ai.last_name),
                         'role', CASE a.role WHEN 'co' THEN 'helper' ELSE a.role END,
                         'confirmed_at', a.confirmed_at,
                         'cant_teach', (SELECT json_build_object('request_id', r.id, 'reason', r.reason, 'sent_at', r.created_at)
                                          FROM instructor_requests r WHERE r.assignment_id = a.id
                                           AND r.kind = 'cant_teach' AND r.status = 'open' LIMIT 1))
                         ORDER BY a.role DESC)
                  FROM assignments a JOIN instructors ai ON ai.id = a.instructor_id
                 WHERE a.session_id = s.id AND a.status IN ('assigned', 'completed')) AS instructors,
               (SELECT count(*) FROM roster_signatures rs WHERE rs.session_id = s.id) AS signed
          FROM sessions s
          JOIN metros m ON m.id = s.metro_id
          JOIN sites si ON si.id = s.site_id
          JOIN course_families cf ON cf.id = s.course_family_id
          JOIN session_availability av ON av.session_id = s.id
          LEFT JOIN session_instructor_details sid ON sid.session_id = s.id
          LEFT JOIN instructors li ON li.id = sid.lead_instructor_id
         WHERE user_has_permission(:uid, 'class.view', s.metro_id)
           AND (:city = '' OR m.slug = :city)
           AND (:course = '' OR cf.code = :course)
           AND (:status = '' OR s.status = :status)
           AND (CAST(:dfrom AS date) IS NULL OR s.local_date >= CAST(:dfrom AS date))
           AND (CAST(:dto AS date) IS NULL OR s.local_date <= CAST(:dto AS date))
           AND (:full = '' OR (:full = 'yes') = (av.seats_left <= 0))
         ORDER BY s.starts_at, s.id
         LIMIT :lim OFFSET :off
    """, uid=auth.current_user().id, city=a.get("city", ""), course=a.get("course", ""),
        status=a.get("status", ""), dfrom=_day(a.get("from")) or (None if a.get("from") else date.today()),
        dto=_day(a.get("to")), full=a.get("full", "") if a.get("full") in ("yes", "no") else "",
        lim=min(max(a.get("limit", 100, type=int), 1), 500), off=max(a.get("offset", 0, type=int), 0))
    return ok(rows, count=len(rows))


def _day(raw: str | None) -> str | None:
    return raw if raw and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw) else None


@bp.get("/classes/<public_id>")
@needs("class.view", "student.view")
def class_detail(public_id: str):
    """One class: its students with attendance, its waiting list, and what may be done to it."""
    user = auth.current_user()
    s = class_or_404(public_id)
    can = attendance.access(s)
    students = attendance.roster(s["id"])
    if not can["contacts"]:
        for st in students:
            st.pop("attendee_email", None)
            st.pop("attendee_phone_e164", None)
    signed = instructor_portal.signatures(s["id"])
    for st in students:
        sg = signed.get(st["id"])
        st["signed_at"] = sg["signed_at"] if sg else None
        st["signed_name"] = sg["signed_name"] if sg else None
        st.pop("id", None)
    waiting = db.fetch_all("""
        SELECT q.id, q.first_name, q.last_name, q.party_size, q.status, q.position, q.joined_at
          FROM waitlist_queue q WHERE q.session_id = :s ORDER BY q.position
    """, s=s["id"]) if user.can("waitlist.view", s["metro_id"]) else None
    out = {k: v for k, v in s.items() if k not in ("id", "metro_id", "course_family_id")}
    return ok({**out, "students": students, "waiting_list": waiting,
               "can": {"mark_attendance": can["can_mark"], "finish": can["can_finish"], "export": can["can_export"],
                       "move_everyone": s["status"] == "cancelled" and user.can("booking.transfer_bulk",
                                                                                 s["metro_id"])}})


@bp.get("/classes/<public_id>/class-list.csv")
@needs("class.roster_export")
def class_list(public_id: str):
    """The class list for the Red Cross (CSV)."""
    s = class_or_404(public_id)
    try:
        csv = attendance.export_csv(s)
    except attendance.AttendanceError as exc:
        return refuse(403, str(exc))
    return Response(csv, mimetype="text/csv")


@bp.post("/classes/<public_id>/attendance")
@needs("booking.attendance")
def class_attendance(public_id: str):
    """Mark one seat checked in, Complete, No show or cleared."""
    s = class_or_404(public_id)
    b = body()
    try:
        done = attendance.mark(s, str(b.get("seat", "")), str(b.get("mark", "")))
    except attendance.AttendanceError as exc:
        return refuse(400, str(exc))
    return ok(done)


@bp.post("/classes/<public_id>/finish")
@needs("booking.attendance")
def class_finish(public_id: str):
    """Finish the class: grades are final and instructor pay records are made."""
    s = class_or_404(public_id)
    try:
        return ok(attendance.finish(s))
    except attendance.AttendanceError as exc:
        return refuse(400, str(exc))


@bp.post("/classes/<public_id>/move-all")
@needs("booking.transfer_bulk")
def class_move_all(public_id: str):
    """Move every student of a cancelled class to another class."""
    s = class_or_404(public_id)
    in_city("booking.transfer_bulk", s["metro_id"])
    b = body()
    to = session_id_from(b.get("to_class"))
    if not to:
        return refuse(400, "Send to_class: the id of the class to move everyone to.")
    try:
        return ok(desk.move_class(s["id"], to_session_id=to, note=str(b.get("note", ""))[:500]))
    except desk.DeskError as exc:
        return refuse(400, str(exc))


@bp.post("/classes/<public_id>/bookings")
@needs("booking.create_phone", "booking.create_invoice")
def class_book(public_id: str):
    """Book by phone (a payment link by text and email) or on an invoice. Card details never come here."""
    from ..integrations import checkout
    user = auth.current_user()
    s = checkout.bookable_session(public_id)
    if not s:
        abort(404)
    b = body()
    how = b.get("how")
    if how not in ("phone", "invoice"):
        return refuse(400, "Send how: \"phone\" (payment link) or \"invoice\".")
    in_city(f"booking.create_{how}", s["metro_id"])
    buyer_in = b.get("buyer") if isinstance(b.get("buyer"), dict) else {}
    phone = checkout.normalise_phone(buyer_in.get("phone"))
    buyer = checkout.Person(str(buyer_in.get("first_name", "")).strip(), str(buyer_in.get("last_name", "")).strip(),
                            str(buyer_in.get("email", "")).strip().lower(), phone or None)
    if not (buyer.first_name and buyer.last_name and checkout.EMAIL.match(buyer.email)):
        return refuse(400, "Send buyer.first_name, buyer.last_name and buyer.email.")
    if phone == "" or (how == "phone" and not phone):
        return refuse(400, "Send a valid mobile number in buyer.phone: the payment link goes by text and email."
                      if how == "phone" else "buyer.phone isn't a valid phone number.")
    attendees = []
    raw = b.get("attendees") if isinstance(b.get("attendees"), list) else []
    if not raw or len(raw) > 50:
        return refuse(400, "Send attendees: 1 to 50 people, each {first_name, last_name, email}, "
                           "or {\"buyer\": true} for the buyer.")
    for i, a in enumerate(raw):
        if isinstance(a, dict) and a.get("buyer") is True:
            attendees.append(checkout.Person(buyer.first_name, buyer.last_name, buyer.email, phone or None))
            continue
        a = a if isinstance(a, dict) else {}
        first, last = str(a.get("first_name", "")).strip(), str(a.get("last_name", "")).strip()
        email = str(a.get("email", "")).strip().lower()
        if not (first and last and checkout.EMAIL.match(email)):
            return refuse(400, f"Attendee {i + 1}: send first_name, last_name and their own email.")
        attendees.append(checkout.Person(first, last, email))
    product_id = b.get("product_id") if isinstance(b.get("product_id"), int) else 0
    try:
        if how == "phone":
            made = checkout.start_phone(staff_user_id=user.id, session_public_id=public_id, product_id=product_id,
                                        buyer=buyer, attendees=attendees, base_url=base_url())
        else:
            try:
                due_on = date.fromisoformat(str(b.get("due_on", "")))
            except ValueError:
                return refuse(400, "Send due_on: the date the invoice is due (YYYY-MM-DD).")
            if due_on < date.today():
                return refuse(400, "due_on can't be in the past.")
            company = str(b.get("company", "")).strip()
            if not company:
                return refuse(400, "Send company: who the invoice is for.")
            made = checkout.book_on_invoice(session_public_id=public_id, product_id=product_id, buyer=buyer,
                                            attendees=attendees, company=company[:200],
                                            po_number=str(b.get("po_number", "")).strip()[:60] or None,
                                            due_on=due_on, staff_user_id=user.id)
    except checkout.BookingError as exc:
        return refuse(400, str(exc))
    order_id = made["order_id"]
    with db.transaction() as conn:
        audit.record(conn, f"booking.create_{how}", "order", order_id, after={"seats": len(attendees)})
        if how == "phone" and not made["testing"]:
            from ..integrations.outbox import enqueue
            total = conn.execute(text("SELECT total_cents FROM orders WHERE id = :o"), {"o": order_id}).scalar()
            enqueue(conn, "ghl", "send_payment_link", key=f"ghl-payment-link-{order_id}-first", entity_type="order",
                    entity_id=order_id, payload={"order_id": order_id, "url": made["url"], "amount_cents": total})
    order = db.fetch_one("SELECT public_id, status, total_cents FROM orders WHERE id = :o", o=order_id)
    seats = db.fetch_all("SELECT public_id, status, attendee_email FROM registrations WHERE order_id = :o "
                         "ORDER BY id", o=order_id)
    return ok({"order": order, "seats": seats, "payment_url": made.get("url") if how == "phone" else None,
               "testing": bool(made.get("testing"))}, 201)


# --- Students and bookings -------------------------------------------------------------

@bp.get("/students")
@needs("student.view")
def students():
    """Find a student: the classes (soonest first) with the seats matching the filters, 8 classes a page."""
    f = console._find_filters(request.args)
    found = console.roster(auth.current_user().id, f)
    show_money = auth.current_user().has_permission("payment.view")
    for c in found["classes"]:
        c.pop("id", None)
        for st in c["students"]:
            st.pop("session_id", None)
            if not show_money:
                st.pop("amount_paid_cents", None)
                st.pop("amount_refunded_cents", None)
    return ok(found["classes"], page=found["page"], pages=found["pages"], classes=found["total"],
              students=found["students"])


@bp.get("/bookings/<public_id>")
@needs("student.view")
def booking(public_id: str):
    """One seat with everything around it: order, payments, refunds, moves, notes, history and what may be done."""
    r = console._booking_or_404(public_id)
    p = console.picture(r)
    seat = {k: r[k] for k in (
        "public_id", "status", "attendee_first_name", "attendee_last_name", "attendee_email", "attendee_phone_e164",
        "order_public_id", "channel", "order_status", "buyer_first_name", "buyer_last_name", "buyer_email",
        "buyer_phone_e164", "session_public_id", "starts_at", "ends_at", "time_zone", "class_status",
        "course_name", "course_code", "site_name", "city_name", "product_name", "seats_left", "transfers_used")}
    if p["show_money"]:
        seat.update({k: r[k] for k in ("amount_paid_cents", "amount_refunded_cents", "total_cents")})
    pay = p["pay"]
    return ok({"seat": seat, "seats_on_order": p["seats"], "payments": p["payments"], "refunds": p["refunds"],
               "transfers": p["transfers"], "notes": p["notes"], "history": p["history"],
               "open_payment_link": pay["link"], "invoice": pay["invoice"],
               "price_differences": [{k: v for k, v in d.items() if k != "id"} | {"order": d["id"]}
                                     for d in pay["differences"]],
               "can": {a["key"]: a["allowed"] for a in p["actions"]} | {"note": p["can_note"],
                                                                       "record_payment": pay["can_record"],
                                                                       "send_payment_link": pay["can_send"]}})


@bp.post("/bookings/<public_id>/notes")
@needs("student.note")
def booking_note(public_id: str):
    """Add a note to a booking."""
    user = auth.current_user()
    r = console._booking_or_404(public_id)
    in_city("student.note", r["metro_id"])
    note = str(body().get("body", "")).strip()[:4000]
    if not note:
        return refuse(400, "Send body: the note.")
    with db.transaction() as conn:
        note_id = conn.execute(text("""
            INSERT INTO notes (registration_id, order_id, author_user_id, body) VALUES (:r, :o, :u, :b) RETURNING id
        """), {"r": r["id"], "o": r["order_id"], "u": user.id, "b": note}).scalar_one()
        audit.record(conn, "booking.note_add", "registration", r["id"], after={"note_id": note_id})
    return ok({"note": note_id}, 201)


@bp.get("/bookings/<public_id>/move-options")
@needs("booking.transfer")
def booking_move_options(public_id: str):
    """Classes this seat can move to, with prices and seats left."""
    user = auth.current_user()
    r = console._booking_or_404(public_id)
    in_city("booking.transfer", r["metro_id"])
    options = desk.targets(r, user.id, q=(request.args.get("q") or "").strip()[:80])
    for o in options:
        o["class"] = o.pop("public_id")
        o.pop("id", None)
    return ok(options, hint=desk.suggestions(r), reasons=desk.MOVE_REASONS,
              can_add_extra_seat=user.can("class.capacity_override", r["metro_id"]))


@bp.post("/bookings/<public_id>/move")
@needs("booking.transfer")
def booking_move(public_id: str):
    """Move a seat to another class, charging or crediting the difference."""
    r = console._booking_or_404(public_id)
    in_city("booking.transfer", r["metro_id"])
    b = body()
    to = session_id_from(b.get("to_class"))
    product = b.get("product_id")
    fee = cents(b.get("fee_cents", 0))
    if not to or not isinstance(product, int):
        return refuse(400, "Send to_class (a class id from move-options) and product_id.")
    if fee is None:
        return refuse(400, "fee_cents must be a whole number of cents, like 2500 or 0.")
    try:
        done = desk.move(r["id"], to_session_id=to, to_product_id=product, reason=str(b.get("reason", "")),
                         fee_cents=fee, note=str(b.get("note", ""))[:500],
                         extra_seat_reason=str(b.get("extra_seat_reason", ""))[:500], base_url=base_url())
    except desk.DeskError as exc:
        return refuse(400, str(exc))
    return ok({k: v for k, v in done.items() if not isinstance(v, (bytes, memoryview))})


@bp.post("/bookings/<public_id>/cancel")
@needs("booking.cancel")
def booking_cancel(public_id: str):
    """Cancel a seat (it goes back on sale, waiting list first), with an optional refund."""
    r = console._booking_or_404(public_id)
    in_city("booking.cancel", r["metro_id"])
    b = body()
    refund = cents(b.get("refund_cents", 0))
    if refund is None:
        return refuse(400, "refund_cents must be a whole number of cents, like 10800 or 0.")
    try:
        done = desk.cancel(r["id"], reason=str(b.get("reason", "")), refund_cents=refund,
                           note=str(b.get("note", ""))[:500])
    except desk.DeskError as exc:
        return refuse(400, str(exc))
    except refunds.RefundError as exc:
        return ok({"cancelled": True, "refund": None}, refund_error=str(exc))
    return ok({"cancelled": True, "refund": _refund_out(done["refund"]["id"]) if done.get("refund") else None})


@bp.get("/bookings/<public_id>/refund-preview")
@needs("refund.issue")
def booking_refund_preview(public_id: str):
    """What can still be refunded on a seat, and the manager threshold."""
    r = console._booking_or_404(public_id)
    in_city("refund.issue", r["metro_id"])
    return ok(refunds.preview(r["id"]), reasons=refunds.REASONS, manager_threshold_cents=refunds.threshold_cents())


@bp.post("/bookings/<public_id>/refunds")
@needs("refund.issue")
def booking_refund(public_id: str):
    """Refund a seat. Over the manager threshold it waits for a manager (POST /refunds/<id>/approve)."""
    r = console._booking_or_404(public_id)
    in_city("refund.issue", r["metro_id"])
    b = body()
    amount = cents(b.get("amount_cents"))
    if not amount:
        return refuse(400, "Send amount_cents: the refund in cents, like 10800.")
    try:
        done = refunds.request(r["id"], amount, str(b.get("reason", "")), str(b.get("note", ""))[:500])
    except refunds.RefundError as exc:
        return refuse(400, str(exc))
    return ok(_refund_out(done["id"]), 201)


def _refund_out(refund_id: int) -> dict:
    return db.fetch_one("""
        SELECT f.id, f.status, f.amount_cents, f.reason, f.requires_manager_approval, f.failure_reason,
               f.stripe_refund_id, f.created_at, f.processed_at, r.public_id AS seat
          FROM refunds f LEFT JOIN registrations r ON r.id = f.registration_id WHERE f.id = :id
    """, id=refund_id)


@bp.post("/bookings/<public_id>/payments")
@needs("booking.create_invoice")
def booking_payment(public_id: str):
    """An invoice or PO paid outside Stripe (a cheque, a bank transfer)."""
    from ..integrations import checkout
    r = console._booking_or_404(public_id)
    in_city("booking.create_invoice", r["metro_id"])
    b = body()
    amount = cents(b.get("amount_cents"))
    reference = str(b.get("reference", "")).strip()[:120]
    if not amount or not reference:
        return refuse(400, "Send amount_cents and reference (the cheque number or bank transfer id).")
    try:
        checkout.record_payment(r["order_id"], amount, reference, auth.current_user().id)
    except checkout.BookingError as exc:
        return refuse(400, str(exc))
    return ok({"order_status": db.fetch_value("SELECT status FROM orders WHERE id = :o", o=r["order_id"])}, 201)


@bp.post("/bookings/<public_id>/payment-link")
@needs("booking.create_phone", "booking.transfer")
def booking_payment_link(public_id: str):
    """Send the open payment link again (GHL texts and emails it)."""
    from ..integrations.outbox import enqueue
    r = console._booking_or_404(public_id)
    pay = console._payment_state(r)
    if not pay["can_send"]:
        abort(403)
    want = body().get("order")
    target = None
    if pay["link"] and want in (None, r["order_id"]):
        target = (r["order_id"], pay["link"]["url"], r["total_cents"])
    for d in pay["differences"]:
        if d["status"] == "awaiting_payment" and want in (None, d["id"]) and target is None:
            url = d["url"] or desk.send_payment_link(d["id"], base_url())
            target = (d["id"], url, d["total_cents"]) if url else None
    if not target:
        return refuse(400, "There's no open payment link on this booking.")
    with db.transaction() as conn:
        enqueue(conn, "ghl", "send_payment_link", key=f"ghl-payment-link-{target[0]}-{uuid.uuid4().hex[:8]}",
                entity_type="order", entity_id=target[0],
                payload={"order_id": target[0], "url": target[1], "amount_cents": target[2]})
        audit.record(conn, "booking.payment_link_send", "order", target[0])
    return ok({"sent": True, "url": target[1], "amount_cents": target[2]})


# --- Refunds -------------------------------------------------------------------------

@bp.get("/refunds")
@needs("refund.issue", "refund.approve_large")
def refund_list():
    """Refunds in the staff member's cities, by status (waiting, sending, done, problems, all)."""
    tab = request.args.get("status", "waiting")
    if tab not in {t[0] for t in console.REFUND_TABS}:
        return refuse(400, "status is one of: " + ", ".join(t[0] for t in console.REFUND_TABS))
    rows, tabs = console.refund_rows(tab, request.args.get("q", "").strip()[:120])
    for row in rows:
        row["seat"] = row.pop("public_id")
        row.pop("metro_id", None)
    return ok(rows, counts={t["key"]: t["count"] for t in tabs})


@bp.post("/refunds/<int:refund_id>/approve")
@needs("refund.approve_large")
def refund_approve(refund_id: int):
    """A manager approves a large refund; it goes to Stripe."""
    try:
        refunds.approve(refund_id)
    except refunds.RefundError as exc:
        return refuse(400, str(exc))
    return ok(_refund_out(refund_id))


@bp.post("/refunds/<int:refund_id>/turn-down")
@needs("refund.approve_large")
def refund_turn_down(refund_id: int):
    """A manager turns a large refund down, with a reason."""
    try:
        refunds.turn_down(refund_id, str(body().get("why", "")))
    except refunds.RefundError as exc:
        return refuse(400, str(exc))
    return ok(_refund_out(refund_id))


# --- Waiting lists -------------------------------------------------------------------

@bp.get("/waitlists")
@needs("waitlist.view")
def waitlists():
    """Waiting lists of upcoming classes (filters: city, status, class)."""
    user = auth.current_user()
    a = request.args
    rows = db.fetch_all("""
        SELECT w.id, w.first_name, w.last_name, w.email, w.phone_e164, w.party_size, w.status, w.joined_at,
               q.position, s.public_id AS class, s.starts_at, s.time_zone, cf.name AS course_name,
               si.name AS site_name, m.slug AS city, av.seats_left,
               user_has_permission(:uid, 'student.view', s.metro_id) AS contacts
          FROM waitlist_entries w
          LEFT JOIN waitlist_queue q ON q.id = w.id
          JOIN sessions s ON s.id = w.session_id
          JOIN sites si ON si.id = s.site_id
          JOIN metros m ON m.id = s.metro_id
          JOIN course_families cf ON cf.id = s.course_family_id
          JOIN session_availability av ON av.session_id = s.id
         WHERE user_has_permission(:uid, 'waitlist.view', s.metro_id)
           AND s.starts_at > now()
           AND (:city = '' OR m.slug = :city)
           AND (:status = '' OR w.status = :status)
           AND (:cls = '' OR CAST(s.public_id AS text) = :cls)
         ORDER BY s.starts_at, q.position NULLS LAST, w.joined_at
         LIMIT 500
    """, uid=user.id, city=a.get("city", ""), status=a.get("status", ""), cls=a.get("class", ""))
    for row in rows:
        if not (row.pop("contacts") and "student.view" in user.permissions):
            row.pop("email", None)
            row.pop("phone_e164", None)
    return ok(rows, count=len(rows))


@bp.post("/waitlist/<int:entry_id>/remove")
@needs("waitlist.manage")
def waitlist_remove(entry_id: int):
    """Take someone off a waiting list."""
    try:
        waitlist.remove(entry_id, str(body().get("why", "")))
    except waitlist.WaitlistError as exc:
        return refuse(400, str(exc))
    return ok({"removed": True})


@bp.post("/waitlist/<int:entry_id>/reoffer")
@needs("waitlist.manage")
def waitlist_reoffer(entry_id: int):
    """Offer a seat again after an offer expired."""
    try:
        waitlist.reoffer(entry_id)
    except waitlist.WaitlistError as exc:
        return refuse(400, str(exc))
    return ok({"offered": True})


# --- Reference lists -----------------------------------------------------------------

# Instructor data is Airtable's (synced read only, migration 0011); these calls are how the Ops Hub reads it.
_INSTRUCTOR_SCOPE = """(user_has_permission(:uid, 'instructor.view', i.home_metro_id)
                        OR user_has_permission(:uid, 'instructor.view', NULL))"""
_INSTRUCTOR_LIST = """
    SELECT i.public_id AS id, i.full_name AS name, i.first_name, i.last_name, i.email, i.phone_e164 AS phone,
           i.status, i.city, i.state, i.postal_code, hm.slug AS home_city, i.page_type, i.is_hub_instructor,
           i.active_in_enrollware, i.available_days, i.available_times, i.classes_per_month,
           i.travel_radius_miles, i.notice_needed, cs.cert_status, cs.next_expiry AS next_cert_expiry,
           coalesce((SELECT array_agg(im.metro_label ORDER BY im.metro_label) FROM instructor_metros im
                      WHERE im.instructor_id = i.id), '{}') AS metros,
           (SELECT count(*) FROM session_instructor_details d JOIN sessions s ON s.id = d.session_id
             WHERE d.lead_instructor_id = i.id AND s.status = 'scheduled' AND s.starts_at > now()) AS upcoming_classes_led,
           i.source_deleted_at IS NOT NULL AS removed_from_airtable
      FROM instructors i
      LEFT JOIN metros hm ON hm.id = i.home_metro_id
      LEFT JOIN instructor_cert_status cs ON cs.instructor_id = i.id
"""


@bp.get("/instructors")
@needs("instructor.view")
def instructors():
    """Instructors from Airtable: contact, city areas, certification status and availability (filters: q, status,
    metro, page_type, cert_status)."""
    a = request.args
    rows = db.fetch_all(_INSTRUCTOR_LIST + f"""
         WHERE {_INSTRUCTOR_SCOPE}
           AND (:q = '' OR i.full_name ILIKE '%' || :q || '%' OR i.email ILIKE '%' || :q || '%')
           AND (:status = '' OR i.status = :status)
           AND (:page_type = '' OR i.page_type = :page_type)
           AND (:cert = '' OR cs.cert_status = :cert)
           AND (:metro = '' OR EXISTS (SELECT 1 FROM instructor_metros im LEFT JOIN metros mm ON mm.id = im.metro_id
                                        WHERE im.instructor_id = i.id AND (mm.slug = :metro OR im.metro_label = :metro)))
           AND (i.source_deleted_at IS NULL OR :removed = 'yes')
         ORDER BY i.last_name, i.first_name
         LIMIT 1000
    """, uid=auth.current_user().id, q=a.get("q", "").strip()[:80], status=a.get("status", ""),
        page_type=a.get("page_type", ""), cert=a.get("cert_status", ""), metro=a.get("metro", ""),
        removed=a.get("removed", ""))
    return ok(rows, count=len(rows))


@bp.get("/instructors/<public_id>")
@needs("instructor.view")
def instructor(public_id: str):
    """One instructor, with everything Airtable holds: availability, outreach and removal, certifications, city
    areas, HUB team, pre-approved and taught-at locations, and the classes they lead."""
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", public_id or ""):
        abort(404)
    uid = auth.current_user().id
    row = db.fetch_one(f"""
        SELECT i.*, hm.slug AS home_city, cs.cert_status, cs.next_expiry AS next_cert_expiry
          FROM instructors i LEFT JOIN metros hm ON hm.id = i.home_metro_id
          LEFT JOIN instructor_cert_status cs ON cs.instructor_id = i.id
         WHERE i.public_id = CAST(:p AS uuid) AND {_INSTRUCTOR_SCOPE}
    """, p=public_id, uid=uid)
    if not row:
        abort(404)
    iid = row["id"]
    out = {k: v for k, v in row.items() if k not in ("id", "user_account_id", "airtable_record_id", "qbo_vendor_id",
                                                    "home_metro_id", "hub_metro_id", "reliability_score")}
    out["id"] = out.pop("public_id")
    out["phone"] = out.pop("phone_e164")
    out["certifications"] = db.fetch_all("""SELECT cert_type, expires_on, issued_on, cert_number FROM instructor_certs
                                             WHERE instructor_id = :i ORDER BY expires_on""", i=iid)
    out["metros"] = db.fetch_all("""SELECT im.metro_label AS label, m.slug AS city FROM instructor_metros im
                                     LEFT JOIN metros m ON m.id = im.metro_id WHERE im.instructor_id = :i
                                     ORDER BY im.metro_label""", i=iid)
    out["hub_team"] = db.fetch_all("""SELECT o.public_id AS id, o.full_name AS name, 'member' AS role
                                        FROM instructor_hub_members h JOIN instructors o ON o.id = h.member_instructor_id
                                       WHERE h.lead_instructor_id = :i
                                      UNION ALL
                                      SELECT o.public_id, o.full_name, 'lead'
                                        FROM instructor_hub_members h JOIN instructors o ON o.id = h.lead_instructor_id
                                       WHERE h.member_instructor_id = :i ORDER BY 3, 2""", i=iid)
    out["locations"] = db.fetch_all("""SELECT l.relation, l.location_record_id, coalesce(si.name, lis.location_name) AS name,
                                              m.slug AS city
                                         FROM instructor_locations l
                                         LEFT JOIN sites si ON si.id = l.site_id LEFT JOIN metros m ON m.id = si.metro_id
                                         LEFT JOIN location_instructor_settings lis
                                                ON lis.location_record_id = l.location_record_id
                                        WHERE l.instructor_id = :i ORDER BY l.relation, 3""", i=iid)
    out["upcoming_classes_led"] = db.fetch_all("""
        SELECT s.public_id AS class, s.starts_at, s.time_zone, cf.name AS course_name, si.name AS site_name
          FROM session_instructor_details d JOIN sessions s ON s.id = d.session_id
          JOIN sites si ON si.id = s.site_id JOIN course_families cf ON cf.id = s.course_family_id
         WHERE d.lead_instructor_id = :i AND s.status = 'scheduled' AND s.starts_at > now()
         ORDER BY s.starts_at LIMIT 100""", i=iid)
    return ok(out)


@bp.get("/recruiting-needs")
@needs("instructor.view", "teaching.coverage_view")
def recruiting_needs():
    """Instructor Recruiting Needs: locations that need an instructor, why, and the cancelled classes behind it."""
    rows = db.fetch_all("""
        SELECT n.airtable_record_id AS id, n.need, n.need_reasons, n.recruiting_status, n.recruiting_notes,
               n.enrollware_no_instructor_cancels, n.cls_lost_cents, n.profit_while_running_cents,
               n.students_per_class, n.last_month_held, n.airtable_created_at AS added_at,
               coalesce((SELECT json_agg(json_build_object('location_record_id', l, 'name',
                                  coalesce(si.name, lis.location_name), 'city', m.slug))
                           FROM unnest(n.location_record_ids) AS l
                           LEFT JOIN location_instructor_settings lis ON lis.location_record_id = l
                           LEFT JOIN sites si ON si.airtable_record_id = l LEFT JOIN metros m ON m.id = si.metro_id),
                        '[]') AS locations,
               (SELECT count(*) FROM recruiting_need_classes c WHERE c.recruiting_need_id = n.id) AS cancelled_classes
          FROM recruiting_needs n
         WHERE n.source_deleted_at IS NULL AND (:status = '' OR n.recruiting_status = :status)
         ORDER BY n.airtable_created_at DESC NULLS LAST
    """, status=request.args.get("status", ""))
    return ok(rows, count=len(rows))


@bp.get("/locations")
@needs("class.view")
def locations():
    """Cities and their locations, with how each is staffed (instructor assignment type, primary and HUB instructors,
    instructor notes; extra pay with pay.view_all)."""
    user = auth.current_user()
    rows = db.fetch_all("""
        SELECT m.slug AS city, m.name AS city_name, m.state_code, m.booking_system, m.time_zone,
               coalesce((SELECT json_agg(json_build_object('id', si.id, 'name', si.name, 'address', si.address_line1,
                                                           'city', si.city, 'state', si.state_code,
                                                           'postal_code', si.postal_code, 'active', si.is_active,
                                                           'location_record_id', si.airtable_record_id,
                                                           'instructors', CASE WHEN lis.location_record_id IS NOT NULL THEN
                                                             json_build_object(
                                                               'assignment_types', lis.assignment_types,
                                                               'primary_instructor', lis.primary_instructor_name,
                                                               'hub_primary_instructor', lis.hub_primary_instructor_name,
                                                               'hub_primary_instructor_id', hp.public_id,
                                                               'hub_secondary_instructor', lis.hub_secondary_instructor_name,
                                                               'hub_secondary_instructor_id', hs.public_id,
                                                               'instructor_training_location', lis.is_instructor_training_location,
                                                               'instructor_notes', lis.instructor_notes,
                                                               'extra_pay_cents', lis.extra_pay_cents,
                                                               'extra_pay_reason', lis.extra_pay_reason) END)
                                         ORDER BY si.name)
                           FROM sites si
                           LEFT JOIN location_instructor_settings lis ON lis.site_id = si.id
                           LEFT JOIN instructors hp ON hp.id = lis.hub_primary_instructor_id
                           LEFT JOIN instructors hs ON hs.id = lis.hub_secondary_instructor_id
                          WHERE si.metro_id = m.id), '[]') AS sites
          FROM metros m WHERE user_has_permission(:uid, 'class.view', m.id) ORDER BY m.name
    """, uid=user.id)
    if "pay.view_all" not in user.permissions:          # instructors' extra pay is for those who see pay
        for city in rows:
            for site in city["sites"]:
                if site.get("instructors"):
                    site["instructors"].pop("extra_pay_cents", None)
                    site["instructors"].pop("extra_pay_reason", None)
    return ok(rows, count=len(rows))


# --- Instructors' requests from the portal --------------------------------------------------------

@bp.get("/instructor-requests")
@needs("instructor.requests")
def instructor_requests():
    """Instructors' availability changes and "can't teach" notices (status: open (default), done, dismissed,
    withdrawn, all; kind: availability, cant_teach)."""
    a = request.args
    status = a.get("status", "open")
    if status not in ("open", "done", "dismissed", "withdrawn", "all"):
        return refuse(400, "status is open, done, dismissed, withdrawn or all.")
    kind = a.get("kind", "")
    if kind not in ("", "availability", "cant_teach"):
        return refuse(400, "kind is availability or cant_teach.")
    rows = db.fetch_all("""
        SELECT r.id, r.kind, r.status, r.reason, r.details, r.created_at, r.handled_at, r.handled_note,
               r.availability_request_id,
               json_build_object('id', i.public_id, 'name', coalesce(i.full_name, i.first_name || ' ' || i.last_name),
                                 'email', i.email, 'phone', i.phone_e164) AS instructor,
               CASE WHEN s.id IS NOT NULL THEN json_build_object(
                 'id', s.public_id, 'course', cf.name, 'site', si.name, 'city', m.name, 'starts_at', s.starts_at,
                 'time_zone', s.time_zone, 'role', CASE a.role WHEN 'co' THEN 'helper' ELSE a.role END,
                 'seats_sold', av.sold_seats) END AS class,
               h.display_name AS handled_by
          FROM instructor_requests r
          JOIN instructors i ON i.id = r.instructor_id
          LEFT JOIN assignments a ON a.id = r.assignment_id
          LEFT JOIN sessions s ON s.id = a.session_id
          LEFT JOIN course_families cf ON cf.id = s.course_family_id
          LEFT JOIN sites si ON si.id = s.site_id
          LEFT JOIN metros m ON m.id = s.metro_id
          LEFT JOIN session_availability av ON av.session_id = s.id
          LEFT JOIN user_accounts h ON h.id = r.handled_by_user_id
         WHERE (:st = 'all' OR r.status = :st) AND (:k = '' OR r.kind = :k)
           AND (s.id IS NULL OR user_has_permission(:uid, 'instructor.requests', s.metro_id))
         ORDER BY (r.kind <> 'cant_teach'), coalesce(s.starts_at, r.created_at), r.id
         LIMIT 500
    """, st=status, k=kind, uid=auth.current_user().id)
    counts = {r["kind"]: r["n"] for r in db.fetch_all(
        "SELECT kind, count(*) AS n FROM instructor_requests WHERE status = 'open' GROUP BY kind")}
    return ok(rows, count=len(rows), open={"availability": counts.get("availability", 0),
                                           "cant_teach": counts.get("cant_teach", 0)})


@bp.post("/instructor-requests/<int:request_id>/close")
@needs("instructor.requests")
def instructor_request_close(request_id: int):
    """Close a request once it's handled in Airtable: {"outcome": "done" | "dismissed", "note": "…"}.
    The note is shown to the instructor."""
    b = body()
    outcome = b.get("outcome")
    if outcome not in ("done", "dismissed"):
        return refuse(400, 'outcome is "done" (changed in Airtable) or "dismissed" (not changed).')
    note = (b.get("note") or "").strip()[:1000] or None
    if outcome == "dismissed" and not note:
        return refuse(400, "Say why it isn't changed (the instructor sees the note).")
    with db.transaction() as conn:
        row = conn.execute(text("""
            SELECT r.id, r.status, r.kind, s.metro_id FROM instructor_requests r
              LEFT JOIN assignments a ON a.id = r.assignment_id LEFT JOIN sessions s ON s.id = a.session_id
             WHERE r.id = :r FOR NO KEY UPDATE OF r
        """), {"r": request_id}).mappings().first()
        if row is None or (row["metro_id"] and not auth.current_user().can("instructor.requests", row["metro_id"])):
            return refuse(404, "No such request.")
        if row["status"] != "open":
            return refuse(400, f"This request is already {row['status']}.")
        conn.execute(text("""
            UPDATE instructor_requests SET status = :o, handled_by_user_id = :u, handled_at = now(), handled_note = :n
             WHERE id = :r
        """), {"o": outcome, "u": auth.current_user().id, "n": note, "r": request_id})
        audit.record(conn, f"instructor_request.{outcome}", "instructor_request", request_id, after={"note": note})
    return ok({"id": request_id, "status": outcome})


@bp.get("/log")
@needs("audit.view")
def action_log():
    """The action log (since, area)."""
    a = request.args
    since = a.get("since")
    try:
        since_dt = datetime.fromisoformat(since) if since else datetime.now() - timedelta(days=7)
    except ValueError:
        return refuse(400, "since must be an ISO date or time.")
    rows = db.fetch_all("""
        SELECT l.id, l.occurred_at, l.action, l.entity_type, l.entity_id, l.reason, l.after, l.before,
               a.display_name AS actor, a.email AS actor_email
          FROM audit_log l LEFT JOIN user_accounts a ON a.id = l.user_account_id
         WHERE l.occurred_at >= :since AND (:area = '' OR l.action LIKE :area || '.%')
         ORDER BY l.occurred_at DESC, l.id DESC LIMIT 500
    """, since=since_dt, area=a.get("area", ""))
    return ok(rows, count=len(rows))


def access_list() -> list[dict]:
    """Every endpoint with what it needs: the API access list (Superadmin → API keys, docs/api.md)."""
    from flask import current_app
    out = []
    for rule in current_app.url_map.iter_rules():
        if not rule.endpoint.startswith("api."):
            continue
        view = current_app.view_functions[rule.endpoint]
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            out.append({"method": method, "path": rule.rule, "permissions": list(getattr(view, "permissions", ())),
                        "what": (view.__doc__ or "").strip().split("\n")[0]})
    return sorted(out, key=lambda e: (e["path"], e["method"]))
