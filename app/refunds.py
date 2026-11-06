"""Refunds from the booking console: ask, approve or turn down, send to Stripe.

    request(registration, amount, reason, note)   customer service or a manager
    approve(refund_id) / turn_down(refund_id)      a manager (refund.approve_large)

Who needs a manager:
  * Business settings → "large refund threshold": refunds above it wait for a
    manager. While it isn't set, EVERY refund asked for by someone without the
    manager permission waits for one (the safe default until NBC decides).
  * Someone holding refund.approve_large (managers, admins) approves their own.
The database has the last word: a refund marked as needing a manager can only
be approved by an account with refund.approve_large in that class's city.

Money: one seat can't get back more than was paid for it, counting refunds
already done or on their way. An approved refund is queued to Stripe through
the outbox (same transaction), so a Stripe outage only delays it. A test
booking with no card payment (test site without Stripe) is marked refunded
at once, with no money moving.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import audit, auth, db

REASONS = {
    "customer_cancel": "Customer cancelled",
    "class_cancelled": "NBC cancelled the class",
    "duplicate": "Booked twice by mistake",
    "goodwill": "Goodwill (outside the policy)",
    "other": "Something else",
}
LIVE_STATES = ("requested", "approved", "processing")      # on their way: count against what's left


class RefundError(ValueError):
    """Why a refund can't go ahead, worded for the desk."""


def threshold_cents() -> int | None:
    value = db.fetch_value("SELECT value FROM app_settings WHERE key = 'large_refund_threshold_cents'")
    return int(value) if isinstance(value, (int, float)) else None


def refundable(conn: Connection, registration_id: int, *, lock: bool = True) -> dict:
    """What this seat paid, what's back or on its way, and the card payment to refund.
    With lock (when about to refund), locks the seat so two refunds can't take the same money."""
    row = conn.execute(text("""
        SELECT r.id, r.order_id, r.amount_paid_cents, r.amount_refunded_cents, s.metro_id, o.livemode,
               (SELECT coalesce(sum(f.amount_cents), 0) FROM refunds f
                 WHERE f.registration_id = r.id AND f.status IN ('requested', 'approved', 'processing')) AS pending
          FROM registrations r JOIN orders o ON o.id = r.order_id JOIN sessions s ON s.id = r.session_id
         WHERE r.id = :r
    """ + ("   FOR NO KEY UPDATE OF r" if lock else "")), {"r": registration_id}).mappings().one()
    payment = conn.execute(text("""
        SELECT p.id, p.amount_cents,
               p.amount_cents - coalesce((SELECT sum(f.amount_cents) FROM refunds f WHERE f.payment_id = p.id
                                           AND f.status IN ('requested', 'approved', 'processing', 'succeeded')), 0)
                 AS left_on_payment
          FROM payments p
         WHERE p.order_id = :o AND p.method = 'card' AND p.status = 'succeeded' AND p.stripe_payment_intent_id IS NOT NULL
         ORDER BY p.id LIMIT 1
    """), {"o": row["order_id"]}).mappings().first()
    left = row["amount_paid_cents"] - row["amount_refunded_cents"] - row["pending"]
    if payment:
        left = min(left, payment["left_on_payment"])
    # left < 0 means refunds asked for so far add up to more than was paid (checked again at approval)
    return {**row, "left": max(left, 0), "raw_left": left, "payment": payment}


def preview(registration_id: int) -> dict:
    """For the refund form: what's left to refund, and whether this person's refund needs a manager."""
    info = refundable(db.connection(), registration_id, lock=False)     # just reading: no lock
    return {**info, "threshold": threshold_cents()}


def needs_manager(amount: int, metro_id: int) -> bool:
    user = auth.current_user()
    if user.can("refund.approve_large", metro_id):
        return False
    limit = threshold_cents()
    return limit is None or amount > limit


def request(registration_id: int, amount_cents: int, reason: str, note: str) -> dict:
    """Ask for (or, when no manager is needed, issue) a refund. Returns {id, status}."""
    user = auth.current_user()
    if reason not in REASONS:
        raise RefundError("Choose why the money is going back.")
    if reason in ("goodwill", "other") and not note.strip():
        raise RefundError("Add a note saying why: it's outside the usual reasons.")
    with db.transaction() as conn:
        info = refundable(conn, registration_id)
        if not user.can("refund.issue", info["metro_id"]):
            raise RefundError("Your role can't refund bookings in this city.")
        if amount_cents <= 0:
            raise RefundError("Enter an amount above $0.")
        if amount_cents > info["left"]:
            raise RefundError(f"At most ${info['left'] / 100:,.2f} can go back for this seat "
                              "(what was paid, less refunds done or on their way).")
        if not info["payment"] and info["livemode"]:
            raise RefundError("There's no card payment on this order to refund (it may have been paid another "
                              "way). Refund it outside the app and add a note.")
        large = threshold_cents() is not None and amount_cents > threshold_cents()
        approver = user.can("refund.approve_large", info["metro_id"])
        wait = needs_manager(amount_cents, info["metro_id"])
        refund_id = conn.execute(text("""
            INSERT INTO refunds (order_id, payment_id, registration_id, amount_cents, reason, source, status,
                                 requires_manager_approval, requested_by_user_id, approved_by_user_id, approved_at,
                                 note)
            VALUES (:o, :p, :r, :a, :reason, 'console', :status, :big, :u, :approver,
                    CASE WHEN CAST(:approver AS bigint) IS NULL THEN NULL ELSE now() END, :note)
            RETURNING id
        """), {"o": info["order_id"], "p": info["payment"]["id"] if info["payment"] else None, "r": registration_id,
               "a": amount_cents, "reason": reason, "status": "requested" if wait else "processing",
               "big": wait or large, "u": user.id, "approver": user.id if (approver and not wait) else None,
               "note": note.strip()[:1000] or None}).scalar_one()
        audit.record(conn, "refund.request" if wait else "refund.issue", "registration", registration_id,
                     after={"refund_id": refund_id, "amount_cents": amount_cents, "reason": reason})
        status = "requested" if wait else _send(conn, refund_id)
    if status == "processing":
        status = deliver(refund_id)
    return {"id": refund_id, "status": status}


