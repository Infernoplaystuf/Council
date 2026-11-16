"""
The experiment store: one SQLite file in the plugin's data folder.

    experiments   one row per plan: id, title, profile, spec JSON, runs
                  JSON, status (draft | approved | rejected), the hash of
                  spec + runs + profile, created time
    audit         APPEND ONLY: every save, check, approval, rejection,
                  export and Dyndrite tool request / decision / run, with
                  who, when, the hash and a detail JSON. Nothing in this
                  module updates or deletes an audit row, and a trigger in
                  the database refuses it too.
    tool_requests a Dyndrite tool call a person or the assistant asked
                  for that changes something: pending until an engineer
                  approves or rejects it; only an approved one can be run,
                  once.
    results       measured values imported per specimen (file, column,
                  value), so a plan and its results stay together.

APPROVAL IS BOUND TO WHAT WAS CHECKED
`approve` takes the hash the engineer was shown and re-checks the plan
against the profile as it is now. If the plan, its runs or the limits
changed since, or a check blocks, nothing is approved and the reason is
returned. An approved plan cannot be edited: a change is a new draft.
"""
from __future__ import annotations

import datetime as _dt
import getpass
import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

DB_FILE = "experiments.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    profile TEXT NOT NULL,
    spec TEXT NOT NULL,
    runs TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    status TEXT NOT NULL,
    hash TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    n INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    who TEXT NOT NULL,
    experiment_id TEXT,
    action TEXT NOT NULL,
    hash TEXT,
    detail TEXT
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END;
CREATE TABLE IF NOT EXISTS tool_requests (
    n INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    who TEXT NOT NULL,
    tool TEXT NOT NULL,
    arguments TEXT NOT NULL,
    reason TEXT,
    status TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    result TEXT
);
CREATE TABLE IF NOT EXISTS results (
    specimen_id TEXT NOT NULL,
    experiment_id TEXT NOT NULL,
    field TEXT NOT NULL,
    value REAL,
    source TEXT,
    imported TEXT NOT NULL
);
"""


def now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def user() -> str:
    try:
        return getpass.getuser()
    except Exception:                                        # noqa: BLE001
        return "unknown"


def plan_hash(spec: Dict[str, Any], runs: Sequence[Dict[str, Any]],
              profile: Dict[str, Any]) -> str:
    blob = json.dumps({"spec": spec, "runs": list(runs), "profile": profile},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class Store:
    def __init__(self, folder: Path):
        self.path = Path(folder) / DB_FILE
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._db() as db:
            db.executescript(_SCHEMA)

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    # -- audit -------------------------------------------------------
    def log(self, action: str, *, experiment_id: str = "", hash: str = "",
            detail: Any = None, who: str = "") -> None:
        with self._lock, self._db() as db:
            db.execute("INSERT INTO audit (at, who, experiment_id, action, "
                       "hash, detail) VALUES (?,?,?,?,?,?)",
                       (now(), who or user(), experiment_id, action, hash,
                        json.dumps(detail, default=str) if detail is not None
                        else None))

    def audit(self, experiment_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._db() as db:
            if experiment_id:
                rows = db.execute("SELECT * FROM audit WHERE experiment_id=? "
                                  "ORDER BY n", (experiment_id,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM audit ORDER BY n").fetchall()
        return [dict(r) for r in rows]

    # -- experiments -------------------------------------------------
    def next_id(self) -> str:
        stamp = _dt.date.today().strftime("%Y%m%d")
        with self._db() as db:
            n = db.execute("SELECT COUNT(*) FROM experiments WHERE id LIKE ?",
                           (f"EXP-{stamp}-%",)).fetchone()[0]
        return f"EXP-{stamp}-{n + 1:02d}"

    def save_draft(self, experiment_id: str, spec: Dict[str, Any],
                   runs: Sequence[Dict[str, Any]], profile: Dict[str, Any],
                   issues: Sequence[Dict[str, Any]] = ()) -> str:
        """Save (or replace) a draft. An approved or rejected plan is never
        replaced: ValueError."""
        h = plan_hash(spec, runs, profile)
        with self._lock, self._db() as db:
            row = db.execute("SELECT status FROM experiments WHERE id=?",
                             (experiment_id,)).fetchone()
            if row and row["status"] != "draft":
                raise ValueError(f"{experiment_id} is {row['status']}; a "
                                 f"change makes a new draft.")
            db.execute("INSERT OR REPLACE INTO experiments VALUES "
                       "(?,?,?,?,?,?,?,?,?)",
                       (experiment_id, spec.get("title", ""),
                        profile.get("name", ""), json.dumps(spec),
                        json.dumps(list(runs)), json.dumps(profile), "draft",
                        h, now()))
        self.log("saved draft", experiment_id=experiment_id, hash=h,
                 detail={"specimens": len(runs), "issues": list(issues)})
        return h

    def get(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        with self._db() as db:
            row = db.execute("SELECT * FROM experiments WHERE id=?",
                             (experiment_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        for k in ("spec", "runs", "profile_json"):
            out[k] = json.loads(out[k])
        return out

    def list(self) -> List[Dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT id, title, profile, status, hash, "
                              "created FROM experiments ORDER BY created DESC,"
                              " id DESC").fetchall()
        return [dict(r) for r in rows]

    def _decide(self, experiment_id: str, shown_hash: str, status: str,
                note: str, who: str, issues: Sequence[Dict[str, Any]]) -> str:
        """'' when done, else why not."""
        with self._lock, self._db() as db:
            row = db.execute("SELECT status, hash FROM experiments WHERE id=?",
                             (experiment_id,)).fetchone()
            if not row:
                return f"There is no experiment {experiment_id}."
            if row["status"] != "draft":
                return f"{experiment_id} is already {row['status']}."
            if row["hash"] != shown_hash:
                return ("The plan changed after it was checked; check it "
                        "again before deciding.")
            db.execute("UPDATE experiments SET status=? WHERE id=?",
                       (status, experiment_id))
        self.log(status, experiment_id=experiment_id, hash=shown_hash,
                 detail={"note": note, "issues": list(issues)}, who=who)
        return ""

    def approve(self, experiment_id: str, shown_hash: str, *, who: str,
                note: str = "", current_profile: Optional[Dict[str, Any]] = None,
                check=None) -> str:
        """Approve a draft. `check(spec, profile, runs) -> issues` is run
        again on the stored plan against `current_profile`; any BLOCK
        refuses. Returns '' when approved, else the reason."""
        if not str(who or "").strip():
            return "Type the approving engineer's name."
        exp = self.get(experiment_id)
        if not exp:
            return f"There is no experiment {experiment_id}."
        if current_profile is not None and \
                plan_hash(exp["spec"], exp["runs"], current_profile) != exp["hash"]:
            return ("The machine limits changed since this plan was "
                    "checked; check it again.")
        issues: List[Dict[str, Any]] = []
        if check is not None:
            found = check(exp["spec"], exp["profile_json"], exp["runs"])
            issues = [i.to_dict() if hasattr(i, "to_dict") else dict(i)
                      for i in found]
            if any(i["level"] == "block" for i in issues):
                self.log("approval refused", experiment_id=experiment_id,
                         hash=shown_hash, detail={"issues": issues}, who=who)
                return "A check blocks this plan; it cannot be approved."
        return self._decide(experiment_id, shown_hash, "approved", note,
                            who.strip(), issues)

    def reject(self, experiment_id: str, shown_hash: str, *, who: str,
               note: str = "") -> str:
        if not str(who or "").strip():
            return "Type the engineer's name."
        return self._decide(experiment_id, shown_hash, "rejected", note,
                            who.strip(), ())

    # -- Dyndrite tool requests --------------------------------------
    def request_tool(self, tool: str, arguments: Dict[str, Any], *,
                     reason: str = "", who: str = "") -> int:
        with self._lock, self._db() as db:
            cur = db.execute("INSERT INTO tool_requests (at, who, tool, "
                             "arguments, reason, status) VALUES "
                             "(?,?,?,?,?, 'pending')",
                             (now(), who or user(), tool,
                              json.dumps(arguments, sort_keys=True), reason))
            n = int(cur.lastrowid)
        self.log("tool requested", detail={"request": n, "tool": tool,
                                           "arguments": arguments,
                                           "reason": reason}, who=who)
        return n

    def tool_requests(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._db() as db:
            if status:
                rows = db.execute("SELECT * FROM tool_requests WHERE status=? "
                                  "ORDER BY n", (status,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM tool_requests ORDER BY n "
                                  "DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["arguments"] = json.loads(d["arguments"])
            out.append(d)
        return out

    def decide_tool(self, n: int, approve: bool, *, who: str) -> str:
        if not str(who or "").strip():
            return "Type the engineer's name."
        status = "approved" if approve else "rejected"
        with self._lock, self._db() as db:
            row = db.execute("SELECT status, tool FROM tool_requests WHERE n=?",
                             (n,)).fetchone()
            if not row:
                return f"There is no request {n}."
            if row["status"] != "pending":
                return f"Request {n} is already {row['status']}."
            db.execute("UPDATE tool_requests SET status=?, decided_by=?, "
                       "decided_at=? WHERE n=?", (status, who.strip(), now(), n))
        self.log(f"tool {status}", detail={"request": n, "tool": row["tool"]},
                 who=who.strip())
        return ""

    def claim_approved(self, n: int) -> Optional[Dict[str, Any]]:
        """Mark an approved request as running and return it — once. A
        second claim (a double click) gets None."""
        with self._lock, self._db() as db:
            row = db.execute("SELECT * FROM tool_requests WHERE n=?",
                             (n,)).fetchone()
            if not row or row["status"] != "approved":
                return None
            db.execute("UPDATE tool_requests SET status='running' WHERE n=?",
                       (n,))
        d = dict(row)
        d["arguments"] = json.loads(d["arguments"])
        return d

    def finish_tool(self, n: int, result: Dict[str, Any]) -> None:
        status = "failed" if result.get("is_error") else "done"
        with self._lock, self._db() as db:
            db.execute("UPDATE tool_requests SET status=?, result=? WHERE n=?",
                       (status, json.dumps(result, default=str)[:200000], n))
        self.log(f"tool {status}", detail={"request": n,
                                           "result": str(result.get("text", ""))[:2000]})

    # -- results ------------------------------------------------------
    def add_results(self, experiment_id: str, table, columns: Sequence[str],
                    source: str) -> int:
        import pandas as pd
        rows = []
        stamp = now()
        for _, r in table.iterrows():
            for c in columns:
                v = pd.to_numeric(pd.Series([r.get(c)]), errors="coerce").iloc[0]
                if pd.notna(v):
                    rows.append((str(r["specimen_id"]), experiment_id, str(c),
                                 float(v), source, stamp))
        with self._lock, self._db() as db:
            db.executemany("INSERT INTO results VALUES (?,?,?,?,?,?)", rows)
        self.log("imported results", experiment_id=experiment_id,
                 detail={"file": source, "values": len(rows),
                         "columns": list(columns)})
        return len(rows)
