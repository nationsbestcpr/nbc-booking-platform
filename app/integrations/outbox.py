"""The outbox: anything the engine must tell another platform.

A job is written with `enqueue` in the same transaction as the change that
needs it (a confirmed seat -> "sync this registration to GHL"), so the two
can never disagree. The worker then delivers due jobs with `run_due`:

  * claimed one at a time with FOR UPDATE SKIP LOCKED, so two workers never take one job;
  * success -> 'succeeded';
  * a failure that may pass (rate limit, outage, missing secret) -> 'failed'
    and tried again later, waiting longer each time;
  * a failure that won't pass (IntegrationError(retry=False)) or the last
    attempt -> 'dead'. Failed and dead jobs are the dashboard's "not synced"
    list, where staff can retry them.

Handlers are registered per (target, operation) with @handler and receive
the job row. They must be safe to run twice for the same job.
"""

from __future__ import annotations

import json
import os
import socket
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .. import db
from .http import IntegrationError, NotConfigured

Handler = Callable[[dict], Any]
_HANDLERS: dict[tuple[str, str], Handler] = {}

WORKER_NAME = f"{socket.gethostname()}:{os.getpid()}"


def handler(target: str, operation: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        _HANDLERS[(target, operation)] = fn
        return fn
    return register


def enqueue(conn: Connection, target: str, operation: str, *, key: str, entity_type: str | None = None,
            entity_id: int | None = None, payload: dict | None = None, delay_seconds: int = 0) -> None:
    """Queue a job. The same key twice is one job (the second is ignored)."""
    conn.execute(text("""
        INSERT INTO outbox_jobs (target, operation, entity_type, entity_id, payload, idempotency_key, run_after)
        VALUES (:t, :o, :et, :eid, CAST(:p AS jsonb), :k, now() + make_interval(secs => :d))
        ON CONFLICT (idempotency_key) DO NOTHING
    """), {"t": target, "o": operation, "et": entity_type, "eid": entity_id,
           "p": json.dumps(payload or {}), "k": key, "d": delay_seconds})


def _backoff_seconds(attempts: int) -> int:
    # 1, 2, 4, 8, 16, 32, 60, 60 ... minutes
    return min(60, 2 ** max(attempts - 1, 0)) * 60


def run_due(limit: int = 25, targets: list[str] | None = None) -> dict[str, int]:
    """Deliver up to `limit` due jobs, claiming one at a time. Returns counts by result.

    A job is claimed just before it runs, so 'running' always means "started
    at locked_at"; one left running for 10 minutes belongs to a worker that
    died and is handed back."""
    # Imported here so their @handler registrations exist before dispatch.
    from . import checkout, ghl, stripe_api  # noqa: F401

    with db.transaction() as conn:
        conn.execute(text("""
            UPDATE outbox_jobs SET status = 'failed', locked_at = NULL, locked_by = NULL,
                   last_error = 'The worker stopped while sending; trying again.'
             WHERE status = 'running' AND locked_at < now() - interval '10 minutes'
        """))
    counts = {"succeeded": 0, "failed": 0, "dead": 0}
    for _ in range(limit):
        with db.transaction() as conn:
            job = conn.execute(text("""
                UPDATE outbox_jobs SET status = 'running', locked_at = now(), locked_by = :w, attempts = attempts + 1
                 WHERE id = (SELECT id FROM outbox_jobs
                              WHERE status IN ('pending', 'failed') AND run_after <= now()
                                AND (CAST(:targets AS text[]) IS NULL OR target = ANY(CAST(:targets AS text[])))
                              ORDER BY run_after, id LIMIT 1 FOR UPDATE SKIP LOCKED)
                RETURNING *
            """), {"w": WORKER_NAME, "targets": targets}).mappings().first()
        if job is None:
            break
        counts[_run_one(dict(job))] += 1
    return counts


def _run_one(job: dict) -> str:
    fn = _HANDLERS.get((job["target"], job["operation"]))
    error: str | None = None
    status = "succeeded"
    if fn is None:
        status, error = "dead", f"No handler for {job['target']} / {job['operation']}"
    else:
        try:
            fn(job)
        except NotConfigured as exc:
            # Not counted as an attempt: it waits for the secret, then runs.
            status, error = "failed", str(exc)
            job["attempts"] -= 1
        except IntegrationError as exc:
            status, error = ("failed" if exc.retry else "dead"), str(exc)
        except Exception as exc:  # a bug or bad data: keep the message, retry with backoff
            status, error = "failed", f"{exc.__class__.__name__}: {exc}"[:1000]
    if status == "failed" and job["attempts"] >= job["max_attempts"]:
        status = "dead"
    with db.transaction() as conn:
        conn.execute(text("""
            UPDATE outbox_jobs
               SET status = :st, last_error = :err, locked_at = NULL, locked_by = NULL, attempts = :a,
                   completed_at = CASE WHEN :st = 'succeeded' THEN now() END,
                   run_after = CASE WHEN :st = 'failed' THEN now() + make_interval(secs => :wait) ELSE run_after END
             WHERE id = :id
        """), {"id": job["id"], "st": status, "err": error, "a": job["attempts"],
               "wait": _backoff_seconds(max(job["attempts"], 1))})
    return status


def retry(conn: Connection, job_id: int) -> bool:
    """Send a failed or dead job again on the next worker pass."""
    return conn.execute(text("""
        UPDATE outbox_jobs SET status = 'pending', run_after = now(), last_error = NULL,
               max_attempts = GREATEST(max_attempts, attempts + 3)
         WHERE id = :id AND status IN ('failed', 'dead')
    """), {"id": job_id}).rowcount == 1
