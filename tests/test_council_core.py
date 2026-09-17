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
