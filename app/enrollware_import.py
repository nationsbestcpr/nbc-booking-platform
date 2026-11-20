"""Importing Enrollware's registrations at cutover (Superadmin → Cutover and import, or `flask enrollware-import`).

Enrollware's registration export (.xlsx or .csv, one row per student) is loaded into the booking engine:

  * Pending          -> a seat in the matching class: paid (the money taken in Enrollware is recorded as a
                        manual payment, "Enrollware CLS-…"), or awaiting payment when Enrollware shows a balance
                        due (due on the class day);
  * Waitlisted       -> a place on the class's waiting list;
  * anything else    -> skipped (Complete, No Show, Cancelled …: classes that happened or seats that were let go).

Only classes from today on are imported. A row's class is found by its location (Enrollware's "State - City
area - Site" name, the same as Airtable's Master Locations name) and its local start time; the course decides
the family and, for First Aid, Adult & Pediatric or Adult only. Nothing is sent to students: they were
confirmed by Enrollware. GHL gets the seats only when asked (they may already be there from Enrollware).

Every rule the database keeps still applies (no overselling, one seat per course per email ...). A row the
database refuses is reported with the reason, never forced, except that a class that is full here can get one
extra seat per Enrollware student when that option is ticked (a seat override with the reason).

Each student is imported once (enrollware_import_rows.row_key: Enrollware class id + email + name), so loading
a newer export only adds what is new. "Check the file" runs the whole import and rolls it back, so the report
shows exactly what importing would do.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from flask import current_app
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from . import audit, db
from .integrations.outbox import enqueue

REQUIRED = ("First Name", "Last Name", "Email", "Course", "Class ID", "Course Date", "Course Location", "Status")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SEAT_STATUSES = {"pending"}
WAITLIST_STATUSES = {"waitlisted"}


class ImportFileError(ValueError):
    """The file can't be read as an Enrollware registration export (worded for the Superadmin)."""


class _DryRun(Exception):
    pass


@dataclass
class Report:
    file_name: str
    rows_total: int = 0
    committed: bool = False
    import_id: int | None = None
    counts: dict[str, int] = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)
    classes: int = 0
    extra_seats: int = 0
    enrollware_counts_cleared: int = 0
    money_cents: int = 0
    balance_due_cents: int = 0

    def add(self, row: dict, outcome: str, reason: str | None = None, **ids: Any) -> None:
        self.counts[outcome] = self.counts.get(outcome, 0) + 1
        self.rows.append({"row": row["_row"], "name": f"{row.get('First Name') or ''} {row.get('Last Name') or ''}".strip(),
                          "email": row.get("Email") or "", "class_ref": row.get("Class ID") or "",
                          "course": row.get("Course") or "", "when": row.get("Course Date"),
                          "location": row.get("Course Location") or "", "outcome": outcome, "reason": reason, **ids})


# --- Reading the file -----------------------------------------------------------------------

def read_file(name: str, data: bytes) -> list[dict]:
    """The export's rows as dicts keyed by Enrollware's column names, with '_row' (the spreadsheet row number)."""
    lower = (name or "").lower()
    if lower.endswith((".xlsx", ".xlsm")):
        try:
            import openpyxl
        except ImportError as exc:  # pragma: no cover - installed from requirements/app.txt
            raise ImportFileError("Reading .xlsx needs the openpyxl package: run pip install -r requirements/app.txt, "
                                  "or save the export as .csv.") from exc
        try:
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as exc:
            raise ImportFileError("That file isn't a readable Excel workbook.") from exc
        rows = wb.worksheets[0].iter_rows(values_only=True)
        head = [str(h).strip() if h is not None else "" for h in next(rows, [])]
        out = [dict(zip(head, r)) for r in rows]
    elif lower.endswith(".csv"):
        try:
            reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
        except UnicodeDecodeError as exc:
            raise ImportFileError("Save the CSV as UTF-8 and try again.") from exc
        head = [h.strip() for h in reader.fieldnames or []]
        out = [{k.strip(): v for k, v in r.items() if k} for r in reader]
    else:
        raise ImportFileError("Choose Enrollware's registration export: an .xlsx or .csv file.")
    missing = [c for c in REQUIRED if c not in head]
    if missing:
        raise ImportFileError("This doesn't look like Enrollware's registration export: it has no "
                              + ", ".join(f"“{c}”" for c in missing) + " column.")
    rows_out = []
    for i, r in enumerate(out, start=2):
        if any(v not in (None, "") for v in r.values()):
            r["_row"] = i
            rows_out.append(r)
    return rows_out


