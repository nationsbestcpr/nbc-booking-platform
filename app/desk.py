"""Customer service actions on a booked seat (launch hub 9b).

    move(...)          "Move to another class": one step. The seat is checked in the new class, the
                       price difference is charged (a Stripe page the buyer gets by text and email) or
                       credited (a refund), GHL's opportunity gets the new date and place, and the
                       attendee gets the "class changed" message.
    cancel(...)        "Cancel the booking": per policy, with the money back through Stripe when the
                       desk refunds; GHL's stage goes to Cancelled.
    move_class(...)    A cancelled class: its whole roster moved to another class together
                       (all or nothing), with nothing charged; credits are left to the Refund step.

The written refund and transfer policy is still open (hub open item 2). When
it is loaded (policy_versions, kind 'refund_transfer_rules'), the pages
suggest the fee and the refund from its rules:

    {"free_transfers": 1, "free_transfer_hours_before": 24, "transfer_fee_cents": 2500,
     "refund_full_days_before": 7, "forfeit_hours_before": 24}

Until then the desk decides, and every decision is logged. The database has
the last word on seats (no class is oversold; an extra seat over the limit
needs a manager), the same-course rule, and refunds above the threshold.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from . import audit, auth, db, refunds

LIVE = ("confirmed", "awaiting_payment", "displaced")       # seats the desk can move or cancel

MOVE_REASONS = {"customer_request": "The customer asked", "class_cancelled": "NBC cancelled the class",
                "class_rescheduled": "NBC moved the class", "admin_correction": "Fixing a mistake"}
CANCEL_REASONS = {
    "customer_request": "The customer asked (with notice)",
    "late_notice": "Less than 24 hours' notice (fee kept)",
    "late_arrival": "Arrived late, couldn't take the class (fee kept)",
    "duplicate": "Booked twice by mistake",
    "nbc_low_enrollment": "NBC cancelled: not enough students",
    "nbc_weather": "NBC cancelled: weather",
    "nbc_other": "NBC cancelled: another reason",
    "admin": "Fixing a mistake",
}
FORFEIT = ("late_notice", "late_arrival")                   # the seat is cancelled and the fee is kept


class DeskError(ValueError):
    """Why the action can't go ahead, worded for the desk."""


def policy() -> dict | None:
    """The refund and transfer rules in force, or None while NBC's policy isn't loaded yet."""
    return db.fetch_one("""
        SELECT id, version, title, rules, approved_by FROM policy_versions
         WHERE kind = 'refund_transfer_rules' AND effective_from <= now()
           AND (effective_to IS NULL OR effective_to > now())
         ORDER BY effective_from DESC LIMIT 1
    """)


def suggestions(r: dict) -> dict:
    """What the loaded policy says for this seat right now: a transfer fee and a refund amount, with why.
    Empty while no policy is loaded."""
    p = policy()
    rules = (p or {}).get("rules") or {}
    out: dict = {"policy": p, "fee_cents": None, "refund_cents": None, "why": []}
    if not p:
        return out
    hours = float(r["hours_to_class"] or 0)
    free = rules.get("free_transfers")
    free_hours = rules.get("free_transfer_hours_before")
    fee = rules.get("transfer_fee_cents")
    if fee is not None:
        within = (free is None or r["transfers_used"] < free) and (free_hours is None or hours >= free_hours)
        out["fee_cents"] = 0 if within else int(fee)
        out["why"].append("Free transfer under the policy." if within else
                          f"Transfer fee: {'no free transfers left' if free is not None and r['transfers_used'] >= free else f'less than {free_hours} hours before class'}.")
    full_days = rules.get("refund_full_days_before")
    forfeit_hours = rules.get("forfeit_hours_before")
    paid = r["amount_paid_cents"] - r["amount_refunded_cents"]
    if forfeit_hours is not None and hours < forfeit_hours:
        out["refund_cents"] = 0
        out["why"].append(f"Less than {forfeit_hours} hours' notice: no refund under the policy.")
    elif full_days is not None:
        out["refund_cents"] = paid if hours >= full_days * 24 else 0
        out["why"].append(f"{'At least' if hours >= full_days * 24 else 'Less than'} {full_days} days before class.")
    return out


