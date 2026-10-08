"""Schema inspection, query checker and guarded read-only execution for the SQL path.

Three independent layers keep the agent from touching the data:
  1. static check   - a single statement that starts with SELECT or WITH
  2. SQLite authorizer - at compile time only SELECT on allow-listed tables is permitted
  3. read-only connection (mode=ro + PRAGMA query_only) - writes are impossible even if 1 and 2 are bypassed
Execution also has a row cap and a wall-clock timeout.
"""
import re
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "delivery.db"
ALLOWED_TABLES = frozenset({"customers", "projects", "components", "deployments", "incidents"})
DENIED_FUNCTIONS = frozenset({"load_extension", "readfile", "writefile", "edit", "zipfile", "fts3_tokenizer"})
MAX_ROWS = 200
TIMEOUT_SECONDS = 5.0
REFERENCE_DATE = "2026-10-05"

CONVENTIONS = f"""Conventions:
- Database engine: SQLite. Today's date is {REFERENCE_DATE}; "last quarter" means the most recent complete calendar quarter before today.
- Timestamps are text in 'YYYY-MM-DD HH:MM:SS'; compare them as text or use strftime / julianday.
- Resolution time in hours = (julianday(resolved_at) - julianday(reported_at)) * 24, meaningful only for status = 'closed'.
- incidents.category equals the layer of the incident's component.
- Open incidents have NULL resolved_at, root_cause, solution_pattern and resolution_summary (the root cause is not yet known).
- Every incident belongs to one deployment (deployment_id), which has env_type and version."""


_CTE_NAME = re.compile(r"(?is)(?:\bwith\b(?:\s+recursive)?|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\([^)]*\))?\s+as\s*(?:not\s+materialized\s*|materialized\s*)?\(")


def _cte_names(sql):
    """Names declared in WITH clauses. SQLite reports reads of a recursive CTE as reads of a 'table' with that
    name, so those names must be tolerated; real hidden tables (sqlite_*) never are."""
    return {n for n in _CTE_NAME.findall(sql) if not n.lower().startswith("sqlite_")}


def _make_authorizer(cte_names=frozenset()):
    def _authorizer(action, arg1, arg2, dbname, source):
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            return sqlite3.SQLITE_OK if (arg1 in ALLOWED_TABLES or arg1 in cte_names) else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_DENY if (arg2 or "").lower() in DENIED_FUNCTIONS else sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    return _authorizer


def connect_readonly(db_path=DB_PATH, guarded=True, cte_names=frozenset()):
    conn = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)  # bounds zeroblob()/randomblob() style memory abuse
    if guarded:
        conn.set_authorizer(_make_authorizer(cte_names))
    return conn


def _strip_comments(sql):
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"--[^\n]*", " ", sql)


def check_query(sql, db_path=DB_PATH):
    """Return {'ok': bool, 'error': str|None, 'sql': cleaned sql}."""
    cleaned = (sql or "").strip().rstrip(";").strip()
    if not cleaned:
        return dict(ok=False, error="Empty query.", sql=cleaned)
    if not re.match(r"(?is)^(select|with)\b", _strip_comments(cleaned).strip()):
        return dict(ok=False, sql=cleaned,
                    error="Only a single read-only SELECT statement (optionally starting with WITH) is allowed.")
    conn = connect_readonly(db_path, cte_names=_cte_names(cleaned))
    try:
        conn.execute("EXPLAIN QUERY PLAN " + cleaned).fetchall()
    except (sqlite3.ProgrammingError, sqlite3.Warning):
        return dict(ok=False, sql=cleaned, error="Only one SQL statement is allowed.")
    except sqlite3.DatabaseError as e:
        msg = str(e)
        if "not authorized" in msg.lower():
            msg = f"Query uses a forbidden operation or table. Only SELECT on {sorted(ALLOWED_TABLES)} is allowed."
        return dict(ok=False, sql=cleaned, error=msg)
    finally:
        conn.close()
    return dict(ok=True, error=None, sql=cleaned)


def execute_query(sql, db_path=DB_PATH, max_rows=MAX_ROWS, timeout=TIMEOUT_SECONDS):
    """Check, then run read-only. Returns {'ok','error','columns','rows','truncated','sql'}."""
    chk = check_query(sql, db_path)
    if not chk["ok"]:
        return dict(ok=False, error=chk["error"], columns=[], rows=[], truncated=False, sql=chk["sql"])
    conn = connect_readonly(db_path, cte_names=_cte_names(chk["sql"]))
    deadline = time.monotonic() + timeout
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
    try:
        cur = conn.execute(chk["sql"])
        columns = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchmany(max_rows + 1)]
    except sqlite3.Error as e:
        msg = "Query timed out; simplify it or add filters." if "interrupted" in str(e).lower() else str(e)
        return dict(ok=False, error=msg, columns=[], rows=[], truncated=False, sql=chk["sql"])
    finally:
        conn.close()
    return dict(ok=True, error=None, columns=columns, rows=rows[:max_rows],
                truncated=len(rows) > max_rows, sql=chk["sql"])


def get_schema_text(db_path=DB_PATH):
    """Table definitions, row counts, sample values of low-cardinality text columns, and conventions."""
    conn = connect_readonly(db_path, guarded=False)
    parts = []
    try:
        for table in sorted(ALLOWED_TABLES):
            cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            lines = [f"TABLE {table} ({n} rows)"]
            for _, name, ctype, _, _, pk in cols:
                note = ""
                skip = name.endswith("_id") or "_at" in name or "date" in name or "summary" in name or name == "name"
                if ctype.upper() == "TEXT" and not skip:
                    vals = [r[0] for r in conn.execute(
                        f"SELECT DISTINCT {name} FROM {table} WHERE {name} IS NOT NULL ORDER BY 1 LIMIT 21")]
                    if len(vals) <= 20:
                        note = "  values: " + ", ".join(vals)
                lines.append(f"  - {name} {ctype}{' PRIMARY KEY' if pk else ''}{note}")
            parts.append("\n".join(lines))
    finally:
        conn.close()
    return "\n\n".join(parts) + "\n\n" + CONVENTIONS
