"""The booking console: the desk for customer service and managers (/console).

Same sign-in page as everyone. Accounts whose roles open the console
(roles.dashboard_key 'customer_service' or 'manager') land here after signing
in; admins reach it from the admin sidebar. What each person sees is set by
permissions (db/seed.sql), and every list only shows classes in the cities
their role covers (user_has_permission(..., metro_id) in SQL).

Finding students and bookings, a booking's full picture (seats, payments,
refunds, transfers, GHL, notes), notes, refunds with manager approval, moving
a seat to another class, cancelling, phone bookings (a payment link) and
invoice bookings, and a class's roster: attendance, moving a cancelled class's
students together, and the Red Cross class list (app/desk.py,
app/attendance.py, app/integrations/checkout.py).
"""

from __future__ import annotations

import re

from datetime import date, timedelta

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy import text

from .. import areas, attendance, audit, auth, classes, db, desk, refunds, waitlist

bp = Blueprint("console", __name__, url_prefix="/console")

REG_STATUS = {
    "pending": ("wait", "Holding a seat"), "awaiting_payment": ("wait", "Payment due"),
    "confirmed": ("ok", "Booked"), "displaced": ("bad", "Class cancelled: needs a new date"),
    "completed": ("ok", "Completed"), "no_show": ("bad", "No show"), "cancelled": ("off", "Cancelled"),
    "forfeited": ("off", "Fee forfeited"), "expired": ("off", "Hold expired"),
}
REFUND_STATUS = {
    "requested": ("wait", "Waiting for approval"), "approved": ("wait", "Approved"),
    "processing": ("wait", "Sending to Stripe"), "succeeded": ("ok", "Refunded"),
    "failed": ("bad", "Failed"), "rejected": ("off", "Turned down"),
}
CHANNELS = {"web": "Online", "phone": "Phone", "invoice": "Invoice", "admin": "Staff",
            "waitlist_offer": "Waiting-list offer", "transfer_adjustment": "Transfer difference"}

# The actions on a booking, with the permission each needs.
ACTIONS = {
    "transfer": ("Move to another class", "booking.transfer"),
    "cancel": ("Cancel the booking", "booking.cancel"),
    "refund": ("Refund", "refund.issue"),
}

# Every console list is limited to the cities this account's role covers.
IN_MY_CITIES = "user_has_permission(:uid, 'student.view', s.metro_id)"

_SEAT = f"""
    SELECT r.id, r.public_id, r.status, r.attendee_first_name, r.attendee_last_name, r.attendee_email,
           r.attendee_phone_e164, r.amount_paid_cents, r.amount_refunded_cents, r.created_at,
           o.public_id AS order_public_id, o.channel, o.buyer_email,
           s.id AS session_id, s.starts_at, s.ends_at, s.time_zone,
           cf.name AS course_name, si.name AS site_name, m.name AS city_name, p.short_name AS product_name
      FROM registrations r
      JOIN orders o ON o.id = r.order_id
      JOIN sessions s ON s.id = r.session_id
      JOIN sites si ON si.id = s.site_id
      JOIN metros m ON m.id = s.metro_id
      JOIN course_families cf ON cf.id = s.course_family_id
      JOIN products p ON p.id = r.product_id
     WHERE {IN_MY_CITIES}
"""


def _ctx(**kw) -> dict:
    return {"tabs": areas.tabs_for(auth.current_user()), "reg_status": REG_STATUS,
            "refund_status": REFUND_STATUS, "channels": CHANNELS, **kw}


# --- Today ---------------------------------------------------------------------------

@bp.get("")
@auth.permission_required("student.view")
def home():
    """What the desk needs first thing: search, today's classes, what needs a look."""
    user = auth.current_user()
    stats = db.fetch_one(f"""
        SELECT
          (SELECT count(*) FROM registrations r JOIN sessions s ON s.id = r.session_id
            WHERE {IN_MY_CITIES} AND r.status IN ('confirmed', 'awaiting_payment')
              AND r.created_at > now() - interval '1 day') AS booked_today,
          (SELECT count(*) FROM registrations r JOIN sessions s ON s.id = r.session_id
            WHERE {IN_MY_CITIES} AND r.status = 'awaiting_payment') AS payment_due,
          (SELECT count(*) FROM registrations r JOIN sessions s ON s.id = r.session_id
            WHERE {IN_MY_CITIES} AND r.status = 'displaced') AS displaced,
          (SELECT count(*) FROM sessions s WHERE {IN_MY_CITIES} AND s.status = 'scheduled'
              AND s.starts_at >= now() - interval '12 hours' AND s.starts_at < now() + interval '2 days') AS classes_soon,
          (SELECT count(*) FROM refunds f JOIN registrations r ON r.id = f.registration_id
             JOIN sessions s ON s.id = r.session_id
            WHERE {IN_MY_CITIES} AND f.status = 'requested' AND f.requires_manager_approval) AS approvals,
          (SELECT count(*) FROM unsynced_items) AS unsynced
    """, uid=user.id)
    soon = db.fetch_all(f"""
        SELECT s.id, s.starts_at, s.ends_at, s.time_zone, cf.name AS course_name, si.name AS site_name,
               m.name AS city_name, av.sold_seats, av.seat_limit, av.waitlist_count
          FROM sessions s
          JOIN sites si ON si.id = s.site_id
          JOIN metros m ON m.id = s.metro_id
          JOIN course_families cf ON cf.id = s.course_family_id
          JOIN session_availability av ON av.session_id = s.id
         WHERE {IN_MY_CITIES} AND s.status = 'scheduled'
           AND s.starts_at >= now() - interval '12 hours' AND s.starts_at < now() + interval '2 days'
         ORDER BY s.starts_at LIMIT 12
    """, uid=user.id)
    attention = db.fetch_all(_SEAT + """
           AND r.status IN ('awaiting_payment', 'displaced') AND s.starts_at > now() - interval '1 day'
         ORDER BY s.starts_at LIMIT 10
    """, uid=user.id)
    recent = db.fetch_all(_SEAT + """
           AND r.status IN ('confirmed', 'awaiting_payment')
         ORDER BY r.created_at DESC LIMIT 8
    """, uid=user.id)
    return render_template("console/home.html", stats=stats, soon=soon, attention=attention, recent=recent,
                           **_ctx())


