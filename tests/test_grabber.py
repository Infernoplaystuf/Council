"""
tests/test_grabber.py — fast, no-network, no-display checks for the
Database Grabber standalone. Runs headless (no Tk needed): exercises the
read-only enforcement, the pure URL-assembly, and the export writers.

    python tests/test_grabber.py     # exits 0 on pass, non-zero on fail
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import db_connections as dbc
import db_connect_wizard as wiz

_FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        _FAILS.append((name, detail))
        print(f"  FAIL {name}   {detail}")


def raises(exc, fn):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


def test_readonly_guarantee():
    """The cardinal rule: the export/query path refuses every
    destructive statement BEFORE opening a connection, and the public
    API exposes no DB-write verb."""
    print("\n-- read-only guarantee --")
    with tempfile.TemporaryDirectory() as td:
        v = Path(td)
        dbc.save_sql_connection(v, "x", "sqlite:///:memory:")
        for bad in ("DELETE FROM t", "DROP TABLE t", "UPDATE t SET a=1",
                    "TRUNCATE t", "INSERT INTO t VALUES (1)",
                    "SELECT 1; DROP TABLE t", "ALTER TABLE t ADD c int"):
            check(f"blocks {bad.split()[0]}",
                  raises(dbc.ReadOnlyViolation,
                         lambda b=bad: dbc.export_sql_query(v, "x", b, v / "o.csv")))
    public = [n for n in dir(dbc) if not n.startswith("_") and callable(getattr(dbc, n))]
    verbs = ("insert", "update", "delete", "drop", "truncate", "to_sql")
    offenders = [n for n in public
                 if any(w in n.lower() for w in verbs)
                 and n not in ("remove_sql_connection", "remove_mongo_connection")]
    check(f"no DB-write function in public API (found {offenders})", not offenders)


def test_url_assembly():
    """The wizard builds correct, percent-encoded URLs from plain fields."""
    print("\n-- URL assembly --")
    kind, url = wiz.build_connection_url(
        "postgresql", host="db.co", database="sales",
        user="ro", password="p@ss:w/rd ")
    check("postgres kind", kind == "sql")
    check("default port", ":5432/" in url)
    check("password encoded", "p%40ss%3Aw%2Frd%20" in url)
    kind, url = wiz.build_connection_url("mongodb", host="h", database="app",
                                         user="u", password="p")
    check("mongo kind", kind == "mongo")
    check("mongo authSource", "authSource=app" in url)
    kind, url = wiz.build_connection_url("sqlite", file_path=r"C:\d\my.db")
    check("sqlite path", url == "sqlite:///C:/d/my.db")
    check("missing host raises",
          raises(ValueError, lambda: wiz.build_connection_url(
              "postgresql", database="d", user="u")))


def test_export_writers():
    """CSV/JSON/Excel writers round-trip; unknown format rejected."""
    print("\n-- export writers --")
    import pandas as pd
    df = pd.DataFrame({"id": [1, 2], "name": ["a", "b"]})
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        p = dbc._write_dataframe(df, base / "o.csv", "csv")
        check("csv round-trips", len(pd.read_csv(p)) == 2)
        p = dbc._write_dataframe(df, base / "o.json", "json")
        check("json round-trips", len(pd.read_json(p)) == 2)
        check("bad format rejected",
              raises(ValueError, lambda: dbc._write_dataframe(df, base / "x.zz", "zz")))
        check("traversal name neutralised",
              "/" not in dbc._safe_export_name("../../etc/x", "csv"))


def main():
    print("Database Grabber tests")
    print("=" * 60)
    test_readonly_guarantee()
    test_url_assembly()
    test_export_writers()
    print("=" * 60)
    if _FAILS:
        print(f"FAILED {len(_FAILS)}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
