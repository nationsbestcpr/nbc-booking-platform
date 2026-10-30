"""Inbound webhooks.

Stripe (/webhooks/stripe): the signature is checked against the raw body
with STRIPE_WEBHOOK_SECRET; each event is stored once in webhook_events
(Stripe retries for 3 days and may send an event twice) and applied:
checkout.session.completed / .async_payment_succeeded confirm the seats,
checkout.session.expired frees them. Anything else is stored and ignored.
A failure answers 500 so Stripe tries again later.

GHL (/webhooks/ghl): ContactUpdate, ContactDndUpdate, InboundMessage,
OutboundMessage and OpportunityStageUpdate arrive through the private
Marketplace app (launch plan 15.5), verified with X-GHL-Signature. Not built
yet; creating contacts and opportunities doesn't need it.
"""

from __future__ import annotations

import json
import os

from flask import Blueprint, jsonify, request

from ..integrations import checkout, stripe_api

bp = Blueprint("webhooks", __name__, url_prefix="/webhooks")


@bp.post("/stripe")
def stripe():
    raw = request.get_data(cache=False)
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    if not secret:
        return jsonify(error="STRIPE_WEBHOOK_SECRET is not set"), 503
    if not stripe_api.verify_signature(raw, request.headers.get("Stripe-Signature", ""), secret):
        return jsonify(error="bad signature"), 400
    try:
        event = json.loads(raw)
    except ValueError:
        return jsonify(error="not JSON"), 400
    if not event.get("id"):
        return jsonify(error="no event id"), 400

    row_id, status = checkout.record_webhook(event, valid=True)
    if status in ("processed", "ignored"):
        return jsonify(received=True, duplicate=True)
    try:
        result = checkout.handle_event(event)
    except Exception as exc:
        checkout.finish_webhook(row_id, "failed", f"{exc.__class__.__name__}: {exc}"[:1000])
        raise
    checkout.finish_webhook(row_id, result)
    return jsonify(received=True, result=result)


@bp.post("/ghl")
def ghl():
    return jsonify(status="not_implemented", detail="Needs the GHL Marketplace app (launch plan 15.5)."), 501
