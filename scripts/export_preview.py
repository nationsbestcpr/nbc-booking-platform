"""Render the app's pages to static HTML for a shareable preview.

Uses Flask's test client against a database loaded with schema + seed + demo
data. Renders the landing page signed out and signed in as several demo
accounts (to show how the role is worked out), the sign-in and
create-account pages, and each area's dashboard tabs (admin, instructor,
student) as that kind of account sees them. Links between pages are rewritten, the stylesheet is
inlined, and forms are switched off (there is no server behind a preview).

    DATABASE_URL=postgresql:///nbc python scripts/export_preview.py [out_dir]

The output's index.html lists every page. It has no <html>/<head> wrapper so
it can be published as a claude.ai Artifact; the other files are full pages.
"""

from __future__ import annotations

import html
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from app import create_app, db  # noqa: E402

# Signed-in views of the landing page: key -> (demo email, label, what it shows)
SIGNED_IN = {
    "user": ("riley.brooks@example.test", "Signed in: new user",
             "A brand-new account. Everyone starts as a regular user."),
    "instructor-signup": ("jamie.ortiz@example.test", "Signed in: signed up as an instructor",
                          "Chose \"Instructor\" at sign-up. Still a regular user until the team verifies them."),
    "student": ("alder1@example.test", "Signed in: student",
                "Booked a class as a guest, then created an account: recognised as a student, sees their class."),
    "instructor": ("marcus.hale@example.test", "Signed in: instructor",
                   "Instructor role granted by an admin."),
    "admin": ("admin@example.test", "Signed in: admin",
              "Office staff: sees the classes, plus a link to the admin dashboard."),
}

# Dashboards: key -> (demo email, [(path, file, title, note)]). Each account's
# tabs link to its own copies of Profile and Settings.
DASHBOARDS = {
    "admin": ("admin@example.test", [
        ("/admin", "admin.html", "Admin: dashboard", "Counts, next classes and recent actions."),
        ("/admin/classes", "admin-classes.html", "Admin: classes", "Every class with live seats, waiting and lead instructor."),
        ("/admin/classes/{session:2027-01-16-henrico}", "admin-class.html", "Admin: one class",
         "Class list, waiting list, prices, instructors, private details."),
        ("/admin/bookings", "admin-bookings.html", "Admin: bookings", "One row per seat; search by student."),
        ("/admin/waitlists", "admin-waitlists.html", "Admin: waiting lists", "Who is waiting, in queue order."),
        ("/admin/locations", "admin-locations.html", "Admin: locations", "Cities, sites and rooms."),
        ("/admin/users", "admin-users.html", "Admin: users", "Find people; filter by role and status."),
        ("/admin/users/{riley.brooks@example.test}", "admin-user.html", "Admin: one user",
         "Give and remove roles, suspend, history."),
        ("/admin/instructors", "admin-instructors.html", "Admin: instructors", "Profiles, certifications, upcoming classes."),
        ("/admin/roles", "admin-roles.html", "Admin: roles and permissions", "Every role, who has it, what it can do."),
        ("/admin/roles/customer_service", "admin-role.html", "Admin: one role", "Tick what the role can do."),
        ("/admin/integrations", "admin-integrations.html", "Admin: integrations",
         "Airtable, Stripe, GHL, Microsoft 365: set up, working or needing attention."),
        ("/admin/log", "admin-log.html", "Admin: action log", "Every staff action, newest first."),
        ("/admin/settings", "admin-settings.html", "Admin: business settings", "Hold minutes, offer window, pay rates."),
        ("/account/profile", "admin-profile.html", "Admin: profile", "Name, email, roles."),
        ("/account/settings", "admin-settings-password.html", "Admin: password", "Change password."),
    ]),
    "instructor": ("marcus.hale@example.test", [
        ("/teach", "teach.html", "Instructor: dashboard", "Classes they teach (lead or helper), certifications."),
        ("/teach/certifications", "teach-certifications.html", "Instructor: certifications", "Expiry dates and checks."),
        ("/teach/pay", "teach-pay.html", "Instructor: pay", "One record per completed class."),
        ("/account/profile", "teach-profile.html", "Instructor: profile", "Account plus instructor profile."),
        ("/account/settings", "teach-settings.html", "Instructor: password", "Change password."),
    ]),
    "student": ("alder1@example.test", [
        ("/my", "my.html", "Student: dashboard", "Upcoming classes, waiting lists, past classes."),
        ("/account/profile", "my-profile.html", "Student: profile", "Name, email, roles."),
        ("/account/settings", "my-settings.html", "Student: password", "Change password."),
    ]),
}

PUBLIC = [
    ("/", "home.html", "Landing page", "What everyone sees: the classes on sale."),
    ("/sign-in", "sign-in.html", "Sign in", "One sign-in for everyone. The role is worked out from the account."),
    ("/create-account", "create-account.html", "Create an account", "Asks: student or instructor."),
]


