"""Dashboard areas and their sidebar navigation.

Each signed-in account opens one area, worked out from its roles
(CurrentUser.area): staff roles -> admin, the instructor role -> instructor,
everyone else -> student. A sidebar item with permissions shows only to
accounts holding at least one of them, and a section with no visible items
is left out, so what each role sees is set by role data, not by code.

Detail pages light up their list's item: an endpoint 'admin.classes_detail'
belongs to the 'admin.classes' item.
"""

from __future__ import annotations

from flask import Flask, request

from . import auth

Item = tuple[str, str, tuple[str, ...]]          # (endpoint, label, any of these permissions)

# area -> (title, home endpoint, [(section label, [items])])
AREAS: dict[str, tuple[str, str, list[tuple[str, list[Item]]]]] = {
    "admin": ("Admin", "admin.home", [
        ("Overview", [
            ("admin.home", "Dashboard", ()),
        ]),
        ("Classes", [
            ("admin.classes", "Classes", ("class.view",)),
            ("admin.bookings", "Bookings", ("student.view",)),
            ("admin.waitlists", "Waiting lists", ("waitlist.view",)),
            ("admin.locations", "Locations", ("class.view",)),
        ]),
        ("People", [
            ("admin.users", "Users", ("user.manage",)),
            ("admin.instructors", "Instructors", ("instructor.view",)),
            ("admin.roles", "Roles & permissions", ("user.manage", "role.manage")),
        ]),
        ("System", [
            ("admin.integrations", "Integrations", ("system.health_view",)),
            ("admin.log", "Action log", ("audit.view",)),
            ("admin.settings", "Business settings", ("settings.manage",)),
        ]),
        ("My account", [
            ("me.profile", "Profile", ()),
            ("me.settings", "Password", ()),
        ]),
    ]),
    "instructor": ("Instructor", "teach.home", [
        ("Teaching", [
            ("teach.home", "Dashboard", ()),
            ("teach.certifications", "Certifications", ("own.certs_view",)),
            ("teach.pay", "Pay", ("own.pay_view",)),
        ]),
        ("My account", [
            ("me.profile", "Profile", ()),
            ("me.settings", "Password", ()),
        ]),
    ]),
    "student": ("My account", "me.home", [
        ("My classes", [
            ("me.home", "Dashboard", ()),
        ]),
        ("My account", [
            ("me.profile", "Profile", ()),
            ("me.settings", "Password", ()),
        ]),
    ]),
}


def home_endpoint(user: auth.CurrentUser) -> str:
    return AREAS[user.area][1]


def _is_current(endpoint: str, current: str) -> bool:
    return current == endpoint or current.startswith(endpoint + "_")


def nav_for(user: auth.CurrentUser) -> list[dict]:
    """[{label, items: [{endpoint, label, current}]}] for this account's area."""
    current = request.endpoint or ""
    sections = []
    for label, items in AREAS[user.area][2]:
        visible = [{"endpoint": e, "label": l, "current": _is_current(e, current)}
                   for e, l, perms in items if not perms or any(p in user.permissions for p in perms)]
        if visible:
            sections.append({"label": label, "items": visible})
    return sections


def tabs_for(user: auth.CurrentUser) -> list[dict]:
    """Every visible item, flat (used for the dashboard's quick links)."""
    return [item for section in nav_for(user) for item in section["items"]]


def init_app(app: Flask) -> None:
    @app.context_processor
    def inject_area():
        user = auth.current_user()
        if not user:
            return {}
        return {"area_title": AREAS[user.area][0], "area_home": home_endpoint(user), "nav": nav_for(user)}
