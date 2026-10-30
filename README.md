# NBC booking platform

Nation's Best CPR's own class booking engine. Classes are planned in Airtable,
sold here (classes.nationsbestcpr.com), paid through Stripe Checkout, and
followed up in GHL. The plan and dates live in the NBC Booking Launch Hub; this
repo holds the code and the technical docs.

## What's here

```
app/                 Flask app
  __init__.py        create_app(): settings, database, blueprints, error pages
  config.py          settings from environment variables
  db.py              one SQLAlchemy engine; plain SQL helpers; transaction()
  auth.py            accounts: create, sign in (email + password), work out the role
  security.py        form protection (CSRF token on every POST form)
  areas.py           which dashboard area each account opens, and its tabs
  audit.py           writes the action log (audit_log) inside the change's transaction
  blueprints/
    public.py        landing page: classes on sale, greeting by role
    account.py       /sign-in, /create-account, /sign-out
    me.py            /dashboard (opens your area), /my (student area), /account/profile, /account/settings
    teach.py         /teach: instructor area (classes taught, certifications)
    admin.py         /admin: dashboard, users, roles & permissions, integrations
    webhooks.py      /webhooks/stripe, /webhooks/ghl (stubs until phases 3 and 4)
    health.py        /healthz
  templates/, static/app.css   styled after nationsbestcpr.com
migrations/          database migrations (Alembic): run with `flask --app wsgi db upgrade`
  versions/          0001 initial schema, 0002 reference data, ...
  sql/               the frozen SQL each migration runs
db/
  schema.sql         the whole current database in one file (reference; kept equal to the migrations)
  seed.sql           course families, settings, roles and permissions
  demo.sql           invented Richmond classes and accounts (local only, never production)
  tests/             database rule checks and the last-seat race test
docs/                architecture, source of truth, data ownership
scripts/
  export_preview.py  render every page to static HTML for sharing
tests/               app tests (pytest)
wsgi.py              entry point for flask / gunicorn
```

## How sign-in works

One sign-in page for everyone; nobody picks a role. Every new account is a
regular **user**. Paying for a seat makes them a **student** automatically
(also if they booked as a guest before creating the account). Choosing
"Instructor" at sign-up is recorded for the team to verify; the
**instructor** and staff roles are only ever granted by an admin. Booking
doesn't need an account.

## Run it locally

You need Python 3.12+ and PostgreSQL 16.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements/app.txt

