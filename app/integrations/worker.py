"""The background worker: `flask --app wsgi worker run`.

The published app starts it beside the web server (scripts/replit-start.sh).
Every few seconds it:
  * releases checkout holds that ran out (and expires their Stripe pages);
  * delivers due outbox jobs (GHL contacts/opportunities, Stripe expiry and refunds);
  * runs the Airtable sync when the last one is older than AIRTABLE_SYNC_MINUTES (default 5).

Only one worker works at a time, whatever the number of machines: it holds a
Postgres advisory lock for as long as it runs. A second one waits.
"""

from __future__ import annotations

import os
import time
import traceback
from datetime import datetime, timezone

from sqlalchemy import text

from .. import db

LOCK_KEY = 7_202_610_001          # any fixed number; shared by every worker on this database


def _log(msg: str) -> None:
    print(f"[worker] {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z {msg}", flush=True)


def sync_minutes() -> int:
    try:
        return max(1, int(os.environ.get("AIRTABLE_SYNC_MINUTES", "5")))
    except ValueError:
        return 5


def airtable_due() -> bool:
    from . import airtable, runs
    if not airtable.configured():
        return False
    last = runs.last_finished("airtable_sync")
    if last is None:
        return True
    started = last["started_at"]
    age = (datetime.now(timezone.utc) - started).total_seconds()
    if last["status"] == "running" and age < 30 * 60:
        return False
    if last["status"] == "failed":
        return age >= max(sync_minutes(), 5) * 60     # don't hammer a broken setup
    return age >= sync_minutes() * 60


def tick() -> dict:
    """One pass. Each step is independent: one failing doesn't stop the others."""
    from . import airtable_sync, checkout, outbox
    done: dict = {}
    for name, step in (("holds_released", checkout.expire_holds),
                       ("jobs", lambda: {k: v for k, v in outbox.run_due().items() if v}),
                       ("airtable", lambda: airtable_sync.run("schedule") if airtable_due() else None)):
        try:
            result = step()
            if result:
                done[name] = result
        except Exception as exc:  # keep the worker alive; the run row / job row has the details
            done[name] = f"error: {exc}"
            _log(f"{name} failed: {exc}")
            if os.environ.get("APP_ENV", "local") == "local":
                traceback.print_exc()
    return done


def run_forever(poll_seconds: int = 15) -> None:
    lock_conn = None
    _log(f"starting (Airtable every {sync_minutes()} min)")
    while True:
        try:
            if lock_conn is None:
                lock_conn = db.engine().connect()
                got = lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar()
                lock_conn.commit()
                if not got:
                    lock_conn.close()
                    lock_conn = None
                    _log("another worker is running; waiting")
                    time.sleep(60)
                    continue
                _log("working")
            else:
                lock_conn.execute(text("SELECT 1"))
                lock_conn.commit()
        except Exception as exc:
            _log(f"database not reachable ({exc.__class__.__name__}); retrying")
            try:
                if lock_conn is not None:
                    lock_conn.close()
            except Exception:
                pass
            lock_conn = None
            time.sleep(poll_seconds)
            continue
        done = tick()
        if done:
            _log(", ".join(f"{k}: {v}" for k, v in done.items()))
        time.sleep(poll_seconds)
