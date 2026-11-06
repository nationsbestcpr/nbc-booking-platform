"""Health check for uptime monitoring and deploy checks."""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify
from sqlalchemy.exc import SQLAlchemyError

from .. import db

bp = Blueprint("health", __name__)


@bp.get("/healthz")
def healthz():
    try:
        roles = db.fetch_value("SELECT count(*) FROM roles")
        db_env = db.fetch_value("SELECT value #>> '{}' FROM app_settings WHERE key = 'environment'")
    except SQLAlchemyError as exc:
        return jsonify(status="error", database="unreachable", detail=exc.__class__.__name__), 503
    app_env = current_app.config["APP_ENV"]
    ok = roles and db_env == app_env
    from .. import schema_check, version
    try:
        missing = schema_check.missing()
    except Exception as exc:  # the check itself must never break the health check
        missing = [f"(couldn't check: {exc.__class__.__name__})"]
    return jsonify(
        status="ok" if ok else "degraded",
        version=version.commit(),
        database_missing=missing,
        app_env=app_env,
        database_env=db_env,
        database_env_matches=db_env == app_env,
        roles_seeded=bool(roles),
    ), 200 if ok else 503
