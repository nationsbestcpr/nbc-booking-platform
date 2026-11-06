"""The booking console: the desk for customer service and managers (/console).

Same sign-in page as everyone. Accounts whose roles open the console
(roles.dashboard_key 'customer_service' or 'manager') land here after signing
in; admins reach it from the admin sidebar. What each person sees is set by
permissions (db/seed.sql), and every list only shows classes in the cities
their role covers (user_has_permission(..., metro_id) in SQL).

This is the skeleton: finding students and bookings, a booking's full
picture (seats, payments, refunds, transfers, GHL, notes) and notes work.
Transfer, cancel, refund, phone and invoice bookings and refund approvals have
their pages, which say what each will do, but don't change anything yet.
"""

from __future__ import annotations

import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from sqlalchemy import text

from .. import areas, audit, auth, classes, db, refunds

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

# The actions on a booking, with the permission each needs. Their pages are placeholders for now.
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
               s.id AS session_id, s.metro_id, s.starts_at, s.ends_at, s.time_zone, s.status AS class_status,
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
        SELECT f.amount_cents, f.reason, f.source, f.status, f.requires_manager_approval, f.stripe_refund_id,
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
    actions = [{"key": k, "label": label, "allowed": user.can(perm, r["metro_id"])}
               for k, (label, perm) in ACTIONS.items()]
    return render_template("console/booking.html", r=r, seats=seats, payments=payments, refunds=refunds,
                           transfers=transfers, notes=notes, history=history, actions=actions,
                           show_money=show_money, can_note=user.can("student.note", r["metro_id"]), **_ctx())


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
    """Transfer and cancel: the page each will have (not built yet). Refund: the real form."""
    if action not in ACTIONS:
        abort(404)
    user = auth.current_user()
    r = _booking_or_404(public_id)
    if not user.can(ACTIONS[action][1], r["metro_id"]):
        abort(403)
    if action == "refund":
        return _refund_page(r)
    policy = db.fetch_one("""
        SELECT version, title, rules, approved_by FROM policy_versions
         WHERE kind = 'refund_transfer_rules' AND effective_from <= now()
           AND (effective_to IS NULL OR effective_to > now())
         ORDER BY effective_from DESC LIMIT 1
    """)
    threshold = db.fetch_value("SELECT value FROM app_settings WHERE key = 'large_refund_threshold_cents'")
    return render_template("console/action.html", r=r, action=action, title=ACTIONS[action][0],
                           policy=policy, threshold=threshold, **_ctx())


# --- Book by phone or invoice --------------------------------------------------------

@bp.get("/new")
@auth.any_permission_required("booking.create_phone", "booking.create_invoice")
def new_booking():
    """Pick the class first (the same search as the home page), then the caller's details."""
    found = classes.search(classes.filters_from(request.args))
    return render_template("console/new_booking.html", found=found, **_ctx())


@bp.get("/new/<public_id>")
@auth.any_permission_required("booking.create_phone", "booking.create_invoice")
def new_booking_class(public_id: str):
    from ..integrations import checkout
    s = checkout.bookable_session(public_id)
    if not s:
        abort(404)
    return render_template("console/new_booking_class.html", s=s, **_ctx())


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
    flash({"requested": f"Refund of ${amount / 100:,.2f} sent to a manager to approve. It shows under Refunds "
                        "until they do.",
           "processing": f"Refund of ${amount / 100:,.2f} is on its way to the card through Stripe.",
           "succeeded": f"Refund of ${amount / 100:,.2f} recorded (test booking: no card was charged)."}
          [done["status"]], "ok")
    return redirect(url_for("console.find_booking", public_id=public_id))


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
    return render_template("console/refunds.html", rows=rows, tab=tab, status_tabs=status_tabs, q=q,
                           reasons=refunds.REASONS, threshold=refunds.threshold_cents(), **_ctx())


@bp.post("/refunds/<int:refund_id>/<decision>")
@auth.permission_required("refund.approve_large")
def refund_decide(refund_id: int, decision: str):
    if decision not in ("approve", "turn-down"):
        abort(404)
    try:
        if decision == "approve":
            status = refunds.approve(refund_id)
            flash("Approved. " + ("The refund is on its way to the card through Stripe." if status == "processing"
                                  else "Recorded (test booking: no card was charged)."), "ok")
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
