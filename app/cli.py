"""`flask db ...` commands: run database migrations from a shell (e.g. Replit's).

    flask --app wsgi db upgrade          # create or update the database to the latest version
    flask --app wsgi db current          # which version the database is on
    flask --app wsgi db history          # list every migration
    flask --app wsgi db downgrade -1     # undo the last migration (asks first)
    flask --app wsgi db new "add x"      # start a new migration file
    flask --app wsgi db load-demo        # invented demo data, local development only
    flask --app wsgi users make-admin EMAIL   # first admin of a new database
    flask --app wsgi users ensure-superadmin  # preset admin from SUPERADMIN_EMAIL / SUPERADMIN_PASSWORD
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


@users_cli.command("ensure-superadmin")
def ensure_superadmin() -> None:
    """Make sure the preset admin account from the secrets exists and is an admin.

    Reads SUPERADMIN_EMAIL, SUPERADMIN_PASSWORD (12+ characters) and optionally
    SUPERADMIN_NAME. Runs on every start of the published app:
      * no account with that email yet: creates it, with that password, active;
      * the account exists: keeps its password (a password changed in the app
        stays changed) and only makes sure it holds the Admin role;
      * SUPERADMIN_EMAIL not set: does nothing.
    The password is never printed or stored except as a scrypt hash. A
    suspended preset account is left suspended.
    """
    from sqlalchemy import text
    from werkzeug.security import generate_password_hash

    from . import db
    email = os.environ.get("SUPERADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("SUPERADMIN_PASSWORD", "")
    name = " ".join(os.environ.get("SUPERADMIN_NAME", "").split()) or "Super Admin"
    if not email:
        click.echo("SUPERADMIN_EMAIL is not set: no preset admin account.")
        return
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise click.ClickException("SUPERADMIN_EMAIL isn't an email address.")
    with db.engine().begin() as conn:
        account = conn.execute(text("SELECT id, status FROM user_accounts WHERE email = :e"),
                               {"e": email}).mappings().first()
        created = False
        if account is None:
            if len(password) < SUPERADMIN_MIN_PASSWORD:
                raise click.ClickException(
                    f"SUPERADMIN_PASSWORD must be set to {SUPERADMIN_MIN_PASSWORD}+ characters to create the preset admin account.")
            uid = conn.execute(text("""
                INSERT INTO user_accounts (email, display_name, status, email_verified_at)
                VALUES (:e, :n, 'active', now()) RETURNING id
            """), {"e": email, "n": name}).scalar_one()
            conn.execute(text("""
                INSERT INTO user_identities (user_account_id, provider, subject, password_hash, password_changed_at)
                VALUES (:uid, 'password', :e, :h, now())
            """), {"uid": uid, "e": email, "h": generate_password_hash(password)})
            conn.execute(text("INSERT INTO user_roles (user_account_id, role_code) VALUES (:uid, 'user')"), {"uid": uid})
            created = True
        else:
            uid = account["id"]
            has_password = conn.execute(text("""SELECT 1 FROM user_identities
                                                 WHERE user_account_id = :uid AND provider = 'password'"""),
                                        {"uid": uid}).first()
            if not has_password and len(password) >= SUPERADMIN_MIN_PASSWORD:
                conn.execute(text("""
                    INSERT INTO user_identities (user_account_id, provider, subject, password_hash, password_changed_at)
                    VALUES (:uid, 'password', :e, :h, now())
                """), {"uid": uid, "e": email, "h": generate_password_hash(password)})
        is_admin = conn.execute(text("""SELECT 1 FROM user_roles WHERE user_account_id = :uid
                                         AND role_code = 'admin' AND revoked_at IS NULL"""), {"uid": uid}).first()
        if not is_admin:
            conn.execute(text("INSERT INTO user_roles (user_account_id, role_code) VALUES (:uid, 'admin')"), {"uid": uid})
            conn.execute(text("""
                INSERT INTO audit_log (actor_type, action, entity_type, entity_id, after, reason)
                VALUES ('system', 'user.role_grant', 'user_account', :uid, '{"role": "admin"}',
                        'Preset admin account (SUPERADMIN_EMAIL)')
            """), {"uid": uid})
    status = "created" if created else ("given the Admin role" if not is_admin else "already set up")
    note = "" if created or not account or account["status"] == "active" else f" (account is {account['status']})"
    click.echo(f"Preset admin account {email}: {status}{note}.")


def init_app(app: Flask) -> None:
    app.cli.add_command(db_cli)
    app.cli.add_command(users_cli)
