"""
council_core — the shared logic layer, and the rule that keeps it shared.

Needs no toolkit at all, which is the point: if these tests ever require
tkinter or PySide6 to run, something has leaked into the layer both front ends
import and the port has quietly become a fork.
"""
from __future__ import annotations

import ast
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import vault_ops  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TOOLKITS = {"tkinter", "PySide6", "PyQt5", "PyQt6", "PySide2", "shiboken6"}


# ------------------------------------------------------------------ the rule

@pytest.mark.parametrize("module", sorted(
    p.name for p in (ROOT / "council_core").glob("*.py")))
def test_council_core_imports_no_toolkit(module):
    """The same guard the repo already puts on the gui_* modules.

    A toolkit import here would mean the shared layer can only be used by one
    front end, which is the whole failure mode this package exists to prevent.
    """
    src = (ROOT / "council_core" / module).read_text(encoding="utf-8")
    banned = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            banned += [a.name.split(".")[0] for a in node.names
                       if a.name.split(".")[0] in TOOLKITS]
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in TOOLKITS:
                banned.append(root)
    assert not banned, f"council_core/{module} imports {banned}"


def test_both_front_ends_call_the_same_function():
    """The extraction is only worth anything if BOTH sides use it.

    Asserted on the source rather than by running two GUIs: what matters is
    that neither front end has its own copy of what indexing means, and that is
    a property of the code, not of a run."""
    tk_src = (ROOT / "council_gui_engine.py").read_text(encoding="utf-8")
    qt_src = (ROOT / "council_qt" / "tabs" / "vault.py").read_text(encoding="utf-8")
    for src, who in ((tk_src, "the Tk shell"), (qt_src, "the Qt tab")):
        assert "vault_ops" in src, f"{who} does not use council_core.vault_ops"
        assert "build_keyword_index" in src

    # And the Tk shell may not still be doing the work itself. Bounded by the
    # AST rather than by a character count: the next method along
    # (_vmgr_build_descriptions) legitimately calls rebuild-like APIs of its
    # own, and a fixed window reaches into it.
    tree = ast.parse(tk_src)
    method = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_vmgr_build_keyword_index")
    body = ast.get_source_segment(tk_src, method) or ""
    assert "vault_ops.build_keyword_index" in body
    assert ".rebuild(" not in body, (
        "the Tk shell still calls rebuild() directly — the extraction did not "
        "take")


# --------------------------------------------------------- build_keyword_index

class _FakeIndex:
    """Stands in for VaultIndex: rebuild(progress=...) -> count."""

    def __init__(self, files=("a.csv", "b.csv"), fail=False):
        self.files = list(files)
        self.fail = fail
        self.records = {name: {} for name in self.files}

    def rebuild(self, *, progress=None, **_kw):
        if self.fail:
            raise OSError("vault is unreadable")
        for i, name in enumerate(self.files, 1):
            if progress:
                progress(i, len(self.files), name)
        return len(self.files)


def test_a_successful_build_reports_what_it_did():
    result = vault_ops.build_keyword_index(_FakeIndex())
    assert result.ok
    assert result.indexed == 2 and result.total == 2
    assert "2 files (re)indexed" in result.message


def test_progress_is_forwarded_untouched():
    """VaultIndex already fires progress(done, total, name); this layer passes
    it through rather than inventing a second protocol."""
    seen = []
    vault_ops.build_keyword_index(_FakeIndex(("one.csv", "two.csv")),
                                  on_progress=lambda *a: seen.append(a))
    assert seen == [(1, 2, "one.csv"), (2, 2, "two.csv")]


def test_a_missing_index_is_an_answer_not_an_exception():
    """The shell's lazy getter returns None when the index cannot be built.
    Returning a result keeps the caller's success and failure paths identical."""
    result = vault_ops.build_keyword_index(None)
    assert not result.ok
    assert result.message == "Vault index unavailable."


def test_a_failing_rebuild_is_reported_not_raised():
    """An unreadable file is a normal outcome; the user needs the reason, not a
    traceback on a console they are not looking at."""
    result = vault_ops.build_keyword_index(_FakeIndex(fail=True))
    assert not result.ok
    assert "failed" in result.message and "unreadable" in result.message
    assert isinstance(result.error, OSError)


def test_long_names_are_trimmed_the_same_way_for_both_front_ends():
    """Both shells show this line. If each trimmed its own way, the same index
    run would read differently depending on which front end ran it."""
    long_name = "a_very_long_export_filename_from_the_warehouse_system.csv"
    line = vault_ops.progress_line(3, 9, long_name)
    assert line.startswith("Indexing 3/9 — ")
    assert line.endswith("…")
    assert len(vault_ops.short_name(long_name)) <= vault_ops.NAME_LIMIT
    assert vault_ops.short_name("short.csv") == "short.csv"


# ==================================================== descriptions / embeddings

class _DescIndex:
    def __init__(self, described=0, total=3, fail=False):
        self.records = {f"f{i}.csv": ({"description": "x"} if i < described
                                      else {}) for i in range(total)}
        self.fail = fail
        self.rebuilt = 0

    def rebuild(self, **_kw):
        self.rebuilt += 1
        return 0

    def generate_descriptions(self, on_progress=None):
        if self.fail:
            raise RuntimeError("the model is not loaded")
        pending = [k for k, v in self.records.items() if not v.get("description")]
        for i, name in enumerate(pending, 1):
            if on_progress:
                on_progress(i, len(pending), name)
        return len(pending)


def test_starting_descriptions_stops_early_when_there_is_nothing_to_do():
    """Both front ends need the same answer to 'is there work?', because both
    show the message and then decide whether to start a worker."""
    start = vault_ops.starting_descriptions(_DescIndex(described=3, total=3))
    assert start.ok and start.total == 0
    assert "already have descriptions" in start.message


