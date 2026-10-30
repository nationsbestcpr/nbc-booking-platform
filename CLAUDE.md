# Conventions for this repo

NBC booking engine: Flask + PostgreSQL on Replit. Read `docs/source-of-truth.md`
and `docs/database-architecture.md` before changing data or integrations.

## Data
- One system of record per piece of data. Airtable owns the schedule,
  locations, courses and prices; this engine owns seats, holds, registrations,
  waitlists, transfers, refunds, roles and the instructor portal; Stripe owns
  cards and money movement; GHL owns contact details and conversations. Don't
  add a column that copies another platform's data without updating
  `docs/source-of-truth.md` with the reason.
- The database enforces the business rules (no overselling, permissions on
  extra seats and large refunds, Enrollware/engine per city). Don't
  re-implement them only in Python; let the database refuse and handle the error.
- Writes go through `db.transaction()`. Lock a class row with
  `FOR NO KEY UPDATE`, never `FOR UPDATE` (it deadlocks against foreign keys).
- Calls to other platforms live in `app/integrations/` and go through
  `http.request` (tests swap its transport). Anything the engine must tell
  another platform is queued with `outbox.enqueue(conn, ...)` in the same
  transaction as the change; the worker delivers it with retries.
- Money is integer cents. Times are timestamptz; show class times in the
  class's own time zone (`filters.local_dt`).
- Schema changes go through a migration: `flask --app wsgi db new "..."`,
  plain SQL in `upgrade()` and a real `downgrade()`. Never edit an existing
  migration or its frozen file in `migrations/sql/`. In the same PR, apply the
  change to `db/schema.sql` (and `db/seed.sql` for reference data) so they
  still describe the whole database, add a check to
  `db/tests/schema_checks.sql`, and run `db/tests/migrations_match.sh`.

## Security and privacy
- Public pages never select from `*_private_details` (access codes, on-site
  contacts) and never show payment details.
- Check permissions with `auth.permission_required(...)` or
  `current_user().can(permission, metro_id)`. Roles and permissions are data
  (`db/seed.sql`), not hard-coded role names. New dashboard pages go in the
  sidebar in `app/areas.py` with the permission that shows them; lists filter
  rows by city with `user_has_permission(:uid, perm, metro_id)` in SQL.
- Every staff action writes `audit.record(conn, ...)` in the same transaction
  as the change. Role grants from the app always set `granted_by_user_id` /
  `revoked_by_user_id`, so the database can check them.
- Secrets come from environment variables only. Never commit keys; `.env` is ignored.
- Roles are never chosen at sign-in. New accounts get `user`; `student` is
  granted by the database when a seat is paid for; instructor and staff
  roles only by an admin. `signup_intent` grants nothing.
- Passwords: werkzeug scrypt hashes only, one generic error for any failed
  sign-in, lockout after repeated failures. Every POST form includes
  `{{ csrf_input() }}`.
- `db/demo.sql` (password `demo-password`) is for local development only.

## Look
- Styled after nationsbestcpr.com: navy and red, red pill navigation, pill
  buttons with the arrow circle, "| label |" eyebrows, two-tone headings.

## Copy
- Customer- and staff-facing text is plain English: "helper instructor", not
  "co-instructor" in the UI; "city", not "metro"; no tool names (GHL, Airtable)
  on customer pages.

## Checks before a PR
`pytest` (with `TEST_DATABASE_URL`), `db/tests/schema_checks.sql`,
`db/tests/migrations_match.sh`, and `scripts/export_preview.py` still runs.