# --- Find a student: students of the next classes, with filters ---------------------

FIND_WHEN = [("upcoming", "Upcoming classes"), ("today", "Today"), ("7", "Next 7 days"), ("30", "Next 30 days"),
             ("past30", "Last 30 days"), ("any", "Any date")]
FIND_STATUS = [("live", "Booked, payment due or needs a new date"), ("all", "Any status")] + \
    [(k, v[1]) for k, v in REG_STATUS.items() if k not in ("pending", "expired")]
CLASSES_PER_PAGE = 8


def _find_filters(args) -> dict:
    f = {k: (args.get(k) or "").strip()[:120] for k in ("q", "city", "course", "when", "status", "channel",
                                                       "from", "to")}
    f["when"] = f["when"] if f["when"] in dict(FIND_WHEN) else "upcoming"
    f["status"] = f["status"] if f["status"] in dict(FIND_STATUS) else "live"
    f["channel"] = f["channel"] if f["channel"] in CHANNELS else ""
    for k in ("from", "to"):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", f[k]):
            f[k] = ""
    try:
        f["page"] = max(1, int(args.get("page") or 1))
    except ValueError:
        f["page"] = 1
    return f


# Which classes (whose students are listed) and which seats in them.
_CLASS_WHERE = f"""
       {IN_MY_CITIES}
   AND (:city = '' OR m.slug = :city)
   AND (:course = '' OR cf.code = :course)
   AND (CAST(:dfrom AS date) IS NULL OR s.local_date >= CAST(:dfrom AS date))
   AND (CAST(:dto AS date) IS NULL OR s.local_date <= CAST(:dto AS date))
   AND CASE :when
         WHEN 'upcoming' THEN s.ends_at > now()
         WHEN 'today'    THEN s.local_date = (now() AT TIME ZONE s.time_zone)::date
         WHEN '7'        THEN s.ends_at > now() AND s.starts_at < now() + interval '7 days'
         WHEN '30'       THEN s.ends_at > now() AND s.starts_at < now() + interval '30 days'
         WHEN 'past30'   THEN s.starts_at < now() AND s.starts_at > now() - interval '30 days'
         ELSE true END
"""
_SEAT_WHERE = """
       CASE :status WHEN 'live' THEN r.status IN ('confirmed', 'awaiting_payment', 'displaced')
                    WHEN 'all'  THEN r.status NOT IN ('pending', 'expired')
                    ELSE r.status = :status END
   AND (:channel = '' OR o.channel = :channel)
   AND (:q = ''
        OR (r.attendee_first_name || ' ' || r.attendee_last_name) ILIKE '%' || :q || '%'
        OR r.attendee_email ILIKE '%' || :q || '%' OR o.buyer_email ILIKE '%' || :q || '%'
        OR (o.buyer_first_name || ' ' || o.buyer_last_name) ILIKE '%' || :q || '%'
        OR (length(:digits) >= 7 AND (r.attendee_phone_e164 LIKE '%' || :digits || '%'
                                   OR o.buyer_phone_e164 LIKE '%' || :digits || '%'))
        OR (length(:ref) >= 6 AND (CAST(o.public_id AS text) LIKE :ref || '%'
                                OR CAST(r.public_id AS text) LIKE :ref || '%')))
"""