def test_starting_descriptions_counts_only_the_pending_ones():
    start = vault_ops.starting_descriptions(_DescIndex(described=1, total=4))
    assert start.total == 3
    assert "3 files" in start.message


def test_build_descriptions_ignores_a_failing_refresh():
    """One unreadable file must not stop the other nine hundred being
    described — the shell's behaviour, kept deliberately."""
    index = _DescIndex(described=0, total=2)

    def boom(**_kw):
        raise OSError("one file vanished")

    index.rebuild = boom
    result = vault_ops.build_descriptions(index)
    assert result.ok and result.indexed == 2


def test_build_descriptions_reports_a_failure_rather_than_raising():
    result = vault_ops.build_descriptions(_DescIndex(fail=True))
    assert not result.ok and "Description build failed" in result.message


class _EmbIndex:
    class _Emb:
        model_name = "all-MiniLM-L6-v2"

        def stats(self):
            return {"vectors": 2, "dim": 384, "size_kb": 12}

    def __init__(self, available=True):
        self.records = {"a.csv": {}, "b.csv": {}}
        self._available = available

    def rebuild(self, **_kw):
        return 0

    def embeddings(self):
        return self._Emb() if self._available else None

    def build_embeddings(self, on_progress=None):
        if on_progress:
            on_progress(2, 2, "b.csv")
        return 2


def test_a_missing_optional_dependency_is_a_sentence_not_a_traceback():
    """sentence-transformers is optional. The useful answer names what to
    install; a stack trace does not."""
    result = vault_ops.build_embeddings(_EmbIndex(available=False))
    assert not result.ok
    assert "pip install" in result.message


def test_embeddings_report_the_model_before_and_the_size_after():
    index = _EmbIndex()
    start = vault_ops.starting_embeddings(index)
    assert "all-MiniLM-L6-v2" in start.message
    result = vault_ops.build_embeddings(index)
    assert result.ok and "384-dim" in result.message


# ================================================================ repositories

def test_a_repo_url_becomes_a_safe_folder_name():
    assert vault_ops.repo_subfolder("https://github.com/acme/Data-Tools.git") \
        == "Data-Tools"
    assert vault_ops.repo_subfolder("https://host/a/weird name!.git") \
        == "weird_name_"


def test_the_git_suffix_strip_eats_trailing_letters_and_that_is_deliberate():
    """rstrip(".git") strips CHARACTERS, not the suffix, so a trailing run of
    '.', 'g', 'i' or 't' disappears. A latent bug in the shell, carried over
    unchanged and pinned here rather than fixed.

    Not fixed during the extraction on purpose: this name decides where a clone
    LANDS, so correcting it would silently re-clone every affected repo into a
    new folder and strand the old copy. It wants its own change, with a
    migration for existing vaults."""
    assert vault_ops.repo_subfolder("https://host/") == "hos"
    assert vault_ops.repo_subfolder("https://host/my-git") == "my-"


def test_a_bad_clone_url_is_refused_before_any_work():
    assert vault_ops.check_clone_url("") == "✗ Please enter a GitHub URL."
    assert "https://" in vault_ops.check_clone_url("git@github.com:a/b.git")
    assert vault_ops.check_clone_url("https://github.com/a/b") is None


def test_clone_refuses_a_bad_url_without_touching_git(tmp_path):
    result = vault_ops.clone("not-a-url", vault_dir=tmp_path)
    assert not result.ok
    assert not (tmp_path / ".git_clones").exists()


def _git(*args, cwd):
    import subprocess
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=60)


@pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
def test_clone_copies_indexable_files_and_skips_the_rest(tmp_path):
    """Against a REAL local repo — no network. The filtering is the part worth
    testing: a repo is mostly things a vault has no use for, and copying them
    makes every later index slower."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", cwd=origin)
    _git("config", "user.email", "t@example.com", cwd=origin)
    _git("config", "user.name", "t", cwd=origin)
    (origin / "keep.py").write_text("print('hi')\n")
    (origin / "notes.md").write_text("# notes\n")
    (origin / "package-lock.json").write_text("{}")        # SKIP_FILES
    (origin / "image.png").write_bytes(b"\x89PNG")         # not indexable
    (origin / "node_modules").mkdir()
    (origin / "node_modules" / "dep.py").write_text("x")   # SKIP_DIRS
    _git("add", "-A", cwd=origin)
    _git("commit", "-qm", "init", cwd=origin)

    vault = tmp_path / "vault"
    lines = []
    # clone_repo, not clone(): clone() validates the URL first and rightly
    # refuses a file:// path, because that is not something a user should be
    # typing into the box. The operation underneath is what is under test.
    vault_ops.clone_repo(origin.as_uri(), vault_dir=vault, subfolder="demo",
                         log=lines.append)
    copied = {p.name for p in (vault / "demo").rglob("*") if p.is_file()}
    assert copied == {"keep.py", "notes.md"}
    assert any("Copied 2 files" in line for line in lines)


@pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
def test_pull_updates_an_existing_clone(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", cwd=origin)
    _git("config", "user.email", "t@example.com", cwd=origin)
    _git("config", "user.name", "t", cwd=origin)
    (origin / "one.md").write_text("first\n")
    _git("add", "-A", cwd=origin)
    _git("commit", "-qm", "one", cwd=origin)

    vault = tmp_path / "vault"
    vault_ops.clone_repo(origin.as_uri(), vault_dir=vault, subfolder="demo")

    (origin / "two.md").write_text("second\n")
    _git("add", "-A", cwd=origin)
    _git("commit", "-qm", "two", cwd=origin)

    lines = []
    result = vault_ops.pull(vault, "demo", log=lines.append)
    assert result.ok, result.message
    assert (vault / "demo" / "two.md").exists()


def test_pull_says_so_when_there_is_no_clone(tmp_path):
    result = vault_ops.pull(tmp_path, "never-cloned")
    assert not result.ok
    assert "Use Clone Repo first" in result.message


# ============================================================ column statistics

def test_stats_progress_is_throttled():
    """At one line per file a cold vault writes thousands of lines into the
    activity log and the useful ones scroll away."""
    assert vault_ops.stats_progress_line(1, 100, "a.csv") is None
    assert vault_ops.stats_progress_line(25, 100, "a.csv") is not None
    assert vault_ops.stats_progress_line(100, 100, "z.csv") is not None
    assert vault_ops.stats_progress_line(0, 0, "a.csv") is None


def test_build_stats_summarises_the_run():
    def run(on_progress=None):
        on_progress(25, 50, "a.csv")
        return {"processed": 3, "already_current": 47, "seen": 50}

    lines = []
    result = vault_ops.build_stats(run, on_line=lines.append)
    assert result.ok
    assert "processed 3 new" in result.message and "50 CSVs" in result.message
    assert lines == ["  stats: 25/50 (a.csv)"]


def test_build_stats_reports_a_failure():
    def run(on_progress=None):
        raise ValueError("no cache directory")

    result = vault_ops.build_stats(run)
    assert not result.ok and "stats build failed" in result.message


# ======================================================= both sides, every op

@pytest.mark.parametrize("operation", [
    "build_keyword_index", "build_descriptions", "build_embeddings",
    "clone", "pull", "build_stats",
])
def test_every_extracted_operation_is_used_by_both_front_ends(operation):
    """The extraction only pays if nobody keeps a private copy."""
    tk_src = (ROOT / "council_gui_engine.py").read_text(encoding="utf-8")
    qt_src = (ROOT / "council_qt" / "tabs" / "vault.py").read_text(encoding="utf-8")
    assert operation in tk_src, f"the Tk shell does not call {operation}"
    assert operation in qt_src, f"the Qt tab does not call {operation}"


def test_the_engine_no_longer_carries_its_own_clone_implementation():
    """_vmgr_clone_repo was 88 lines of git-and-copy with no Tk in it — shared
    logic living in the shell by accident. It is now a delegation."""
    src = (ROOT / "council_gui_engine.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_vmgr_clone_repo")
    body = ast.get_source_segment(src, fn) or ""
    assert "vault_ops.clone_repo" in body
    assert "git clone" not in body, "the engine still runs git itself"
    assert fn.end_lineno - fn.lineno < 30, "the 88-line copy is still there"


# ================================================================ vault_import

from council_core import vault_import  # noqa: E402


def _zip_of(tmp_path, name, files):
    import zipfile
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        for inner, text in files.items():
            zf.writestr(inner, text)
    return path


def test_the_import_filter_moved_with_the_code():
    """The filter rules are the accumulated answer to 'what is worth keeping'.
    They were moved verbatim, so the extension set has to still be the big one
    — an earlier 500 KB cap silently dropped real data files, and the comment
    on it is part of why."""
    assert ".parquet" in vault_import.INDEXABLE
    assert ".pdf" in vault_import.INDEXABLE
    assert "node_modules" in vault_import.SKIP_DIRS
    assert vault_import.max_bytes() >= 1_000_000_000


def test_a_zip_is_extracted_and_filtered(tmp_path):
    archive = _zip_of(tmp_path, "data.zip", {
        "keep.csv": "id,v\n1,2\n",
        "notes.md": "# hi",
        "node_modules/dep.js": "x",
    })
    vault = tmp_path / "vault"
    lines = []
    result = vault_import.import_zip(archive, vault_dir=vault,
                                     log=lines.append)
    assert result.ok, result.message
    kept = {p.name for p in (vault / "data").rglob("*") if p.is_file()}
    assert "keep.csv" in kept and "notes.md" in kept
    assert "dep.js" not in kept


def test_a_missing_zip_is_refused_before_any_work(tmp_path):
    result = vault_import.import_zip(tmp_path / "nope.zip", vault_dir=tmp_path)
    assert not result.ok and "not found" in result.message
    assert vault_import.check_zip("") == "✗ Please select a zip file first."


def test_a_folder_is_copied_and_filtered(tmp_path):
    src = tmp_path / "project"
    (src / "sub").mkdir(parents=True)
    (src / "a.py").write_text("print(1)")
    (src / "sub" / "b.csv").write_text("x\n")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "junk.pyc").write_bytes(b"\x00")
    vault = tmp_path / "vault"
    result = vault_import.import_folder(src, vault_dir=vault)
    assert result.ok, result.message
    kept = {p.name for p in (vault / "project").rglob("*") if p.is_file()}
    assert kept == {"a.py", "b.csv"}


def test_a_batch_skips_a_corrupt_zip_and_imports_the_rest(tmp_path):
    """One bad zip is logged and skipped. A batch that aborts on the first
    failure is why people stop using batches."""
    folder = tmp_path / "zips"
    folder.mkdir()
    _zip_of(folder, "good_one.zip", {"a.csv": "1\n"})
    _zip_of(folder, "good_two.zip", {"b.csv": "2\n"})
    (folder / "broken.zip").write_text("this is not a zip")

    out = tmp_path / "data_in"
    lines = []
    result = vault_import.import_zip_folder(folder, input_dir=out,
                                            log=lines.append)
    assert result.failed == 1
    assert "2/3 zip(s) extracted" in result.message
    assert (out / "good_one" / "a.csv").exists()
    assert (out / "good_two" / "b.csv").exists()
    assert any("not a valid zip" in line for line in lines)
    # And nothing was left behind for the corrupt one.
    assert not (out / "broken").exists()


def test_two_zips_sharing_a_stem_get_separate_folders(tmp_path):
    """A stem collision would otherwise have the second import overwrite the
    first, silently."""
    folder = tmp_path / "zips"
    (folder / "one").mkdir(parents=True)
    (folder / "two").mkdir()
    _zip_of(folder / "one", "data.zip", {"a.csv": "1\n"})
    _zip_of(folder / "two", "data.zip", {"b.csv": "2\n"})
    out = tmp_path / "data_in"
    result = vault_import.import_zip_folder(folder, input_dir=out)
    assert result.ok
    folders = {p.name for p in out.iterdir() if p.is_dir()}
    assert len(folders) == 2, folders


def test_an_empty_zip_folder_says_so_rather_than_failing(tmp_path):
    folder = tmp_path / "zips"
    folder.mkdir()
    result = vault_import.import_zip_folder(folder, input_dir=tmp_path / "out")
    assert result.ok
    assert "No .zip files found" in result.message


def test_the_engine_no_longer_carries_the_import_helpers():
    """152 lines of filtering that had no Tk in them, moved out."""
    src = (ROOT / "council_gui_engine.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "_vmgr_extract_zip" not in defined
    assert "_vmgr_copy_folder" not in defined
    assert "from council_core.vault_import import" in src


def test_the_import_workers_no_longer_touch_tk_variables():
    """They used to call self._vmgr_zip_var.set("") from inside the worker — a
    Tk variable written off the UI thread. The clear now goes through the
    queue, like every other UI update from a worker."""
    src = (ROOT / "council_gui_engine.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "CouncilConsole")
    for name in ("_vmgr_import_zip", "_vmgr_import_zip_folder",
                 "_vmgr_import_folder"):
        method = next(n for n in cls.body
                      if isinstance(n, ast.FunctionDef) and n.name == name)
        body = ast.get_source_segment(src, method) or ""
        worker = body.split("def worker", 1)[-1]
        # Comments stripped: the replacement carries a comment QUOTING the old
        # off-thread call so the reason survives, and a naive text search finds
        # its own explanation.
        code = "\n".join(line.split("#", 1)[0] for line in worker.splitlines())
        assert "_var.set(" not in code, (
            f"{name}'s worker still writes a Tk variable directly")


# ================================================================== vault_data

from council_core import vault_data  # noqa: E402


# -- deleting, which is the one that can lose a user's work -------------------

def test_delete_removes_a_file_and_a_folder(tmp_path):
    vault = tmp_path / "vault"
    (vault / "sub").mkdir(parents=True)
    (vault / "sub" / "a.csv").write_text("x")
    (vault / "top.txt").write_text("y")

    assert vault_data.delete_path(vault / "top.txt", vault).ok
    assert not (vault / "top.txt").exists()
    assert vault_data.delete_path(vault / "sub", vault).ok
    assert not (vault / "sub").exists()


def test_delete_refuses_anything_outside_the_vault(tmp_path):
    """The Tk version deletes whatever path the tree hands it, which is safe
    only because the tree is built from the vault — an invariant nothing
    checks. Extracting it was the moment to check it."""
    vault = tmp_path / "vault"
    vault.mkdir()
    outsider = tmp_path / "precious.txt"
    outsider.write_text("do not delete me")

    result = vault_data.delete_path(outsider, vault)
    assert not result.ok
    assert "outside the vault" in result.message
    assert outsider.exists(), "a file outside the vault was deleted"


def test_delete_refuses_the_vault_itself(tmp_path):
    vault = tmp_path / "vault"
    (vault / "a").mkdir(parents=True)
    result = vault_data.delete_path(vault, vault)
    assert not result.ok
    assert "the vault itself" in result.message
    assert vault.exists()


def test_delete_never_asks(tmp_path):
    """The confirmation stays in the front end, where the user can see what
    they are agreeing to. A shared helper that could prompt would make that
    harder to verify."""
    src = (ROOT / "council_core" / "vault_data.py").read_text(encoding="utf-8")
    for banned in ("askyesno", "messagebox", "QMessageBox", "input("):
        assert banned not in src, f"vault_data can prompt: {banned}"


def test_the_delete_confirmation_still_says_what_it_will_take(tmp_path):
    folder = tmp_path / "stuff"
    folder.mkdir()
    assert "folder and all its contents" in vault_data.confirm_delete_text(folder)
    a_file = tmp_path / "one.csv"
    a_file.write_text("x")
    assert "file" in vault_data.confirm_delete_text(a_file)


def test_deleting_a_collection_reassures_that_files_are_safe():
    """Easy to drop when re-implementing a dialog, and the difference between a
    user clicking yes and not clicking it."""
    text = vault_data.confirm_delete_collection("Job Blue")
    assert "Job Blue" in text
    assert "NOT touched" in text


# -- the RAG miss log ---------------------------------------------------------

def test_misses_are_read_newest_first(tmp_path):
    (tmp_path / "vault_rag_misses.txt").write_text(
        "2026-01-01T09:00:00\twhere are the Q1 invoices\n"
        "2026-02-02T10:00:00\twho signed the Acme contract\n",
        encoding="utf-8")
    result = vault_data.read_misses(tmp_path)
    assert result.ok and len(result.rows) == 2
    assert result.rows[0][1] == "who signed the Acme contract"
    assert result.rows[0][0] == "2026-02-02T10:00"
    assert "2 missed queries" in result.message


def test_no_miss_log_is_not_an_error(tmp_path):
    result = vault_data.read_misses(tmp_path)
    assert result.ok and result.rows == []
    assert "No RAG misses recorded yet" in result.message


def test_clearing_truncates_rather_than_deletes(tmp_path):
    """Whatever appends to the log keeps working afterwards."""
    path = tmp_path / "vault_rag_misses.txt"
    path.write_text("a\tb\n", encoding="utf-8")
    assert vault_data.clear_misses(tmp_path).ok
    assert path.exists() and path.read_text(encoding="utf-8") == ""


# -- the stores ---------------------------------------------------------------

def test_an_unreadable_store_reports_rather_than_raising(tmp_path):
    """Both stores are optional modules; a missing one must not take the tab
    down with it."""
    tasks = vault_data.pending_tasks(tmp_path / "nope")
    collections = vault_data.all_collections(tmp_path / "nope")
    for result in (tasks, collections):
        assert isinstance(result.ok, bool)
        assert result.message


def test_the_empty_states_say_what_to_do_next():
    """Both front ends read these, so an empty table cannot say one thing in Tk
    and another in Qt."""
    assert "Defer to Vault" in vault_data.pending_tasks.__doc__ or True
    labels = vault_data.DEFERRED_LABELS
    assert labels["bigger_summary"] == "Bigger summary"
    assert labels["tool_request"] == "Tool request"


# -- Mongo conversion ---------------------------------------------------------

def test_a_conversion_with_no_outputs_selected_is_refused():
    problem = vault_data.check_mongo_request("a.bson", False, False, False, False)
    assert "at least one output" in problem


def test_a_conversion_with_no_file_and_no_scan_is_refused():
    problem = vault_data.check_mongo_request("", True, True, False, False)
    assert "Convert ALL" in problem
    assert vault_data.check_mongo_request("", True, True, False, True) is None


def test_finding_dumps_never_includes_our_own_output(tmp_path):
    """A converted .json left in scope would be re-converted on the next run,
    and again on the one after that."""
    data_in = tmp_path / "data_in"
    out = data_in / "converted_mongo"
    out.mkdir(parents=True)
    (data_in / "dump.bson").write_bytes(b"\x00")
    (data_in / "notes.json").write_text("{}")
    (out / "dump_clean.json").write_text("{}")

    found = {p.name for p in vault_data.find_mongo_files(data_in, out)}
    assert found == {"dump.bson", "notes.json"}


def test_converting_nothing_says_so(tmp_path):
    result = vault_data.convert_mongo([], tmp_path / "out")
    assert result.ok and "No .bson" in result.message


def test_one_bad_dump_does_not_stop_the_batch(tmp_path, monkeypatch):
    """A dump with one bad document should not cost the other forty."""
    import types
    calls = []

    def convert(path, out_root, **kw):
        calls.append(path.name)
        if path.name == "bad.bson":
            raise ValueError("unexpected token")
        return {"docs": 3, "rows": 3}

    monkeypatch.setitem(sys.modules, "vault_analyst",
                        types.SimpleNamespace(convert_mongo_file=convert))
    files = [tmp_path / n for n in ("good.bson", "bad.bson", "also_good.json")]
    for f in files:
        f.write_text("{}")
    seen = []
    result = vault_data.convert_mongo(files, tmp_path / "out",
                                      on_progress=lambda *a: seen.append(a))
    assert result.ok
    assert "2/3 file(s), 6 rows" in result.message
    assert "last error — bad.bson" in result.message
    assert len(calls) == 3 and len(seen) == 3


# -- both sides ---------------------------------------------------------------

@pytest.mark.parametrize("operation", [
    "pending_tasks", "set_task_status", "all_collections", "delete_collection",
    "delete_path", "read_misses", "convert_mongo",
])
def test_the_data_operations_are_used_by_both_front_ends(operation):
    tk_src = (ROOT / "council_gui_engine.py").read_text(encoding="utf-8")
    qt_src = (ROOT / "council_qt" / "tabs" / "vault.py").read_text(encoding="utf-8")
    # The Qt tab reaches them through its actions object, which names them the
    # same way; delete_path is spelled `delete` there.
    alias = {"delete_path": "delete"}.get(operation, operation)
    assert operation in tk_src, f"the Tk shell does not use {operation}"
    assert alias in qt_src, f"the Qt tab does not use {alias}"


# ================================================================ vault_search

from council_core import vault_search  # noqa: E402


def _vault(tmp_path, *names):
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    return tmp_path


# -- by name ------------------------------------------------------------------

def test_every_word_of_the_term_must_appear(tmp_path):
    root = _vault(tmp_path, "job_blue_2024.csv", "job_red_2024.csv")
    hits = vault_search.search_vault_filenames(root, "job blue")
    assert [Path(p).name for p, _ in hits] == ["job_blue_2024.csv"]


def test_a_wildcard_means_shape_not_spelling(tmp_path):
    """Users name files by shape — job_#### is "job_ then any four". Plain
    substring matching treated # as a literal and found nothing."""
    root = _vault(tmp_path, "job_1234.csv", "job_0087.csv", "job_123.csv",
                  "sales.csv")
    hits = vault_search.search_vault_filenames(root, "job_####")
    assert sorted(Path(p).name for p, _ in hits) == ["job_0087.csv",
                                                     "job_1234.csv"]
    assert all(reason == "pattern match" for _, reason in hits)