def targets(r: dict, uid: int, *, q: str = "") -> list[dict]:
    """Classes this seat can move to: the same course, upcoming, in cities this person can move bookings in."""
    return db.fetch_all("""
        SELECT s.id, s.public_id, s.starts_at, s.ends_at, s.time_zone, s.status, si.name AS site_name,
               m.name AS city_name, av.seats_left, av.seat_limit, av.waitlist_count,
               (SELECT json_agg(json_build_object('id', p.id, 'short_name', p.short_name, 'name', p.name,
                                                  'price_cents', sp.price_cents) ORDER BY p.sort_order)
                  FROM session_products sp JOIN products p ON p.id = sp.product_id
                 WHERE sp.session_id = s.id AND sp.is_offered AND p.is_active) AS offers
          FROM sessions s
          JOIN sites si ON si.id = s.site_id
          JOIN metros m ON m.id = s.metro_id
          JOIN session_availability av ON av.session_id = s.id
         WHERE s.course_family_id = (SELECT course_family_id FROM sessions WHERE id = :from_s)
           AND s.id <> :from_s AND s.status = 'scheduled' AND s.starts_at > now() AND m.booking_system = 'engine'
           AND user_has_permission(:uid, 'booking.transfer', s.metro_id)
           AND (:q = '' OR si.name ILIKE '%' || :q || '%' OR m.name ILIKE '%' || :q || '%')
         ORDER BY (s.metro_id = (SELECT metro_id FROM sessions WHERE id = :from_s)) DESC, s.starts_at
         LIMIT 40
    """, from_s=r["session_id"], uid=uid, q=q)


def _seat(conn: Connection, registration_id: int) -> dict:
    row = conn.execute(text("""
        SELECT r.id, r.public_id, r.status, r.order_id, r.session_id, r.product_id, r.unit_price_cents,
               r.discount_cents, r.amount_paid_cents, r.amount_refunded_cents, r.attendee_email, s.metro_id,
               o.status AS order_status, o.buyer_first_name, o.buyer_last_name, o.buyer_email, o.buyer_phone_e164,
               o.livemode
          FROM registrations r JOIN sessions s ON s.id = r.session_id JOIN orders o ON o.id = r.order_id
         WHERE r.id = :r FOR NO KEY UPDATE OF r
    """), {"r": registration_id}).mappings().first()
    if row is None:
        raise DeskError("That booking isn't there any more.")
    return dict(row)


def _db_message(exc: DBAPIError) -> str | None:
    msg = str(exc.orig)
    if "is full" in msg:
        return "The new class has no seat left. A manager can add an extra seat over the limit, with a reason."
    if "not open for booking" in msg:
        return "The new class isn't open for booking (cancelled, already started, or its city isn't on this system)."
    if "same course within" in msg:
        return "This person already has another class of this course within the same-course limit."
    if "registrations_one_seat_per_person" in msg:
        return "This person already has a seat in that class."
    if "lacks permission class.capacity_override" in msg:
        return "Only a manager can add a seat over the class limit."
    return None