def roster(uid: int, f: dict) -> dict:
    """Classes (soonest first; newest first for past ones) with the students matching the filters."""
    params = {"uid": uid, "city": f["city"], "course": f["course"], "when": f["when"], "status": f["status"],
              "channel": f["channel"], "q": f["q"], "digits": re.sub(r"\D", "", f["q"]),
              "ref": f["q"].lower().replace("#", "").strip(), "dfrom": f["from"] or None, "dto": f["to"] or None}
    joins = """FROM sessions s JOIN metros m ON m.id = s.metro_id JOIN sites si ON si.id = s.site_id
               JOIN course_families cf ON cf.id = s.course_family_id"""
    has_seats = f"""EXISTS (SELECT 1 FROM registrations r JOIN orders o ON o.id = r.order_id
                             WHERE r.session_id = s.id AND {_SEAT_WHERE})"""
    total = db.fetch_value(f"SELECT count(*) {joins} WHERE {_CLASS_WHERE} AND {has_seats}", **params)
    pages = max(1, -(-total // CLASSES_PER_PAGE))
    page = min(f["page"], pages)
    order = "s.starts_at DESC" if f["when"] == "past30" else "s.starts_at"
    found = db.fetch_all(f"""
        SELECT s.id, s.public_id, s.starts_at, s.ends_at, s.time_zone, s.status AS class_status,
               cf.name AS course_name, si.name AS site_name, m.name AS city_name, m.state_code,
               av.sold_seats, av.seat_limit, av.waitlist_count
          {joins} JOIN session_availability av ON av.session_id = s.id
         WHERE {_CLASS_WHERE} AND {has_seats}
         ORDER BY {order}, s.id LIMIT :lim OFFSET :off
    """, lim=CLASSES_PER_PAGE, off=(page - 1) * CLASSES_PER_PAGE, **params)
    seats = db.fetch_all(f"""
        SELECT r.session_id, r.public_id, r.status, r.attendee_first_name, r.attendee_last_name, r.attendee_email,
               r.attendee_phone_e164, r.amount_paid_cents, r.amount_refunded_cents, r.checked_in_at,
               o.channel, o.buyer_email, o.buyer_first_name, o.buyer_last_name, o.public_id AS order_public_id,
               p.short_name AS product_name,
               EXISTS (SELECT 1 FROM refunds f WHERE f.registration_id = r.id
                        AND f.status IN ('requested', 'approved', 'processing')) AS refund_pending
          FROM registrations r JOIN orders o ON o.id = r.order_id JOIN products p ON p.id = r.product_id
         WHERE r.session_id = ANY(:ids) AND {_SEAT_WHERE}
         ORDER BY lower(r.attendee_last_name), lower(r.attendee_first_name), r.id
    """, ids=[c["id"] for c in found], **params)
    by_class: dict[int, list] = {}
    for seat in seats:
        by_class.setdefault(seat["session_id"], []).append(seat)
    for c in found:
        c["students"] = by_class.get(c["id"], [])
        c["can_refund"] = db.fetch_value("SELECT user_has_permission(:u, 'refund.issue', s.metro_id) "
                                         "FROM sessions s WHERE s.id = :s", u=uid, s=c["id"])
    query = {k: v for k, v in f.items() if k != "page" and v and not (k == "when" and v == "upcoming")
             and not (k == "status" and v == "live")}
    return {"classes": found, "total": total, "page": page, "pages": pages,
            "page_list": classes.page_list(page, pages), "query": query, "students": len(seats)}


@bp.get("/find")
@auth.permission_required("student.view")
def find():
    """Students of the next classes in this person's cities; filters narrow it down."""
    user = auth.current_user()
    f = _find_filters(request.args)
    cities = db.fetch_all("""SELECT m.slug, m.name, m.state_code FROM metros m
                              WHERE m.is_active AND user_has_permission(:uid, 'student.view', m.id)
                              ORDER BY m.state_code, m.name""", uid=user.id)
    courses = db.fetch_all("SELECT code, name FROM course_families ORDER BY name")
    return render_template("console/find.html", f=f, r=roster(user.id, f), cities=cities, courses=courses,
                           whens=FIND_WHEN, statuses=FIND_STATUS, q=f["q"],
                           show_money=user.has_permission("payment.view"), **_ctx())


def _booking_or_404(public_id: str) -> dict:
    user = auth.current_user()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", public_id or ""):
        abort(404)
    row = db.fetch_one(f"""
        SELECT r.*, o.id AS order_id, o.public_id AS order_public_id, o.channel, o.status AS order_status,
               o.buyer_first_name, o.buyer_last_name, o.buyer_email, o.buyer_phone_e164, o.buyer_user_id,
               o.total_cents, o.discount_cents AS order_discount_cents, o.livemode, o.created_at AS ordered_at,
               s.id AS session_id, s.public_id AS session_public_id, s.metro_id, s.starts_at, s.ends_at,
               s.time_zone, s.status AS class_status,
               s.delivery_mode, cf.name AS course_name, cf.code AS course_code, si.name AS site_name,
               si.address_line1, si.city AS site_city, si.state_code AS site_state, m.name AS city_name,
               p.short_name AS product_name, p.name AS product_full_name,
               av.seats_left, av.seat_limit,
               EXTRACT(EPOCH FROM s.starts_at - now()) / 3600 AS hours_to_class,
               (SELECT count(*) FROM registration_transfers t WHERE t.registration_id = r.id) AS transfers_used
          FROM registrations r
          JOIN orders o ON o.id = r.order_id
          JOIN sessions s ON s.id = r.session_id
          JOIN sites si ON si.id = s.site_id
          JOIN metros m ON m.id = s.metro_id
          JOIN course_families cf ON cf.id = s.course_family_id
          JOIN products p ON p.id = r.product_id
          JOIN session_availability av ON av.session_id = s.id
         WHERE r.public_id = CAST(:p AS uuid) AND {IN_MY_CITIES}
    """, p=public_id, uid=user.id)
    if not row:
        abort(404)                # not there, or in a city this account doesn't cover
    return row


@bp.get("/bookings/<public_id>")
@auth.permission_required("student.view")
def find_booking(public_id: str):
    """One seat, with everything around it."""
    user = auth.current_user()
    r = _booking_or_404(public_id)
    seats = db.fetch_all("""
        SELECT r.public_id, r.attendee_first_name, r.attendee_last_name, r.attendee_email, r.status
          FROM registrations r WHERE r.order_id = :o ORDER BY r.id
    """, o=r["order_id"])
    show_money = user.can("payment.view", r["metro_id"])
    payments = db.fetch_all("""
        SELECT method, status, amount_cents, stripe_payment_intent_id, received_at, livemode, manual_reference
          FROM payments WHERE order_id = :o ORDER BY created_at
    """, o=r["order_id"]) if show_money else []
    refunds = db.fetch_all("""
        SELECT f.id, f.amount_cents, f.reason, f.source, f.status, f.requires_manager_approval, f.stripe_refund_id,
               f.created_at, f.processed_at, f.failure_reason, a.display_name AS requested_by
          FROM refunds f LEFT JOIN user_accounts a ON a.id = f.requested_by_user_id
         WHERE f.registration_id = :r OR (f.registration_id IS NULL AND f.order_id = :o)
         ORDER BY f.created_at
    """, r=r["id"], o=r["order_id"]) if show_money else []
    transfers = db.fetch_all("""
        SELECT t.created_at, t.reason, t.via, t.fee_cents, t.price_difference_cents,
               fs.starts_at AS from_starts_at, fs.time_zone AS from_tz, fsi.name AS from_site,
               ts.starts_at AS to_starts_at, ts.time_zone AS to_tz, tsi.name AS to_site,
               a.display_name AS performed_by
          FROM registration_transfers t
          JOIN sessions fs ON fs.id = t.from_session_id JOIN sites fsi ON fsi.id = fs.site_id
          JOIN sessions ts ON ts.id = t.to_session_id JOIN sites tsi ON tsi.id = ts.site_id
          LEFT JOIN user_accounts a ON a.id = t.performed_by_user_id
         WHERE t.registration_id = :r ORDER BY t.created_at
    """, r=r["id"])
    notes = db.fetch_all("""
        SELECT n.body, n.created_at, a.display_name AS author
          FROM notes n JOIN user_accounts a ON a.id = n.author_user_id
         WHERE n.registration_id = :r OR n.order_id = :o
         ORDER BY n.created_at DESC
    """, r=r["id"], o=r["order_id"])
    history = db.fetch_all("""
        SELECT l.occurred_at, l.action, a.display_name AS actor
          FROM audit_log l LEFT JOIN user_accounts a ON a.id = l.user_account_id
         WHERE (l.entity_type = 'registration' AND l.entity_id = :r) OR (l.entity_type = 'order' AND l.entity_id = :o)
         ORDER BY l.occurred_at DESC LIMIT 20
    """, r=r["id"], o=r["order_id"])
    live = r["status"] in desk.LIVE
    actions = [{"key": k, "label": label, "allowed": user.can(perm, r["metro_id"]) and (live or k == "refund")}
               for k, (label, perm) in ACTIONS.items()]
    can_decide = user.can("refund.approve_large", r["metro_id"])
    for f in refunds:
        f["can_decide"] = can_decide and f["status"] == "requested"
    return render_template("console/booking.html", r=r, seats=seats, payments=payments, refunds=refunds,
                           transfers=transfers, notes=notes, history=history, actions=actions,
                           show_money=show_money, can_note=user.can("student.note", r["metro_id"]),
                           pay=_payment_state(r), **_ctx())


def _payment_state(r: dict) -> dict:
    """What's still to pay on this booking: a phone booking's open payment link, an invoice, or a class
    change's price difference."""
    user = auth.current_user()
    link = db.fetch_one("""
        SELECT c.url, h.expires_at FROM checkout_sessions c JOIN seat_holds h ON h.id = c.seat_hold_id
         WHERE c.order_id = :o AND c.status = 'open' AND h.status = 'active' AND h.expires_at > now()
         ORDER BY c.id DESC LIMIT 1
    """, o=r["order_id"]) if r["order_status"] == "open" else None
    invoice = db.fetch_one("""
        SELECT invoice_due_on, po_number, buyer_company, stripe_invoice_id, total_cents FROM orders WHERE id = :o
    """, o=r["order_id"]) if r["order_status"] == "awaiting_payment" else None
    differences = db.fetch_all("""
        SELECT o.id, o.status, o.total_cents, o.invoice_due_on,
               (SELECT c.url FROM checkout_sessions c WHERE c.order_id = o.id AND c.status = 'open'
                 ORDER BY c.id DESC LIMIT 1) AS url
          FROM registration_transfers t JOIN orders o ON o.id = t.settlement_order_id
         WHERE t.registration_id = :r ORDER BY t.created_at
    """, r=r["id"])
    return {"link": link, "invoice": invoice, "differences": differences,
            "can_record": invoice is not None and user.can("booking.create_invoice", r["metro_id"]),
            "can_send": user.can("booking.create_phone", r["metro_id"]) or user.can("booking.transfer", r["metro_id"])}


def _base_url() -> str:
    configured = current_app.config.get("PUBLIC_BASE_URL", "")
    if configured and "localhost" not in configured:
        return configured.rstrip("/")
    return request.url_root.rstrip("/")


@bp.post("/bookings/<public_id>/payment")
@auth.permission_required("booking.create_invoice")
def booking_record_payment(public_id: str):
    """An invoice / PO booking paid outside Stripe (a cheque, a bank transfer): record it."""
    from ..integrations import checkout
    r = _booking_or_404(public_id)
    if not auth.current_user().can("booking.create_invoice", r["metro_id"]):
        abort(403)
    amount = _cents(request.form.get("amount", ""))
    reference = request.form.get("reference", "").strip()
    if amount is None or not reference:
        flash("Enter the amount received and a reference (cheque number, bank transfer id).", "error")
    else:
        try:
            checkout.record_payment(r["order_id"], amount, reference, auth.current_user().id)
            flash("Payment recorded. The seats on this invoice are paid.", "ok")
        except checkout.BookingError as exc:
            flash(str(exc), "error")
    return redirect(url_for("console.find_booking", public_id=public_id) + "#money")


@bp.post("/bookings/<public_id>/send-link")
@auth.any_permission_required("booking.create_phone", "booking.transfer")
def booking_send_link(public_id: str):
    """Send the open payment link (a phone booking, or a class change's price difference) again through GHL."""
    from ..integrations.outbox import enqueue
    r = _booking_or_404(public_id)
    pay = _payment_state(r)
    if not pay["can_send"]:
        abort(403)
    order_id = request.form.get("order", type=int)
    target = None
    if pay["link"] and order_id == r["order_id"]:
        target = (r["order_id"], pay["link"]["url"], r["total_cents"])
    for d in pay["differences"]:
        if d["id"] == order_id and d["status"] == "awaiting_payment":
            url = d["url"] or desk.send_payment_link(d["id"], _base_url())
            target = (d["id"], url, d["total_cents"]) if url else None
    if not target:
        flash("There's no open payment link on this booking.", "error")
    else:
        import uuid
        with db.transaction() as conn:
            enqueue(conn, "ghl", "send_payment_link", key=f"ghl-payment-link-{target[0]}-{uuid.uuid4().hex[:8]}",
                    entity_type="order", entity_id=target[0],
                    payload={"order_id": target[0], "url": target[1], "amount_cents": target[2]})
            audit.record(conn, "booking.payment_link_send", "order", target[0])
        flash("Sending the payment link again (GHL texts and emails it).", "ok")
    return redirect(url_for("console.find_booking", public_id=public_id) + "#money")


@bp.post("/bookings/<public_id>/notes")
@auth.permission_required("student.note")
def find_booking_note(public_id: str):
    user = auth.current_user()
    r = _booking_or_404(public_id)
    if not user.can("student.note", r["metro_id"]):
        abort(403)
    body = request.form.get("body", "").strip()[:4000]
    if not body:
        flash("Write the note first.", "error")
        return redirect(url_for("console.find_booking", public_id=public_id) + "#notes")
    with db.transaction() as conn:
        note_id = conn.execute(text("""
            INSERT INTO notes (registration_id, order_id, author_user_id, body)
            VALUES (:r, :o, :u, :b) RETURNING id
        """), {"r": r["id"], "o": r["order_id"], "u": user.id, "b": body}).scalar_one()
        audit.record(conn, "booking.note_add", "registration", r["id"], after={"note_id": note_id})
    flash("Note added.", "ok")
    return redirect(url_for("console.find_booking", public_id=public_id) + "#notes")


@bp.get("/bookings/<public_id>/<action>")
@auth.permission_required("student.view")
def find_booking_action(public_id: str, action: str):
    """Move to another class, cancel, refund."""
    if action not in ACTIONS:
        abort(404)
    user = auth.current_user()
    r = _booking_or_404(public_id)
    if not user.can(ACTIONS[action][1], r["metro_id"]):
        abort(403)
    if action == "refund":
        return _refund_page(r)
    if action == "transfer":
        return _move_page(r)
    return _cancel_page(r)


def _move_page(r: dict, error: str | None = None, form: dict | None = None, code: int = 200):
    user = auth.current_user()
    q = (request.values.get("q") or "").strip()[:80]
    return render_template("console/move.html", r=r, error=error, form=form or {}, q=q,
                           targets=desk.targets(r, user.id, q=q), reasons=desk.MOVE_REASONS,
                           hint=desk.suggestions(r), live=r["status"] in desk.LIVE,
                           can_override=user.can("class.capacity_override", r["metro_id"]), **_ctx()), code


def _cancel_page(r: dict, error: str | None = None, form: dict | None = None, code: int = 200):
    user = auth.current_user()
    return render_template("console/cancel.html", r=r, error=error, form=form or {}, reasons=desk.CANCEL_REASONS,
                           hint=desk.suggestions(r), live=r["status"] in desk.LIVE,
                           info=refunds.preview(r["id"]), can_refund=user.can("refund.issue", r["metro_id"]),
                           **_ctx()), code


@bp.post("/bookings/<public_id>/transfer")
@auth.permission_required("booking.transfer")
def booking_move(public_id: str):
    r = _booking_or_404(public_id)
    if not auth.current_user().can("booking.transfer", r["metro_id"]):
        abort(403)
    form = {k: request.form.get(k, "") for k in ("target", "reason", "fee", "note", "extra_seat")}
    target, _, product = form["target"].partition(":")
    fee = _cents(form["fee"] or "0")
    if not (target.isdigit() and product.isdigit()):
        return _move_page(r, "Pick the class to move to.", form, 400)
    if fee is None:
        return _move_page(r, "Enter the fee in dollars, like 25 or 0.", form, 400)
    try:
        done = desk.move(r["id"], to_session_id=int(target), to_product_id=int(product), reason=form["reason"],
                         fee_cents=fee, note=form["note"], extra_seat_reason=form["extra_seat"],
                         base_url=_base_url())
    except desk.DeskError as exc:
        return _move_page(r, str(exc), form, 400)
    message = "Moved. GHL gets the new date and place and sends the “class changed” message."
    if done["charge_cents"]:
        message += (f" {done['charge_cents'] / 100:,.2f} to pay: a payment link is on its way to the buyer by "
                    "text and email." if done["payment_url"] else
                    f" {done['charge_cents'] / 100:,.2f} difference recorded as paid (test booking, no card).")
    if done["credit_cents"]:
        message += f" {done['credit_cents'] / 100:,.2f} is going back to the card."
    flash(message, "ok")
    return redirect(url_for("console.find_booking", public_id=public_id))


@bp.post("/bookings/<public_id>/cancel")
@auth.permission_required("booking.cancel")
def booking_cancel(public_id: str):
    r = _booking_or_404(public_id)
    if not auth.current_user().can("booking.cancel", r["metro_id"]):
        abort(403)
    form = {k: request.form.get(k, "") for k in ("reason", "refund", "note", "confirm")}
    refund = _cents(form["refund"] or "0")
    if refund is None:
        return _cancel_page(r, "Enter the refund in dollars, like 108 or 0.", form, 400)
    if form["confirm"] != "yes":
        return _cancel_page(r, "Tick the box to confirm: the seat goes back on sale at once.", form, 400)
    try:
        done = desk.cancel(r["id"], reason=form["reason"], refund_cents=refund, note=form["note"])
    except desk.DeskError as exc:
        return _cancel_page(r, str(exc), form, 400)
    except refunds.RefundError as exc:
        flash(f"Cancelled, but the refund didn't go ahead: {exc}", "error")
        return redirect(url_for("console.find_booking", public_id=public_id))
    message = "Cancelled. The seat is free again (offered to the waiting list first), and GHL moves to Cancelled."
    if done["refund"]:
        flash(_refund_message(done["refund"]["id"], message + " ")[0], "ok")
    else:
        flash(message, "ok")
    return redirect(url_for("console.find_booking", public_id=public_id))


# --- Book by phone or invoice --------------------------------------------------------

@bp.get("/new")
@auth.any_permission_required("booking.create_phone", "booking.create_invoice")
def new_booking():
    """Pick the class first (the same search as the home page), then the caller's details."""
    found = classes.search(classes.filters_from(request.args))
    return render_template("console/new_booking.html", found=found, **_ctx())


def _new_page(s: dict, form, error: str | None = None, code: int = 200):
    user = auth.current_user()
    from ..integrations import checkout
    due_days = db.fetch_value("SELECT value FROM app_settings WHERE key = 'invoice_due_days'")
    due = date.today() + timedelta(days=int(due_days) if isinstance(due_days, (int, float)) else 30)
    hold = db.fetch_value("SELECT value FROM app_settings WHERE key = 'phone_link_hold_minutes'")
    seats = max(1, min(request.values.get("seats", type=int) or 1, 50))
    return render_template("console/new_booking_class.html", s=s, form=form, error=error, seats=seats,
                           due=due, hold_minutes=hold, testing=checkout.test_checkout(),
                           can_phone=user.can("booking.create_phone", s["metro_id"]),
                           can_invoice=user.can("booking.create_invoice", s["metro_id"]), **_ctx()), code


@bp.get("/new/<public_id>")
@auth.any_permission_required("booking.create_phone", "booking.create_invoice")
def new_booking_class(public_id: str):
    from ..integrations import checkout
    s = checkout.bookable_session(public_id)
    if not s:
        abort(404)
    return _new_page(s, {})


@bp.post("/new/<public_id>")
@auth.any_permission_required("booking.create_phone", "booking.create_invoice")
def new_booking_submit(public_id: str):
    """Book a caller (payment link) or a company (invoice). Card details are never typed here."""
    from ..integrations import checkout
    user = auth.current_user()
    s = checkout.bookable_session(public_id)
    if not s:
        abort(404)
    f = request.form
    how = f.get("how", "")
    if how not in ("phone", "invoice") or not user.can(f"booking.create_{how}", s["metro_id"]):
        return _new_page(s, f, "Choose how they'll pay.", 400)
    seats = max(1, min(f.get("seats", type=int) or 1, 50))
    phone = checkout.normalise_phone(f.get("buyer_phone"))
    buyer = checkout.Person(f.get("buyer_first", "").strip(), f.get("buyer_last", "").strip(),
                            f.get("buyer_email", "").strip().lower(), phone or None)
    if not (buyer.first_name and buyer.last_name and checkout.EMAIL.match(buyer.email)):
        return _new_page(s, f, "Enter the buyer's first name, last name and email.", 400)
    if phone == "" or (how == "phone" and not phone):
        return _new_page(s, f, "Enter a valid mobile number: the payment link goes by text and email." if how == "phone"
                         else "Enter a valid phone number, or leave it blank.", 400)
    attendees = []
    for i in range(seats):
        if i == 0 and f.get("buyer_attends") == "yes":
            attendees.append(checkout.Person(buyer.first_name, buyer.last_name, buyer.email, phone or None))
            continue
        first, last = f.get(f"a{i}_first", "").strip(), f.get(f"a{i}_last", "").strip()
        email = f.get(f"a{i}_email", "").strip().lower()
        if not (first and last and checkout.EMAIL.match(email)):
            return _new_page(s, f, f"Seat {i + 1}: enter the attendee's first name, last name and their own email.",
                             400)
        attendees.append(checkout.Person(first, last, email))
    product_id = f.get("product_id", type=int) or 0
    try:
        if how == "phone":
            made = checkout.start_phone(staff_user_id=user.id, session_public_id=public_id, product_id=product_id,
                                        buyer=buyer, attendees=attendees, base_url=_base_url())
            order_id = made["order_id"]
        else:
            try:
                due_on = date.fromisoformat(f.get("due_on", ""))
            except ValueError:
                return _new_page(s, f, "Pick the date the invoice is due.", 400)
            if due_on < date.today():
                return _new_page(s, f, "The due date can't be in the past.", 400)
            company = f.get("company", "").strip()
            if not company:
                return _new_page(s, f, "Enter the company (or group) the invoice is for.", 400)
            made = checkout.book_on_invoice(session_public_id=public_id, product_id=product_id, buyer=buyer,
                                            attendees=attendees, company=company[:200],
                                            po_number=f.get("po_number", "").strip()[:60] or None, due_on=due_on,
                                            staff_user_id=user.id)
            order_id = made["order_id"]
    except checkout.BookingError as exc:
        return _new_page(checkout.bookable_session(public_id) or s, f, str(exc), 400)
    with db.transaction() as conn:
        audit.record(conn, f"booking.create_{how}", "order", order_id, after={"seats": len(attendees)})
        if how == "phone" and not made["testing"]:
            from ..integrations.outbox import enqueue
            total = conn.execute(text("SELECT total_cents FROM orders WHERE id = :o"), {"o": order_id}).scalar()
            enqueue(conn, "ghl", "send_payment_link", key=f"ghl-payment-link-{order_id}-first", entity_type="order",
                    entity_id=order_id, payload={"order_id": order_id, "url": made["url"], "amount_cents": total})
    first_seat = db.fetch_value("SELECT public_id FROM registrations WHERE order_id = :o ORDER BY id LIMIT 1",
                                o=order_id)
    if how == "phone":
        flash("Booked as a test (no payment taken): the seats are confirmed." if made["testing"] else
              "Seats held. The payment link is on its way to the buyer by text and email; it's below to copy too.",
              "ok")
    else:
        flash("Booked on invoice: the seats are confirmed and the booking shows “Payment due” until it's paid."
              + ("" if checkout.test_checkout() else " Stripe emails the invoice."), "ok")
    return redirect(url_for("console.find_booking", public_id=first_seat) + ("#money" if how == "phone" else ""))


# --- A class: roster, attendance, moving a cancelled class's students ------------------

def _class_or_404(public_id: str) -> dict:
    s = attendance.session_by_public_id(public_id)
    if not s or not auth.current_user().can("class.view", s["metro_id"]) and \
            not auth.current_user().can("student.view", s["metro_id"]):
        abort(404)
    return s


@bp.get("/classes/<public_id>")
@auth.any_permission_required("class.view", "student.view")
def class_page(public_id: str):
    """One class at the desk: the roster with attendance, its waiting list, and (cancelled) moving everyone."""
    user = auth.current_user()
    s = _class_or_404(public_id)
    can = attendance.access(s)
    moves = desk.targets({"session_id": s["id"]}, user.id) \
        if s["status"] == "cancelled" and user.can("booking.transfer_bulk", s["metro_id"]) else []
    waiting = db.fetch_all("""
        SELECT q.id, q.first_name, q.last_name, q.party_size, q.status, q.position, q.joined_at
          FROM waitlist_queue q WHERE q.session_id = :s ORDER BY q.position
    """, s=s["id"]) if user.can("waitlist.view", s["metro_id"]) else None
    return render_template("console/class.html", s=s, can=can, students=attendance.roster(s["id"]),
                           marks=attendance.MARKS, moves=moves, waiting=waiting, **_ctx())


@bp.post("/classes/<public_id>/attendance")
@auth.any_permission_required("booking.attendance", "own.attendance")
def class_mark(public_id: str):
    s = _class_or_404(public_id)
    try:
        attendance.mark(s, request.form.get("seat", ""), request.form.get("mark", ""))
    except attendance.AttendanceError as exc:
        flash(str(exc), "error")
    return redirect(url_for("console.class_page", public_id=public_id) + "#roster")


@bp.post("/classes/<public_id>/finish")
@auth.any_permission_required("booking.attendance", "own.attendance")
def class_finish(public_id: str):
    s = _class_or_404(public_id)
    try:
        done = attendance.finish(s)
        flash("Class finished." + (f" {done['pay_records']} pay record{'s' if done['pay_records'] != 1 else ''} "
                                   "created." if done["pay_records"] else "") +
              (" No rate is set for the " + " and ".join(done["missing_rates"]) +
               " instructor yet, so no pay record was made for them." if done["missing_rates"] else ""), "ok")
    except attendance.AttendanceError as exc:
        flash(str(exc), "error")
    return redirect(url_for("console.class_page", public_id=public_id))


@bp.get("/classes/<public_id>/class-list.csv")
@auth.any_permission_required("class.roster_export", "own.roster_export")
def class_export(public_id: str):
    s = _class_or_404(public_id)
    try:
        body = attendance.export_csv(s)
    except attendance.AttendanceError:
        abort(403)
    name = f"class-list-{s['local_date'].isoformat()}-{s['course_code'].lower()}.csv"
    return Response(body, mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@bp.post("/classes/<public_id>/move-all")
@auth.permission_required("booking.transfer_bulk")
def class_move_all(public_id: str):
    s = _class_or_404(public_id)
    to = request.form.get("to", type=int)
    if not to:
        flash("Pick the class to move everyone to.", "error")
        return redirect(url_for("console.class_page", public_id=public_id) + "#move")
    try:
        done = desk.move_class(s["id"], to_session_id=to, note=request.form.get("note", ""))
        flash(f"Moved {done['moved']} student{'s' if done['moved'] != 1 else ''}. GHL gets each new date and "
              "place and sends the “class changed” message.", "ok")
    except desk.DeskError as exc:
        flash(str(exc), "error")
    return redirect(url_for("console.class_page", public_id=public_id))


@bp.post("/waitlist/<int:entry_id>/<what>")
@auth.permission_required("waitlist.manage")
def waitlist_action(entry_id: int, what: str):
    """Remove someone from a waiting list, or offer a seat again after "Offer expired"."""
    try:
        if what == "remove":
            waitlist.remove(entry_id, request.form.get("why", ""))
            flash("Removed from the waiting list.", "ok")
        elif what == "reoffer":
            waitlist.reoffer(entry_id)
            flash("Seat offered again: the offer goes out by text and email (8 AM – 6 PM class time).", "ok")
        else:
            abort(404)
    except waitlist.WaitlistError as exc:
        flash(str(exc), "error")
    back = request.form.get("back")
    return redirect(back if auth.is_safe_next(back) else url_for("admin.waitlists"))


# --- Refunds -------------------------------------------------------------------------

def _refund_page(r: dict, error: str | None = None, form: dict | None = None, code: int = 200):
    info = refunds.preview(r["id"])
    user = auth.current_user()
    return render_template("console/refund.html", r=r, info=info, error=error, form=form or {},
                           reasons=refunds.REASONS, approver=user.can("refund.approve_large", r["metro_id"]),
                           **_ctx()), code


def _cents(raw: str) -> int | None:
    m = re.fullmatch(r"\$?\s*(\d{1,6})(?:\.(\d{1,2}))?", (raw or "").strip().replace(",", ""))
    if not m:
        return None
    return int(m.group(1)) * 100 + int((m.group(2) or "0").ljust(2, "0"))


@bp.post("/bookings/<public_id>/refund")
@auth.permission_required("refund.issue")
def find_booking_refund(public_id: str):
    r = _booking_or_404(public_id)
    if not auth.current_user().can("refund.issue", r["metro_id"]):
        abort(403)
    form = {k: request.form.get(k, "") for k in ("amount", "reason", "note")}
    amount = _cents(form["amount"])
    if amount is None:
        return _refund_page(r, "Enter the amount in dollars, like 108 or 54.50.", form, 400)
    try:
        done = refunds.request(r["id"], amount, form["reason"], form["note"])
    except refunds.RefundError as exc:
        return _refund_page(r, str(exc), form, 400)
    flash(*_refund_message(done["id"]))
    return redirect(url_for("console.find_booking", public_id=public_id))


def _refund_message(refund_id: int, prefix: str = "") -> tuple[str, str]:
    f = db.fetch_one("SELECT status, amount_cents, payment_id, failure_reason FROM refunds WHERE id = :id",
                     id=refund_id)
    amount = f"${f['amount_cents'] / 100:,.2f}"
    if f["status"] == "requested":
        return prefix + (f"Refund of {amount} sent to a manager to approve. It's under Refunds → Waiting for a "
                         "manager."), "ok"
    if f["status"] == "succeeded":
        return prefix + (f"Refunded: {amount} is going back to the card (Stripe)." if f["payment_id"] else
                         f"Refund of {amount} recorded (test booking: no card was charged)."), "ok"
    if f["status"] == "failed":
        return prefix + f"Stripe refused the refund of {amount}: {f['failure_reason']}", "error"
    return prefix + (f"Refund of {amount} is queued for Stripe; it goes through in a minute or two "
                     "(watch Refunds → On their way)."), "ok"


REFUND_TABS = [("waiting", "Waiting for a manager", ("requested",)),
               ("sending", "On their way", ("approved", "processing")),
               ("done", "Refunded", ("succeeded",)),
               ("problems", "Failed or turned down", ("failed", "rejected")),
               ("all", "All", tuple(REFUND_STATUS))]


@bp.get("/refunds")
@auth.any_permission_required("refund.issue", "refund.approve_large")
def refunds_list():
    """Every refund in this person's cities: who asked, who approved, where it is."""
    user = auth.current_user()
    tab = request.args.get("tab", "waiting")
    tab = tab if tab in {t[0] for t in REFUND_TABS} else "waiting"
    states = dict((k, st) for k, _l, st in REFUND_TABS)[tab]
    q = request.args.get("q", "").strip()[:120]
    rows = db.fetch_all(f"""
        SELECT f.id, f.amount_cents, f.reason, f.note, f.status, f.source, f.requires_manager_approval,
               f.created_at, f.processed_at, f.failure_reason, f.stripe_refund_id,
               a.display_name AS requested_by, b.display_name AS approved_by, s.metro_id,
               r.public_id, r.attendee_first_name, r.attendee_last_name, r.attendee_email,
               s.starts_at, s.time_zone, cf.name AS course_name, si.name AS site_name, o.livemode
          FROM refunds f
          JOIN registrations r ON r.id = f.registration_id
          JOIN orders o ON o.id = f.order_id
          JOIN sessions s ON s.id = r.session_id
          JOIN sites si ON si.id = s.site_id
          JOIN course_families cf ON cf.id = s.course_family_id
          LEFT JOIN user_accounts a ON a.id = f.requested_by_user_id
          LEFT JOIN user_accounts b ON b.id = f.approved_by_user_id
         WHERE {IN_MY_CITIES} AND f.status = ANY(:states)
           AND (:q = '' OR (r.attendee_first_name || ' ' || r.attendee_last_name) ILIKE '%' || :q || '%'
                OR r.attendee_email ILIKE '%' || :q || '%')
         ORDER BY f.created_at DESC LIMIT 200
    """, uid=user.id, states=list(states), q=q)
    counts = {x["status"]: x["n"] for x in db.fetch_all(f"""
        SELECT f.status, count(*) AS n FROM refunds f JOIN registrations r ON r.id = f.registration_id
          JOIN sessions s ON s.id = r.session_id WHERE {IN_MY_CITIES} GROUP BY f.status
    """, uid=user.id)}
    status_tabs = [{"key": k, "label": label, "count": sum(counts.get(x, 0) for x in st)}
                   for k, label, st in REFUND_TABS]
    for row in rows:
        row["can_decide"] = row["status"] == "requested" and user.can("refund.approve_large", row["metro_id"])
    # Start a refund: find the paid seat by student, buyer, phone or booking number.
    who = request.args.get("who", "").strip()[:120]
    candidates = db.fetch_all(f"""
        SELECT r.public_id, r.status, r.attendee_first_name, r.attendee_last_name, r.attendee_email,
               r.amount_paid_cents, r.amount_refunded_cents, o.buyer_email,
               s.starts_at, s.ends_at, s.time_zone, cf.name AS course_name, si.name AS site_name,
               EXISTS (SELECT 1 FROM refunds f WHERE f.registration_id = r.id
                        AND f.status IN ('requested', 'approved', 'processing')) AS refund_pending
          FROM registrations r JOIN orders o ON o.id = r.order_id JOIN sessions s ON s.id = r.session_id
          JOIN sites si ON si.id = s.site_id JOIN course_families cf ON cf.id = s.course_family_id
         WHERE user_has_permission(:uid, 'refund.issue', s.metro_id)
           AND r.amount_paid_cents > r.amount_refunded_cents
           AND {_SEAT_WHERE}
         ORDER BY r.status IN ('confirmed', 'awaiting_payment', 'displaced', 'completed') DESC,
                  s.starts_at DESC, r.id DESC LIMIT 25
    """, uid=user.id, status="all", channel="", q=who, digits=re.sub(r"\D", "", who),
        ref=who.lower().replace("#", "").strip()) if len(who) >= 2 else []
    return render_template("console/refunds.html", rows=rows, tab=tab, status_tabs=status_tabs, q=q, who=who,
                           candidates=candidates, can_issue=user.has_permission("refund.issue"),
                           reasons=refunds.REASONS, threshold=refunds.threshold_cents(), **_ctx())


@bp.post("/refunds/<int:refund_id>/<decision>")
@auth.permission_required("refund.approve_large")
def refund_decide(refund_id: int, decision: str):
    if decision not in ("approve", "turn-down"):
        abort(404)
    try:
        if decision == "approve":
            refunds.approve(refund_id)
            flash(*_refund_message(refund_id, "Approved. "))
        else:
            refunds.turn_down(refund_id, request.form.get("why", ""))
            flash("Turned down. The person who asked can see it under Refunds → Failed or turned down.", "ok")
    except refunds.RefundError as exc:
        flash(str(exc), "error")
    back = request.form.get("back")
    return redirect(back if auth.is_safe_next(back) else url_for("console.refunds_list"))


# --- Refund approvals (managers) -----------------------------------------------------

@bp.get("/approvals")
@auth.permission_required("refund.approve_large")
def approvals():
    user = auth.current_user()
    rows = db.fetch_all("""
        SELECT f.id, f.amount_cents, f.reason, f.note, f.created_at, a.display_name AS requested_by,
               r.amount_paid_cents, r.public_id, r.attendee_first_name, r.attendee_last_name, s.starts_at, s.time_zone,
               cf.name AS course_name, si.name AS site_name
          FROM refunds f
          JOIN registrations r ON r.id = f.registration_id
          JOIN sessions s ON s.id = r.session_id
          JOIN sites si ON si.id = s.site_id
          JOIN course_families cf ON cf.id = s.course_family_id
          LEFT JOIN user_accounts a ON a.id = f.requested_by_user_id
         WHERE f.status = 'requested' AND f.requires_manager_approval
           AND user_has_permission(:uid, 'refund.approve_large', s.metro_id)
         ORDER BY f.created_at
    """, uid=user.id)
    return render_template("console/approvals.html", rows=rows, threshold=refunds.threshold_cents(),
                           reasons=refunds.REASONS, **_ctx())
