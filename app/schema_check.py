"""Does the live database still have everything db/schema.sql describes?

`flask --app wsgi db verify` (also run by scripts/replit-start.sh after the
migrations). It reads db/schema.sql (which always describes the whole
database) and lists the tables, columns, indexes, functions, triggers and
views the database is missing.

Why: Replit's "database changes" prompt on publish makes the published
database match the workspace's. When the workspace database is behind (Run
didn't apply our migrations), approving it silently removes columns that
migrations added, while the migration record still says they're there. This
check catches that on the next start, writes a problem on the Integrations
page, and migration 0007-style repairs put things back.
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import text

from . import db

SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
_NOT_COLUMNS = {"check", "unique", "constraint", "primary", "foreign", "exclude", "like"}


def expected(path: Path = SCHEMA) -> dict[str, set]:
    """What schema.sql creates: {'columns': {(table, column)}, 'indexes', 'functions', 'triggers', 'views'}."""
    sql = re.sub(r"--[^\n]*", "", path.read_text())
    out: dict[str, set] = {"columns": set(), "indexes": set(), "functions": set(), "triggers": set(),
                           "views": set(), "tables": set()}
    for m in re.finditer(r"^CREATE TABLE (\w+) \((.*?)^\);", sql, re.M | re.S):
        table = m.group(1)
        out["tables"].add(table)
        for line in m.group(2).splitlines():
            col = re.match(r"^  ([a-z_][a-z0-9_]*)\s+\S", line)
            if col and col.group(1) not in _NOT_COLUMNS:
                out["columns"].add((table, col.group(1)))
    out["indexes"] = set(re.findall(r"^CREATE (?:UNIQUE )?INDEX (\w+)", sql, re.M))
    out["functions"] = set(re.findall(r"^CREATE (?:OR REPLACE )?FUNCTION (\w+)", sql, re.M))
    out["triggers"] = set(re.findall(r"^CREATE (?:CONSTRAINT )?TRIGGER (\w+)", sql, re.M))
    out["views"] = set(re.findall(r"^CREATE (?:OR REPLACE )?VIEW (\w+)", sql, re.M))
    return out


def missing() -> list[str]:
    """What the database lacks, as readable lines ('column orders.buyer_user_id'); [] when complete."""
    want = expected()
    with db.engine().connect() as conn:
        q = lambda sql: {tuple(r) if len(r) > 1 else r[0] for r in conn.execute(text(sql)).all()}  # noqa: E731
        have_cols = q("""SELECT table_name, column_name FROM information_schema.columns
                          WHERE table_schema = current_schema()""")
        have_tables = q("""SELECT table_name FROM information_schema.tables
                            WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'""")
        have_idx = q("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")
        have_fn = q("""SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                        WHERE n.nspname = current_schema()""")
        have_trg = q("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
        have_views = q("SELECT viewname FROM pg_views WHERE schemaname = current_schema()")
    lines = [f"table {t}" for t in sorted(want["tables"] - have_tables)]
    lines += [f"column {t}.{c}" for t, c in sorted(want["columns"] - have_cols) if t in have_tables]
    lines += [f"index {i}" for i in sorted(want["indexes"] - have_idx)]
    lines += [f"function {f}" for f in sorted(want["functions"] - have_fn)]
    lines += [f"trigger {t}" for t in sorted(want["triggers"] - have_trg)]
    lines += [f"view {v}" for v in sorted(want["views"] - have_views)]
    return lines


def record(lines: list[str]) -> None:
    """Show the result on the Integrations page (one problem, cleared when complete again)."""
    from .integrations import runs
    with db.transaction() as conn:
        if lines:
            runs.issue(conn, source="engine", code="DB_SCHEMA_MISSING", severity="error", external_ref="schema",
                       message="The database is missing parts the app needs: " + ", ".join(lines[:15])
                               + (f" and {len(lines) - 15} more" if len(lines) > 15 else "")
                               + ". Usually Replit's \"database changes\" prompt on publish removed them; "
                                 "don't approve it. Tell the developer.", details={"missing": lines})
        else:
            runs.resolve(conn, source="engine", external_ref="schema")
