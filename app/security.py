"""Form protection (CSRF): every POST form carries a per-session token."""

from __future__ import annotations

import hmac
import secrets

from flask import Flask, abort, request, session
from markupsafe import Markup

_KEY = "_csrf"
# Webhooks are signed by Stripe / GHL instead.
_EXEMPT_PREFIXES = ("/webhooks/", "/api/")    # the API is called with a key, never a browser session


def csrf_token() -> str:
    if _KEY not in session:
        session[_KEY] = secrets.token_urlsafe(32)
    return session[_KEY]


def csrf_input() -> Markup:
    return Markup(f'<input type="hidden" name="csrf_token" value="{csrf_token()}">')


def init_app(app: Flask) -> None:
    app.add_template_global(csrf_input)

    @app.before_request
    def check_csrf():
        if request.method != "POST" or request.path.startswith(_EXEMPT_PREFIXES):
            return
        sent = request.form.get("csrf_token", "")
        if not sent or not hmac.compare_digest(sent, session.get(_KEY, "")):
            abort(400, description="This form expired. Reload the page and try again.")
