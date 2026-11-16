"""The waiting list's public pages (launch hub 9c).

    POST /waitlist/join/<class id>       a full class's page: join (no payment, no account needed)
    GET  /waitlist/joined?entry=…        you're on the list: your place, the 2-hour rule, the cut-off
    GET  /waitlist/offer/<offer id>      the link in the offer text / email: the held seat and its deadline
    POST /waitlist/offer/<offer id>      pay for it (needs an account, like any booking)
"""

from __future__ import annotations

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for

from .. import auth, db, waitlist
from ..integrations import checkout

bp = Blueprint("waiting", __name__, url_prefix="/waitlist")


def _base_url() -> str:
    configured = current_app.config.get("PUBLIC_BASE_URL", "")
    if configured and "localhost" not in configured:
        return configured.rstrip("/")
    return request.url_root.rstrip("/")


@bp.post("/join/<public_id>")
def join(public_id: str):
    from .book import _render, _buyer
    s = checkout.bookable_session(public_id)
    if not s:
        abort(404)
    f = request.form
    phone = checkout.normalise_phone(f.get("wl_phone"))
    try:
        if phone == "":
            raise waitlist.WaitlistError("Enter a valid mobile phone number.")
        entry = waitlist.join(public_id, first=f.get("wl_first", "").strip(), last=f.get("wl_last", "").strip(),
                              email=f.get("wl_email", "").strip().lower(), phone=phone,
                              product_id=f.get("wl_product", type=int) or 0,
                              party_size=f.get("wl_party", type=int) or 1, sms_consent=f.get("wl_sms") == "yes",
                              ip=request.remote_addr, user_agent=request.headers.get("User-Agent"))
    except waitlist.WaitlistError as exc:
        user = auth.current_user()
        return _render(s, seats=1, form={}, buyer=_buyer(user) if user else {}, error=None, product_id=None,
                       status=400, wl_form=f, wl_error=str(exc))
    return redirect(url_for("waiting.joined", entry=entry["public_id"]), code=303)


@bp.get("/joined")
def joined():
    entry_id = request.args.get("entry", "")
    if not checkout._is_uuid(entry_id):
        abort(404)
    e = db.fetch_one("""
        SELECT w.public_id, w.first_name, w.party_size, w.status, w.email, q.position, s.public_id AS session_public_id
          FROM waitlist_entries w LEFT JOIN waitlist_queue q ON q.id = w.id JOIN sessions s ON s.id = w.session_id
         WHERE w.public_id = CAST(:p AS uuid)
    """, p=entry_id)
    if not e:
        abort(404)
    s = checkout.bookable_session(str(e["session_public_id"]))
    return render_template("public/waitlist_joined.html", e=e, s=s, t=waitlist.terms(s))


def _offer_or_404(offer_id: str) -> dict:
    o = waitlist.offer_for(offer_id)
    if not o:
        abort(404)
    return o


@bp.get("/offer/<offer_id>")
def offer(offer_id: str):
    o = _offer_or_404(offer_id)
    return render_template("public/waitlist_offer.html", o=o, form={}, error=None, testing=checkout.test_checkout())


@bp.post("/offer/<offer_id>")
def offer_pay(offer_id: str):
    o = _offer_or_404(offer_id)
    user = auth.current_user()
    if user is None:
        return redirect(url_for("account.sign_in", next=url_for("waiting.offer", offer_id=offer_id)))
    f = request.form
    error = None
    first, _, last = (user.display_name or "").partition(" ")
    buyer = checkout.Person((f.get("buyer_first") or first).strip(), (f.get("buyer_last") or last).strip(),
                            user.email.lower())
    attendees = [checkout.Person(o["first_name"], o["last_name"], o["email"], o["phone_e164"])]
    for i in range(1, o["seats"]):
        a_first, a_last = f.get(f"a{i}_first", "").strip(), f.get(f"a{i}_last", "").strip()
        email = f.get(f"a{i}_email", "").strip().lower()
        if not (a_first and a_last and checkout.EMAIL.match(email)):
            error = f"Seat {i + 1}: enter the person's first name, last name and their own email."
            break
        attendees.append(checkout.Person(a_first, a_last, email))
    if not error and not (buyer.first_name and buyer.last_name):
        error = "Enter your first and last name."
    if not error:
        try:
            url = waitlist.start_checkout(o, buyer=buyer, attendees=attendees, base=_base_url(),
                                          buyer_user_id=user.id, ip=request.remote_addr,
                                          user_agent=request.headers.get("User-Agent"))
            return redirect(url, code=303)
        except waitlist.WaitlistError as exc:
            error = str(exc)
            o = _offer_or_404(offer_id)
    return render_template("public/waitlist_offer.html", o=o, form=f, error=error,
                           testing=checkout.test_checkout()), 400