createdb nbc
cp .env.example .env              # set DATABASE_URL=postgresql:///nbc
flask --app wsgi db upgrade       # creates every table and loads roles, settings, course types
flask --app wsgi db load-demo     # optional: invented Richmond classes and accounts (local only)
flask --app wsgi run --debug      # http://localhost:5000
```

Demo accounts all use the password `demo-password` (local only), for example
`riley.brooks@example.test` (user), `alder1@example.test` (student),
`marcus.hale@example.test` (instructor) and `admin@example.test` (admin).
Or create your own at `/create-account`.

## Tests

```bash
createdb nbc_test
psql nbc_test -v ON_ERROR_STOP=1 -f db/schema.sql -f db/seed.sql -f db/demo.sql
TEST_DATABASE_URL=postgresql:///nbc_test pytest                    # app tests
DATABASE_URL=postgresql:///postgres db/tests/migrations_match.sh   # migrations = db/schema.sql + seed.sql
psql nbc_test -v ON_ERROR_STOP=1 -f db/tests/schema_checks.sql     # database rules
DATABASE_URL=postgresql:///postgres db/tests/last_seat_race.sh 40 12
```

## Shareable preview

```bash
DATABASE_URL=postgresql:///nbc python scripts/export_preview.py   # writes build/preview/
```

Renders the pages with the demo data into static HTML, including the landing
page as each kind of account sees it (links rewritten, stylesheet inlined,
forms switched off). `build/preview/index.html`
lists them all. `build/` is not committed.

## Database migrations

The database is built and changed only through migrations in `migrations/`.

| Command (from the repo root) | What it does |
|---|---|
| `flask --app wsgi db upgrade` | Creates or updates the database to the latest version. Safe to run again: it only runs what's missing. |
| `flask --app wsgi db current` | Shows which version the database is on (`0003 (head)` = up to date). |
| `flask --app wsgi db history` | Lists every migration. |
| `flask --app wsgi db downgrade -1` | Undoes the last migration (asks first; `base` undoes all). |
| `flask --app wsgi db new "what changes"` | Starts the next numbered migration file. |
| `flask --app wsgi db load-demo` | Loads demo data into an empty database. Refused unless `APP_ENV=local`. |

Each migration runs in one transaction: if any statement fails, nothing from
that migration is kept. `alembic upgrade head` works too.

## Set up the database on Replit

In the Replit workspace shell, after the code is in the Repl:

1. Add a PostgreSQL database from Replit's Database tool. Replit then sets
   `DATABASE_URL` for you; don't paste it anywhere else.
2. In Secrets, set `APP_ENV` (`staging` or `production`) and `SECRET_KEY`
   (32+ random characters, e.g. from `python -c "import secrets; print(secrets.token_urlsafe(48))"`).
3. Run:
   ```bash
   sh scripts/replit-setup.sh         # installs the packages into .venv
   . .venv/bin/activate               # once per new Shell tab
   flask --app wsgi db upgrade
   flask --app wsgi db current        # expect: 0003 (head)
   ```
   Don't use a plain `pip install` on Replit: its Python is read-only and set
   up for `--user` installs, so it fails with "Permission denied" or "Can not
   perform a '--user' install". The Run button and the deployment build run
   the same setup script. The package list is `requirements/app.txt`; there is
   deliberately no `requirements.txt` at the root, because Replit's publish
   step would run its own failing `pip install` on it.
4. Preset admin login: in Secrets (workspace) and in the published app's
   secrets (Publishing → Adjust settings → Secrets), set
   `SUPERADMIN_EMAIL` (e.g. your NBC email) and `SUPERADMIN_PASSWORD`
   (12+ characters, e.g. from `python -c "import secrets; print(secrets.token_urlsafe(18))"`).
   Every start runs `flask --app wsgi users ensure-superadmin`: it creates that
   account with that password the first time and makes sure it is an admin.
   Change the password afterwards in the app (Password page); restarts keep it.
   The start log (Logs tab) says what happened: `[start] checking the preset
   admin account` then `Preset admin account …: created …`. If that email had
   already signed up, its own password is kept: add the secret
   `SUPERADMIN_RESET_PASSWORD=yes`, republish, sign in, then delete that secret.
   The same secret also clears a lockout after too many wrong tries.
   Without these secrets, make the first admin by hand: create an account at
   `/create-account`, then `flask --app wsgi users make-admin you@nationsbestcpr.com`.
5. Start the app (Run) and open `/healthz`: `"status": "ok"` and
   `"database_env_matches": true` mean the database is ready and belongs to
   this environment.

Notes:
- The upgrade records `APP_ENV` in the database (`app_settings.environment`),
  so a staging app pointed at the production database shows up as a mismatch
  on `/healthz`.
- Replit's published app uses its own production database, separate from the
  workspace one. The published app runs `flask db upgrade` on that database
  every time it starts (`scripts/replit-start.sh`), so publishing applies new
  migrations by itself.
- If a start-up step fails (no database, missing secret, migration error,
  a step stuck for minutes), a non-production app serves its start-up log at
  the published address instead of failing at **Promote**: open the address
  to see the `[start]` lines and the error. In production a failed start
  stops, so Replit keeps the previous version running; read the deployment's
  **Logs** tab instead.
- **When publishing, don't approve Replit's "database changes" prompt.**
  Replit offers to copy the workspace tables to production with its own SQL,
  which skips the extensions and fails (`type "citext" does not exist`).
  Cancel or skip it; the app's own migrations do that job.
- The schema needs the PostgreSQL extensions `citext`, `pg_trgm` and
  `btree_gist`; the first migration installs them. If the upgrade stops with
  "permission denied to create extension", the database user can't install
  extensions and the hosting settings need changing first.
- Never run `db load-demo` there (it refuses anyway).

## Updating a Replit project from GitHub

Every publish adds a "Published your App" commit of Replit's own, so
`git pull` stops with "Diverging branches". The Repl never holds changes of
its own (all code comes from GitHub), so update it with:

```bash
git fetch origin
git reset --hard origin/main
```

Your `.venv`, secrets and databases aren't touched.

## Dashboards

`/dashboard` opens the area for the signed-in account, worked out from its
roles (`app/areas.py`): staff roles → **Admin**, the instructor role →
**Instructor**, everyone else → **My account** (students and users). Each area
has a sidebar; an item shows only to accounts holding its permission, and a
role limited to one city only sees that city's records.

| Area | Sidebar | Permission |
|---|---|---|
| Admin | Dashboard (counts, next classes, recent actions) | any staff role |
| | Classes, one class (class list, waiting list, prices, instructors) | `class.view`; contact details `student.view`; amounts `payment.view`; access codes `class.packet_view` |
| | Bookings | `student.view` |
| | Waiting lists | `waitlist.view` |
| | Locations (cities, sites, rooms) | `class.view` |
| | Users (give/remove roles, suspend) | `user.manage` |
| | Instructors | `instructor.view` |
| | Roles & permissions | view `user.manage` or `role.manage`; change `role.manage` |
| | Integrations | `system.health_view` |
| | Action log | `audit.view` |
| | Business settings (`app_settings`) | `settings.manage` |
| Instructor | Dashboard, Certifications, Pay | `own.classes_view`, `own.certs_view`, `own.pay_view` |
| My account | Dashboard (upcoming, waiting, past classes) | any account |
| Every area | Profile, Password | any account |

Everything is read live from the schema. The only changes made from these
pages are role grants, account status, role permissions and business
settings; each is written to the action log. The database refuses role
grants by people who aren't allowed to make them, and never lets the last
active admin be removed or suspended. `environment` and
`airtable_writeback_enabled` can't be changed from the screen.

## Integrations: Airtable, Stripe, GHL

The code is in `app/integrations/` (one file per platform, plus the outbox
queue and the worker). The published app runs a background worker next to
the web server (its log lines start with `[worker]`); every few seconds it
releases unpaid seat holds and sends queued GHL and Stripe work, and every
`AIRTABLE_SYNC_MINUTES` (default 5) it pulls from Airtable. The
**Integrations** page shows each one's state, what hasn't synced, and data
problems to fix, with Sync now / Retry / Mark fixed buttons.

Put each secret in the workspace's Secrets **and** the published app's
secrets (Publishing → Adjust settings → Secrets), then republish.

**Airtable** (classes, locations, courses → engine)
1. airtable.com/create/tokens → Create token. Scopes `data.records:read` and
   `schema.bases:read`; access: NBC Master Base only. Copy it into
   `AIRTABLE_TOKEN`.
2. Open the base; its address starts `airtable.com/app…/`. That `app…` part
   goes in `AIRTABLE_BASE_ID`.
3. Republish. The first sync runs as the app starts (then every few
   minutes). Integrations → **Check Airtable fields** shows which field the
   sync uses for each item; the names it looks for are in `FIELDS` in
   `app/integrations/airtable.py` (matched to the base as of Oct 2026:
   Class Schedule's Date, Start Time, Abbreviation → location, Course Type →
   course; Master Locations' Name, Address, Metro Area, Time Zone, Max
   Student Count; Course Master's Converted Course Name, Course Family,
   Delivery Type, Price, Companion Price, Max Seats, Duration in Minutes).
4. New cities arrive "Still on Enrollware": nothing sells until an admin
   presses **Start booking here** for that city on Locations. Virtual
   (online) classes and locations are not synced.
5. Optional "Sync now" button in Airtable: on Class Schedule add a Button
   field, action Open URL, formula
   `"https://<your app address>/admin/sync?record=" & RECORD_ID()`.
   It asks staff to sign in; no key is in the link.

A class is on sale when its Status includes **Enrollware**; a Status with
"Cancelled" cancels it; a row that disappears comes off sale (and is listed
as a problem if seats were sold). Rental fees, POC payments and trackers are
never read.

**Stripe** (checkout, payments, refunds)
1. Dashboard (test mode) → Developers → API keys → the secret key `sk_test_…`
   → `STRIPE_SECRET_KEY`. Live keys (`sk_live_…`) only in production; the app
   refuses them elsewhere.
2. Workbench → Webhooks → Create an event destination → events
   `checkout.session.completed`, `checkout.session.async_payment_succeeded`,
   `checkout.session.expired` → endpoint URL as shown on the Integrations page
   (`https://<your app address>/webhooks/stripe`) → copy the signing secret
   `whsec_…` → `STRIPE_WEBHOOK_SECRET`.
