"""Bookkeeping for background work: integration_runs and integration_issues.

A run row is written in its own transaction when the job starts, so the
Integrations page shows "Running" even if the job later fails. Issues are
de-duplicated by (source, code, external_ref) while open: the same bad
Airtable row seen every five minutes is one issue with a growing count.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .. import db


def start(kind: str, trigger: str, *, user_id: int | None = None, scope: dict | None = None) -> int:
    with db.transaction() as conn:
        return conn.execute(text("""
            INSERT INTO integration_runs (kind, trigger, triggered_by_user_id, scope)
            VALUES (:k, :t, :u, CAST(:s AS jsonb)) RETURNING id
        """), {"k": kind, "t": trigger, "u": user_id, "s": json.dumps(scope) if scope else None}).scalar_one()


def finish(run_id: int, status: str, stats: dict | None = None, error: str | None = None) -> None:
    with db.transaction() as conn:
        conn.execute(text("""
            UPDATE integration_runs SET status = :st, stats = CAST(:stats AS jsonb), error = :e, finished_at = now()
             WHERE id = :id
        """), {"id": run_id, "st": status, "stats": json.dumps(stats or {}), "e": (error or None) and error[:1000]})


def issue(conn: Connection, *, source: str, code: str, message: str, severity: str = "warning",
          run_id: int | None = None, entity_type: str | None = None, entity_id: int | None = None,
          external_ref: str | None = None, details: dict | None = None) -> None:
    conn.execute(text("""
        INSERT INTO integration_issues (run_id, source, severity, code, entity_type, entity_id,
                                        external_ref, message, details)
        VALUES (:run, :src, :sev, :code, :et, :eid, :ref, :msg, CAST(:det AS jsonb))
        ON CONFLICT (source, code, (COALESCE(external_ref, ''))) WHERE resolved_at IS NULL
        DO UPDATE SET occurrences = integration_issues.occurrences + 1, last_seen_at = now(),
                      message = EXCLUDED.message, severity = EXCLUDED.severity,
                      details = EXCLUDED.details, run_id = EXCLUDED.run_id
    """), {"run": run_id, "src": source, "sev": severity, "code": code, "et": entity_type, "eid": entity_id,
           "ref": external_ref, "msg": message[:1000], "det": json.dumps(details) if details else None})


def resolve(conn: Connection, *, source: str, external_ref: str, codes: list[str] | None = None) -> None:
    """Close open issues for something that is fine now (resolved by the system, no user)."""
    conn.execute(text("""
        UPDATE integration_issues SET resolved_at = now()
         WHERE source = :src AND COALESCE(external_ref, '') = :ref AND resolved_at IS NULL
           AND (CAST(:codes AS text[]) IS NULL OR code = ANY(CAST(:codes AS text[])))
    """), {"src": source, "ref": external_ref, "codes": codes})


def last_finished(kind: str) -> dict[str, Any] | None:
    with db.engine().connect() as conn:
        row = conn.execute(text("""
            SELECT id, status, started_at, finished_at FROM integration_runs
             WHERE kind = :k ORDER BY started_at DESC LIMIT 1
        """), {"k": kind}).mappings().first()
        return dict(row) if row else None


# --- live progress -------------------------------------------------------------------

STALE_AFTER_MINUTES = 10
_last_progress: dict[int, float] = {}


def progress(run_id: int, step: str, done: int | None = None, total: int | None = None,
             *, force: bool = False) -> None:
    """Say what a running job is doing ('Saving classes', 350, 1100). Written at most every
    two seconds (unless the step changes, force=True) and doubles as the run's heartbeat."""
    import time as _time
    now = _time.monotonic()
    if not force and now - _last_progress.get(run_id, 0.0) < 2.0:
        return
    _last_progress[run_id] = now
    with db.transaction() as conn:
        conn.execute(text("""
            UPDATE integration_runs
               SET stats = COALESCE(stats, '{}'::jsonb)
                           || jsonb_build_object('step', CAST(:s AS text), 'done', CAST(:d AS int),
                                                 'total', CAST(:t AS int), 'heartbeat', now())
             WHERE id = :id AND status = 'running'
        """), {"id": run_id, "s": step, "d": done, "t": total})


def reap_stale(kind: str) -> int:
    """A run still 'running' with no heartbeat for 10 minutes was cut off (the app
    restarted mid-sync): mark it failed so the next one can start."""
    with db.transaction() as conn:
        return conn.execute(text("""
            UPDATE integration_runs
               SET status = 'failed', finished_at = now(),
                   error = 'Stopped part-way (the app restarted or the worker stopped). The next sync starts over.'
             WHERE kind = :k AND status = 'running'
               AND COALESCE(CAST(stats->>'heartbeat' AS timestamptz), started_at)
                   < now() - make_interval(mins => :m)
        """), {"k": kind, "m": STALE_AFTER_MINUTES}).rowcount


def running(kind: str | None = None) -> list[dict]:
    """Runs in progress with their current step, for the Integrations page."""
    with db.engine().connect() as conn:
        return [dict(r) for r in conn.execute(text("""
            SELECT id, kind, trigger, started_at, stats->>'step' AS step,
                   CAST(stats->>'done' AS int) AS done, CAST(stats->>'total' AS int) AS total,
                   CAST(stats->>'heartbeat' AS timestamptz) AS heartbeat,
                   EXTRACT(EPOCH FROM now() - started_at)::int AS seconds
              FROM integration_runs
             WHERE status = 'running' AND (CAST(:k AS text) IS NULL OR kind = :k)
             ORDER BY started_at DESC
        """), {"k": kind}).mappings()]
