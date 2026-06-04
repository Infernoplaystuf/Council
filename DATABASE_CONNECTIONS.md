# Database connections — read-only by design

The Council app can pull data from SQL databases (PostgreSQL, MySQL,
MSSQL, SQLite, DuckDB) and MongoDB. Every connection is **enforced
read-only across five independent layers** so a misconfigured query
or a model with admin credentials still can't write.

This document covers:

1. [The threat model](#threat-model) — why five layers
2. [Adding a connection from the UI](#adding-a-connection-from-the-vault-tab)
3. [Recommended DB-side read-only roles](#recommended-db-side-read-only-roles) — copy-paste SQL / Mongo shell
4. [Where credentials live](#credentials-and-env-vars) — `${ENV_VAR}` placeholders
5. [The audit log](#audit-log) — `vault/db_audit.log`
6. [Querying from the Council](#querying-from-the-council) — analyst examples
7. [What's intentionally NOT supported](#what-is-not-supported)

---

## Threat model

Two adversaries to defend against:

1. **The model itself.** The local LLM is sandboxed but not trusted —
   it generates pandas code from a user prompt. An off-prompt
   instruction ("delete the orders table") that slipped past the
   prompt validator must not actually drop the table.
2. **A user with admin credentials.** Sometimes the user only has
   admin DB creds to hand. The app's read-only stack should hold
   even when the connection URL has write privileges.

The model gets sandboxed at the call site (the analyst sandbox); the
admin-credential case is what the layered enforcement below is for.

### Five layers of read-only enforcement

| # | Layer | What enforces it | What if THIS layer fails |
|---|---|---|---|
| 1 | **DB role** | A `readonly_user` you create per the recipes below. Primary defense — the DB rejects writes before they hit the wire. | Layer 2 still issues `SET TRANSACTION READ ONLY`; the session can't write. |
| 2 | **Session-level read-only** | The app issues per-dialect read-only hints:<br>• PostgreSQL → `SET TRANSACTION READ ONLY`<br>• MySQL → `SET SESSION TRANSACTION READ ONLY`<br>• SQLite → `?mode=ro&uri=true` appended<br>• MSSQL → relies on layers 1+3 (no clean session flag) | Layer 3 still validates the SQL string. |
| 3 | **Client-side SQL validator** | `_validate_select_only` strips comments, rejects multi-statement payloads, rejects 25+ DML/DDL keywords (DROP, DELETE, INSERT, UPDATE, TRUNCATE, ALTER, CREATE, GRANT, REVOKE, MERGE, REPLACE, RENAME, ATTACH, DETACH, VACUUM, REINDEX, COPY, LOAD, BULK, EXEC, EXECUTE, CALL, PRAGMA, SET, LOCK, BEGIN, COMMIT, ROLLBACK, …). | Mongo doesn't use SQL — see layer 4. |
| 4 | **API surface design** | Mongo helpers expose ONLY `find` / `aggregate` / `count_documents` / `distinct` / `list_*`. There is no public wrapper for insert / update / delete / drop / replace / find_one_and_*. Aggregation pipelines are validated — `$out`, `$merge`, `$function`, `$accumulator`, `$where` stages are rejected (can write or run server-side JS). | Layer 5 still logs everything. |
| 5 | **Audit log** | Every query → `vault/db_audit.log` as JSONL. Forensic, not preventive — but if anything ever slips through, the log says exactly what happened. | — |

---

## Adding a connection from the Vault tab

1. Open the **🗄 Vault** tab.
2. Find the **🔌 Database Connections (read-only)** panel on the left.
3. Fill the fields:
   - **Name** — short handle you'll reference from analyst queries (e.g. `sales_db`, `orders_mongo`).
   - **Type** — picks the right URL template + storage location.
   - **URL** — pre-filled with a template when you change the Type dropdown. Use `${ENV_VAR}` for the password.
4. Click **💾 Save**.
5. Click **🧪 Test** — verifies connectivity and the read-only role.
6. Double-click the saved connection in the list to open a Browser window showing tables (SQL) or `db.collection` pairs (Mongo). Click any entry to preview the first 50 rows.

The saved connection appears in the list with the password masked:

```
[sql]    sales_db    postgresql://readonly_user:***@10.1.2.3:5432/sales
[mongo]  logs        mongodb://readonly_user:***@10.1.2.3:27017/?authSource=admin
```

`${ENV_VAR}` placeholders pass through unmasked — nothing to hide.

---

## Recommended DB-side read-only roles

### PostgreSQL

```sql
-- As a DB superuser, run once:
CREATE ROLE readonly_user WITH LOGIN PASSWORD 'CHANGEME';

-- Grant USAGE on every schema the app should see
GRANT USAGE ON SCHEMA public TO readonly_user;

-- Grant SELECT on every existing table
GRANT SELECT ON ALL TABLES IN SCHEMA public TO readonly_user;

-- And on every FUTURE table created in that schema:
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO readonly_user;

-- Repeat for any other schemas the app should reach.
```

Connection URL:
```
postgresql://readonly_user:${PG_PASS}@host:5432/dbname
```

### MySQL / MariaDB

```sql
CREATE USER 'readonly_user'@'%' IDENTIFIED BY 'CHANGEME';
GRANT SELECT ON sales.* TO 'readonly_user'@'%';
FLUSH PRIVILEGES;
```

Connection URL:
```
mysql+pymysql://readonly_user:${MYSQL_PASS}@host:3306/sales
```

### MSSQL (SQL Server)

```sql
-- In the target database
CREATE LOGIN readonly_user WITH PASSWORD = 'CHANGEME';
CREATE USER readonly_user FOR LOGIN readonly_user;
ALTER ROLE db_datareader ADD MEMBER readonly_user;
-- db_datareader = SELECT on every table; cannot INSERT/UPDATE/DELETE.
```

Connection URL (requires `pyodbc` + the MS ODBC driver installed):
```
mssql+pyodbc://readonly_user:${MSSQL_PASS}@host:1433/sales?driver=ODBC+Driver+17+for+SQL+Server
```

### MongoDB

```javascript
// As a Mongo admin:
use admin
db.createUser({
  user: "readonly_user",
  pwd: passwordPrompt(),
  roles: [{ role: "read", db: "sales" }]
  // For multi-DB access:
  // roles: [{ role: "readAnyDatabase", db: "admin" }]
})
```

Connection URI:
```
mongodb://readonly_user:${MONGO_PASS}@host:27017/?authSource=admin
```

### SQLite (file-based, no role system)

The SQLite engine is opened with `?mode=ro&uri=true` automatically — the file becomes read-only at the engine level. No role needed.

```
sqlite:///C:/path/to/database.db
```

### DuckDB

DuckDB is opened in read-only mode automatically for connection URLs that resolve to an existing file. If you need explicit control, append `?access_mode=read_only`:

```
duckdb:///C:/path/to/database.duckdb
```

---

## Credentials and `${ENV_VAR}` placeholders

The connection URL stored in `vault/sql_connections.json` or `vault/mongo_connections.json` is **verbatim** — placeholders expand at connect time:

```bash
# Linux / macOS / WSL
export PG_PASS="hunter2"
export MONGO_PASS="hunter2"
```

```cmd
:: Windows cmd
set PG_PASS=hunter2
```

```powershell
# Windows PowerShell
$env:PG_PASS = "hunter2"
```

You can rotate the env var without re-saving the connection — the new value is picked up on the next query. The JSON file is safe to commit / share / inspect because it doesn't contain credentials.

If a `${PG_PASS}` placeholder ever fails to resolve (the env var isn't set), the literal `${PG_PASS}` is sent to the DB which fails with an authentication error — clear signal that the env var is missing.

---

## Audit log

Every database action is appended to `vault/db_audit.log` as one JSONL record per line:

```json
{"ts": "2026-06-04T17:32:11+00:00", "kind": "sql.query", "conn": "sales_db", "sql": "SELECT * FROM orders WHERE date > '2024-01-01' LIMIT 10000", "rows": 1247, "duration_ms": 142, "session": ["postgresql: SET TRANSACTION READ ONLY"]}
{"ts": "2026-06-04T17:33:05+00:00", "kind": "mongo.aggregate", "conn": "logs", "db": "production", "collection": "events", "stages": 4, "rows": 50, "duration_ms": 87}
{"ts": "2026-06-04T17:34:21+00:00", "kind": "sql.list_tables", "conn": "sales_db", "n_tables": 47}
```

Fields per record kind:

| `kind` | Fields |
|---|---|
| `sql.query` | conn, sql (truncated to 500 chars), rows, duration_ms, session (list of hints applied) |
| `sql.read_table` | conn, table, rows, duration_ms, session, limit |
| `sql.list_tables` | conn, n_tables |
| `mongo.find` | conn, db, collection, rows, duration_ms, limit, unlimited (true if `limit=None`) |
| `mongo.aggregate` | conn, db, collection, stages, rows, duration_ms |
| `mongo.count` / `mongo.distinct` | conn, db, collection, n / n_values, duration_ms |
| `mongo.list_databases` / `mongo.list_collections` | conn, db (for collections), n |

The log file is gitignored along with the rest of `vault/`. Tail it with `tail -f vault/db_audit.log` to watch live, or grep for failed/long queries with `jq` once you have something stored.

---

## Querying from the Council

### Analyst sandbox helpers

The model writes pandas code that calls these wrappers. The Vault tab connection name is the second arg:

```python
# SQL
result_df = read_sql_table(VAULT_DIR, "sales_db", "orders", limit=5000)
result_df = sql_query(VAULT_DIR, "sales_db",
                      "SELECT region, SUM(total) AS revenue "
                      "FROM orders WHERE date > '2024-01-01' "
                      "GROUP BY region ORDER BY revenue DESC")

# Mongo — find / aggregate / count / distinct
result_df = read_mongo_collection(
    VAULT_DIR, "logs", "production", "events",
    query={"level": "error"}, limit=1000,
)
result_df = mongo_aggregate(
    VAULT_DIR, "logs", "production", "events",
    pipeline=[
        {"$match": {"level": "error"}},
        {"$group": {"_id": "$service", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ],
)
result_df = pd.DataFrame([{
    "metric": "total_errors",
    "value":  mongo_count(VAULT_DIR, "logs", "production", "events",
                           query={"level": "error"}),
}])
```

### Validator behaviour — examples

What the validator accepts:

```python
sql_query(VAULT_DIR, "sales_db", "SELECT * FROM orders LIMIT 10")
sql_query(VAULT_DIR, "sales_db", "WITH t AS (SELECT * FROM o) SELECT * FROM t")
sql_query(VAULT_DIR, "sales_db", "SELECT * FROM /* comment */ orders")
sql_query(VAULT_DIR, "sales_db", "EXPLAIN SELECT * FROM orders")
```

What the validator rejects (every one raises `ReadOnlyViolation`):

```python
sql_query(VAULT_DIR, "sales_db", "DROP TABLE orders")
sql_query(VAULT_DIR, "sales_db", "DELETE FROM orders WHERE 1=1")
sql_query(VAULT_DIR, "sales_db", "SELECT 1; DROP TABLE orders")  # multi-stmt
sql_query(VAULT_DIR, "sales_db", "/* SELECT */ DROP TABLE orders")  # comment-cloak
sql_query(VAULT_DIR, "sales_db", "INSERT INTO orders VALUES (1, 'x')")
sql_query(VAULT_DIR, "sales_db", "SET ROLE admin; SELECT 1")
```

Mongo pipeline rejection examples:

```python
mongo_aggregate(VAULT_DIR, "logs", "production", "events", pipeline=[
    {"$match": {"level": "error"}},
    {"$out": "errors_archive"},          # rejected — writes a collection
])
mongo_aggregate(VAULT_DIR, "logs", "production", "events", pipeline=[
    {"$function": {"body": "function(){...}", "args": [], "lang": "js"}},
])  # rejected — server-side JS
```

---

## What is not supported

- **Live writes of any kind.** This is the whole point — the system is read-only.
- **DDL via the validator.** Even `CREATE TEMPORARY TABLE` is blocked. If you need temp tables, do it in a Postgres view the read-only role can see.
- **Streaming / cursor-based reads.** Every query materialises into a DataFrame. Pull a million-row table at your peril; default limits exist for a reason.
- **Multiple connections in one query.** Each query targets one named connection. Cross-DB joins happen in pandas after both pulls.
- **Server-side stored procedures.** Even read-only sprocs are blocked because we can't statically prove they don't mutate. Wrap them in a SQL view (which is SELECT-only) instead.
- **Mongo change streams / tailable cursors.** No subscription paths — only one-shot reads.

---

## Removing a connection

Click the saved connection in the listbox, then click **🗑 Remove**. The connection URL is dropped from `vault/sql_connections.json` / `vault/mongo_connections.json`. The DB itself is never touched — the read-only role you created continues to exist on the server.
