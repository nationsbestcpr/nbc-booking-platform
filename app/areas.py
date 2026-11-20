"""Dashboard areas and their sidebar navigation, and which pages the booking app still shows.

The booking app keeps three things: user management (the Superadmin), the
instructor portal and the student portal (with booking and checkout). The
desk's work (finding students, refunds, moves, cancellations, phone and
invoice bookings, attendance at the desk, waiting lists, class lists) is done
in the NBC Ops Hub through the booking engine's API (/api/v1, docs/api.md).

Each signed-in account opens one area, worked out from its roles
(CurrentUser.area): people who manage users, roles or API keys -> admin (the
Superadmin's user management), other staff -> console (a page pointing them
to the Ops Hub), the instructor role -> instructor, everyone else -> student.
A sidebar item with permissions shows only to accounts holding at least one
of them, and a section with no visible items is left out.

The old desk and admin pages stay as a fallback until the Ops Hub's screens
are live: off every sidebar and open only to a Superadmin (FALLBACK_OPEN_TO;
the tests open them to staff to keep testing them). Superadmin → Overview
lists them.

Detail pages light up their list's item: an endpoint 'admin.users_detail'
belongs to the 'admin.users' item.
"""

from __future__ import annotations

from flask import Flask, abort, current_app, request

from . import auth

Item = tuple[str, str, tuple[str, ...]]          # (endpoint, label, any of these permissions)

# area -> (title, home endpoint, [(section label, [items])])
_MY_ACCOUNT: tuple[str, list[Item]] = ("My account", [
    ("me.profile", "Profile", ()),
    ("me.settings", "Password", ()),
])
AREAS: dict[str, tuple[str, str, list[tuple[str, list[Item]]]]] = {
    "admin": ("Superadmin", "admin.home", [
        ("User management", [
            ("admin.home", "Overview", ()),
            ("admin.users", "Users", ("user.manage",)),
            ("admin.users_new", "Add a person", ("user.manage",)),
            ("admin.roles", "Roles & permissions", ("user.manage", "role.manage")),
            ("admin.api_keys", "API keys", ("api_key.manage",)),
        ]),
        _MY_ACCOUNT,
    ]),
    "console": ("Staff", "console.home", [
        ("Your work", [
            ("console.home", "Where your desk is", ()),
        ]),
        _MY_ACCOUNT,
    ]),
    "instructor": ("Instructor", "teach.home", [
        ("Teaching", [
            ("teach.home", "Dashboard", ()),
            ("teach.certifications", "Certifications", ("own.certs_view",)),
            ("teach.pay", "Pay", ("own.pay_view",)),
        ]),
        _MY_ACCOUNT,
    ]),
    "student": ("My account", "me.home", [
        ("My classes", [
            ("me.home", "Dashboard", ()),
        ]),
        _MY_ACCOUNT,
    ]),
}

# Pages of the admin and console sections every account allowed by its
# permissions keeps: user management and the staff page.
KEPT = {
    "admin.home", "admin.users", "admin.users_detail", "admin.users_grant", "admin.users_revoke",
    "admin.users_status", "admin.users_new", "admin.view_as", "admin.view_as_stop", "admin.roles",
    "admin.roles_detail", "admin.api_keys", "admin.api_keys_revoke", "console.home",
}

# The fallback pages (until the Ops Hub's screens are live), as the Superadmin's overview lists them.
FALLBACK = [
    ("Desk", [("console.desk_today", "Today at the desk"), ("console.find", "Find a student"),
              ("console.new_booking", "Book by phone or invoice"), ("console.refunds_list", "Refunds"),
              ("console.approvals", "Refund approvals")]),
    ("Classes", [("admin.classes", "Classes"), ("admin.bookings", "Bookings"), ("admin.waitlists", "Waiting lists"),
                 ("admin.locations", "Locations"), ("admin.instructors", "Instructors")]),
    ("Set-up and system", [("admin.questions", "Booking questions"), ("admin.addons", "Add-ons"),
                           ("admin.integrations", "Integrations"), ("admin.settings", "Business settings"),
                           ("admin.log", "Action log"), ("admin.test_data", "Test data")]),
]


def fallback_open(user: auth.CurrentUser) -> bool:
    """May this person open the fallback pages? A Superadmin always; staff only where the config says."""
    return user.is_admin or (current_app.config.get("FALLBACK_OPEN_TO") == "staff" and user.is_staff)


TEST_SITE_ONLY = {"admin.test_data"}          # never in the live site's sidebar


def home_endpoint(user: auth.CurrentUser) -> str:
    return AREAS[user.area][1]


def _is_current(endpoint: str, current: str) -> bool:
    return current == endpoint or current.startswith(endpoint + "_")


def nav_for(user: auth.CurrentUser) -> list[dict]:
    """[{label, items: [{endpoint, label, current}]}] for this account's area."""
    current = request.endpoint or ""
    live = current_app.config["APP_ENV"] == "production"
    sections = []
    for label, items in AREAS[user.area][2]:
        visible = [{"endpoint": e, "label": l, "current": _is_current(e, current)}
                   for e, l, perms in items if (not perms or any(p in user.permissions for p in perms))
                   and not (live and e in TEST_SITE_ONLY)]
        if visible:
            sections.append({"label": label, "items": visible})
    return sections


def tabs_for(user: auth.CurrentUser) -> list[dict]:
    """Every visible item, flat (used for the dashboard's quick links)."""
    return [item for section in nav_for(user) for item in section["items"]]


def init_app(app: Flask) -> None:
    app.config.setdefault("FALLBACK_OPEN_TO", "superadmin")

    @app.before_request
    def fallback_pages_only_for_superadmin():
        """The desk and admin pages the Ops Hub replaces answer only a Superadmin (others: not found)."""
        if request.blueprint not in ("admin", "console") or request.endpoint in KEPT:
            return None
        user = auth.current_user()
        if user is None or not user.is_staff or auth.viewing_as():
            return None                      # sign-in, permissions and View as answer as before
        if not fallback_open(user):
            abort(404)
        return None

    @app.context_processor
    def inject_area():
        user = auth.current_user()
        if not user:
            return {}
        return {"area_title": AREAS[user.area][0], "area_home": home_endpoint(user), "nav": nav_for(user)}