def deliver(refund_id: int) -> str:
    """Send a queued refund to Stripe now rather than at the worker's next pass.
    Returns the refund's status afterwards ('succeeded', or 'processing' / 'failed')."""
    from .integrations import outbox
    try:
        outbox.deliver_now(f"refund-{refund_id}")
    except Exception:          # stays queued; the worker retries and the Refunds page shows it
        pass
    return db.fetch_value("SELECT status FROM refunds WHERE id = :id", id=refund_id)


def _send(conn: Connection, refund_id: int) -> str:
    """Queue the refund to Stripe, or settle a test booking that never took a card. Returns the new status."""
    from .integrations.outbox import enqueue
    row = conn.execute(text("SELECT payment_id FROM refunds WHERE id = :id"), {"id": refund_id}).mappings().one()
    if row["payment_id"] is None:
        settle(conn, refund_id, stripe_refund_id=None)
        return "succeeded"
    conn.execute(text("UPDATE refunds SET status = 'processing' WHERE id = :id"), {"id": refund_id})
    enqueue(conn, "stripe", "refund", key=f"refund-{refund_id}", entity_type="refund", entity_id=refund_id,
            payload={"refund_id": refund_id})
    return "processing"


def settle(conn: Connection, refund_id: int, *, stripe_refund_id: str | None) -> None:
    """The money is back: mark the refund done and add it to the seat and the order (once)."""
    done = conn.execute(text("""
        UPDATE refunds SET status = 'succeeded', stripe_refund_id = :sr, processed_at = now()
         WHERE id = :id AND status <> 'succeeded' RETURNING amount_cents, order_id, registration_id
    """), {"id": refund_id, "sr": stripe_refund_id}).mappings().first()
    if not done:
        return
    if done["registration_id"]:
        conn.execute(text("UPDATE registrations SET amount_refunded_cents = amount_refunded_cents + :a WHERE id = :r"),
                     {"a": done["amount_cents"], "r": done["registration_id"]})
    conn.execute(text("""
        UPDATE orders SET amount_refunded_cents = amount_refunded_cents + :a,
               status = CASE WHEN amount_refunded_cents + :a >= amount_paid_cents THEN 'refunded'
                             ELSE 'partially_refunded' END
         WHERE id = :o
    """), {"a": done["amount_cents"], "o": done["order_id"]})


def _waiting(conn: Connection, refund_id: int) -> dict:
    row = conn.execute(text("""
        SELECT f.id, f.status, f.amount_cents, f.registration_id, s.metro_id
          FROM refunds f JOIN registrations r ON r.id = f.registration_id JOIN sessions s ON s.id = r.session_id
         WHERE f.id = :id FOR NO KEY UPDATE OF f
    """), {"id": refund_id}).mappings().first()
    if row is None:
        raise RefundError("That refund isn't there any more.")
    if row["status"] != "requested":
        raise RefundError("Someone has already dealt with this refund.")
    if not auth.current_user().can("refund.approve_large", row["metro_id"]):
        raise RefundError("Only a manager for this city can approve or turn down this refund.")
    return row


def approve(refund_id: int) -> str:
    user = auth.current_user()
    with db.transaction() as conn:
        row = _waiting(conn, refund_id)
        # Still within what the seat paid (other refunds may have gone through meanwhile)?
        info = refundable(conn, row["registration_id"])          # counts this refund among those on their way
        if info["raw_left"] < 0:
            raise RefundError("This refund is now more than what's left to refund on the seat; turn it down "
                              "and ask again with the right amount.")
        conn.execute(text("""UPDATE refunds SET status = 'approved', approved_by_user_id = :u, approved_at = now()
                              WHERE id = :id"""), {"u": user.id, "id": refund_id})
        audit.record(conn, "refund.approve", "registration", row["registration_id"],
                     after={"refund_id": refund_id, "amount_cents": row["amount_cents"]})
        status = _send(conn, refund_id)
    return deliver(refund_id) if status == "processing" else status


def turn_down(refund_id: int, why: str) -> None:
    with db.transaction() as conn:
        row = _waiting(conn, refund_id)
        conn.execute(text("""UPDATE refunds SET status = 'rejected', failure_reason = :w, processed_at = now()
                              WHERE id = :id"""), {"id": refund_id, "w": why.strip()[:500] or None})
        audit.record(conn, "refund.turn_down", "registration", row["registration_id"],
                     after={"refund_id": refund_id, "amount_cents": row["amount_cents"]}, reason=why.strip() or None)