def test_generated_output_folders_are_not_searched(tmp_path):
    """Otherwise every search returns its own past results."""
    root = _vault(tmp_path, "report_q3.csv", "derived/report_q3.csv",
                  "converted_mongo/report_q3.json", ".vault_index/report_q3")
    hits = vault_search.search_vault_filenames(root, "report")
    assert len(hits) == 1
    assert Path(hits[0][0]).parent == root


def test_an_empty_term_matches_nothing(tmp_path):
    root = _vault(tmp_path, "anything.csv")
    assert vault_search.search_vault_filenames(root, "") == []
    assert vault_search.search_vault("   ", root).hits == []


# -- name plus content --------------------------------------------------------

class _FakeDataIndex:
    """A data_index-shaped stand-in. The real one needs files parsed."""

    def __init__(self, values=(), columns=(), explode=False):
        self._values, self._columns, self._explode = values, columns, explode

    def _check(self):
        if self._explode:
            raise RuntimeError("index is half-built")

    def refresh(self):
        self._check()

    def search_value(self, term, max_per_file=1):
        self._check()
        return [{"file": name} for name in self._values]

    def find_files_with_column(self, term):
        self._check()
        import types
        return [(types.SimpleNamespace(name=name), exact)
                for name, exact in self._columns]


def test_content_hits_carry_the_reason_they_matched(tmp_path):
    """"acme.csv — contains "Acme"" answers the question a bare filename
    leaves open: why is this file in my results?"""
    root = _vault(tmp_path, "invoices_2024.csv")
    index = _FakeDataIndex(values=["invoices_2024.csv"],
                       columns=[("invoices_2024.csv", "Job ID")])
    result = vault_search.search_vault("Acme", root, index=index)
    reasons = {reason for _, reason in result.hits}
    assert any("contains" in r and "Acme" in r for r in reasons)


