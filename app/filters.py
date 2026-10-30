"""Template helpers: money, local class times, office times."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask

_TZ_ABBR = {
    "America/New_York": "ET",
    "America/Chicago": "CT",
    "America/Denver": "MT",
    "America/Phoenix": "MST",
    "America/Los_Angeles": "PT",
}


def money(cents: int | None) -> str:
    if cents is None:
        return ""
    dollars, rem = divmod(int(cents), 100)
    return f"${dollars:,}" if rem == 0 else f"${dollars:,}.{rem:02d}"


def local_dt(value: datetime, tz: str) -> datetime:
    return value.astimezone(ZoneInfo(tz))


def class_date(value: datetime, tz: str) -> str:
    return local_dt(value, tz).strftime("%a, %b %-d, %Y")


def class_time(start: datetime, end: datetime, tz: str) -> str:
    s, e = local_dt(start, tz), local_dt(end, tz)

    def fmt(d: datetime) -> str:
        return d.strftime("%-I:%M %p").replace(":00 ", " ").lower()

    return f"{fmt(s)} – {fmt(e)} {_TZ_ABBR.get(tz, tz)}"


# Office times (action log, sign-ins, sync runs) are shown in NBC's own time
# zone. Class times always use the class's own zone (class_date / class_time).
OFFICE_TZ = "America/New_York"


def office_time(value: datetime | None) -> str:
    """'Oct 30, 2026 6:12 am ET', or an em dash when there's no time."""
    if value is None:
        return "—"
    d = value.astimezone(ZoneInfo(OFFICE_TZ))
    return d.strftime("%b %-d, %Y %-I:%M %p").replace("AM", "am").replace("PM", "pm") + " ET"


_STAFF_WORDS = [("Co-instructor", "Helper instructor"), ("co-instructor", "helper instructor"),
                ("co instructor", "helper instructor"), ("HUB metros", "Hub cities"), ("metros", "cities"),
                ("metro", "city"), ("ghl", "GHL"), ("otl", "OTL")]


def plain(value: str | None) -> str:
    """Staff-facing wording for text that comes from the database (setting
    descriptions): 'helper instructor', not 'co-instructor'; 'city', not 'metro'."""
    out = value or ""
    for old, new in _STAFF_WORDS:
        out = out.replace(old, new)
    return out


def setting_label(key: str) -> str:
    """'co_instructor_pay_cents' -> 'Helper instructor pay'."""
    words = key.replace("_", " ")
    for suffix in (" cents",):
        if words.endswith(suffix):
            words = words[: -len(suffix)]
    words = plain(words)
    return words[:1].upper() + words[1:]


_CERT_WORDS = {"ARC": "Red Cross", "BLS": "BLS", "FA": "First Aid", "CPR": "CPR", "AED": "AED",
               "INSTRUCTOR": "instructor", "TRAINER": "trainer"}


def cert_label(cert_type: str) -> str:
    """'ARC_FA_CPR_AED_INSTRUCTOR' → 'Red Cross First Aid/CPR/AED instructor'."""
    words = [_CERT_WORDS.get(w, w.lower()) for w in cert_type.split("_")]
    out = " ".join(words).replace("First Aid CPR AED", "First Aid/CPR/AED")
    return out[:1].upper() + out[1:]


def init_app(app: Flask) -> None:
    app.add_template_filter(cert_label)
    app.add_template_filter(money)
    app.add_template_filter(office_time)
    app.add_template_filter(plain)
    app.add_template_filter(setting_label)
    app.add_template_global(local_dt)
    app.add_template_global(class_date)
    app.add_template_global(class_time)