def main(out_dir: Path) -> None:
    app = create_app({"APP_ENV": "local", "DATABASE_URL": os.environ["DATABASE_URL"], "PREVIEW_MODE": True,
                      "PUBLIC_BASE_URL": "https://classes.nationsbestcpr.com"})
    css = (ROOT / "app" / "static" / "app.css").read_text()
    with app.app_context():
        ids = dict(db.connection().execute(text("SELECT email, id FROM user_accounts")).all())

    with app.app_context():
        sessions = dict(db.connection().execute(text(
            "SELECT DISTINCT ON (slug) slug, id FROM sessions ORDER BY slug, id")).all())

    def real_path(path: str) -> str:
        def sub(m: re.Match) -> str:
            ref = m.group(1)
            return str(sessions[ref.split(":", 1)[1]]) if ref.startswith("session:") else str(ids[ref])
        return re.sub(r"\{([^}]+)\}", sub, path)

    public_files = {path: name for path, name, *_ in PUBLIC}
    public_files.update({f"/__as__/{k}": f"home-{k}.html" for k in SIGNED_IN})
    out_dir.mkdir(parents=True, exist_ok=True)

    def files_for(key: str | None) -> dict[str, str]:
        files = dict(public_files)
        if key in DASHBOARDS:
            files.update({real_path(p): f for p, f, *_ in DASHBOARDS[key][1]})
            files["/dashboard"] = DASHBOARDS[key][1][0][1]
        return files

    def rewrite(body: str, files: dict[str, str]) -> str:
        body = body.replace('<link rel="stylesheet" href="/static/app.css">', f"<style>\n{css}\n</style>")
        body = re.sub(r'<form ([^>]*?)action="[^"]*"', r'<form \1action="#" onsubmit="event.preventDefault()"', body)

        def link(m: re.Match) -> str:
            target = m.group(1)
            if target == "/__preview_index__":
                return 'href="index.html"'
            path, _, frag = target.partition("#")
            path = path.split("?", 1)[0]
            if path in files:
                return f'href="{files[path]}' + (f'#{frag}' if frag else "") + '"'
            return 'href="#" aria-disabled="true"'

        return re.sub(r'href="(/[^"]*)"', link, body)

    def render(path: str, user_email: str | None, out_name: str, files: dict[str, str]) -> None:
        client = app.test_client()
        if user_email:
            with client.session_transaction() as sess:
                sess["user_id"] = ids[user_email]
        res = client.get(path)
        if res.status_code != 200:
            raise SystemExit(f"{path} returned {res.status_code}")
        (out_dir / out_name).write_text(rewrite(res.get_data(as_text=True), files))

    for path, name, *_ in PUBLIC:
        render(path, None, name, files_for(None))
    for key, (email, *_rest) in SIGNED_IN.items():
        render("/", email, f"home-{key}.html", files_for(key))
    for key, (email, pages) in DASHBOARDS.items():
        for path, name, *_ in pages:
            render(real_path(path), email, name, files_for(key))

    write_index(out_dir, css)
    count = len(PUBLIC) + len(SIGNED_IN) + sum(len(p) for _e, p in DASHBOARDS.values()) + 1
    print(f"Wrote {count} files to {out_dir}")


def write_index(out_dir: Path, css: str) -> None:
    def git(*args: str) -> str:
        try:
            return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()
        except (OSError, subprocess.CalledProcessError):
            return "unknown"

    def card(href: str, title: str, note: str) -> str:
        return (f'<a class="class-card" style="grid-template-columns:1fr;text-decoration:none" href="{html.escape(href)}">'
                f'<div class="class-body"><h3>{html.escape(title)}</h3><span class="meta">{html.escape(note)}</span></div></a>')

    public = "".join(card(name, title, note) for _p, name, title, note in PUBLIC)
    signed = "".join(card(f"home-{k}.html", label, note) for k, (_e, label, note) in SIGNED_IN.items())
    dash = "".join(card(f, title, note) for _e, pages in DASHBOARDS.values() for _p, f, title, note in pages)
    generated = datetime.now(timezone.utc).strftime("%b %-d, %Y %H:%M UTC")
    page = f"""<title>NBC Booking Engine Preview</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Urbanist:wght@400;500;600;700;800&display=swap">
<style>
{css}
</style>
<header class="topbar"><div class="wrap">
  <span class="logo"><span class="logo-mark" aria-hidden="true"></span><span class="logo-text">NATION'S BEST<small>CPR</small></span></span>
  <a class="pill" href="home.html">Open the landing page <span class="arrow" aria-hidden="true">→</span></a>
</div></header>
<main><div class="wrap">
  <section class="section">
    <div class="section-head"><div class="stack">
      <span class="eyebrow">Booking engine skeleton</span>
      <h1 class="title">The Pages So Far <span class="accent">and Who Sees What</span></h1>
      <p class="muted">Rendered from the real Flask app with demo Richmond classes for January 2027. Forms need the running app, so they're switched off here.</p>
      <p class="muted small num">Branch {html.escape(git('rev-parse', '--abbrev-ref', 'HEAD'))} · commit {html.escape(git('rev-parse', '--short', 'HEAD'))} · generated {generated}</p>
    </div></div>
    <div class="class-grid">{public}</div>
  </section>
  <section class="section" style="padding-top:0">
    <div class="section-head"><div class="stack">
      <span class="eyebrow">After signing in</span>
      <h2 class="title">Same Page, <span class="accent">Role Worked Out for You</span></h2>
      <p class="muted">Nobody picks a role. New accounts are regular users; paying for a seat makes them a student; instructor and staff roles are granted by an admin.</p>
    </div></div>
    <div class="class-grid">{signed}</div>
  </section>
  <section class="section" style="padding-top:0">
    <div class="section-head"><div class="stack">
      <span class="eyebrow">Dashboards</span>
      <h2 class="title">Each Area, <span class="accent">As Its Owner Sees It</span></h2>
      <p class="muted">A sidebar per area: admin (classes, bookings, waiting lists, locations, users, instructors, roles, integrations, action log, business settings), instructor and student. Each section shows only to roles allowed to see it.</p>
    </div></div>
    <div class="class-grid">{dash}</div>
  </section>
</div></main>
"""
    (out_dir / "index.html").write_text(page)


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "preview")