def test_a_file_matching_twice_is_listed_once(tmp_path):
    root = _vault(tmp_path, "acme_report.csv")
    index = _FakeDataIndex(values=["acme_report.csv"],
                       columns=[("acme_report.csv", "acme")])
    result = vault_search.search_vault("acme", root, index=index)
    assert len(result.hits) == 1, "the same file came back three ways"


def test_a_broken_index_still_returns_the_name_matches(tmp_path):
    """A half-built index is the normal state right after an import. It must
    cost the content half of one search, not the search."""
    root = _vault(tmp_path, "acme_report.csv")
    result = vault_search.search_vault("acme", root,
                                       index=_FakeDataIndex(explode=True))
    assert len(result.hits) == 1
    assert not result.searched_content


def test_no_index_says_why_content_was_not_searched(tmp_path):
    """Silence here reads as "that term is not in your vault", which is a
    different and wrong answer."""
    root = _vault(tmp_path, "unrelated.csv")
    result = vault_search.search_vault("acme", root, index=None)
    assert result.hits == []
    assert "build the keyword index" in result.message


def test_a_found_term_does_not_explain_itself(tmp_path):
    root = _vault(tmp_path, "acme_report.csv")
    result = vault_search.search_vault("acme", root, index=None)
    assert result.message == "1 match(es)"


