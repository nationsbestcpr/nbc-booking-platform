"""`flask db ...` commands: run database migrations from a shell (e.g. Replit's).

    flask --app wsgi db upgrade          # create or update the database to the latest version
    flask --app wsgi db current          # which version the database is on
    flask --app wsgi db history          # list every migration
    flask --app wsgi db downgrade -1     # undo the last migration (asks first)
    flask --app wsgi db new "add x"      # start a new migration file
    flask --app wsgi db load-demo        # invented demo data, local development only
    flask --app wsgi users make-admin EMAIL   # first admin of a new database
    flask --app wsgi users ensure-superadmin  # preset admin from SUPERADMIN_EMAIL / SUPERADMIN_PASSWORD
    flask --app wsgi worker run          # background jobs: Airtable sync, holds, GHL/Stripe queue
    flask --app wsgi worker once         # one pass of the above, then stop
    flask --app wsgi sync airtable [--record recXXXX]   # pull from Airtable now
    flask --app wsgi sync check-airtable # how the base's fields match the sync
    flask --app wsgi sync ghl-stages     # load the GHL pipeline's stage ids
    flask --app wsgi sync ghl-create-fields [--dry-run]   # create the GHL custom fields the engine fills
    flask --app wsgi db label            # mark this database as belonging to this APP_ENV
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import click
from alembic import command
from alembic.config import Config as AlembicConfig
from flask import Flask, current_app
from flask.cli import AppGroup

ROOT = Path(__file__).resolve().parent.parent
db_cli = AppGroup("db", help="Database migrations.")


def _alembic() -> AlembicConfig:
    cfg = AlembicConfig(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["database_url"] = current_app.config["DATABASE_URL"]
    return cfg


@db_cli.command("upgrade")
@click.argument("revision", default="head")
def upgrade(revision: str) -> None:
    """Bring the database up to REVISION (default: the latest)."""
    click.echo(f"Upgrading the {current_app.config['APP_ENV']} database to {revision} ...")
    command.upgrade(_alembic(), revision)
    click.echo("Done.")


@db_cli.command("downgrade", context_settings={"ignore_unknown_options": True})
@click.argument("revision")
@click.option("--yes", is_flag=True, help="Don't ask for confirmation.")
def downgrade(revision: str, yes: bool) -> None:
    """Undo migrations back to REVISION ('-1' for the last one, 'base' for all)."""
    env = current_app.config["APP_ENV"]
    if not yes:
        click.confirm(f"This removes database objects and their data on the {env} database. Continue?",
                      abort=True)
    if env == "production" and revision == "base":
        click.confirm("That deletes the whole production database. Type y only if you are sure.", abort=True)
    command.downgrade(_alembic(), revision)
    click.echo("Done.")


@db_cli.command("verify")
@click.option("--record/--no-record", default=False, help="Also show the result on the Integrations page.")
def verify(record: bool) -> None:
    """Check the database has every table, column, index, function, trigger and view in db/schema.sql."""
    from . import schema_check
    lines = schema_check.missing()
    if record:
        schema_check.record(lines)
    if not lines:
        click.echo("The database has everything db/schema.sql describes.")
        return
    click.echo("The database is MISSING:")
    for line in lines:
        click.echo(f"  - {line}")
    raise SystemExit(1)


@db_cli.command("repair")
def repair() -> None:
    """Put back parts the database is missing (db/repair.sql; safe on a complete database)."""
    from pathlib import Path
    from . import db as _db, schema_check
    sql = (Path(__file__).resolve().parent.parent / "db" / "repair.sql").read_text()
    before = schema_check.missing()
    with _db.engine().begin() as conn:
        with conn.connection.dbapi_connection.cursor() as cur:
            cur.execute(sql)                       # no parameters: several statements allowed
    after = schema_check.missing()
    fixed = sorted(set(before) - set(after))
    click.echo(("Put back: " + ", ".join(fixed)) if fixed else "Nothing needed putting back.")
    if after:
        click.echo("Still missing (needs the developer): " + ", ".join(after))
        raise SystemExit(1)


@db_cli.command("label")
@click.option("--auto", is_flag=True, help="Only label a database still marked 'local' (the seed's value) "
                                           "for the test site. Used on every start.")
@click.option("--yes", is_flag=True, help="Don't ask first.")
def label(auto: bool, yes: bool) -> None:
    """Mark this database as the one for this app's APP_ENV (app_settings 'environment').

    /healthz says "degraded" while the label and APP_ENV differ: that's how a production app
    pointed at a test database (or the other way round) shows up. --auto only fixes a test-site
    database that was never labelled; it never relabels staging <-> production."""
    from . import db as _db
    app_env = current_app.config["APP_ENV"]
    now = _db.fetch_value("SELECT value #>> '{}' FROM app_settings WHERE key = 'environment'")
    if now == app_env:
        click.echo(f"This database is already labelled {app_env}.")
        return
    if auto:
        if not (now == "local" and app_env == "staging"):
            click.echo(f"Database labelled {now!r}, app is {app_env!r}: leaving it (run `db label` to change it).")
            return
    elif not yes:
        click.confirm(f"This database is labelled {now!r}. Label it {app_env!r}, the app's APP_ENV?", abort=True)
    with _db.transaction() as conn:
        from sqlalchemy import text as _text
        conn.execute(_text("UPDATE app_settings SET value = to_jsonb(CAST(:v AS text)), updated_at = now() "
                           "WHERE key = 'environment'"), {"v": app_env})
    click.echo(f"Database labelled {app_env} (was {now}).")


@db_cli.command("current")
def current() -> None:
    """Show which migration the database is on."""
    command.current(_alembic(), verbose=False)


@db_cli.command("history")
def history() -> None:
    """List every migration, oldest last."""
    command.history(_alembic(), verbose=False)


@db_cli.command("new")
@click.argument("message")
def new(message: str) -> None:
    """Start a new, empty migration file named after MESSAGE."""
    cfg = _alembic()
    from alembic.script import ScriptDirectory
    heads = ScriptDirectory.from_config(cfg).get_heads()
    next_id = f"{int(heads[0]) + 1:04d}" if heads and heads[0].isdigit() else None
    command.revision(cfg, message=message, rev_id=next_id)


@db_cli.command("load-demo")
def load_demo() -> None:
    """Load invented demo classes and accounts (password: demo-password). Local only."""
    if current_app.config["APP_ENV"] != "local":
        raise click.ClickException("Demo data is for local development only. APP_ENV is "
                                   f"{current_app.config['APP_ENV']!r}; refusing.")
    from . import db
    sql = (ROOT / "db" / "demo.sql").read_text()
    with db.engine().begin() as conn:
        if conn.exec_driver_sql("SELECT count(*) FROM metros").scalar():
            raise click.ClickException("This database already has city data. Load the demo into an empty one.")
        with conn.connection.dbapi_connection.cursor() as cur:
            cur.execute(sql)
    click.echo("Demo data loaded. Demo accounts sign in with: demo-password")


users_cli = AppGroup("users", help="Accounts.")


@users_cli.command("make-admin")
@click.argument("email")
@click.option("--yes", is_flag=True, help="Don't ask for confirmation.")
def make_admin(email: str, yes: bool) -> None:
    """Give an existing account the Admin role (for the first admin of a database).

    After that, admins give roles from the dashboard (Admin > Users). The account
    must exist first: create it at /create-account.
    """
    from sqlalchemy import text

    from . import db
    env = current_app.config["APP_ENV"]
    with db.engine().begin() as conn:
        account = conn.execute(text("SELECT id, display_name, status FROM user_accounts WHERE email = :e"),
                               {"e": email.strip().lower()}).mappings().first()
        if account is None:
            raise click.ClickException(f"No account for {email}. Create it at /create-account first, then run this again.")
        has = conn.execute(text("""SELECT 1 FROM user_roles WHERE user_account_id = :id AND role_code = 'admin'
                                    AND revoked_at IS NULL"""), {"id": account["id"]}).first()
        if has:
            click.echo(f"{account['display_name']} is already an admin.")
            return
        if not yes:
            click.confirm(f"Give {account['display_name']} <{email}> the Admin role (every permission) "
                          f"on the {env} database?", abort=True)
        conn.execute(text("UPDATE user_accounts SET status = 'active' WHERE id = :id AND status <> 'active'"),
                     {"id": account["id"]})
        conn.execute(text("INSERT INTO user_roles (user_account_id, role_code) VALUES (:id, 'admin')"),
                     {"id": account["id"]})
        conn.execute(text("""
            INSERT INTO audit_log (actor_type, action, entity_type, entity_id, after, reason)
            VALUES ('system', 'user.role_grant', 'user_account', :id, '{"role": "admin"}', 'flask users make-admin')
        """), {"id": account["id"]})
    click.echo(f"{account['display_name']} is now an admin. They see the admin dashboard after signing in.")


SUPERADMIN_MIN_PASSWORD = 12


def _secret(name: str) -> str:
    """A secret's value without surrounding spaces or quotes pasted along with it."""
    value = os.environ.get(name, "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


@users_cli.command("ensure-superadmin")
def ensure_superadmin() -> None:
    """Make sure the preset admin account from the secrets exists and can sign in.

    Reads SUPERADMIN_EMAIL, SUPERADMIN_PASSWORD (12+ characters) and optionally
    SUPERADMIN_NAME and SUPERADMIN_RESET_PASSWORD. Runs on every start:
      * no account with that email yet: creates it, with that password, active;
      * the account exists: keeps its password (a password changed in the app
        stays changed) and makes sure it holds the Admin role;
      * SUPERADMIN_RESET_PASSWORD=yes: sets the password to SUPERADMIN_PASSWORD,
        clears a sign-in lockout and switches the account back on (for an
        account that existed before, or a forgotten password); remove the
        secret afterwards so a password changed in the app isn't overwritten;
      * SUPERADMIN_EMAIL not set: does nothing.
    The password is never printed or stored except as a scrypt hash.
    """
    from sqlalchemy import text
    from werkzeug.security import generate_password_hash

    from . import db
    email = _secret("SUPERADMIN_EMAIL").lower()
    password = _secret("SUPERADMIN_PASSWORD")
    name = " ".join(_secret("SUPERADMIN_NAME").split()) or "Super Admin"
    reset = _secret("SUPERADMIN_RESET_PASSWORD").lower() in {"1", "yes", "true", "on"}
    if not email:
        click.echo("SUPERADMIN_EMAIL is not set: no preset admin account.")
        return
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise click.ClickException("SUPERADMIN_EMAIL isn't an email address.")
    if (reset or password) and len(password) < SUPERADMIN_MIN_PASSWORD:
        raise click.ClickException(
            f"SUPERADMIN_PASSWORD must be {SUPERADMIN_MIN_PASSWORD}+ characters.")
    notes: list[str] = []
    with db.engine().begin() as conn:
        account = conn.execute(text("SELECT id, status FROM user_accounts WHERE email = :e"),
                               {"e": email}).mappings().first()
        if account is None:
            if not password:
                raise click.ClickException("SUPERADMIN_PASSWORD must be set to create the preset admin account.")
            uid = conn.execute(text("""
                INSERT INTO user_accounts (email, display_name, status, email_verified_at)
                VALUES (:e, :n, 'active', now()) RETURNING id
            """), {"e": email, "n": name}).scalar_one()
            conn.execute(text("INSERT INTO user_roles (user_account_id, role_code) VALUES (:uid, 'user')"), {"uid": uid})
            notes.append("created with the password in SUPERADMIN_PASSWORD")
        else:
            uid = account["id"]
        identity = conn.execute(text("""SELECT id, locked_until > now() AS locked FROM user_identities
                                         WHERE user_account_id = :uid AND provider = 'password'"""),
                                {"uid": uid}).mappings().first()
        if identity is None and password:
            conn.execute(text("""
                INSERT INTO user_identities (user_account_id, provider, subject, password_hash, password_changed_at)
                VALUES (:uid, 'password', :e, :h, now())
            """), {"uid": uid, "e": email, "h": generate_password_hash(password)})
            if account is not None:
                notes.append("password from SUPERADMIN_PASSWORD added")
        elif identity is not None and reset:
            conn.execute(text("""
                UPDATE user_identities SET password_hash = :h, password_changed_at = now(),
                       failed_attempts = 0, locked_until = NULL
                 WHERE id = :id
            """), {"h": generate_password_hash(password), "id": identity["id"]})
            notes.append("password reset to SUPERADMIN_PASSWORD and sign-in unlocked "
                         "(remove SUPERADMIN_RESET_PASSWORD now)")
        elif identity is not None:
            notes.append("existing account: its own password was kept (to use the one in "
                         "SUPERADMIN_PASSWORD, add the secret SUPERADMIN_RESET_PASSWORD=yes and republish)")
            if identity["locked"]:
                notes.append("sign-in is locked for a few minutes after too many wrong tries")
        if account is not None and account["status"] != "active":
            if reset:
                conn.execute(text("UPDATE user_accounts SET status = 'active' WHERE id = :uid"), {"uid": uid})
                notes.append("account switched back on")
            else:
                notes.append(f"account is {account['status']}; SUPERADMIN_RESET_PASSWORD=yes switches it back on")
        is_admin = conn.execute(text("""SELECT 1 FROM user_roles WHERE user_account_id = :uid
                                         AND role_code = 'admin' AND revoked_at IS NULL"""), {"uid": uid}).first()
        if not is_admin:
            conn.execute(text("INSERT INTO user_roles (user_account_id, role_code) VALUES (:uid, 'admin')"), {"uid": uid})
            conn.execute(text("""
                INSERT INTO audit_log (actor_type, action, entity_type, entity_id, after, reason)
                VALUES ('system', 'user.role_grant', 'user_account', :uid, '{"role": "admin"}',
                        'Preset admin account (SUPERADMIN_EMAIL)')
            """), {"uid": uid})
            if account is not None:
                notes.append("given the Admin role")
        if reset and account is not None:
            conn.execute(text("""
                INSERT INTO audit_log (actor_type, action, entity_type, entity_id, reason)
                VALUES ('system', 'user.password_reset', 'user_account', :uid,
                        'Preset admin account (SUPERADMIN_RESET_PASSWORD)')
            """), {"uid": uid})
    if account is not None and not notes:
        notes.append("already set up")
    click.echo(f"Preset admin account {email}: " + "; ".join(notes) + ".")


worker_cli = AppGroup("worker", help="Background jobs.")
sync_cli = AppGroup("sync", help="Run one integration job now.")


@worker_cli.command("run")
def worker_run() -> None:
    """Run the background worker until stopped."""
    from .integrations import worker
    worker.run_forever()


@worker_cli.command("once")
def worker_once() -> None:
    """One pass: release lapsed holds, send due jobs, sync Airtable if due."""
    from .integrations import worker
    click.echo(worker.tick() or "Nothing to do.")


@sync_cli.command("airtable")
@click.option("--record", default=None, help="Only this Class Schedule record (rec...).")
def sync_airtable(record: str | None) -> None:
    """Pull courses, locations and classes from Airtable now."""
    from .integrations import airtable_sync
    from .integrations.http import IntegrationError
    try:
        stats = airtable_sync.run("manual", record_id=record)
    except IntegrationError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in stats.items()))


