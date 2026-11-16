"""The waiting list (launch hub 9c, decided Oct 12; the rules doc wins where they differ).

    join(...)              A full class's page shows "Join the waiting list": name, email, phone,
                           SMS consent, A&P or Adult, how many people. No payment. GHL gets an
                           opportunity in the Waitlisted stage.
    offer_free_seats()     A freed seat is held for the next person in line who fits (a group is
                           offered only when the whole group fits). The seat is held from that
                           moment, so it never goes back on public sale meanwhile.
    dispatch()             Offers go out only 8 AM – 6 PM in the class's local time: GHL texts and
                           emails a pay link, and the seat stays held for 2 hours.
    expire()               Not paid in time: "Offer expired". They keep their place but aren't offered
                           again automatically (staff can re-offer); the seat goes to the next person.
                           A checkout started before the 2 hours end may finish.
    close_at_cutoff()      At the cut-off (24 hours before class at a one-instructor location; 8 AM the
                           day before at a co-instructor location, Saturday 8 AM for Sunday and Monday
                           classes) anyone still waiting gets the "no seat opened" notice.
    tick()                 All of the above, run by the worker on every pass.

Paying for an offered seat uses the offer's own hold: Stripe Checkout as for
any booking, then waiting -> booked, and GHL's opportunity moves Waitlisted -> Pending.

Open (rules doc §10, to confirm before the Jan 11 build): what happens to a
seat freed after the cut-off (here: it goes back on public sale), re-offers
after "Offer expired" (here: only by staff), and whether a group that doesn't
fit blocks smaller entries behind it (here: it doesn't; the next person who
fits is offered the seat).
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError

from . import audit, auth, db

MAX_PARTY = 12


class WaitlistError(ValueError):
    """Shown to the person as is."""


def _settings(conn: Connection) -> dict:
    rows = {r["key"]: r["value"] for r in conn.execute(text("""
        SELECT key, value FROM app_settings
         WHERE key IN ('waitlist_offer_hours', 'waitlist_offer_window_local', 'waitlist_cutoff_one_instructor_hours',
                       'waitlist_cutoff_co_instructor', 'checkout_hold_minutes')
    """)).mappings()}
    window = rows.get("waitlist_offer_window_local") or {}
    co = rows.get("waitlist_cutoff_co_instructor") or {}
    return {"offer_hours": float(rows.get("waitlist_offer_hours") or 2),
            "window": (_hm(window.get("start", "08:00")), _hm(window.get("end", "18:00"))),
            "one_hours": float(rows.get("waitlist_cutoff_one_instructor_hours") or 24),
            "co_time": _hm(co.get("local_time", "08:00")), "co_days": int(co.get("days_before", 1)),
            "co_saturday": bool(co.get("sun_mon_use_saturday", True)),
            "checkout_minutes": int(rows.get("checkout_hold_minutes") or 15)}


def _hm(v: str) -> time:
    h, m = (v or "08:00").split(":")
    return time(int(h), int(m))


def cutoff(starts_at: datetime, time_zone: str, location_type: str, cfg: dict) -> datetime:
    """When the waiting list closes for this class (hub 9c table)."""
    if location_type != "co_instructor":
        return starts_at - timedelta(hours=cfg["one_hours"])
    zone = ZoneInfo(time_zone)
    day = starts_at.astimezone(zone).date() - timedelta(days=cfg["co_days"])
    if cfg["co_saturday"] and starts_at.astimezone(zone).weekday() in (6, 0):      # Sunday, Monday
        day = starts_at.astimezone(zone).date() - timedelta(days=(starts_at.astimezone(zone).weekday() - 5) % 7)
    return datetime.combine(day, cfg["co_time"], tzinfo=zone).astimezone(timezone.utc)


def send_time(now: datetime, time_zone: str, cfg: dict) -> datetime:
    """The first moment at or after now inside the offer window (class's local time)."""
    zone = ZoneInfo(time_zone)
    local = now.astimezone(zone)
    start, end = cfg["window"]
    if local.time() < start:
        return datetime.combine(local.date(), start, tzinfo=zone).astimezone(timezone.utc)
    if local.time() >= end:
        return datetime.combine(local.date() + timedelta(days=1), start, tzinfo=zone).astimezone(timezone.utc)
    return now


def _now(conn: Connection) -> datetime:
    return conn.execute(text("SELECT now()")).scalar()


def base_url() -> str:
    return current_app.config.get("PUBLIC_BASE_URL", "").rstrip("/") or "http://localhost:5000"


def terms(s: dict) -> dict:
    """What the join form shows: the 2-hour window and the "Offer expired" rule (hub 9c)."""
    with db.engine().connect() as conn:
        cfg = _settings(conn)
    closes = cutoff(s["starts_at"], s["time_zone"], s["location_type"], cfg)
    start, end = cfg["window"]
    return {"hours": f"{cfg['offer_hours']:g}", "closes": closes,
            "window": f"{start.strftime('%-I %p')} – {end.strftime('%-I %p')}",
            "open": datetime.now(timezone.utc) < closes and s["on_sale"] and s["seats_left"] <= 0
            and (s["waitlist_count"] or 0) < s["waitlist_limit"],
            "room": max(s["waitlist_limit"] - (s["waitlist_count"] or 0), 0)}


# --- Joining -----------------------------------------------------------------------------

def join(session_public_id: str, *, first: str, last: str, email: str, phone: str | None, product_id: int,
         party_size: int, sms_consent: bool, ip: str | None = None, user_agent: str | None = None) -> dict:
    from .integrations import checkout
    from .integrations.outbox import enqueue
    s = checkout.bookable_session(session_public_id)
    if not s:
        raise WaitlistError("That class isn't there any more.")
    if not (first and last and checkout.EMAIL.match(email or "")):
        raise WaitlistError("Enter your first name, last name and email.")
    if not phone:
        raise WaitlistError("Enter a mobile phone number: offers come by text as well as email.")
    if not any(o["id"] == product_id for o in s["offers"]):
        raise WaitlistError("Choose the course you'd take.")
    if not 1 <= party_size <= MAX_PARTY:
        raise WaitlistError(f"Join for 1 to {MAX_PARTY} people.")
    t = terms(s)
    if s["on_sale"] and s["seats_left"] > 0:
        raise WaitlistError("Seats just opened in this class: book one now instead.")
    if not t["open"]:
        raise WaitlistError("The waiting list for this class is closed. Pick another date.")
    if party_size > t["room"]:
        raise WaitlistError(f"The waiting list has room for {t['room']} more "
                            f"{'person' if t['room'] == 1 else 'people'}. Pick another date.")
    try:
        with db.transaction() as conn:
            terms_id = conn.execute(text("""
                SELECT id FROM policy_versions WHERE kind = 'waitlist_terms' AND effective_from <= now()
                   AND (effective_to IS NULL OR effective_to > now()) ORDER BY effective_from DESC LIMIT 1
            """)).scalar()
            entry = conn.execute(text("""
                INSERT INTO waitlist_entries (session_id, preferred_product_id, first_name, last_name, email,
                                              phone_e164, party_size, terms_version_id)
                VALUES (:s, :p, :fn, :ln, :em, :ph, :n, :terms)
                RETURNING id, public_id
            """), {"s": s["id"], "p": product_id, "fn": first, "ln": last, "em": email.lower(), "ph": phone,
                   "n": party_size, "terms": terms_id}).mappings().one()
            consent = conn.execute(text("""
                INSERT INTO consent_records (email, phone_e164, kind, granted, source, waitlist_entry_id, ip, user_agent)
                VALUES (:em, :ph, 'sms', :ok, 'waitlist_form', :w, CAST(:ip AS inet), :ua)
                RETURNING id
            """), {"em": email.lower(), "ph": phone, "ok": sms_consent, "w": entry["id"], "ip": ip,
                   "ua": (user_agent or "")[:500] or None}).scalar_one()
            conn.execute(text("UPDATE waitlist_entries SET sms_consent_id = :c WHERE id = :w"),
                         {"c": consent, "w": entry["id"]})
            enqueue(conn, "ghl", "sync_waitlist_entry", key=f"ghl-waitlist-{entry['id']}", entity_type="waitlist_entry",
                    entity_id=entry["id"], payload={"entry_id": entry["id"]})
    except IntegrityError as exc:
        if "waitlist_entries_one_live" in str(exc.orig):
            raise WaitlistError("You're already on the waiting list for this class.") from exc
        raise
    except DBAPIError as exc:
        if "waitlist" in str(exc.orig) and "is full" in str(exc.orig):
            raise WaitlistError("The waiting list for this class just filled up. Pick another date.") from exc
        raise
    position = db.fetch_value("SELECT position FROM waitlist_queue WHERE id = :id", id=entry["id"])
    return {"id": entry["id"], "public_id": str(entry["public_id"]), "position": position, "closes": t["closes"]}


# --- Offers --------------------------------------------------------------------------------

def _open_sessions(conn: Connection, session_id: int | None) -> list[dict]:
    """Classes that could offer a seat to someone waiting now."""
    return [dict(r) for r in conn.execute(text("""
        SELECT s.id, s.starts_at, s.time_zone, s.location_type
          FROM sessions s JOIN metros m ON m.id = s.metro_id
         WHERE s.status = 'scheduled' AND s.starts_at > now() AND m.booking_system = 'engine'
           AND (CAST(:sid AS bigint) IS NULL OR s.id = :sid)
           AND EXISTS (SELECT 1 FROM waitlist_entries w WHERE w.session_id = s.id AND w.status = 'waiting')
           AND session_seats_available(s.id) > 0
         ORDER BY s.starts_at LIMIT 200
    """), {"sid": session_id}).mappings()]


def offer_free_seats(session_id: int | None = None) -> int:
    """Hold each free seat for the next person waiting who fits, and queue their offer."""
    made = 0
    with db.engine().connect() as c:
        sessions = _open_sessions(c, session_id)
    for s in sessions:
        with db.transaction() as conn:
            cfg = _settings(conn)
            now = _now(conn)
            if now >= cutoff(s["starts_at"], s["time_zone"], s["location_type"], cfg):
                continue
            conn.execute(text("SELECT 1 FROM sessions WHERE id = :s FOR NO KEY UPDATE"), {"s": s["id"]})
            free = conn.execute(text("SELECT session_seats_available(:s)"), {"s": s["id"]}).scalar()
            for e in conn.execute(text("""
                SELECT id, party_size FROM waitlist_entries
                 WHERE session_id = :s AND status = 'waiting' ORDER BY joined_at, id FOR NO KEY UPDATE
            """), {"s": s["id"]}).mappings().all():
                if free <= 0:
                    break
                if e["party_size"] <= free:
                    _make_offer(conn, s, e, cfg, now, trigger="seat_freed")
                    free -= e["party_size"]
                    made += 1
    return made


def _make_offer(conn: Connection, s: dict, entry: dict, cfg: dict, now: datetime, *, trigger: str,
                user_id: int | None = None) -> int:
    send_after = send_time(now, s["time_zone"], cfg)
    hold_until = send_after + timedelta(hours=cfg["offer_hours"])
    hold_id = conn.execute(text("""
        INSERT INTO seat_holds (session_id, kind, quantity, expires_at) VALUES (:s, 'waitlist_offer', :n, :until)
        RETURNING id
    """), {"s": s["id"], "n": entry["party_size"], "until": hold_until}).scalar_one()
    offer_id = conn.execute(text("""
        INSERT INTO waitlist_offers (waitlist_entry_id, offered_session_id, seats, trigger, offered_by_user_id,
                                     send_after, seat_hold_id)
        VALUES (:e, :s, :n, :t, :u, :after, :h) RETURNING id
    """), {"e": entry["id"], "s": s["id"], "n": entry["party_size"], "t": trigger, "u": user_id,
           "after": send_after, "h": hold_id}).scalar_one()
    conn.execute(text("UPDATE waitlist_entries SET status = 'offered', status_changed_at = now() WHERE id = :e"),
                 {"e": entry["id"]})
    return offer_id


def dispatch() -> int:
    """Send offers whose time has come (inside the 8 AM – 6 PM window)."""
    from .filters import class_date, local_dt
    from .integrations.outbox import enqueue
    with db.engine().connect() as c:
        due = [r["id"] for r in c.execute(text("""
            SELECT id FROM waitlist_offers WHERE status = 'queued' AND send_after <= now() ORDER BY send_after LIMIT 200
        """)).mappings()]
    sent = 0
    for offer_id in due:
        with db.transaction() as conn:
            o = conn.execute(text("""
                SELECT o.id, o.public_id, o.status, o.seat_hold_id, o.waitlist_entry_id, s.starts_at, s.time_zone,
                       s.location_type, s.status AS class_status
                  FROM waitlist_offers o JOIN sessions s ON s.id = o.offered_session_id
                 WHERE o.id = :id FOR NO KEY UPDATE OF o
            """), {"id": offer_id}).mappings().first()
            if not o or o["status"] != "queued":
                continue
            cfg = _settings(conn)
            now = _now(conn)
            closes = cutoff(o["starts_at"], o["time_zone"], o["location_type"], cfg)
            if now >= closes or o["class_status"] != "scheduled":
                _withdraw(conn, o, "cutoff_passed")
                continue
            expires = min(now + timedelta(hours=cfg["offer_hours"]), closes)
            conn.execute(text("""UPDATE waitlist_offers SET status = 'sent', sent_at = now(), expires_at = :x
                                  WHERE id = :id"""), {"x": expires, "id": o["id"]})
            conn.execute(text("UPDATE seat_holds SET expires_at = :x WHERE id = :h AND status = 'active'"),
                         {"x": expires, "h": o["seat_hold_id"]})
            local = local_dt(expires, o["time_zone"])
            enqueue(conn, "ghl", "waitlist_notice", key=f"ghl-waitlist-offer-{o['id']}", entity_type="waitlist_entry",
                    entity_id=o["waitlist_entry_id"],
                    payload={"entry_id": o["waitlist_entry_id"], "kind": "offer",
                             "url": f"{base_url()}/waitlist/offer/{o['public_id']}",
                             "expires": f"{local.strftime('%-I:%M %p')} on {class_date(expires, o['time_zone'])}"})
            sent += 1
    return sent


def _withdraw(conn: Connection, o: dict, reason: str) -> None:
    conn.execute(text("UPDATE waitlist_offers SET status = 'withdrawn' WHERE id = :id"), {"id": o["id"]})
    conn.execute(text("""UPDATE seat_holds SET status = 'released', released_at = now(), release_reason = :r
                          WHERE id = :h AND status = 'active'"""), {"h": o["seat_hold_id"], "r": reason})
    conn.execute(text("""UPDATE waitlist_entries SET status = 'waiting', status_changed_at = now()
                          WHERE id = :e AND status = 'offered'"""), {"e": o["waitlist_entry_id"]})


def expire() -> int:
    """Offers not paid in time: "Offer expired" (the place is kept), the seat is freed for the next person.
    An offer whose checkout is still running (started before the 2 hours ended) waits for it."""
    from .integrations import checkout
    from .integrations.outbox import enqueue
    with db.engine().connect() as c:
        due = [r["id"] for r in c.execute(text("""
            SELECT id FROM waitlist_offers WHERE status = 'sent' AND expires_at <= now() ORDER BY expires_at LIMIT 200
        """)).mappings()]
    done = 0
    for offer_id in due:
        with db.transaction() as conn:
            o = conn.execute(text("""
                SELECT o.id, o.status, o.seat_hold_id, o.waitlist_entry_id, h.status AS hold_status,
                       h.expires_at AS hold_expires_at, h.order_id
                  FROM waitlist_offers o JOIN seat_holds h ON h.id = o.seat_hold_id
                 WHERE o.id = :id FOR NO KEY UPDATE OF o
            """), {"id": offer_id}).mappings().first()
            if not o or o["status"] != "sent":
                continue
            if o["hold_status"] == "active" and o["hold_expires_at"] > _now(conn):
                continue                       # a checkout started in time is still running
            if o["order_id"]:
                order_open = conn.execute(text("SELECT status = 'open' FROM orders WHERE id = :o FOR NO KEY UPDATE"),
                                          {"o": o["order_id"]}).scalar()
                if order_open:
                    checkout._release(conn, o["order_id"], o["seat_hold_id"], "hold_lapsed", "expired")
                    checkout._expire_stripe_pages(conn, o["order_id"])
            conn.execute(text("UPDATE waitlist_offers SET status = 'expired', expired_at = now() WHERE id = :id"),
                         {"id": o["id"]})
            conn.execute(text("""UPDATE seat_holds SET status = 'expired', released_at = now(),
                                        release_reason = 'offer_expired', order_id = NULL
                                  WHERE id = :h AND status = 'active'"""), {"h": o["seat_hold_id"]})
            conn.execute(text("""UPDATE waitlist_entries SET status = 'offer_expired', status_changed_at = now()
                                  WHERE id = :e AND status = 'offered'"""), {"e": o["waitlist_entry_id"]})
            enqueue(conn, "ghl", "waitlist_notice", key=f"ghl-waitlist-expired-{o['id']}",
                    entity_type="waitlist_entry", entity_id=o["waitlist_entry_id"],
                    payload={"entry_id": o["waitlist_entry_id"], "kind": "expired"})
            done += 1
    return done


def close_at_cutoff() -> int:
    """Past the cut-off: queued offers are withdrawn and everyone still on the list gets "no seat opened"."""
    from .integrations.outbox import enqueue
    closed = 0
    with db.engine().connect() as c:
        sessions = [dict(r) for r in c.execute(text("""
            SELECT s.id, s.starts_at, s.time_zone, s.location_type, s.status FROM sessions s
             WHERE s.starts_at < now() + interval '4 days'
               AND EXISTS (SELECT 1 FROM waitlist_entries w WHERE w.session_id = s.id
                              AND w.status IN ('waiting', 'offered', 'offer_expired'))
             LIMIT 200
        """)).mappings()]
    for s in sessions:
        with db.transaction() as conn:
            cfg = _settings(conn)
            if _now(conn) < cutoff(s["starts_at"], s["time_zone"], s["location_type"], cfg) \
                    and s["status"] == "scheduled":
                continue
            for o in conn.execute(text("""
                SELECT o.id, o.seat_hold_id, o.waitlist_entry_id FROM waitlist_offers o
                  JOIN waitlist_entries w ON w.id = o.waitlist_entry_id
                 WHERE w.session_id = :s AND o.status = 'queued' FOR NO KEY UPDATE OF o
            """), {"s": s["id"]}).mappings().all():
                _withdraw(conn, o, "cutoff_passed")
            for e in conn.execute(text("""
                UPDATE waitlist_entries SET status = 'closed_no_seat', status_changed_at = now(),
                       no_seat_notice_sent_at = now()
                 WHERE session_id = :s AND status IN ('waiting', 'offer_expired')
                RETURNING id
            """), {"s": s["id"]}).mappings().all():
                enqueue(conn, "ghl", "waitlist_notice", key=f"ghl-waitlist-no-seat-{e['id']}",
                        entity_type="waitlist_entry", entity_id=e["id"], payload={"entry_id": e["id"], "kind": "no_seat"})
                closed += 1
    return closed


def tick() -> dict:
    """One worker pass: close lists past their cut-off, expire offers, offer free seats, send due offers."""
    out = {"closed": close_at_cutoff(), "expired": expire(), "offered": offer_free_seats(), "sent": dispatch()}
    return {k: v for k, v in out.items() if v}


# --- Paying for an offered seat ---------------------------------------------------------------

def offer_for(public_id: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", public_id or ""):
        return None
    o = db.fetch_one("""
        SELECT o.id, o.public_id, o.status, o.seats, o.expires_at, o.seat_hold_id, o.sent_at,
               w.id AS entry_id, w.first_name, w.last_name, w.email, w.phone_e164, w.party_size,
               w.preferred_product_id, w.status AS entry_status,
               s.id AS session_id, s.public_id AS session_public_id, s.starts_at, s.ends_at, s.time_zone,
               s.delivery_mode, s.status AS class_status, cf.name AS course_name, si.name AS site_name,
               si.address_line1, si.city AS site_city, si.state_code AS site_state, si.postal_code,
               p.name AS product_name, p.short_name, sp.price_cents, h.status AS hold_status, h.expires_at AS hold_until
          FROM waitlist_offers o
          JOIN waitlist_entries w ON w.id = o.waitlist_entry_id
          JOIN sessions s ON s.id = o.offered_session_id
          JOIN course_families cf ON cf.id = s.course_family_id
          JOIN sites si ON si.id = s.site_id
          JOIN products p ON p.id = w.preferred_product_id
          JOIN session_products sp ON sp.session_id = s.id AND sp.product_id = w.preferred_product_id
          LEFT JOIN seat_holds h ON h.id = o.seat_hold_id
         WHERE o.public_id = CAST(:p AS uuid)
    """, p=public_id)
    if o:
        o["can_pay"] = o["status"] == "sent" and o["hold_status"] == "active" and \
            o["expires_at"] > datetime.now(timezone.utc) and o["class_status"] == "scheduled"
    return o


def start_checkout(offer: dict, *, buyer, attendees: list, base: str, buyer_user_id: int | None,
                   ip: str | None = None, user_agent: str | None = None) -> str:
    """Pay for the offered seat(s) on the offer's own hold. Returns the Stripe page (or the done page on
    the test site without Stripe, where the seat is confirmed at once)."""
    from .integrations import checkout, stripe_api
    from .integrations.http import IntegrationError
    if len(attendees) != offer["seats"]:
        raise WaitlistError(f"Name all {offer['seats']} people in this offer.")
    emails = [a.email.lower() for a in attendees]
    if len(set(emails)) != len(emails):
        raise WaitlistError("Each person attending needs their own email address.")
    testing = checkout.test_checkout()
    if not testing and stripe_api.check_key_for_environment():
        raise WaitlistError("Online payment isn't open yet. Call us to take this seat.")
    price = offer["price_cents"]
    total = price * len(attendees)
    livemode = False if testing else stripe_api.is_live_key()
    try:
        with db.transaction() as conn:
            o = conn.execute(text("""
                SELECT o.status, o.expires_at, h.id AS hold_id, h.status AS hold_status, h.order_id, h.public_id
                  FROM waitlist_offers o JOIN seat_holds h ON h.id = o.seat_hold_id
                 WHERE o.id = :id FOR NO KEY UPDATE OF o, h
            """), {"id": offer["id"]}).mappings().one()
            if o["status"] != "sent" or o["hold_status"] != "active" or o["expires_at"] <= _now(conn):
                raise WaitlistError("This offer has ended. You're still on the waiting list; we'll be in touch if "
                                    "another seat opens.")
            if o["order_id"]:                     # an earlier attempt (left Stripe): start again
                checkout._release(conn, o["order_id"], o["hold_id"], "customer_left_checkout", "cancelled")
                checkout._expire_stripe_pages(conn, o["order_id"])
            cfg = _settings(conn)
            order = conn.execute(text("""
                INSERT INTO orders (channel, buyer_first_name, buyer_last_name, buyer_email, buyer_phone_e164,
                                    subtotal_cents, total_cents, livemode, client_ip, client_user_agent, buyer_user_id)
                VALUES ('waitlist_offer', :fn, :ln, :em, :ph, :t, :t, :live, CAST(:ip AS inet), :ua, :uid)
                RETURNING id, public_id
            """), {"fn": buyer.first_name, "ln": buyer.last_name, "em": buyer.email, "ph": buyer.phone_e164,
                   "t": total, "live": livemode, "ip": ip, "ua": (user_agent or "")[:500] or None,
                   "uid": buyer_user_id}).mappings().one()
            # A checkout started before the offer ends may finish (hub 9c).
            conn.execute(text("""
                UPDATE seat_holds SET order_id = :o,
                       expires_at = GREATEST(expires_at, now() + make_interval(mins => :m))
                 WHERE id = :h
            """), {"o": order["id"], "h": o["hold_id"], "m": cfg["checkout_minutes"]})
            for a in attendees:
                conn.execute(text("""
                    INSERT INTO registrations (order_id, seat_hold_id, session_id, product_id, waitlist_entry_id,
                                               attendee_first_name, attendee_last_name, attendee_email,
                                               attendee_phone_e164, unit_price_cents)
                    VALUES (:o, :h, :s, :p, :w, :fn, :ln, :em, :ph, :price)
                """), {"o": order["id"], "h": o["hold_id"], "s": offer["session_id"],
                       "p": offer["preferred_product_id"], "w": offer["entry_id"], "fn": a.first_name,
                       "ln": a.last_name, "em": a.email, "ph": a.phone_e164, "price": price})
    except DBAPIError as exc:
        if "same course within" in str(exc.orig):
            raise WaitlistError("Someone in this offer already has this course within the same-course limit. "
                                "Call us and we'll sort it out.") from exc
        raise
    if testing:
        with db.transaction() as conn:
            found = checkout._lock_order(conn, order["id"], o["hold_id"], None)
            checkout._confirm(conn, found, {"id": None, "customer": None, "payment_intent": None},
                              {"subtotal": total, "discount": 0, "tax": 0, "total": total})
        return f"{base}/book/done?order={order['public_id']}"
    from .filters import class_date
    expires_at = int((datetime.now(timezone.utc) + timedelta(minutes=31)).timestamp())
    try:
        cs = stripe_api.create_checkout_session(
            order_public_id=str(order["public_id"]), hold_public_id=f"{o['public_id']}-{order['public_id']}",
            line_items=[{"name": offer["product_name"], "quantity": len(attendees), "unit_amount_cents": price,
                         "description": f"{class_date(offer['starts_at'], offer['time_zone'])} · {offer['site_name']}"}],
            customer_email=buyer.email, expires_at=expires_at,
            success_url=f"{base}/book/done?order={order['public_id']}",
            cancel_url=f"{base}/book/cancelled?order={order['public_id']}")
    except IntegrationError as exc:
        with db.transaction() as conn:
            checkout._release(conn, order["id"], o["hold_id"], "checkout_error", "cancelled")
        raise WaitlistError("We couldn't open the payment page. Please try again in a minute.") from exc
    with db.transaction() as conn:
        conn.execute(text("""
            INSERT INTO checkout_sessions (order_id, seat_hold_id, stripe_checkout_session_id, url, stripe_expires_at)
            VALUES (:o, :h, :cs, :url, to_timestamp(:exp))
        """), {"o": order["id"], "h": o["hold_id"], "cs": cs["id"], "url": cs.get("url"), "exp": expires_at})
    return cs["url"]


def converted(conn: Connection, hold_id: int, order_id: int) -> None:
    """Paid for an offered seat (called while the payment is confirmed): the offer is accepted, the entry
    is done, and the seat keeps the entry's GHL opportunity (Waitlisted -> Pending)."""
    row = conn.execute(text("""
        UPDATE waitlist_offers SET status = 'accepted', accepted_at = now(), order_id = :o
         WHERE seat_hold_id = :h RETURNING waitlist_entry_id
    """), {"h": hold_id, "o": order_id}).mappings().first()
    if not row:
        return
    entry = conn.execute(text("""
        UPDATE waitlist_entries SET status = 'converted', status_changed_at = now() WHERE id = :e
        RETURNING email, ghl_opportunity_id
    """), {"e": row["waitlist_entry_id"]}).mappings().one()
    if entry["ghl_opportunity_id"]:
        conn.execute(text("""
            UPDATE registrations SET ghl_opportunity_id = :opp
             WHERE id = (SELECT id FROM registrations WHERE order_id = :o
                          ORDER BY (attendee_email = :em) DESC, id LIMIT 1)
               AND ghl_opportunity_id IS NULL
               AND NOT EXISTS (SELECT 1 FROM registrations x WHERE x.ghl_opportunity_id = :opp)
        """), {"opp": entry["ghl_opportunity_id"], "o": order_id, "em": entry["email"]})


# --- Staff: remove someone, re-offer after "Offer expired" ------------------------------------

def _entry_for_staff(conn: Connection, entry_id: int, permission: str) -> dict:
    e = conn.execute(text("""
        SELECT w.id, w.status, w.party_size, w.session_id, s.metro_id, s.starts_at, s.time_zone, s.location_type
          FROM waitlist_entries w JOIN sessions s ON s.id = w.session_id
         WHERE w.id = :id FOR NO KEY UPDATE OF w
    """), {"id": entry_id}).mappings().first()
    if e is None:
        raise WaitlistError("That person isn't on the waiting list any more.")
    if not auth.current_user().can(permission, e["metro_id"]):
        raise WaitlistError("Your role can't change waiting lists in this city.")
    return dict(e)


def remove(entry_id: int, why: str) -> None:
    with db.transaction() as conn:
        e = _entry_for_staff(conn, entry_id, "waitlist.manage")
        if e["status"] not in ("waiting", "offered", "offer_expired"):
            raise WaitlistError("This person is no longer waiting.")
        for o in conn.execute(text("""SELECT id, seat_hold_id, waitlist_entry_id FROM waitlist_offers
                                       WHERE waitlist_entry_id = :e AND status IN ('queued', 'sent')"""),
                              {"e": entry_id}).mappings().all():
            _withdraw(conn, o, "removed_from_list")
        conn.execute(text("UPDATE waitlist_entries SET status = 'removed', status_changed_at = now() WHERE id = :e"),
                     {"e": entry_id})
        audit.record(conn, "waitlist.remove", "waitlist_entry", entry_id, before={"status": e["status"]},
                     reason=why.strip()[:500] or None)
    offer_free_seats(e["session_id"])


def reoffer(entry_id: int) -> None:
    """Staff offer a free seat again to someone whose offer expired (not automatic, hub 9c)."""
    user = auth.current_user()
    with db.transaction() as conn:
        e = _entry_for_staff(conn, entry_id, "waitlist.manage")
        if e["status"] != "offer_expired":
            raise WaitlistError("Only someone whose offer expired can be offered a seat again.")
        cfg = _settings(conn)
        now = _now(conn)
        if now >= cutoff(e["starts_at"], e["time_zone"], e["location_type"], cfg):
            raise WaitlistError("The waiting list for this class has closed.")
        conn.execute(text("SELECT 1 FROM sessions WHERE id = :s FOR NO KEY UPDATE"), {"s": e["session_id"]})
        free = conn.execute(text("SELECT session_seats_available(:s)"), {"s": e["session_id"]}).scalar()
        if free < e["party_size"]:
            raise WaitlistError("There's no free seat for them right now.")
        _make_offer(conn, {"id": e["session_id"], "time_zone": e["time_zone"]}, e, cfg, now, trigger="manual",
                    user_id=user.id)
        audit.record(conn, "waitlist.reoffer", "waitlist_entry", entry_id, before={"status": "offer_expired"},
                     after={"status": "offered"})
    dispatch()