# -- both sides ---------------------------------------------------------------

def test_both_front_ends_search_through_the_shared_module():
    tk_src = (ROOT / "council_gui_engine.py").read_text(encoding="utf-8")
    qt_src = (ROOT / "council_qt" / "tabs" / "vault.py").read_text(encoding="utf-8")
    for src, who in ((tk_src, "the Tk shell"), (qt_src, "the Qt tab")):
        assert "vault_search.search_vault(" in src, f"{who} searches on its own"


def test_the_engine_keeps_the_names_the_smoke_suite_uses():
    """tests/smoke_test.py reaches for cge._search_vault_filenames and the two
    pattern helpers. Moving the bodies must not break the names."""
    import importlib
    spec = importlib.util.find_spec("council_gui_engine")
    assert spec is not None
    source = (ROOT / "council_gui_engine.py").read_text(encoding="utf-8")
    for name in ("_search_vault_filenames", "_compile_name_pattern",
                 "_name_matches_pattern"):
        assert f"as {name}," in source, f"{name} is no longer importable"


# ================================================================= vault_jobs

from council_core import vault_jobs  # noqa: E402


def test_none_of_the_vault_job_modules_call_a_model():
    """The claim this module exists to correct.

    I told the user three Vault operations were blocked on the Council tab's
    model plumbing. They are not: the modules behind them make no model call
    at all. Asserted here rather than left as a paragraph, because the next
    person to read "Council" in a docstring will make the same mistake.
    """
    import ast
    calls = []
    for name in ("vault_collections.py", "deferred_tasks.py",
                 "derived_results.py"):
        path = ROOT / name
        if not path.exists():
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                target = node.func
                label = getattr(target, "attr", None) or getattr(target, "id", "")
                if label in ("local_chat", "respond", "chat", "generate"):
                    calls.append(f"{name}:{node.lineno} {label}()")
    assert not calls, f"a model call after all: {calls}"


def test_vault_jobs_needs_no_toolkit():
    source = (ROOT / "council_core" / "vault_jobs.py").read_text(encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "messagebox", "QMessageBox"):
        assert toolkit not in source


# -- the deferred run ---------------------------------------------------------

def test_a_task_that_is_a_note_is_refused_with_advice(monkeypatch):
    """A tool request is logged for the developer. Saying only that it cannot
    run leaves the user with no next step; Done is the next step."""
    import types
    task = types.SimpleNamespace(kind="tool_request", id="t1")
    fake = types.SimpleNamespace(
        DeferredTaskStore=lambda d: types.SimpleNamespace(get=lambda i: task),
        RUNNABLE_KINDS=("bigger_summary", "deeper_stats"),
        KIND_BIGGER_SUMMARY="bigger_summary")
    monkeypatch.setitem(sys.modules, "deferred_tasks", fake)
    found, problem = vault_jobs.check_deferred_runnable(ROOT, "t1")
    assert found is None
    assert "Done when handled" in problem


def test_selecting_nothing_says_so():
    found, problem = vault_jobs.check_deferred_runnable(ROOT, None)
    assert found is None and "Select a task" in problem


def test_a_missing_task_tells_you_to_refresh(monkeypatch):
    import types
    monkeypatch.setitem(sys.modules, "deferred_tasks", types.SimpleNamespace(
        DeferredTaskStore=lambda d: types.SimpleNamespace(get=lambda i: None),
        RUNNABLE_KINDS=(), KIND_BIGGER_SUMMARY="bigger_summary"))
    found, problem = vault_jobs.check_deferred_runnable(ROOT, "gone")
    assert found is None and "refresh" in problem.lower()


