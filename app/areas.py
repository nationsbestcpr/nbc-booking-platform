"""Dashboard areas and their tabs.

Each signed-in account opens one area, worked out from its roles
(CurrentUser.area): staff roles -> admin, the instructor role -> instructor,
everyone else -> student. A tab with a permission shows only to accounts
holding it, so what each role sees is set by role data, not by code.
"""

from __future__ import annotations

from flask import Flask, request

from . import auth

# area -> (title, home endpoint, [(endpoint, label, permissions needed: any of)])
AREAS: dict[str, tuple[str, str, list[tuple[str, str, tuple[str, ...]]]]] = {
    "admin": ("Admin", "admin.home", [
        ("admin.home", "Dashboard", ()),
        ("admin.users", "Users", ("user.manage",)),
        ("admin.roles", "Roles & permissions", ("user.manage", "role.manage")),
        ("admin.integrations", "Integrations", ("system.health_view",)),
        ("me.profile", "Profile", ()),
        ("me.settings", "Settings", ()),
    ]),
    "instructor": ("Instructor", "teach.home", [
        ("teach.home", "Dashboard", ()),
        ("me.profile", "Profile", ()),
        ("me.settings", "Settings", ()),
    ]),
    "student": ("My account", "me.home", [
        ("me.home", "Dashboard", ()),
        ("me.profile", "Profile", ()),
        ("me.settings", "Settings", ()),
    ]),
}


def home_endpoint(user: auth.CurrentUser) -> str:
    return AREAS[user.area][1]


def tabs_for(user: auth.CurrentUser) -> list[dict]:
    title, _home, tabs = AREAS[user.area]
    current = request.endpoint or ""
    out = []
    for endpoint, label, perms in tabs:
        if perms and not any(p in user.permissions for p in perms):
            continue
        section = endpoint.split(".")[0] + "." + endpoint.split(".")[1].split("_")[0]
        out.append({"endpoint": endpoint, "label": label,
                    "current": current == endpoint or (endpoint != AREAS[user.area][1] and current.startswith(section + "_"))})
    return out


def init_app(app: Flask) -> None:
    @app.context_processor
    def inject_area():
        user = auth.current_user()
        if not user:
            return {}
        return {"area_title": AREAS[user.area][0], "area_home": home_endpoint(user)}
