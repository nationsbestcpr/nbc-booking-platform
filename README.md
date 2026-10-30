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
  blueprints/
    public.py        landing page: classes on sale, greeting by role
    account.py       /sign-in, /create-account, /sign-out
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
| `flask --app wsgi db current` | Shows which version the database is on (`0002 (head)` = up to date). |
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
   flask --app wsgi db current        # expect: 0002 (head)
   ```
   Don't use a plain `pip install` on Replit: its Python is read-only and set
   up for `--user` installs, so it fails with "Permission denied" or "Can not
   perform a '--user' install". The Run button and the deployment build run
   the same setup script. The package list is `requirements/app.txt`; there is
   deliberately no `requirements.txt` at the root, because Replit's publish
   step would run its own failing `pip install` on it.
4. Start the app (Run) and open `/healthz`: `"status": "ok"` and
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
- If a publish fails at **Promote** ("did not become ready in time"), open
  the deployment's **Logs** tab (not Overview): each start-up step prints a
  `[start]` line, so the last one shows where it stopped.
- **When publishing, don't approve Replit's "database changes" prompt.**
  Replit offers to copy the workspace tables to production with its own SQL,
  which skips the extensions and fails (`type "citext" does not exist`).
  Cancel or skip it; the app's own migrations do that job.
- The schema needs the PostgreSQL extensions `citext`, `pg_trgm` and
  `btree_gist`; the first migration installs them. If the upgrade stops with
  "permission denied to create extension", the database user can't install
  extensions and the hosting settings need changing first.
- Never run `db load-demo` there (it refuses anyway).

## Deploy (Replit Reserved VM)

`.replit` builds with `scripts/replit-setup.sh` and runs gunicorn from `.venv` on port 5000. Secrets: `APP_ENV`, `SECRET_KEY`,
`PUBLIC_BASE_URL` (Replit provides `DATABASE_URL`). Run the database steps
above before the first publish.

## Build phases

| Phase | Status |
|---|---|
| Database schema, roles and permissions | Done (PR #1) |
| App skeleton: sign-in, create account, landing page of classes | This branch |
| 1. Airtable sync | Next |
| 2. Search pages and SEO | To do |
| 3. Seat holds and Stripe Checkout | To do |
| 4. GHL sync · 4a tracking · 4b staff dashboards + Microsoft sign-in · 4c instructor portal | To do |