def move(registration_id: int, *, to_session_id: int, to_product_id: int, reason: str, fee_cents: int,
         note: str, extra_seat_reason: str = "", base_url: str = "") -> dict:
    """Move one seat to another class in one step. Returns {transfer_id, charge_cents, credit_cents,
    payment_url, refund_status}."""
    user = auth.current_user()
    if reason not in MOVE_REASONS:
        raise DeskError("Choose why the seat is moving.")
    if fee_cents < 0:
        raise DeskError("A fee can't be below $0.")
    if reason == "admin_correction" and not note.strip():
        raise DeskError("Add a note saying what's being fixed.")
    try:
        with db.transaction() as conn:
            r = _seat(conn, registration_id)
            if r["status"] not in LIVE:
                raise DeskError("Only a booked seat (or one whose class NBC cancelled) can be moved.")
            if not user.can("booking.transfer", r["metro_id"]):
                raise DeskError("Your role can't move bookings in this city.")
            target = conn.execute(text("""
                SELECT s.id, s.metro_id, s.course_family_id, s.status, sp.price_cents,
                       (SELECT course_family_id FROM sessions WHERE id = :from_s) AS from_family
                  FROM sessions s JOIN session_products sp ON sp.session_id = s.id AND sp.product_id = :p
                 WHERE s.id = :s AND sp.is_offered
            """), {"s": to_session_id, "p": to_product_id, "from_s": r["session_id"]}).mappings().first()
            if target is None:
                raise DeskError("Pick a class and one of the courses it offers.")
            if target["course_family_id"] != target["from_family"]:
                raise DeskError("A seat can only move to a class of the same course.")
            if to_session_id == r["session_id"] and to_product_id == r["product_id"]:
                raise DeskError("That's the class this seat is already in.")
            if not user.can("booking.transfer", target["metro_id"]):
                raise DeskError("Your role can't move bookings into that city.")
            if extra_seat_reason.strip():
                # Manager-only: one seat over the limit, with a reason (the database checks the permission).
                conn.execute(text("""
                    INSERT INTO seat_overrides (session_id, seats, reason, approved_by_user_id)
                    VALUES (:s, 1, :why, :u)
                """), {"s": to_session_id, "why": extra_seat_reason.strip()[:500], "u": user.id})
            old_price, new_price = r["unit_price_cents"], target["price_cents"]
            paid = r["amount_paid_cents"] > 0
            difference = new_price - old_price if paid else 0
            net = difference + fee_cents if paid else 0
            new_status = r["status"]
            if r["status"] == "displaced":
                new_status = "awaiting_payment" if r["order_status"] == "awaiting_payment" else "confirmed"
            conn.execute(text("""
                UPDATE registrations SET session_id = :s, product_id = :p, unit_price_cents = :price,
                       discount_cents = LEAST(discount_cents, :price), status = :st,
                       confirmed_at = CASE WHEN :st = 'confirmed' THEN COALESCE(confirmed_at, now()) ELSE confirmed_at END,
                       checked_in_at = NULL
                 WHERE id = :r
            """), {"s": to_session_id, "p": to_product_id, "price": new_price, "st": new_status, "r": r["id"]})
            p = policy()
            transfer_id = conn.execute(text("""
                INSERT INTO registration_transfers (registration_id, from_session_id, from_product_id, to_session_id,
                                                    to_product_id, reason, via, price_difference_cents, fee_cents,
                                                    policy_rule, policy_version_id, performed_by_user_id)
                VALUES (:r, :fs, :fp, :ts, :tp, :reason, 'console', :diff, :fee, :rule, :pv, :u)
                RETURNING id
            """), {"r": r["id"], "fs": r["session_id"], "fp": r["product_id"], "ts": to_session_id,
                   "tp": to_product_id, "reason": reason, "diff": difference, "fee": fee_cents,
                   "rule": "desk_decision" if not p else "policy", "pv": p["id"] if p else None,
                   "u": user.id}).scalar_one()
            charge_order = None
            if net > 0:
                due_on = conn.execute(text("SELECT local_date FROM sessions WHERE id = :s"),
                                      {"s": to_session_id}).scalar()
                charge_order = conn.execute(text("""
                    INSERT INTO orders (channel, status, buyer_first_name, buyer_last_name, buyer_email,
                                        buyer_phone_e164, invoice_due_on, subtotal_cents, total_cents, livemode,
                                        created_by_user_id, parent_order_id)
                    VALUES ('transfer_adjustment', 'awaiting_payment', :fn, :ln, :em, :ph, :due, :t, :t, :live,
                            :u, :parent)
                    RETURNING id
                """), {"fn": r["buyer_first_name"], "ln": r["buyer_last_name"], "em": r["buyer_email"],
                       "ph": r["buyer_phone_e164"], "due": due_on, "t": net, "live": r["livemode"], "u": user.id,
                       "parent": r["order_id"]}).scalar_one()
                conn.execute(text("UPDATE registration_transfers SET settlement_order_id = :o WHERE id = :t"),
                             {"o": charge_order, "t": transfer_id})
            refund_id = None
            if net < 0:
                refund_id = _credit(conn, r, -net, transfer_id, note)
            from .integrations.outbox import enqueue
            enqueue(conn, "ghl", "move_registration", key=f"ghl-move-{transfer_id}", entity_type="registration",
                    entity_id=r["id"], payload={"registration_id": r["id"]})
            audit.record(conn, "booking.transfer", "registration", r["id"],
                         before={"session_id": r["session_id"], "product_id": r["product_id"],
                                 "unit_price_cents": old_price},
                         after={"session_id": to_session_id, "product_id": to_product_id, "unit_price_cents": new_price,
                                "fee_cents": fee_cents, "charge_cents": max(net, 0), "credit_cents": max(-net, 0),
                                "transfer_id": transfer_id, "extra_seat": bool(extra_seat_reason.strip())},
                         reason=note.strip()[:500] or MOVE_REASONS[reason])
    except DBAPIError as exc:
        message = _db_message(exc)
        if message:
            raise DeskError(message) from exc
        raise
    _waitlist_after(r["session_id"])
    out = {"transfer_id": transfer_id, "charge_cents": max(net, 0), "credit_cents": max(-net, 0),
           "payment_url": None, "refund_status": None}
    if refund_id:
        out["refund_status"] = refunds.deliver(refund_id) if db.fetch_value(
            "SELECT status FROM refunds WHERE id = :id", id=refund_id) == "processing" else "succeeded"
    if charge_order:
        out["payment_url"] = send_payment_link(charge_order, base_url)
    return out


