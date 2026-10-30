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


def init_app(app: Flask) -> None:
    app.cli.add_command(db_cli)
    app.cli.add_command(users_cli)
