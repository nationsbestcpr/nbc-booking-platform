"""The action log (audit_log): every staff action is recorded, never changed.

Call record() inside the same transaction as the change, so the change and
its log entry are saved together or not at all.
"""

from __future__ import annotations

import json
from typing import Any

from flask import request
from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import auth


def record(conn: Connection, action: str, entity_type: str, entity_id: int | None = None, *,
           before: Any = None, after: Any = None, reason: str | None = None) -> None:
    user = auth.current_user()
    conn.execute(text("""
        INSERT INTO audit_log (actor_type, user_account_id, acting_role, action, entity_type, entity_id,
                               reason, before, after, ip)
        VALUES ('user', :uid, :role, :action, :etype, :eid, :reason,
                CAST(:before AS jsonb), CAST(:after AS jsonb), CAST(:ip AS inet))
    """), {
        "uid": user.id, "role": user.main_role["code"], "action": action, "etype": entity_type,
        "eid": entity_id, "reason": reason,
        "before": None if before is None else json.dumps(before, default=str),
        "after": None if after is None else json.dumps(after, default=str),
        "ip": request.remote_addr,
    })
