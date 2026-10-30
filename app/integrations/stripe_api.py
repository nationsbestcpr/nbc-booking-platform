"""Stripe: Checkout sessions, expiring them, refunds, and webhook signatures.

Secrets: STRIPE_SECRET_KEY (sk_test_… in staging, sk_live_… in production
only) and STRIPE_WEBHOOK_SECRET (whsec_…, different for test and live).
Card details never reach the engine: customers pay on Stripe's hosted page.

Prices are sent with each Checkout session (price_data) from the class's own
price, so what the page shows is exactly what Stripe charges; no Stripe
price ids have to be kept in step with Airtable.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any

from flask import current_app

from .http import IntegrationError, NotConfigured, request
from .outbox import handler

API = "https://api.stripe.com/v1"
SIGNATURE_TOLERANCE_SECONDS = 300


def secret_key() -> str:
    return os.environ.get("STRIPE_SECRET_KEY", "").strip()


def configured() -> bool:
    return bool(secret_key())


def is_live_key(key: str | None = None) -> bool:
    return (key if key is not None else secret_key()).startswith(("sk_live_", "rk_live_"))


def check_key_for_environment() -> str | None:
    """A reason the key can't be used here, or None. Live keys only in production."""
    key = secret_key()
    if not key:
        return "STRIPE_SECRET_KEY is not set."
    if not key.startswith(("sk_test_", "sk_live_", "rk_test_", "rk_live_")):
        return "STRIPE_SECRET_KEY doesn't look like a Stripe secret key (sk_test_… or sk_live_…)."
    if is_live_key(key) and current_app.config["APP_ENV"] != "production":
        return "A live Stripe key is set outside production. Use the test key (sk_test_…) here."
    return None


def _call(method: str, path: str, data: Any = None, *, idempotency_key: str | None = None) -> dict:
    problem = check_key_for_environment()
    if problem:
        raise NotConfigured(f"Stripe: {problem}")
    headers = {"Authorization": f"Bearer {secret_key()}", "Stripe-Version": "2024-06-20"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return request(method, f"{API}/{path}", service="Stripe", headers=headers, data=data)


def create_checkout_session(*, order_public_id: str, hold_public_id: str, product_name: str, description: str,
                            unit_amount_cents: int, quantity: int, customer_email: str, expires_at: int,
                            success_url: str, cancel_url: str) -> dict:
    data = [
        ("mode", "payment"),
        ("payment_method_types[0]", "card"),     # paid at once: no bank debits that clear after class day
        ("line_items[0][quantity]", str(quantity)),
        ("line_items[0][price_data][currency]", "usd"),
        ("line_items[0][price_data][unit_amount]", str(unit_amount_cents)),
        ("line_items[0][price_data][product_data][name]", product_name[:250]),
        ("line_items[0][price_data][product_data][description]", description[:500]),
        ("allow_promotion_codes", "true"),
        ("customer_email", customer_email),
        ("customer_creation", "if_required"),
        ("expires_at", str(expires_at)),
        ("success_url", success_url),
        ("cancel_url", cancel_url),
        ("client_reference_id", order_public_id),
        ("metadata[order]", order_public_id),
        ("metadata[hold]", hold_public_id),
        ("payment_intent_data[metadata][order]", order_public_id),
        ("payment_intent_data[description]", f"{product_name} × {quantity}"[:1000]),
    ]
    return _call("POST", "checkout/sessions", data, idempotency_key=f"checkout-{hold_public_id}")


def expire_checkout_session(checkout_session_id: str) -> None:
    try:
        _call("POST", f"checkout/sessions/{checkout_session_id}/expire")
    except IntegrationError as exc:
        # Already expired or already paid: nothing left to do here (a payment
        # is handled by the webhook, which refunds if the seat is gone).
        if exc.status == 400:
            return
        raise


def refund(payment_intent_id: str, amount_cents: int, *, key: str, reason: str = "requested_by_customer") -> dict:
    return _call("POST", "refunds", [("payment_intent", payment_intent_id), ("amount", str(amount_cents)),
                                     ("reason", reason), ("metadata[engine_refund]", key)],
                 idempotency_key=f"refund-{key}")


def verify_signature(payload: bytes, header: str, secret: str, now: float | None = None) -> bool:
    """Stripe-Signature: t=<unix>,v1=<hex hmac-sha256 of 't.payload'> (any v1 may match)."""
    if not header or not secret:
        return False
    parts: dict[str, list[str]] = {}
    for item in header.split(","):
        k, _, v = item.strip().partition("=")
        parts.setdefault(k, []).append(v)
    try:
        ts = int(parts.get("t", [""])[0])
    except ValueError:
        return False
    if abs((now or time.time()) - ts) > SIGNATURE_TOLERANCE_SECONDS:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", []))


# --- outbox handlers -----------------------------------------------------------------

@handler("stripe", "expire_checkout")
def _expire_job(job: dict) -> None:
    expire_checkout_session(job["payload"]["checkout_session_id"])