def _credit(conn: Connection, r: dict, amount: int, transfer_id: int, note: str) -> int:
    """A cheaper class: the difference goes back to the card (within what the seat paid). A credit from
    a move is within policy, so it needs no manager (hub 9b, Nov 6)."""
    info = refunds.refundable(conn, r["id"])
    amount = min(amount, info["left"])
    if amount <= 0:
        return None
    refund_id = conn.execute(text("""
        INSERT INTO refunds (order_id, payment_id, registration_id, transfer_id, amount_cents, reason, source, status,
                             requires_manager_approval, requested_by_user_id, note)
        VALUES (:o, :p, :r, :t, :a, 'transfer_credit', 'console', 'processing', false, :u, :note)
        RETURNING id
    """), {"o": r["order_id"], "p": info["payment"]["id"] if info["payment"] else None, "r": r["id"],
           "t": transfer_id, "a": amount, "u": auth.current_user().id, "note": note.strip()[:1000] or None}
    ).scalar_one()
    conn.execute(text("UPDATE registration_transfers SET settlement_refund_id = :f WHERE id = :t"),
                 {"f": refund_id, "t": transfer_id})
    refunds._send(conn, refund_id)
    return refund_id


def send_payment_link(order_id: int, base_url: str) -> str | None:
    """Open the Stripe page for a price difference and send it to the buyer through GHL.
    Returns the link (None on the test site without Stripe, where it's recorded as paid at once)."""
    from .integrations import checkout
    from .integrations.outbox import enqueue
    url = checkout.open_difference_page(order_id, base_url)
    if url:
        amount = db.fetch_value("SELECT total_cents FROM orders WHERE id = :o", o=order_id)
        with db.transaction() as conn:
            enqueue(conn, "ghl", "send_payment_link", key=f"ghl-payment-link-{order_id}-{uuid.uuid4().hex[:8]}",
                    entity_type="order", entity_id=order_id,
                    payload={"order_id": order_id, "url": url, "amount_cents": amount})
    return url


def cancel(registration_id: int, *, reason: str, refund_cents: int, note: str) -> dict:
    """Cancel one seat. The seat is freed at once (to the waiting list first), GHL's stage goes to
    Cancelled, and any refund goes through the usual refund rules. Returns {status, refund}."""
    user = auth.current_user()
    if reason not in CANCEL_REASONS:
        raise DeskError("Choose why the booking is being cancelled.")
    if refund_cents < 0:
        raise DeskError("A refund can't be below $0.")
    if reason in ("admin", "nbc_other") and not note.strip():
        raise DeskError("Add a note saying why.")
    with db.transaction() as conn:
        r = _seat(conn, registration_id)
        if r["status"] not in LIVE:
            raise DeskError("This seat isn't booked any more, so there's nothing to cancel.")
        if not user.can("booking.cancel", r["metro_id"]):
            raise DeskError("Your role can't cancel bookings in this city.")
        if refund_cents:
            if not user.can("refund.issue", r["metro_id"]):
                raise DeskError("Your role can't refund. Cancel without a refund and ask someone who can.")
            left = refunds.refundable(conn, r["id"], lock=False)["left"]
            if refund_cents > left:
                raise DeskError(f"At most ${left / 100:,.2f} can go back for this seat.")
        status = "forfeited" if reason in FORFEIT else "cancelled"
        conn.execute(text("""
            UPDATE registrations SET status = :st, cancelled_at = now(), cancel_reason_code = :code,
                   cancel_reason = :note, checked_in_at = NULL
             WHERE id = :r
        """), {"st": status, "code": reason, "note": note.strip()[:500] or None, "r": r["id"]})
        # An invoice with nothing left on it is cancelled too (billing voids the Stripe invoice).
        conn.execute(text("""
            UPDATE orders SET status = 'cancelled', cancelled_at = now()
             WHERE id = :o AND status = 'awaiting_payment'
               AND NOT EXISTS (SELECT 1 FROM registrations x WHERE x.order_id = :o
                                  AND x.status IN ('awaiting_payment', 'confirmed', 'completed', 'no_show', 'displaced'))
        """), {"o": r["order_id"]})
        from .integrations.outbox import enqueue
        enqueue(conn, "ghl", "sync_registration", key=f"ghl-registration-{r['id']}-cancelled-{uuid.uuid4().hex[:8]}",
                entity_type="registration", entity_id=r["id"], payload={"registration_id": r["id"]})
        audit.record(conn, "booking.cancel", "registration", r["id"], before={"status": r["status"]},
                     after={"status": status, "reason": reason, "refund_cents": refund_cents},
                     reason=note.strip()[:500] or CANCEL_REASONS[reason])
    _waitlist_after(r["session_id"])
    done = {"status": status, "refund": None}
    if refund_cents:
        refund_reason = "class_cancelled" if reason.startswith("nbc_") else \
            ("duplicate" if reason == "duplicate" else "customer_cancel")
        done["refund"] = refunds.request(r["id"], refund_cents, refund_reason, note)
    return done


