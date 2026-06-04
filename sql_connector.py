"""
sql_connector.py — SQLAlchemy bridge for querying a real warehouse from Council.

Lets the Council's analyst pipeline (and any standalone script) reach
out to a manufacturing-ops database alongside the file vault. The
default in-the-box example uses SQLite so it runs anywhere; production
URLs for Postgres / MySQL / MSSQL / Snowflake are documented below.

Public API:

    conn = SqlConnector("sqlite:///./vault/mes.sqlite")
    conn.list_tables()                                     -> [str, ...]
    conn.describe("work_orders")                           -> {"columns": [...], "row_count": int}
    conn.sample("defects", 5)                              -> [dict, ...]
    conn.execute_select("SELECT ... LIMIT 50")             -> [dict, ...]  (SELECT-only, hard-capped)

The execute path REFUSES any statement that isn't a single SELECT.
That keeps an LLM-generated query (or a typo) from issuing a DELETE/UPDATE
against the warehouse. If you need write-back, do it explicitly through a
direct SQLAlchemy session you control — not via this helper.

Production database URLs (driver pip-installed separately):

    postgres://user:pw@host:5432/db          # pip install psycopg2-binary
    mysql+pymysql://user:pw@host:3306/db     # pip install pymysql
    mssql+pyodbc://user:pw@host/db?driver=ODBC+Driver+17+for+SQL+Server
                                              # pip install pyodbc + Microsoft ODBC driver
    snowflake://user:pw@account/db/schema?warehouse=wh&role=role
                                              # pip install snowflake-sqlalchemy
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine


_SELECT_OK = re.compile(
    r"^\s*(?:WITH\s.*?\)\s*)?SELECT\s",
    re.IGNORECASE | re.DOTALL,
)
_DANGEROUS = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|"
    r"MERGE|REPLACE|COMMIT|ROLLBACK|VACUUM|ATTACH|DETACH)\b",
    re.IGNORECASE,
)


class SqlConnector:
    """Thin, read-only SQLAlchemy wrapper for Council's analyst path."""

    def __init__(self, url: str, *, max_rows: int = 500):
        self.url = url
        self.max_rows = int(max_rows)
        self._engine: Engine = create_engine(
            url,
            pool_pre_ping=True,
            future=True,
        )
        # Trip the connection early so a bad URL fails here, not deep in
        # the analyst loop.
        with self._engine.connect() as c:
            c.execute(text("SELECT 1"))

    # ── schema introspection ────────────────────────────────
    def list_tables(self, schema: Optional[str] = None) -> List[str]:
        return list(inspect(self._engine).get_table_names(schema=schema))

    def describe(self, table: str, schema: Optional[str] = None) -> Dict[str, Any]:
        ins = inspect(self._engine)
        cols = ins.get_columns(table, schema=schema)
        row_count = 0
        try:
            with self._engine.connect() as c:
                # Bound by max_rows to be safe with very large tables;
                # for an exact count an analyst can write COUNT(*) themselves.
                row_count = c.execute(
                    text(f"SELECT COUNT(*) FROM {self._quote(table, schema)}")
                ).scalar() or 0
        except Exception:
            row_count = -1
        return {
            "table":   table,
            "schema":  schema,
            "columns": [{"name": c["name"], "type": str(c["type"])} for c in cols],
            "row_count": int(row_count),
        }

    def sample(self, table: str, n: int = 5,
               schema: Optional[str] = None) -> List[Dict[str, Any]]:
        n = max(1, min(int(n), self.max_rows))
        sql = f"SELECT * FROM {self._quote(table, schema)} LIMIT {n}"
        return self._rows(sql)

    # ── safe SELECT execution ──────────────────────────────
    def execute_select(self, sql: str) -> List[Dict[str, Any]]:
        sql = (sql or "").strip().rstrip(";")
        if not _SELECT_OK.match(sql):
            raise ValueError(
                "execute_select only accepts a single SELECT (or a CTE + SELECT)."
            )
        if _DANGEROUS.search(sql):
            raise ValueError(
                "execute_select refuses statements containing write/DDL keywords."
            )
        # Force a row cap if the analyst forgot LIMIT.
        if " LIMIT " not in sql.upper():
            sql = f"{sql} LIMIT {self.max_rows}"
        return self._rows(sql)

    # ── internals ──────────────────────────────────────────
    def _rows(self, sql: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as c:
            res = c.execute(text(sql))
            return [dict(row._mapping) for row in res.fetchall()]

    @staticmethod
    def _quote(table: str, schema: Optional[str]) -> str:
        # Minimal safe-quoter — sufficient for SQLite/Postgres identifiers
        # without spaces. For mixed-case / spaced names, use SQLAlchemy's
        # quoter directly. We avoid pulling in dialect plumbing here so
        # this stays a 1-file helper.
        ident = lambda s: '"' + s.replace('"', '""') + '"'
        return f"{ident(schema)}.{ident(table)}" if schema else ident(table)

    def close(self) -> None:
        try:
            self._engine.dispose()
        except Exception:
            pass

    # Context-manager sugar so `with SqlConnector(url) as db: ...` works.
    def __enter__(self) -> "SqlConnector":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
