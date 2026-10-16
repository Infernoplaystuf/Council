"""
Helper run by tests/test_analyst_floor.py IN A CHILD PROCESS: the REAL analyst
child (council_core.analyst_child), with the sandbox's Python-level containment
NEUTRALISED, so the only thing standing between model code and a file outside
the data folders is the audit-hook floor. Proves the floor holds in the real
child and that a refusal ends the run with a plain error the caller reads.

    python _floor_shim.py <repo_root> <code.py> <data_folder> <out.json>

COUNCIL_VAULT_ROOT selects the vault (so data_out / .stats_cache resolve).

FLOOR_SHIM_EXTRA (comma-separated) adds helpers to the sandbox that stand
for what a LIBRARY does on the code's behalf, which model code cannot write
itself (its imports are allow-listed):

  spill_to_temp()  writes, reads back and deletes a file in the default
                   temp folder (tempfile.TemporaryFile), as a library that
                   spills to disk does; returns (text, gettempdir(), TEMP)
  audit_db()       appends one record to the vault's db_audit.log the way
                   the SQL / Mongo bridge does after a query
                   (db_connections._audit); returns the log's line count
"""
import os
import sys


def _extra_helpers(names):
    out = {}
    if "spill_to_temp" in names:
        def spill_to_temp():
            import tempfile
            with tempfile.TemporaryFile() as fh:
                fh.write(b"spilled")
                fh.seek(0)
                text = fh.read().decode()
            return text, tempfile.gettempdir(), os.environ.get("TEMP", "")
        out["spill_to_temp"] = spill_to_temp
    if "audit_db" in names:
        def audit_db():
            import db_connections
            from council_core import paths
            vault = paths.vault_dir()
            db_connections._audit(vault, kind="sql.query", conn="plant",
                                  rows=1)
            with open(os.path.join(str(vault), "db_audit.log"),
                      encoding="utf-8") as fh:
                return len(fh.read().splitlines())
        out["audit_db"] = audit_db
    return out


def main(argv):
    repo, code_file, folder, out_json = argv[1:5]
    sys.path.insert(0, repo)
    import vault_analyst as va
    extra = _extra_helpers({n.strip() for n in os.environ.get(
        "FLOOR_SHIM_EXTRA", "").split(",") if n.strip()})
    if extra:
        import analyst_helpers
        _register = analyst_helpers.register_helpers

        def register(globals_dict):
            _register(globals_dict)
            globals_dict.update(extra)
        analyst_helpers.register_helpers = register
    # Defeat the CONTAINMENT (the inside-the-data-folders check), while still
    # RESOLVING relative paths against the data folder the way the real sandbox
    # does — so an inside relative read like read_text('runs.csv') still finds
    # its file, and an absolute outside path is passed straight through to the
    # open. The floor is then the only thing left to refuse the outside file.
    def _resolve_no_contain(value, root_strs, root_reals):
        s = va._OS_EXPANDUSER(va._sandbox_path_str(value))
        if va._OS_ISABS(s):
            return va._OS_REALPATH(s)
        for root in root_strs:
            c = va._OS_REALPATH(va._OS_JOIN(root, s))
            if va._OS_EXISTS(c):
                return c
        return (va._OS_REALPATH(va._OS_JOIN(root_strs[0], s))
                if root_strs else None)

    va.validate_generated_code = lambda code: (True, "ok")
    va._sandbox_resolve_within = _resolve_no_contain
    va._BudgetedPandas._contain_str = (
        lambda self, value: _resolve_no_contain(
            value, self._root_strs, self._root_reals)
        or va._sandbox_path_str(value))
    from council_core import analyst_child
    return analyst_child.main(["analyst_child", code_file, folder, out_json])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
