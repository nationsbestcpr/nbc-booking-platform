"""Sign in (email and password, Google or Microsoft), create an account, sign out.

`next` is carried through every way in, so someone who was about to book
lands back on that class once they're signed in.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

from .. import auth, oauth


def _booking_context(next_url: str) -> dict | None:
    """The class they were booking, if they came from a Book button (shown beside the form)."""
    import re
    from ..integrations import checkout
    m = re.match(r"^/book/([0-9a-fA-F-]{36})(?:\?|$)", next_url or "")
    return checkout.bookable_session(m.group(1)) if m else None


def _page_args(next_url: str) -> dict:
    return {"next": next_url, "providers": oauth.available(), "booking": _booking_context(next_url)}


bp = Blueprint("account", __name__)


@bp.route("/sign-in", methods=["GET", "POST"])
def sign_in():
    if auth.current_user() and request.method == "GET":
        return redirect(auth.safe_next(request.args.get("next")))
    error, email = None, ""
    if request.method == "POST":
        email = request.form.get("email", "")
        try:
            auth.authenticate(email, request.form.get("password", ""))
            return redirect(auth.safe_next(request.form.get("next")))
        except auth.SignInError as exc:
            error = str(exc)
    next_url = request.values.get("next", "")
    return render_template("account/sign_in.html", error=error, email=email,
                           **_page_args(next_url)), (400 if error else 200)


@bp.route("/create-account", methods=["GET", "POST"])
def create_account():
    if auth.current_user() and request.method == "GET":
        return redirect(auth.safe_next(request.args.get("next")))
    next_url = request.values.get("next", "")
    errors: dict[str, str] = {}
    form = {"first_name": "", "last_name": "", "email": "", "intent": request.args.get("as", "student")}
    if request.method == "POST":
        form = {k: request.form.get(k, "") for k in ("first_name", "last_name", "email", "intent")}
        try:
            auth.create_account(password=request.form.get("password", ""), **form)
            if next_url:
                return redirect(auth.safe_next(next_url))
            return redirect(url_for("public.home", welcome=1))
        except auth.SignUpError as exc:
            errors[exc.field] = exc.message
    return render_template("account/create_account.html", form=form, errors=errors,
                           **_page_args(next_url)), (400 if errors else 200)


@bp.get("/sign-in/<provider>")
def oauth_start(provider: str):
    if not oauth.configured(provider):
        abort(404)
    return redirect(oauth.start(provider, request.args.get("next")))


@bp.get("/sign-in/<provider>/callback")
def oauth_callback(provider: str):
    if provider not in oauth.PROVIDERS:
        abort(404)
    next_url = (session.get(oauth.SESSION_KEY) or {}).get("next", "")
    try:
        _uid, go, note = oauth.finish(provider, request.args)
    except auth.SignInError as exc:
        return render_template("account/sign_in.html", error=str(exc), email="",
                               **_page_args(next_url)), 400
    if note:
        flash(note, "ok")
    return redirect(go)


@bp.post("/sign-out")
def sign_out():
    auth.sign_out()
    return redirect(url_for("public.home"))
