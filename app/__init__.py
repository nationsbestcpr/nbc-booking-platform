"""NBC booking engine: Flask application factory."""

from __future__ import annotations

from flask import Flask, request, render_template
from werkzeug.middleware.proxy_fix import ProxyFix

from . import areas, auth, cli, db, filters, security
from .config import Config


def create_app(overrides: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    if overrides:
        app.config.update(overrides)
    Config.validate(app.config)
    if app.config["APP_ENV"] != "local":
        # Replit's proxy sits in front: trust its https scheme, host and client address.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]

    db.init_app(app)
    security.init_app(app)
    auth.init_app(app)
    areas.init_app(app)
    filters.init_app(app)
    cli.init_app(app)

    from .blueprints import account, admin, admin_integrations, admin_records, book, console, health, me, public, teach, webhooks  # noqa: F401

    app.register_blueprint(health.bp)
    app.register_blueprint(webhooks.bp)
    app.register_blueprint(account.bp)
    app.register_blueprint(public.bp)
    app.register_blueprint(book.bp)
    app.register_blueprint(me.bp)
    app.register_blueprint(teach.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(console.bp)

    def error_page(code: int, title: str, message: str):
        return render_template("errors/error.html", code=code, title=title, message=message), code

    app.register_error_handler(400, lambda e: error_page(400, "Something in that request wasn't right",
                                                          getattr(e, "description", "") or "Go back and try again."))
    app.register_error_handler(403, lambda e: error_page(403, "You don't have access to this page",
                                                          "Ask an admin to give your account the role this page needs."))
    app.register_error_handler(404, lambda e: error_page(404, "Page not found",
                                                          "The class may have moved or finished. See the classes on sale."))

    @app.errorhandler(500)
    def server_error(e):
        """Log the real error with a short reference; on the test site, show staff what broke
        (production shows only the reference, which matches the line in the logs)."""
        import secrets as _secrets
        from flask import g
        original = getattr(e, "original_exception", None) or e
        ref = _secrets.token_hex(4)
        app.logger.error("Error %s on %s %s", ref, request.method, request.path, exc_info=original)
        conn = g.pop("db_conn", None)          # the failed request's connection may be unusable now
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        detail = None
        try:
            user = auth.current_user()
            if app.config["APP_ENV"] != "production" and user and user.is_staff:
                detail = f"{original.__class__.__name__}: {original}"[:1500]
        except Exception:
            user = None
        try:
            return render_template("errors/error.html", code=500, title="Something went wrong on our side",
                                   message=f"Please try again. If it keeps happening, tell us reference {ref}.",
                                   detail=detail), 500
        except Exception:
            return (f"Something went wrong on our side. Reference {ref}." +
                    (f"\n\n{detail}" if detail else "")), 500, {"Content-Type": "text/plain; charset=utf-8"}
    return app
