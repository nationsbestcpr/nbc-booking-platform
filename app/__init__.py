"""NBC booking engine: Flask application factory."""

from __future__ import annotations

from flask import Flask, render_template
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

    from .blueprints import account, admin, admin_integrations, admin_records, book, health, me, public, teach, webhooks  # noqa: F401

    app.register_blueprint(health.bp)
    app.register_blueprint(webhooks.bp)
    app.register_blueprint(account.bp)
    app.register_blueprint(public.bp)
    app.register_blueprint(book.bp)
    app.register_blueprint(me.bp)
    app.register_blueprint(teach.bp)
    app.register_blueprint(admin.bp)

    def error_page(code: int, title: str, message: str):
        return render_template("errors/error.html", code=code, title=title, message=message), code

    app.register_error_handler(400, lambda e: error_page(400, "Something in that request wasn't right",
                                                          getattr(e, "description", "") or "Go back and try again."))
    app.register_error_handler(403, lambda e: error_page(403, "You don't have access to this page",
                                                          "Ask an admin to give your account the role this page needs."))
    app.register_error_handler(404, lambda e: error_page(404, "Page not found",
                                                          "The class may have moved or finished. See the classes on sale."))
    return app