# --- Small parsers -----------------------------------------------------------------------------

def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _cents(v: Any) -> int:
    s = _text(v).replace("$", "").replace(",", "")
    if not s:
        return 0
    try:
        return int((Decimal(s) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except ArithmeticError:
        return 0


def _when(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v.replace(tzinfo=None, second=0, microsecond=0)
    s = _text(v)
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M:%S %p", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def phone_e164(v: Any) -> str | None:
    digits = re.sub(r"\D", "", _text(v))
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split())


def course_of(name: str) -> tuple[str | None, str | None, str | None]:
    """(course family code, delivery, product hint) for an Enrollware course name; (None, …) for one not sold
    here (online only, virtual)."""
    t = (name or "").lower()
    if "online only" in t or "virtual" in t:
        return None, None, None
    if "instructor course" in t or "instructor training" in t or re.search(r"\bit\b", t):
        family = "IT"
    elif "bls" in t:
        family = "BLS"
    elif "first aid" in t or "cpr" in t:
        family = "FA_CPR_AED"
    else:
        return None, None, None
    delivery = "classroom" if "all work done in class" in t else ("blended" if ("blended" in t or "skills" in t) else None)
    hint = None
    if family == "FA_CPR_AED":
        hint = "A&P" if "pediatric" in t else "Adult"
    return family, delivery, hint


def row_key(r: dict) -> str:
    return "|".join(_norm(_text(r.get(k))) for k in ("Class ID", "Email", "First Name", "Last Name"))


# --- Matching rows to classes ----------------------------------------------------------------

class _Places:
    """Sites by every name Enrollware may use for them."""

    def __init__(self, conn: Connection):
        self.by_name: dict[str, int] = {}
        for r in conn.execute(text("""
            SELECT location_name, site_id FROM location_instructor_settings WHERE site_id IS NOT NULL
        """)).mappings():
            self.by_name.setdefault(_norm(r["location_name"]), r["site_id"])
        for r in conn.execute(text("""
            SELECT si.id, si.name, m.name AS metro FROM sites si JOIN metros m ON m.id = si.metro_id
        """)).mappings():
            self.by_name.setdefault(_norm(f"{r['metro']} {r['name']}"), r["id"])
            self.by_name.setdefault(_norm(r["name"]), r["id"])

    def site(self, location: str) -> int | None:
        full = _norm(location)
        if full in self.by_name:
            return self.by_name[full]
        parts = [p.strip() for p in (location or "").split(" - ")]
        if len(parts) >= 3:                                      # "State - City area - Site (City)"
            for key in (_norm(" ".join(parts[1:])), _norm(" - ".join(parts[2:]))):
                if key in self.by_name:
                    return self.by_name[key]
        return None


def _classes_at(conn: Connection, site_id: int, local: datetime) -> list[dict]:
    return [dict(r) for r in conn.execute(text("""
        SELECT s.id, s.public_id, s.status, s.delivery_mode, cf.code AS family, s.local_date,
               s.starts_at <= now() AS started
          FROM sessions s JOIN course_families cf ON cf.id = s.course_family_id
         WHERE s.site_id = :site AND timezone(s.time_zone, s.starts_at) = :local AND s.source_deleted_at IS NULL
         ORDER BY s.id
    """), {"site": site_id, "local": local}).mappings()]


def _product(conn: Connection, session_id: int, hint: str | None) -> int | None:
    rows = conn.execute(text("""
        SELECT p.id, p.short_name, p.name FROM session_products sp JOIN products p ON p.id = sp.product_id
         WHERE sp.session_id = :s ORDER BY p.sort_order, p.id
    """), {"s": session_id}).mappings().all()
    if not rows:
        return None
    if hint == "Adult":
        for r in rows:
            if r["short_name"].lower().startswith("adult") and "pediatric" not in r["name"].lower():
                return r["id"]
    if hint == "A&P":
        for r in rows:
            if "pediatric" in r["name"].lower() or r["short_name"] in ("A&P", "Adult & Pediatric"):
                return r["id"]
    return rows[0]["id"]


def _refusal(exc: DBAPIError) -> str:
    msg = str(getattr(exc, "orig", exc)).split("\n")[0]
    if "same course within" in msg:
        return "This email already has a seat in this course within the same-course window (" + msg.split(": ", 1)[-1] + ")."
    if "registrations_order_id_attendee_email_key" in msg:
        return "This email already has a seat in this booking."
    if "waitlist for session" in msg:
        return "The class's waiting list is full in the booking app."
    if "is full" in msg:
        return ("The class is full in the booking app. Tick “add an extra seat” to give this student the seat they "
                "bought in Enrollware.")
    if "not open for booking" in msg:
        if "metro on enrollware" in msg:
            return "The class's city still books in Enrollware: move every city to the booking app first."
        return "The class has already started or isn't scheduled."
    return msg


# --- The import ------------------------------------------------------------------------------

def run(rows: list[dict], *, file_name: str, user_id: int, commit: bool, add_seats: bool = False,
        send_to_ghl: bool = False, today: date | None = None) -> Report:
    """Import the rows (commit=True) or show what importing would do (commit=False: everything is rolled back)."""
    report = Report(file_name=file_name, rows_total=len(rows))
    today = today or date.today()
    live = current_app.config.get("APP_ENV") == "production"
    try:
        with db.transaction() as conn:
            _import(conn, rows, report, user_id=user_id, add_seats=add_seats, send_to_ghl=send_to_ghl,
                    today=today, live=live, commit=commit)
            if not commit:
                raise _DryRun()
    except _DryRun:
        pass
    report.committed = commit
    return report


def _import(conn: Connection, rows: list[dict], report: Report, *, user_id: int, add_seats: bool, send_to_ghl: bool,
            today: date, live: bool, commit: bool) -> None:
    places = _Places(conn)
    # Seat and waiting-list limits are normally checked at commit; check each row at once, so a row the
    # database refuses is reported (and the check shows it) instead of undoing the whole import.
    conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    done = set(conn.execute(text("""SELECT row_key FROM enrollware_import_rows
                                     WHERE outcome IN ('imported', 'waitlisted')""")).scalars())
    import_id = None
    if commit:
        import_id = conn.execute(text("""
            INSERT INTO enrollware_imports (file_name, rows_total, sent_to_ghl, imported_by_user_id)
            VALUES (:f, :n, :g, :u) RETURNING id
        """), {"f": report.file_name[:200], "n": len(rows), "g": send_to_ghl, "u": user_id}).scalar_one()
        report.import_id = import_id
    sessions_touched: set[int] = set()
    seen_in_file: set[str] = set()

    def note(r: dict, outcome: str, reason: str | None = None, *, session_id=None, registration_id=None,
             waitlist_entry_id=None) -> None:
        report.add(r, outcome, reason, session_id=session_id)
        if import_id:
            conn.execute(text("""
                INSERT INTO enrollware_import_rows (import_id, row_number, row_key, outcome, reason, enrollware_class_ref,
                                                    session_id, registration_id, waitlist_entry_id)
                VALUES (:i, :n, :k, :o, :why, :ref, :s, :r, :w)
            """), {"i": import_id, "n": r["_row"], "k": row_key(r), "o": outcome, "why": reason,
                   "ref": _text(r.get("Class ID"))[:60] or None, "s": session_id, "r": registration_id,
                   "w": waitlist_entry_id})

    for r in rows:
        status = _text(r.get("Status")).lower()
        key = row_key(r)
        if key in done:
            note(r, "already_imported", "Imported from an earlier export.")
            continue
        if key in seen_in_file:
            note(r, "skipped", "The same student and class appear twice in this file.")
            continue
        seen_in_file.add(key)
        if status not in SEAT_STATUSES | WAITLIST_STATUSES:
            note(r, "skipped", f"Enrollware status {_text(r.get('Status')) or '(blank)'}: nothing to carry over.")
            continue
        when = _when(r.get("Course Date"))
        if when is None:
            note(r, "skipped", f"No class date ({_text(r.get('Course Date')) or 'blank'}).")
            continue
        if when.date() < today:
            note(r, "skipped", "The class has already happened.")
            continue
        family, delivery, hint = course_of(_text(r.get("Course")))
        if family is None:
            note(r, "skipped", "An online or virtual course: not sold in the booking app.")
            continue
        site_id = places.site(_text(r.get("Course Location")))
        if site_id is None:
            note(r, "unmatched", "No location in the booking app has this name. Check the location's name in "
                                 "Airtable's Master Locations, and that its classes are on sale.")
            continue
        found = [c for c in _classes_at(conn, site_id, when) if c["family"] == family]
        if delivery and len(found) > 1:
            found = [c for c in found if c["delivery_mode"] == delivery] or found
        if not found:
            note(r, "unmatched", "No class at this location and time in the booking app. Is the class on sale in "
                                 "Airtable (Status includes Enrollware or Booking App)?")
            continue
        cls = found[0]
        if cls["status"] == "cancelled":
            note(r, "skipped", "The class is cancelled in Airtable.", session_id=cls["id"])
            continue
        if cls["started"]:
            note(r, "skipped", "The class has already happened.", session_id=cls["id"])
            continue
        product_id = _product(conn, cls["id"], hint)
        if product_id is None:
            note(r, "unmatched", "The class has no course price set up.", session_id=cls["id"])
            continue
        first, last = _text(r.get("First Name")), _text(r.get("Last Name")) or "-"
        email = _text(r.get("Email")).lower()
        phone = phone_e164(r.get("Phone")) or phone_e164(r.get("Alt Phone"))
        if not first or not EMAIL.match(email):
            note(r, "refused", "A seat needs the student's first name and a valid email.", session_id=cls["id"])
            continue
        try:
            with conn.begin_nested():
                if status in WAITLIST_STATUSES:
                    if not phone:
                        raise _Refused("A waiting-list place needs a phone number (offers go out by text).")
                    entry_id = _waitlist(conn, r, cls["id"], product_id, first, last, email, phone)
                    note(r, "waitlisted", None, session_id=cls["id"], waitlist_entry_id=entry_id)
                    continue
                reg_id, paid, due = _seat(conn, r, cls, product_id, first, last, email, phone, user_id, live,
                                          add_seats, report)
                report.money_cents += paid
                report.balance_due_cents += due
                sessions_touched.add(cls["id"])
                note(r, "imported", "Awaiting payment: Enrollware shows a balance due." if due else None,
                     session_id=cls["id"], registration_id=reg_id)
                if send_to_ghl and commit:
                    enqueue(conn, "ghl", "sync_registration", key=f"ghl-registration-{reg_id}-enrollware-import",
                            entity_type="registration", entity_id=reg_id, payload={"imported_from": "enrollware"})
        except _Refused as exc:
            note(r, "refused", str(exc), session_id=cls["id"])
        except DBAPIError as exc:
            note(r, "refused", _refusal(exc), session_id=cls["id"])

    report.classes = len(sessions_touched)
    if sessions_touched:
        # Enrollware's seat counts for these classes are now real seats here: stop counting them twice.
        report.enrollware_counts_cleared = conn.execute(text("""
            DELETE FROM external_seat_reservations WHERE session_id = ANY(:ids) AND source = 'enrollware'
        """), {"ids": list(sessions_touched)}).rowcount
    if import_id:
        summary = {"counts": report.counts, "classes": report.classes, "extra_seats": report.extra_seats,
                   "money_cents": report.money_cents, "balance_due_cents": report.balance_due_cents,
                   "enrollware_counts_cleared": report.enrollware_counts_cleared}
        conn.execute(text("UPDATE enrollware_imports SET summary = CAST(:s AS jsonb) WHERE id = :i"),
                     {"s": json.dumps(summary), "i": import_id})
        audit.record(conn, "enrollware.import", "enrollware_import", import_id,
                     after={"file": report.file_name, **summary, "sent_to_ghl": send_to_ghl, "add_seats": add_seats})


class _Refused(Exception):
    pass


def _person(conn: Connection, first: str, last: str, email: str, phone: str | None) -> int:
    pid = conn.execute(text("""SELECT id FROM people WHERE email = :e AND merged_into_person_id IS NULL
                                ORDER BY id LIMIT 1"""), {"e": email}).scalar()
    if pid:
        return pid
    return conn.execute(text("""INSERT INTO people (first_name, last_name, email, phone_e164)
                                VALUES (:f, :l, :e, :p) RETURNING id"""),
                        {"f": first, "l": last, "e": email, "p": phone}).scalar_one()


def _seat(conn: Connection, r: dict, cls: dict, product_id: int, first: str, last: str, email: str,
          phone: str | None, user_id: int, live: bool, add_seats: bool, report: Report) -> tuple[int, int, int]:
    unit = _cents(r.get("Class Price"))
    total = _cents(r.get("Order Total")) or unit
    due = max(min(_cents(r.get("Balance Due")), total), 0)
    paid = total - due
    reg_at = r.get("Reg. Date") if isinstance(r.get("Reg. Date"), datetime) else None
    person_id = _person(conn, first, last, email, phone)
    order_id = conn.execute(text("""
        INSERT INTO orders (channel, status, buyer_person_id, buyer_first_name, buyer_last_name, buyer_email,
                            buyer_phone_e164, invoice_due_on, subtotal_cents, total_cents, amount_paid_cents,
                            promotion_code, created_by_user_id, livemode, paid_at)
        VALUES ('admin', :st, :p, :f, :l, :e, :ph, :due_on, :t, :t, :paid, :promo, :u, :live,
                CASE WHEN :st = 'paid' THEN coalesce(CAST(:reg AS timestamp), now()) END)
        RETURNING id
    """), {"st": "awaiting_payment" if due else "paid", "p": person_id, "f": first, "l": last, "e": email, "ph": phone,
           "due_on": cls["local_date"] if due else None, "t": total, "paid": paid,
           "promo": _text(r.get("Promo Code")) or None, "u": user_id, "live": live, "reg": reg_at}).scalar_one()
    insert = text("""
        INSERT INTO registrations (order_id, session_id, product_id, person_id, attendee_first_name,
                                   attendee_last_name, attendee_email, attendee_phone_e164, status, unit_price_cents,
                                   amount_paid_cents, confirmed_at, is_bring_a_friend)
        VALUES (:o, :s, :p, :person, :f, :l, :e, :ph, :st, :unit, :paid, coalesce(CAST(:reg AS timestamp), now()), :baf)
        RETURNING id
    """)
    params = {"o": order_id, "s": cls["id"], "p": product_id, "person": person_id, "f": first, "l": last, "e": email,
              "ph": phone, "st": "awaiting_payment" if due else "confirmed", "unit": unit,
              "paid": min(paid, unit), "reg": reg_at,
              "baf": "bring a friend" in _text(r.get("Options")).lower()}
    try:
        with conn.begin_nested():
            reg_id = conn.execute(insert, params).scalar_one()
    except DBAPIError as exc:
        if not (add_seats and "is full" in str(exc.orig)):
            raise
        conn.execute(text("""INSERT INTO seat_overrides (session_id, seats, reason, approved_by_user_id)
                             VALUES (:s, 1, :why, :u)"""),
                     {"s": cls["id"], "u": user_id,
                      "why": f"Enrollware import: {first} {last} was sold this seat in Enrollware ({_text(r.get('Class ID'))})."})
        report.extra_seats += 1
        reg_id = conn.execute(insert, params).scalar_one()
    if paid > 0:
        conn.execute(text("""
            INSERT INTO payments (order_id, method, status, amount_cents, received_at, manual_reference,
                                  recorded_by_user_id, livemode)
            VALUES (:o, 'manual', 'succeeded', :a, coalesce(CAST(:reg AS timestamp), now()), :ref, :u, :live)
        """), {"o": order_id, "a": paid, "reg": reg_at, "ref": f"Enrollware {_text(r.get('Class ID'))}"[:120],
               "u": user_id, "live": live})
    lines = []
    if _text(r.get("Options")):
        lines.append(f"Options in Enrollware: {_text(r.get('Options'))} (${_cents(r.get('Options Price')) / 100:,.2f}).")
    if _text(r.get("Labels")):
        lines.append(f"Enrollware labels: {_text(r.get('Labels'))}.")
    if _text(r.get("Comments")):
        lines.append(f"Comments: {_text(r.get('Comments'))}")
    body = "Imported from Enrollware (" + _text(r.get("Class ID")) + ")." + ("\n" + "\n".join(lines) if lines else "")
    conn.execute(text("INSERT INTO notes (registration_id, author_user_id, body) VALUES (:r, :u, :b)"),
                 {"r": reg_id, "u": user_id, "b": body[:4000]})
    return reg_id, paid, due


def _waitlist(conn: Connection, r: dict, session_id: int, product_id: int, first: str, last: str, email: str,
              phone: str) -> int:
    reg_at = r.get("Reg. Date") if isinstance(r.get("Reg. Date"), datetime) else None
    person_id = _person(conn, first, last, email, phone)
    return conn.execute(text("""
        INSERT INTO waitlist_entries (session_id, preferred_product_id, person_id, first_name, last_name, email,
                                      phone_e164, joined_at)
        VALUES (:s, :p, :person, :f, :l, :e, :ph, coalesce(CAST(:reg AS timestamp), now())) RETURNING id
    """), {"s": session_id, "p": product_id, "person": person_id, "f": first, "l": last, "e": email, "ph": phone,
           "reg": reg_at}).scalar_one()
