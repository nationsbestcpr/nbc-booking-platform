"""Connections to the other platforms, and the background work that drives them.

    airtable.py      read the NBC Master Base (schedule, locations, courses)
    airtable_sync.py turn those records into cities, sites, products and classes
    stripe_api.py    Checkout sessions, expiring them, refunds, webhook signatures
    checkout.py      seat hold -> Stripe Checkout -> confirmed registrations
    ghl.py           one contact and one opportunity per seat
    outbox.py        the delivery queue (outbox_jobs) with retries
    runs.py          integration_runs and integration_issues bookkeeping
    worker.py        the background loop the published app starts beside the web server

Every call to another platform goes through http.request, so tests can swap
it for a fake. Secrets come from environment variables only.
"""
