"""What the booking page asks and offers, set up by staff (no code change needed).

    questions_for(session)   active booking questions for this class: {'attendee': [...], 'booking': [...]}
    addons_for(session)      add-ons shown on this class's booking page: {'seat': [...], 'booking': [...]}
    bring_a_friend()         {'enabled', 'discount_cents'} from Business settings
    read_answers(...)        the answers in a submitted form, checked against the questions
    same_course_clashes(...) emails that already have this course within the 18-month window

Questions and add-ons live in booking_questions / addons (Admin → Booking
questions, Add-ons). A question or add-on with no course rows applies to every
course; a question may also be limited to blended or classroom classes.
"""

from __future__ import annotations

from typing import Mapping

from . import db

KINDS = {"short_text": "Short answer", "long_text": "Paragraph", "yes_no": "Yes or no", "choice": "Pick one"}
ASKED_FOR = {"attendee": "Each attendee", "booking": "Once per booking"}
MAX_ANSWER = 1000


def setting(key: str, default=None):
    value = db.fetch_value("SELECT value FROM app_settings WHERE key = :k", k=key)
    return default if value is None else value


def bring_a_friend() -> dict:
    discount = setting("bring_a_friend_discount_cents")
    return {"enabled": bool(setting("bring_a_friend_enabled", False)),
            "discount_cents": int(discount) if isinstance(discount, (int, float)) and discount > 0 else 0}


def friend_price(price_cents: int) -> int:
    return max(price_cents - bring_a_friend()["discount_cents"], 0)


def questions_for(session: Mapping) -> dict[str, list[dict]]:
    rows = db.fetch_all("""
        SELECT q.id, q.label, q.help_text, q.kind, q.choices, q.is_required, q.asked_for
          FROM booking_questions q
         WHERE q.is_active
           AND (q.delivery_mode IS NULL OR q.delivery_mode = :mode)
           AND (NOT EXISTS (SELECT 1 FROM booking_question_courses c WHERE c.question_id = q.id)
                OR EXISTS (SELECT 1 FROM booking_question_courses c
                            WHERE c.question_id = q.id AND c.course_family_id = :fam))
         ORDER BY q.sort_order, q.id
    """, mode=session["delivery_mode"], fam=session["course_family_id"])
    return {"attendee": [q for q in rows if q["asked_for"] == "attendee"],
            "booking": [q for q in rows if q["asked_for"] == "booking"]}


def addons_for(session: Mapping) -> dict[str, list[dict]]:
    rows = db.fetch_all("""
        SELECT a.id, a.code, a.name, a.description, a.price_cents, a.per
          FROM addons a
         WHERE a.show_on_booking_page
           AND (NOT EXISTS (SELECT 1 FROM addon_courses c WHERE c.addon_id = a.id)
                OR EXISTS (SELECT 1 FROM addon_courses c WHERE c.addon_id = a.id AND c.course_family_id = :fam))
         ORDER BY a.sort_order, a.name
    """, fam=session["course_family_id"])
    return {"seat": [a for a in rows if a["per"] == "seat"], "booking": [a for a in rows if a["per"] == "booking"]}


def read_answers(form: Mapping[str, str], questions: list[dict], suffix: str, who: str) -> tuple[dict[int, str], str | None]:
    """Answers named q<id>_<suffix> in the form. Returns ({question id: answer}, error or None).
    `who` names the person in the error ("Seat 2", "Your friend", "This booking")."""
    answers: dict[int, str] = {}
    for q in questions:
        raw = (form.get(f"q{q['id']}_{suffix}") or "").strip()
        if q["kind"] == "yes_no" and raw not in ("", "Yes", "No"):
            raw = ""
        if q["kind"] == "choice" and raw and raw not in (q["choices"] or []):
            raw = ""
        if not raw:
            if q["is_required"]:
                return answers, f"{who}: answer “{q['label']}”."
            continue
        answers[q["id"]] = raw[:MAX_ANSWER]
    return answers, None


def read_addons(form: Mapping[str, str], addons: list[dict], suffix: str) -> list[int]:
    return [a["id"] for a in addons if form.get(f"addon{a['id']}_{suffix}") == "yes"]


def same_course_clashes(session_id: int, emails: list[str]) -> list[dict]:
    """Emails that already have a seat in this course within the same-course window
    (the database refuses those seats; this finds them first to say which person)."""
    months = setting("same_course_block_months", 0)
    if not isinstance(months, int) or months <= 0 or not emails:
        return []
    return db.fetch_all("""
        SELECT DISTINCT ON (r.attendee_email) r.attendee_email AS email, cf.name AS course_name,
               s.starts_at, s.time_zone, :months AS months
          FROM sessions me
          JOIN course_families cf ON cf.id = me.course_family_id
          JOIN sessions s ON s.course_family_id = me.course_family_id
          JOIN registrations r ON r.session_id = s.id
         WHERE me.id = :sid AND r.attendee_email = ANY(CAST(:emails AS citext[]))
           AND s.starts_at > me.starts_at - make_interval(months => :months)
           AND s.starts_at < me.starts_at + make_interval(months => :months)
           AND (r.status IN ('awaiting_payment', 'confirmed', 'completed', 'displaced')
                OR (r.status = 'pending' AND EXISTS (SELECT 1 FROM seat_holds h WHERE h.id = r.seat_hold_id
                                                       AND h.status = 'active' AND h.expires_at > now())))
         ORDER BY r.attendee_email, s.starts_at
    """, sid=session_id, emails=[e.lower() for e in emails], months=months)