3. Coupons: create promotion codes in Stripe; customers enter them on the
   Stripe page.
Test cards: 4242 4242 4242 4242, any future date, any CVC.

**GHL** (one contact + one opportunity per seat)
1. In the sub-account (Sandbox 2 for testing): Settings → Private
   Integrations → Create. Scopes: View/Edit Contacts and View/Edit
   Opportunities (`contacts.readonly`, `contacts.write`,
   `opportunities.readonly`, `opportunities.write`). Copy the token →
   `GHL_API_TOKEN`.
2. The sub-account id (Settings → Business Profile, or the `location/…` part
   of its address) → `GHL_LOCATION_ID`.
3. Republish. On start the app finds the "Community Class" pipeline (or
   `GHL_PIPELINE_NAME`) and its Pending, Complete, No Show, Cancelled and
   Waitlisted stages by name; nothing to press. (If the pipeline isn't
   there, it's listed under Open problems.)
4. Optional custom fields: in Business settings, "GHL custom field ids" takes the ids of the GHL custom fields to fill, as
   `{"session_id": "…", "site": "…", "class_date": "…", "class_time": "…",
   "product": "…", "reg_type": "…", "city": "…", "registration_id": "…"}`.
Opportunities are named `YYYY-MM-DD | COURSE-DELIVERY | MARKET-City` and
start at Pending. GHL → engine webhooks (contact edits, messages, dragged
stages) need the Marketplace app (launch plan 15.5); not built yet.

From a shell: `flask --app wsgi sync airtable`, `sync check-airtable`,
`sync ghl-stages`, `worker once`.

## Deploy (Replit Reserved VM)

`.replit` builds with `scripts/replit-setup.sh` and runs gunicorn from `.venv` on port 5000. Secrets: `APP_ENV`, `SECRET_KEY`,
`PUBLIC_BASE_URL` (Replit provides `DATABASE_URL`). Run the database steps
above before the first publish.

## Build phases

| Phase | Status |
|---|---|
| Database schema, roles and permissions | Done (PR #1) |
| App skeleton: sign-in, create account, landing page of classes | Done |
| Dashboards: admin (classes, bookings, waiting lists, locations, users, instructors, roles, integrations, action log, business settings), instructor, student; preset admin login | Done |
| 1. Airtable sync (pull) | Done |
| 3. Seat holds and Stripe Checkout | Done |
| 4. GHL contacts and opportunities (engine → GHL) | Done |
| 2. Search pages and SEO | Next |
| 4. GHL webhooks (GHL → engine) · 4a tracking · 4b Microsoft sign-in · 4c instructor portal · Airtable write-back | To do |