@sync_cli.command("check-airtable")
def sync_check_airtable() -> None:
    """Show which Airtable fields the sync found."""
    from .integrations import airtable_sync
    from .integrations.http import IntegrationError
    try:
        mapping = airtable_sync.check_fields()
    except IntegrationError as exc:
        raise click.ClickException(str(exc)) from exc
    for key, m in mapping.items():
        click.echo(f"{m['wanted']}: " + ("MISSING TABLE" if not m["table"] else f"table “{m['table']}”"))
        for logical, actual in m["fields"].items():
            click.echo(f"    {logical:16} <- {actual}")
        for logical in m["missing_required"]:
            click.echo(f"    {logical:16} MISSING (required)")
        for logical in m["missing_optional"]:
            click.echo(f"    {logical:16} not found (optional)")


@sync_cli.command("ghl-create-fields")
@click.option("--dry-run", is_flag=True, help="Only list what would be created.")
def sync_ghl_create_fields(dry_run: bool) -> None:
    """Create the GHL custom fields the engine fills, where the sub-account doesn't have them yet.
    Only adds fields (never changes or removes one), so it's safe to run more than once."""
    from .integrations import ghl
    from .integrations.http import IntegrationError
    try:
        out = ghl.create_missing_fields(dry_run=dry_run)
    except IntegrationError as exc:
        raise click.ClickException(str(exc))
    labels = {"created": "Would create" if dry_run else "Created", "existing": "Already there",
              "renamed_by_ghl": "GHL gave a different key (rename the field in GHL or tell the developer)",
              "skipped": "Skipped (a field with that name exists)", "failed": "Failed"}
    for part, label in labels.items():
        if out[part]:
            click.echo(f"{label}:")
            for line in out[part]:
                click.echo(f"  - {line}")
    if out["failed"]:
        raise SystemExit(1)


