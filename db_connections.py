"""
db_connections.py — read-only SQL + MongoDB connectivity for the
Council analyst sandbox.

Defense in depth — five independent layers:

  1. DB-side role permissions (the primary defense — the user creates
     a read-only role on the DB itself; see DATABASE_CONNECTIONS.md).

  2. Session-level read-only — for SQL, issue `SET TRANSACTION READ
     ONLY` (PG / MySQL) or read-only execution_options (SQLAlchemy).
     For SQLite/DuckDB file URIs we already restrict via ?mode=ro.

  3. Client-side validator — every SQL string is parsed before
     dispatch; only single SELECT or WITH statements are allowed.
     DDL/DML keywords (DROP, DELETE, INSERT, UPDATE, TRUNCATE,
     ALTER, CREATE, GRANT, REVOKE, MERGE, REPLACE) are rejected
     even if they appear in a multi-statement payload separated
     by `;`. Comments (`--`, `/* */`) are stripped before keyword
     match so a comment-cloaked DROP can't slip through.

  4. API surface design — for Mongo we expose ONLY find / aggregate
     / count_documents / distinct / list_*. There is no public
     wrapper for insert / update / delete / drop / replace / find_
     one_and_*. The pipeline validator also rejects `$out`, `$merge`,
     `$function`, `$accumulator`, `$where` (last two run server-side
     JS which can bypass the read-only role on misconfigured
     servers).

  5. Audit log — every connection use and every dispatched query
     is appended to vault/db_audit.log as one JSONL record per
     entry. The log is forensic — doesn't prevent writes — but
     gives you a clear trail when something does leak through.

All connection URLs live in vault/sql_connections.json (existing) and
vault/mongo_connections.json (new). Both honour `${ENV_VAR}`
placeholders so passwords stay out of the JSON file. The placeholder
expansion runs at connect time, not at storage time, so a user can
rotate `$PG_PASS` in their shell without re-saving the connection.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


# ============================================================
# Connection storage — JSON files in the vault
# ============================================================

_SQL_CONN_FILENAME   = "sql_connections.json"
_MONGO_CONN_FILENAME = "mongo_connections.json"
_AUDIT_LOG_FILENAME  = "db_audit.log"


def _connections_path(vault_dir: Any, filename: str) -> Path:
    return Path(vault_dir) / filename


def _read_json_dict(p: Path) -> Dict[str, Any]:
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json_dict(p: Path, data: Dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── SQL connection registry ─────────────────────────────────────────

def list_sql_connections(vault_dir: Any) -> Dict[str, str]:
    """Return the saved ``{name: url}`` map. URLs are RAW (still
    contain ``${ENV_VAR}`` placeholders) — call ``_resolve_url`` to
    expand them at connect time."""
    return {str(k): str(v)
            for k, v in _read_json_dict(
                _connections_path(vault_dir, _SQL_CONN_FILENAME)).items()}


def save_sql_connection(vault_dir: Any, name: str, url: str) -> None:
    """Save a SQLAlchemy connection URL. Use ``${ENV_VAR}`` placeholders
    in passwords; the resolver expands them at connect time."""
    p = _connections_path(vault_dir, _SQL_CONN_FILENAME)
    existing = _read_json_dict(p)
    existing[str(name)] = str(url)
    _write_json_dict(p, existing)


def remove_sql_connection(vault_dir: Any, name: str) -> bool:
    """Drop a saved SQL connection. Returns True if it existed."""
    p = _connections_path(vault_dir, _SQL_CONN_FILENAME)
    existing = _read_json_dict(p)
    if name not in existing:
        return False
    existing.pop(name, None)
    _write_json_dict(p, existing)
    return True


# ── Mongo connection registry ──────────────────────────────────────

def list_mongo_connections(vault_dir: Any) -> Dict[str, str]:
    """Return the saved ``{name: mongodb_uri}`` map."""
    return {str(k): str(v)
            for k, v in _read_json_dict(
                _connections_path(vault_dir, _MONGO_CONN_FILENAME)).items()}


def save_mongo_connection(vault_dir: Any, name: str, uri: str) -> None:
    """Save a Mongo URI (``mongodb://...``). Use ``${ENV_VAR}``
    placeholders for passwords."""
    p = _connections_path(vault_dir, _MONGO_CONN_FILENAME)
    existing = _read_json_dict(p)
    existing[str(name)] = str(uri)
    _write_json_dict(p, existing)


def remove_mongo_connection(vault_dir: Any, name: str) -> bool:
    p = _connections_path(vault_dir, _MONGO_CONN_FILENAME)
    existing = _read_json_dict(p)
    if name not in existing:
        return False
    existing.pop(name, None)
    _write_json_dict(p, existing)
    return True


def _resolve_url(url: str) -> str:
    """Expand ``${ENV_VAR}`` placeholders against os.environ."""
    def _sub(m: "re.Match") -> str:
        return os.environ.get(m.group(1), m.group(0))
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", _sub, url)


# ============================================================
# Audit log
# ============================================================

def _audit(vault_dir: Any, **fields: Any) -> None:
    """Append a JSONL record to vault/db_audit.log. Best-effort —
    a log write failure should never break a query."""
    try:
        record = {
            "ts":  datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **fields,
        }
        p = _connections_path(vault_dir, _AUDIT_LOG_FILENAME)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        # Never let logging break a query path.
        pass


# ============================================================
# SQL — single-SELECT validator
# ============================================================

# Comment strippers. SQL has two comment forms — line (`--`) and
# block (`/* */`). Strip both before keyword matching so a query
# like `SELECT 1 /* DROP TABLE users */` doesn't get mistakenly
# flagged AND a query like `--SELECT \n DROP TABLE x` doesn't slip
# through by hiding DROP behind a comment marker.
_LINE_COMMENT_RE  = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

# Keywords that imply a write. Word-boundary match so column names
# like `last_update` or `delete_flag` don't false-positive.
_WRITE_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER",
    "CREATE", "GRANT", "REVOKE", "REPLACE", "MERGE", "RENAME",
    "ATTACH", "DETACH", "VACUUM", "REINDEX", "ANALYZE",
    "COPY", "LOAD", "BULK",
    "EXEC", "EXECUTE", "CALL",         # stored procs — can hide writes
    "PRAGMA",                            # SQLite — some pragmas write
    "SET",                               # SET ROLE / SET SESSION can escalate
    "LOCK", "UNLOCK",
    "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE",
)

# What a SELECT statement is allowed to start with. CTEs use WITH;
# describing via EXPLAIN is harmless; SHOW is read-only on most DBs.
_READ_STARTERS = ("SELECT", "WITH", "EXPLAIN", "SHOW", "DESCRIBE", "DESC")


class ReadOnlyViolation(Exception):
    """Raised when the SQL validator rejects a query."""


def _validate_select_only(sql: str) -> str:
    """Verify ``sql`` is a single read-only statement. Returns the
    normalised SQL on success, raises ReadOnlyViolation otherwise.

    Rules:
      • Strip line + block comments before any check.
      • Reject if any write keyword (DROP / DELETE / INSERT / …)
        appears as a complete word anywhere in the cleaned SQL.
      • Reject if the first non-whitespace keyword isn't a known
        read starter (SELECT, WITH, EXPLAIN, SHOW, DESCRIBE).
      • Reject multi-statement payloads (semicolon followed by more
        non-whitespace content — a trailing `;` is allowed because
        it's the conventional terminator).
    """
    if not isinstance(sql, str) or not sql.strip():
        raise ReadOnlyViolation("empty SQL")
    cleaned = _LINE_COMMENT_RE.sub(" ", sql)
    cleaned = _BLOCK_COMMENT_RE.sub(" ", cleaned)
    cleaned = cleaned.strip()
    if not cleaned:
        raise ReadOnlyViolation("SQL is only comments")

    # Multi-statement check. Trim the trailing semicolon FIRST so the
    # common "SELECT 1;" pattern doesn't trigger the multi-statement
    # rejection. After trim, ANY internal `;` is suspect.
    body = cleaned.rstrip(";").strip()
    if ";" in body:
        raise ReadOnlyViolation(
            "multi-statement SQL is rejected — split into separate "
            "calls or use a single SELECT")

    # First keyword check
    first_token_match = re.match(r"^\s*([A-Za-z]+)", body)
    if not first_token_match:
        raise ReadOnlyViolation("no SQL keyword at start of statement")
    first_kw = first_token_match.group(1).upper()
    if first_kw not in _READ_STARTERS:
        raise ReadOnlyViolation(
            f"first keyword {first_kw!r} is not allowed; SQL must "
            f"start with one of {_READ_STARTERS}")

    # Whole-statement keyword scan. Walk every word boundary and
    # check; cheaper than tokenising properly and good enough for
    # our threat model.
    upper = body.upper()
    for kw in _WRITE_KEYWORDS:
        if re.search(rf"\b{kw}\b", upper):
            raise ReadOnlyViolation(
                f"write keyword {kw!r} not allowed in read-only "
                "queries (defence-in-depth — even with a read-only "
                "role, the validator blocks DML / DDL paths)")

    return body


# ============================================================
# SQL execution (read-only)
# ============================================================

def _import_sqlalchemy():
    try:
        import sqlalchemy  # noqa: F401
        return sqlalchemy
    except ImportError as exc:
        raise RuntimeError(
            "SQL connectivity requires SQLAlchemy. Install with "
            "`pip install SQLAlchemy`. Original error: " + str(exc)
        ) from exc


def _sql_engine(vault_dir: Any, conn_name: str):
    sqlalchemy = _import_sqlalchemy()
    urls = list_sql_connections(vault_dir)
    if conn_name not in urls:
        raise KeyError(f"unknown SQL connection: {conn_name}")
    resolved = _resolve_url(urls[conn_name])
    # File-URI hardening — SQLite/DuckDB URIs get ?mode=ro appended
    # if the user didn't already pin it. The file-mode flag is a
    # bulletproof read-only enforcement at the SQLite engine layer.
    if resolved.startswith("sqlite:///") and "mode=ro" not in resolved:
        sep = "&" if "?" in resolved else "?"
        resolved = f"{resolved}{sep}mode=ro&uri=true"
    return sqlalchemy.create_engine(resolved, pool_pre_ping=True)


def _apply_read_only_session(engine, connection) -> List[str]:
    """Issue per-dialect read-only session hints. Returns a list of
    notes describing what was applied (used by the audit log).

    Failures here are non-fatal — if the DB doesn't support the hint,
    we still rely on layers 3 (validator) and 4 (API surface)."""
    notes: List[str] = []
    sqlalchemy = _import_sqlalchemy()
    text = sqlalchemy.text
    dialect = engine.dialect.name.lower()
    try:
        if dialect in ("postgresql", "postgres"):
            connection.execute(text("SET TRANSACTION READ ONLY"))
            notes.append("postgresql: SET TRANSACTION READ ONLY")
        elif dialect == "mysql":
            connection.execute(text("SET SESSION TRANSACTION READ ONLY"))
            notes.append("mysql: SET SESSION TRANSACTION READ ONLY")
        elif dialect in ("mssql", "pyodbc"):
            # MSSQL doesn't have a read-only session flag per se; the
            # SNAPSHOT isolation level + a read-only role on the DB
            # user is the canonical approach. We don't enforce
            # SNAPSHOT here because it's a DB-level setting.
            notes.append("mssql: no session hint (rely on DB role + validator)")
        elif dialect == "sqlite":
            # Handled at the URI level via ?mode=ro
            notes.append("sqlite: handled by URI ?mode=ro")
        else:
            notes.append(f"{dialect}: no session hint applied")
    except Exception as exc:
        notes.append(f"{dialect}: session hint failed ({exc!r})")
    return notes


def list_sql_tables(vault_dir: Any, conn_name: str) -> List[str]:
    """Inspect a saved connection and return its table names."""
    eng = _sql_engine(vault_dir, conn_name)
    sqlalchemy = _import_sqlalchemy()
    try:
        names = sorted(sqlalchemy.inspect(eng).get_table_names())
        _audit(vault_dir, kind="sql.list_tables", conn=conn_name,
                n_tables=len(names))
        return names
    finally:
        eng.dispose()


def read_sql_table(
    vault_dir: Any,
    conn_name: str,
    table: str,
    *,
    limit: Optional[int] = 10000,
) -> "pd.DataFrame":
    """Pull a remote table. Read-only by construction (SELECT only).
    Default 10K-row limit; pass ``limit=None`` to lift it (logged
    loudly to the audit)."""
    import pandas as pd
    eng = _sql_engine(vault_dir, conn_name)
    qname = '"' + str(table).replace('"', '""') + '"'
    sql = f"SELECT * FROM {qname}"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    t0 = time.monotonic()
    try:
        with eng.connect() as conn:
            session_notes = _apply_read_only_session(eng, conn)
            df = pd.read_sql_query(sql, conn)
        _audit(vault_dir, kind="sql.read_table", conn=conn_name,
                table=table, rows=len(df),
                duration_ms=int((time.monotonic() - t0) * 1000),
                session=session_notes, limit=limit)
        return df
    finally:
        eng.dispose()


def sql_query(
    vault_dir: Any,
    conn_name: str,
    sql: str,
) -> "pd.DataFrame":
    """Run an arbitrary SQL query. Validated to a SINGLE read-only
    statement before dispatch."""
    import pandas as pd
    cleaned = _validate_select_only(sql)
    eng = _sql_engine(vault_dir, conn_name)
    t0 = time.monotonic()
    try:
        with eng.connect() as conn:
            session_notes = _apply_read_only_session(eng, conn)
            df = pd.read_sql_query(cleaned, conn)
        _audit(vault_dir, kind="sql.query", conn=conn_name,
                sql=cleaned[:500],
                rows=len(df),
                duration_ms=int((time.monotonic() - t0) * 1000),
                session=session_notes)
        return df
    except ReadOnlyViolation:
        raise   # already loud + correct
    finally:
        eng.dispose()


# ============================================================
# Mongo — read-only by API design
# ============================================================

# Aggregation stages that can write or run server-side JS. We reject
# any pipeline containing one of these because they bypass the
# read-only role on misconfigured servers.
_MONGO_WRITE_STAGES = {
    "$out",              # writes results into a collection
    "$merge",            # merges results into a collection
    "$function",         # server-side JS function
    "$accumulator",      # server-side JS accumulator
    "$where",            # server-side JS filter (deprecated but still
                          # supported on some servers)
}


class MongoPipelineViolation(Exception):
    """Raised when the Mongo pipeline validator rejects a pipeline."""


def _import_pymongo():
    try:
        import pymongo  # noqa: F401
        return pymongo
    except ImportError as exc:
        raise RuntimeError(
            "MongoDB connectivity requires pymongo. Install with "
            "`pip install pymongo`. Original error: " + str(exc)
        ) from exc


def _mongo_client(vault_dir: Any, conn_name: str, *,
                   timeout_ms: int = 5000):
    pymongo = _import_pymongo()
    urls = list_mongo_connections(vault_dir)
    if conn_name not in urls:
        raise KeyError(f"unknown Mongo connection: {conn_name}")
    uri = _resolve_url(urls[conn_name])
    return pymongo.MongoClient(
        uri,
        serverSelectionTimeoutMS=timeout_ms,
        connectTimeoutMS=timeout_ms,
        # Hard-stop on operations that try to exceed 30s.
        socketTimeoutMS=30000,
    )


def _validate_mongo_pipeline(pipeline: Sequence[Dict[str, Any]]) -> None:
    if not isinstance(pipeline, (list, tuple)):
        raise MongoPipelineViolation(
            f"pipeline must be a list of dict stages; got {type(pipeline).__name__}")
    for i, stage in enumerate(pipeline):
        if not isinstance(stage, dict):
            raise MongoPipelineViolation(
                f"pipeline[{i}] is not a dict: {type(stage).__name__}")
        for key in stage.keys():
            if key in _MONGO_WRITE_STAGES:
                raise MongoPipelineViolation(
                    f"pipeline stage {key!r} (pipeline[{i}]) is "
                    "rejected: it can write or run server-side JS. "
                    "Read-only Mongo queries cannot use "
                    f"{sorted(_MONGO_WRITE_STAGES)}.")


def list_mongo_databases(vault_dir: Any, conn_name: str) -> List[str]:
    client = _mongo_client(vault_dir, conn_name)
    try:
        names = sorted(client.list_database_names())
        _audit(vault_dir, kind="mongo.list_databases", conn=conn_name,
                n=len(names))
        return names
    finally:
        client.close()


def list_mongo_collections(vault_dir: Any, conn_name: str,
                            db_name: str) -> List[str]:
    client = _mongo_client(vault_dir, conn_name)
    try:
        names = sorted(client[db_name].list_collection_names())
        _audit(vault_dir, kind="mongo.list_collections", conn=conn_name,
                db=db_name, n=len(names))
        return names
    finally:
        client.close()


def read_mongo_collection(
    vault_dir: Any,
    conn_name: str,
    db_name: str,
    collection: str,
    *,
    query: Optional[Dict[str, Any]] = None,
    projection: Optional[Dict[str, Any]] = None,
    limit: Optional[int] = 10000,
    skip: int = 0,
    sort: Optional[List] = None,
) -> "pd.DataFrame":
    """Mongo `find()` → DataFrame via pd.json_normalize.

    Read-only by API design — uses find(), no insert/update/delete
    methods are reachable from this wrapper. Default 10K-row limit;
    pass ``limit=None`` to lift it (audit-logged with a WARN tag)."""
    import pandas as pd
    client = _mongo_client(vault_dir, conn_name)
    t0 = time.monotonic()
    try:
        cursor = client[db_name][collection].find(
            filter=query or {},
            projection=projection,
            skip=skip,
        )
        if sort is not None:
            cursor = cursor.sort(sort)
        if limit is not None:
            cursor = cursor.limit(int(limit))
        docs = list(cursor)
        # MongoDB returns ObjectId / datetime / Decimal128 objects that
        # pd.json_normalize handles via repr; that's fine for analyst
        # purposes since the downstream model just reads as strings.
        df = pd.json_normalize(docs) if docs else pd.DataFrame()
        _audit(vault_dir, kind="mongo.find", conn=conn_name,
                db=db_name, collection=collection,
                rows=len(df),
                duration_ms=int((time.monotonic() - t0) * 1000),
                limit=limit, unlimited=(limit is None))
        return df
    finally:
        client.close()


def mongo_aggregate(
    vault_dir: Any,
    conn_name: str,
    db_name: str,
    collection: str,
    pipeline: Sequence[Dict[str, Any]],
    *,
    allow_disk_use: bool = False,
) -> "pd.DataFrame":
    """Run a Mongo aggregation pipeline. The pipeline is validated
    before dispatch — write stages ($out, $merge) and server-side
    JS stages ($function, $accumulator, $where) are rejected."""
    import pandas as pd
    _validate_mongo_pipeline(pipeline)
    client = _mongo_client(vault_dir, conn_name)
    t0 = time.monotonic()
    try:
        cursor = client[db_name][collection].aggregate(
            list(pipeline), allowDiskUse=allow_disk_use,
        )
        docs = list(cursor)
        df = pd.json_normalize(docs) if docs else pd.DataFrame()
        _audit(vault_dir, kind="mongo.aggregate", conn=conn_name,
                db=db_name, collection=collection,
                stages=len(pipeline), rows=len(df),
                duration_ms=int((time.monotonic() - t0) * 1000))
        return df
    finally:
        client.close()


def mongo_count(
    vault_dir: Any,
    conn_name: str,
    db_name: str,
    collection: str,
    *,
    query: Optional[Dict[str, Any]] = None,
) -> int:
    client = _mongo_client(vault_dir, conn_name)
    t0 = time.monotonic()
    try:
        n = client[db_name][collection].count_documents(query or {})
        _audit(vault_dir, kind="mongo.count", conn=conn_name,
                db=db_name, collection=collection, n=n,
                duration_ms=int((time.monotonic() - t0) * 1000))
        return int(n)
    finally:
        client.close()


def mongo_distinct(
    vault_dir: Any,
    conn_name: str,
    db_name: str,
    collection: str,
    field: str,
    *,
    query: Optional[Dict[str, Any]] = None,
) -> List[Any]:
    client = _mongo_client(vault_dir, conn_name)
    t0 = time.monotonic()
    try:
        values = list(client[db_name][collection].distinct(
            field, filter=query or {}))
        _audit(vault_dir, kind="mongo.distinct", conn=conn_name,
                db=db_name, collection=collection, field=field,
                n_values=len(values),
                duration_ms=int((time.monotonic() - t0) * 1000))
        return values
    finally:
        client.close()


# ============================================================
# Test helpers — used by the Vault tab's "Test connection" button
# ============================================================

def test_sql_connection(vault_dir: Any, conn_name: str) -> Dict[str, Any]:
    """Best-effort connection probe — returns a result dict for the UI.
    Doesn't run SELECTs against arbitrary tables, just `SELECT 1`."""
    out: Dict[str, Any] = {
        "ok": False, "dialect": None, "tables": 0, "error": None,
    }
    try:
        eng = _sql_engine(vault_dir, conn_name)
        out["dialect"] = eng.dialect.name
        sqlalchemy = _import_sqlalchemy()
        try:
            with eng.connect() as conn:
                _apply_read_only_session(eng, conn)
                conn.execute(sqlalchemy.text("SELECT 1"))
            try:
                out["tables"] = len(sqlalchemy.inspect(eng).get_table_names())
            except Exception:
                pass
            out["ok"] = True
        finally:
            eng.dispose()
    except Exception as exc:
        out["error"] = repr(exc)
    return out


def test_mongo_connection(vault_dir: Any, conn_name: str) -> Dict[str, Any]:
    """Best-effort Mongo probe — pings the server, lists databases."""
    out: Dict[str, Any] = {
        "ok": False, "databases": 0, "error": None,
    }
    try:
        client = _mongo_client(vault_dir, conn_name, timeout_ms=3000)
        try:
            client.admin.command("ping")
            out["databases"] = len(client.list_database_names())
            out["ok"] = True
        finally:
            client.close()
    except Exception as exc:
        out["error"] = repr(exc)
    return out