def move_class(from_session_id: int, *, to_session_id: int, note: str) -> dict:
    """Move every booked student of a cancelled class to another class of the same course, together:
    either all fit or none move. Each keeps their course (A&P / Adult) when the new class offers it.
    Nothing is charged (NBC moved them); a cheaper class's credit is left to the Refund step."""
    user = auth.current_user()
    batch = uuid.uuid4()
    try:
        with db.transaction() as conn:
            src = conn.execute(text("SELECT id, metro_id, status, course_family_id FROM sessions WHERE id = :s"),
                               {"s": from_session_id}).mappings().one()
            if not user.can("booking.transfer_bulk", src["metro_id"]):
                raise DeskError("Your role can't move a whole class in this city.")
            if src["status"] != "cancelled":
                raise DeskError("Only a cancelled class's students can be moved together. Move students one by one.")
            dst = conn.execute(text("SELECT id, metro_id, course_family_id FROM sessions WHERE id = :s"),
                               {"s": to_session_id}).mappings().first()
            if dst is None or dst["course_family_id"] != src["course_family_id"] or dst["id"] == src["id"]:
                raise DeskError("Pick another class of the same course.")
            if not user.can("booking.transfer_bulk", dst["metro_id"]):
                raise DeskError("Your role can't move bookings into that city.")
            offers = {row["product_id"]: row["price_cents"] for row in conn.execute(text("""
                SELECT product_id, price_cents FROM session_products WHERE session_id = :s AND is_offered
                 ORDER BY price_cents DESC"""), {"s": to_session_id}).mappings()}
            if not offers:
                raise DeskError("The new class isn't selling any course.")
            seats = conn.execute(text("""
                SELECT r.id, r.status, r.product_id, r.unit_price_cents, o.status AS order_status
                  FROM registrations r JOIN orders o ON o.id = r.order_id
                 WHERE r.session_id = :s AND r.status IN ('confirmed', 'awaiting_payment', 'displaced')
                 ORDER BY r.id FOR NO KEY UPDATE OF r
            """), {"s": from_session_id}).mappings().all()
            if not seats:
                raise DeskError("No booked students left in this class.")
            fallback = next(iter(offers))
            for r in seats:
                product = r["product_id"] if r["product_id"] in offers else fallback
                status = r["status"] if r["status"] != "displaced" else \
                    ("awaiting_payment" if r["order_status"] == "awaiting_payment" else "confirmed")
                conn.execute(text("""
                    UPDATE registrations SET session_id = :s, product_id = :p, status = :st, checked_in_at = NULL
                     WHERE id = :r
                """), {"s": to_session_id, "p": product, "st": status, "r": r["id"]})
                tid = conn.execute(text("""
                    INSERT INTO registration_transfers (registration_id, from_session_id, from_product_id, to_session_id,
                                                        to_product_id, reason, via, bulk_batch_id,
                                                        price_difference_cents, policy_rule, performed_by_user_id)
                    VALUES (:r, :fs, :fp, :ts, :tp, 'class_cancelled', 'bulk', :b, :diff, 'nbc_cancelled_class', :u)
                    RETURNING id
                """), {"r": r["id"], "fs": from_session_id, "fp": r["product_id"], "ts": to_session_id,
                       "tp": product, "b": batch, "diff": offers[product] - r["unit_price_cents"],
                       "u": user.id}).scalar_one()
                from .integrations.outbox import enqueue
                enqueue(conn, "ghl", "move_registration", key=f"ghl-move-{tid}", entity_type="registration",
                        entity_id=r["id"], payload={"registration_id": r["id"]})
            audit.record(conn, "booking.transfer_bulk", "session", from_session_id,
                         after={"to_session_id": to_session_id, "students": len(seats), "batch": str(batch)},
                         reason=note.strip()[:500] or "NBC cancelled the class")
    except DBAPIError as exc:
        message = _db_message(exc)
        if message:
            raise DeskError(message.replace("The new class has no seat left.",
                                            "The new class doesn't have a seat for every student.")) from exc
        raise
    return {"moved": len(seats), "batch": str(batch)}


def _waitlist_after(session_id: int) -> None:
    """A seat came free: offer it to the waiting list now rather than at the worker's next pass."""
    from . import waitlist
    try:
        waitlist.offer_free_seats(session_id)
    except Exception:          # the worker tries again on its next pass
        pass