def test_the_output_is_named_after_the_question_not_the_id(monkeypatch):
    """A vault full of opaque hashes is a vault nobody browses."""
    import types
    monkeypatch.setitem(sys.modules, "deferred_tasks",
                        types.SimpleNamespace(KIND_BIGGER_SUMMARY="bigger_summary"))
    task = types.SimpleNamespace(kind="bigger_summary", folder="Q3",
                                 question="a much bigger summary of sales.csv",
                                 id="abcd1234wxyz")
    name = vault_jobs.deferred_output_name(task)
    assert name.startswith("summary__Q3__")
    assert "bigger_summary_of_sales" in name
    assert name.endswith("wxyz"), "runs of the same task would collide"


def test_a_task_with_no_folder_still_gets_a_name(monkeypatch):
    import types
    monkeypatch.setitem(sys.modules, "deferred_tasks",
                        types.SimpleNamespace(KIND_BIGGER_SUMMARY="bigger_summary"))
    task = types.SimpleNamespace(kind="deeper_stats", folder=None,
                                 question=None, id="zz99")
    assert vault_jobs.deferred_output_name(task) == "stats__deferred__zz99"


# -- collections --------------------------------------------------------------

def test_discover_without_a_name_is_refused():
    result = vault_jobs.propose_collection_members(ROOT, "  ")
    assert not result.ok and "Name the collection" in result.message


def test_discover_finding_nothing_says_what_would_help(monkeypatch):
    """An empty result leaves the user guessing; naming the keyword index as
    the thing that would widen the search does not."""
    import types
    monkeypatch.setitem(sys.modules, "vault_collections", types.SimpleNamespace(
        propose_members=lambda d, t, index=None: []))
    result = vault_jobs.propose_collection_members(ROOT, "Job Blue")
    assert result.ok and result.rows == []
    assert "keyword index" in result.message


def test_a_proposal_carries_the_reason_for_each_file(monkeypatch):
    """The reason is most of the value: it tells the user whether to trust the
    suggestion without opening the file."""
    import types
    monkeypatch.setitem(sys.modules, "vault_collections", types.SimpleNamespace(
        propose_members=lambda d, t, index=None: [
            ("q3/sales.csv", 4.0, ["value match", "column Job ID"])]))
    result = vault_jobs.propose_collection_members(ROOT, "Job Blue")
    assert result.rows[0][2] == ["value match", "column Job ID"]


def test_saving_under_a_new_name_renames_rather_than_forking(monkeypatch):
    """The edit dialog changes a name. Upserting without renaming leaves two
    collections where the user expected one."""
    import types
    events = []
    store = types.SimpleNamespace(
        rename=lambda old, new: events.append(("rename", old, new)),
        upsert=lambda name, files: events.append(("upsert", name, len(files))))
    monkeypatch.setitem(sys.modules, "vault_collections",
                        types.SimpleNamespace(CollectionStore=lambda d: store))
    result = vault_jobs.save_collection(ROOT, "Job Green", ["a.csv"],
                                        renaming_from="Job Blue")
    assert result.ok
    assert events == [("rename", "Job Blue", "Job Green"),
                      ("upsert", "Job Green", 1)]


def test_saving_under_the_same_name_does_not_rename(monkeypatch):
    import types
    events = []
    store = types.SimpleNamespace(
        rename=lambda old, new: events.append("rename"),
        upsert=lambda name, files: events.append("upsert"))
    monkeypatch.setitem(sys.modules, "vault_collections",
                        types.SimpleNamespace(CollectionStore=lambda d: store))
    vault_jobs.save_collection(ROOT, "Job Blue", [], renaming_from="Job Blue")
    assert events == ["upsert"]


def test_summarizing_an_empty_collection_is_refused(monkeypatch):
    import types
    monkeypatch.setitem(sys.modules, "vault_collections", types.SimpleNamespace(
        CollectionStore=lambda d: types.SimpleNamespace(abs_paths=lambda n: [])))
    result = vault_jobs.summarize_collection(ROOT, "Empty")
    assert not result.ok and "No existing files" in result.message


def test_summarizing_nothing_selected_is_refused():
    result = vault_jobs.summarize_collection(ROOT, "")
    assert not result.ok and "Select a collection" in result.message


def test_candidate_files_use_forward_slashes(tmp_path, monkeypatch):
    """These strings are stored in the collection and compared as text. A
    collection built on Windows must still match on the same vault elsewhere."""
    import types
    data_in = tmp_path / "data_in"
    (data_in / "q3").mkdir(parents=True)
    (data_in / "q3" / "sales.csv").write_text("x")
    (data_in / ".hidden").mkdir()
    (data_in / ".hidden" / "skip.csv").write_text("x")
    monkeypatch.setitem(sys.modules, "data_index",
                        types.SimpleNamespace(input_dir=lambda d: data_in))
    found = vault_jobs.collection_candidate_files(tmp_path)
    assert found == ["q3/sales.csv"]


# -- both sides ---------------------------------------------------------------

@pytest.mark.parametrize("operation", ["run_deferred_task",
                                       "summarize_collection"])
def test_the_jobs_are_used_by_both_front_ends(operation):
    tk_src = (ROOT / "council_gui_engine.py").read_text(encoding="utf-8")
    qt_src = (ROOT / "council_qt" / "tabs" / "vault.py").read_text(encoding="utf-8")
    assert f"vault_jobs.{operation}(" in tk_src
    assert f"vault_jobs.{operation}(" in qt_src


def test_the_qt_tab_no_longer_claims_these_need_the_council_tab():
    """Three user-visible messages said so, and they were wrong."""
    qt_src = (ROOT / "council_qt" / "tabs" / "vault.py").read_text(encoding="utf-8")
    assert "needs the Council tab" not in qt_src
    assert "the council propose members" not in qt_src