@sync_cli.command("ghl-stages")
def sync_ghl_stages() -> None:
    """Load the Community Class pipeline's stage ids from GHL."""
    from .integrations import ghl
    from .integrations.http import IntegrationError
    try:
        click.echo(ghl.load_stages())
    except IntegrationError as exc:
        raise click.ClickException(str(exc)) from exc


testdata_cli = AppGroup("testdata", help="Test classes and bookings for the test site (never the live site).")


@testdata_cli.command("load")
def testdata_load() -> None:
    """Remove the earlier test classes, then load a fresh set (see app/testdata.py)."""
    from . import testdata
    uid = testdata.default_admin()
    if not uid:
        raise click.ClickException("No active admin account to record as the one who booked by phone or invoice.")
    try:
        r = testdata.load(uid)
    except testdata.TestDataError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Loaded {r['classes']} test classes: {r['course']} at {r['site']}, {r['city']}.")


@testdata_cli.command("remove")
def testdata_remove() -> None:
    """Delete the test classes and everything booked in them."""
    from . import testdata
    try:
        r = testdata.remove(0)
    except testdata.TestDataError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Removed {r['classes']} test classes ({r['how']}).")


enrollware_cli = AppGroup("enrollware", help="Leaving Enrollware.")


@enrollware_cli.command("import")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option("--as", "as_email", required=True, help="The Superadmin the import is recorded under.")
@click.option("--commit", is_flag=True, help="Import for real (without it, only show what would happen).")
@click.option("--add-seats", is_flag=True, help="Add an extra seat to a full class for each Enrollware student.")
@click.option("--send-to-ghl", is_flag=True, help="Also send the imported seats to GHL.")
def enrollware_import_cmd(path: str, as_email: str, commit: bool, add_seats: bool, send_to_ghl: bool) -> None:
    """Import Enrollware's upcoming registrations from its export (.xlsx or .csv)."""
    from flask import g
    from . import auth, db, enrollware_import
    uid = db.fetch_value("SELECT id FROM user_accounts WHERE email = :e", e=as_email.strip().lower())
    if not uid:
        raise click.ClickException(f"No account has {as_email}.")
    g.current_user = auth.load_user(uid)
    if not g.current_user or not g.current_user.has_permission("system.cutover"):
        raise click.ClickException(f"{as_email} may not import (needs the Superadmin role).")
    try:
        rows = enrollware_import.read_file(path, Path(path).read_bytes())
    except enrollware_import.ImportFileError as exc:
        raise click.ClickException(str(exc)) from exc
    r = enrollware_import.run(rows, file_name=Path(path).name, user_id=uid, commit=commit, add_seats=add_seats,
                              send_to_ghl=send_to_ghl)
    click.echo(("Imported" if commit else "Checked (nothing changed)") + f": {r.rows_total} rows.")
    for k in ("imported", "waitlisted", "already_imported", "skipped", "unmatched", "refused"):
        click.echo(f"  {k.replace('_', ' '):18} {r.counts.get(k, 0)}")
    click.echo(f"  classes            {r.classes}")
    for row in r.rows:
        if row["outcome"] in ("unmatched", "refused"):
            click.echo(f"  row {row['row']}: {row['outcome']}: {row['reason']}")


def init_app(app: Flask) -> None:
    app.cli.add_command(enrollware_cli)
    app.cli.add_command(testdata_cli)
    app.cli.add_command(db_cli)
    app.cli.add_command(users_cli)
    app.cli.add_command(worker_cli)
    app.cli.add_command(sync_cli)