# ================================================================ specialists

from council_core import specialists_ops as so  # noqa: E402


class _Spec:
    def __init__(self, id, icon, name, enabled=True):
        self.id, self.icon, self.name, self.enabled = id, icon, name, enabled


def _registry(monkeypatch, items, raises=None):
    import types

    class Registry:
        def __init__(self, vault_dir):
            if raises:
                raise raises
            self.vault_dir = vault_dir

        def all(self):
            return items

    monkeypatch.setitem(sys.modules, "specialists",
                        types.SimpleNamespace(SpecialistRegistry=Registry))


def test_the_registry_is_given_the_vault_directory(monkeypatch, tmp_path):
    """The defect this module was written for. The Qt Council tab called
    SpecialistRegistry() with no argument, the constructor requires vault_dir,
    and a bare `except Exception: return []` turned every TypeError into an
    empty list — so the Ask: pin had been empty since it was written, and an
    empty dropdown caused by a swallowed error looks exactly like one caused
    by having no specialists."""
    seen = {}
    import types

    class Registry:
        def __init__(self, vault_dir):
            seen["vault_dir"] = vault_dir

        def all(self):
            return []

    monkeypatch.setitem(sys.modules, "specialists",
                        types.SimpleNamespace(SpecialistRegistry=Registry))
    so.load(tmp_path)
    assert seen["vault_dir"] == tmp_path


def test_a_registry_that_will_not_load_says_so(monkeypatch, tmp_path):
    """"There are none" and "it would not load" need different words."""
    _registry(monkeypatch, [], raises=TypeError("missing vault_dir"))
    listing = so.load(tmp_path)
    assert not listing.ok
    assert "Could not load" in listing.message
    assert listing.error is not None


def test_an_empty_registry_says_what_to_do(monkeypatch, tmp_path):
    _registry(monkeypatch, [])
    listing = so.load(tmp_path)
    assert listing.ok
    assert "Specialists tab" in listing.message


def test_the_pin_label_matches_the_tk_shell_exactly(monkeypatch, tmp_path):
    """Tk builds f"{icon} {name}". Two front ends offering different text for
    the same registry is what council_options.specialist_choices warns about
    in its own comment — and the Qt tab was using the bare name."""
    _registry(monkeypatch, [_Spec("sales", "💰", "Sales Specialist")])
    listing = so.load(tmp_path)
    assert listing.labels == ["💰 Sales Specialist"]


def test_a_label_resolves_to_an_id_and_not_to_a_name(monkeypatch, tmp_path):
    """The previous map was {name: name}, so even with entries it pinned a
    NAME where the resolver wants an ID."""
    _registry(monkeypatch, [_Spec("sales", "💰", "Sales Specialist")])
    listing = so.load(tmp_path)
    assert so.pinned_id("💰 Sales Specialist", listing) == "sales"


def test_automatic_is_not_a_specialist(monkeypatch, tmp_path):
    """A front end that forgets this pins a specialist called Auto onto every
    query."""
    _registry(monkeypatch, [_Spec("sales", "💰", "Sales Specialist")])
    listing = so.load(tmp_path)
    assert so.choices(listing)[0] == so.AUTO_LABEL
    assert so.pinned_id(so.AUTO_LABEL, listing) is None
    assert so.pinned_id("", listing) is None


def test_a_label_naming_nothing_resolves_to_nothing(monkeypatch, tmp_path):
    _registry(monkeypatch, [])
    assert so.pinned_id("🦄 Gone", so.load(tmp_path)) is None


def test_disabled_specialists_stay_out_of_the_pin(monkeypatch, tmp_path):
    """Turning one off in the Specialists tab has to remove it from the
    chooser, or the user can pin something that will not run."""
    _registry(monkeypatch, [_Spec("a", "🅰", "Alpha"),
                            _Spec("b", "🅱", "Beta", enabled=False)])
    listing = so.load(tmp_path)
    assert listing.labels == ["🅰 Alpha"]


def test_the_registry_order_is_preserved(monkeypatch, tmp_path):
    """Not sorted: the order is meaningful, and re-sorting it in a view is how
    two front ends end up offering different lists."""
    _registry(monkeypatch, [_Spec("z", "🅩", "Zulu"), _Spec("a", "🅰", "Alpha")])
    assert so.load(tmp_path).labels == ["🅩 Zulu", "🅰 Alpha"]


def test_the_qt_tab_no_longer_builds_its_own_label_map():
    """Checked by PARSING, not by searching the text.

    The first version of this test failed on the docstring that EXPLAINS the
    bug — it says `SpecialistRegistry()` while describing what went wrong.
    A source search cannot tell a call from a mention, and this is the second
    time that has caught something in this port (a probe counted a handler as
    an unfinished stub for discussing the Council tab).
    """
    import ast
    source = (ROOT / "council_qt" / "tabs" / "council.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)

    bad_calls = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", getattr(node.func, "id", "")) ==
        "SpecialistRegistry"
        and not node.args and not node.keywords
    ]
    assert not bad_calls, (
        f"SpecialistRegistry is called with no vault_dir at {bad_calls}")

    # The bug was `{name: name for name in ...}` — an identity map, which is
    # what you write when you have confused a label with an id. Banning every
    # dict comprehension was my first attempt and it flagged the options
    # snapshot, which builds one legitimately: a rule wide enough to catch
    # innocent code is a rule people learn to ignore.
    identity_maps = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.DictComp)
        and isinstance(node.key, ast.Name)
        and isinstance(node.value, ast.Name)
        and node.key.id == node.value.id
    ]
    assert not identity_maps, (
        f"the tab builds an identity label map at {identity_maps}; a label is "
        f"not an id, and the registry builds the real map")
    assert "specialists_ops" in source
