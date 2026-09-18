"""
Agent Jobs and Changelog — two stores, four defects.

Both tabs are small. Between them they carry a goal box that becomes
permanently read-only, a tab that cannot work at all in any installed build,
and a filter that spawns a subprocess per keystroke.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

from council_core import changelog as changelog_core  # noqa: E402
from council_core import jobs as jobs_core  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("module", ["jobs.py", "changelog.py"])
def test_neither_module_imports_a_toolkit(module):
    source = (ROOT / "council_core" / module).read_text(encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "PyQt5", "messagebox"):
        assert toolkit not in source


# ============================================================
# Agent Jobs
# ============================================================

class FakeJob:
    def __init__(self, id, status="running", goal="do a thing",
                 steps_done=1, max_steps=6, report_path=None):
        self.id, self.status, self.goal = id, status, goal
        self.steps_done, self.max_steps = steps_done, max_steps
        self.report_path = report_path


class FakeRunner:
    def __init__(self, jobs=(), fail=None):
        self._jobs = list(jobs)
        self.submitted = []
        self.cancelled = []
        self._fail = fail

        runner = self

        class Store:
            def all(self):
                return runner._jobs

            def get(self, job_id):
                return next((j for j in runner._jobs if j.id == job_id), None)

        self.store = Store()

    def submit(self, goal, max_steps=6):
        if self._fail:
            raise self._fail
        self.submitted.append((goal, max_steps))
        return f"job_{len(self.submitted)}"

    def cancel(self, job_id):
        self.cancelled.append(job_id)


def test_the_safety_promise_is_written_once():
    """It is the reason a user hands the agent a goal and walks away, so it
    must not be retyped per front end and drift."""
    assert "cannot write, delete, install, or reach the network" in \
        jobs_core.SAFETY
    view = (ROOT / "council_qt" / "tabs" / "jobs.py").read_text(
        encoding="utf-8")
    assert "jobs_core.SAFETY" in view
    assert "Safe by design" not in view, (
        "the promise is written in the view as well as in the module")


def test_an_empty_goal_is_refused():
    assert jobs_core.check_goal("   ") == "Enter a goal first."
    assert jobs_core.check_goal("do a thing") is None


@pytest.mark.parametrize("given,expected", [
    ("6", 6), ("abc", 6), ("", 6), (None, 6), ("99", 20), ("0", 2), (12, 12),
])
def test_a_step_budget_is_clamped(given, expected):
    assert jobs_core.clamp_steps(given) == expected


def test_starting_a_job_reports_its_id():
    runner = FakeRunner()
    result = jobs_core.start(runner, "summarise the vault", 8)
    assert result.ok
    assert result.job_id == "job_1"
    assert runner.submitted == [("summarise the vault", 8)]
    assert "job_1" in result.message


def test_a_runner_that_refuses_is_reported_not_raised():
    result = jobs_core.start(FakeRunner(fail=RuntimeError("queue is full")),
                             "goal", 6)
    assert not result.ok
    assert "queue is full" in result.message
    assert result.error is not None


def test_cancelling_nothing_says_so():
    assert "Select a job" in jobs_core.cancel(FakeRunner(), None).message


def test_cancelling_names_what_will_happen():
    """The job stops after its current step, not instantly — saying so stops
    the user pressing it again."""
    result = jobs_core.cancel(FakeRunner(), "job_1")
    assert result.ok
    assert "after the current step" in result.message


def test_job_ids_travel_beside_the_rows_not_inside_them():
    """Three separate places in this app recover an identifier by splitting a
    display label — the chart overlay, the specialist pin and the session
    list — and all three are defects. A row is for reading."""
    runner = FakeRunner([FakeJob("job_a", goal="first"),
                         FakeJob("job_b", goal="second")])
    result = jobs_core.listing(runner)
    assert result.ids == ["job_a", "job_b"]
    assert all("job_a" not in cell for cell in result.rows[0])


def test_a_job_row_shows_progress_against_its_budget():
    runner = FakeRunner([FakeJob("j", steps_done=3, max_steps=6)])
    assert jobs_core.listing(runner).rows[0][2] == "3/6"


def test_an_unreadable_queue_does_not_look_like_an_empty_one():
    class Broken:
        class store:
            @staticmethod
            def all():
                raise OSError("job file is corrupt")

    result = jobs_core.listing(Broken())
    assert not result.ok
    assert "corrupt" in result.message


def test_an_empty_queue_says_what_to_do():
    assert "Give the agent a goal" in jobs_core.listing(FakeRunner()).message


def test_finished_jobs_are_the_ones_that_will_not_change():
    runner = FakeRunner([FakeJob("a", status="done"),
                         FakeJob("b", status="running"),
                         FakeJob("c", status="cancelled")])
    assert sorted(jobs_core.finished_ids(runner)) == ["a", "c"]


def test_clearing_the_goal_box_cannot_disable_it():
    """THE DEFECT. _aj_start clears the box with _set_text, and _set_text
    re-disables any widget not in a hardcoded tuple of three names — _aj_goal
    is not one of them, so the box is permanently read-only after the first
    job and the only way back is restarting the app."""
    import ast
    source = (ROOT / "council_qt" / "tabs" / "jobs.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    handler = next(node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and node.name == "on_start")
    body = ast.get_source_segment(source, handler) or ""
    assert "self.goal.clear()" in body
    assert "setReadOnly" not in body and "setEnabled(False)" not in body


# ============================================================
# Changelog
# ============================================================

def test_the_real_repository_reads_back():
    """This repo has a .git, so the happy path is exercisable for real."""
    result = changelog_core.history(ROOT, limit=5)
    assert result.ok, result.message
    assert len(result.commits) == 5
    assert all(c.sha and c.label for c in result.commits)


def test_a_build_with_no_repository_says_so_plainly(tmp_path):
    """.git is not bundled by council.spec, so this tab cannot work in ANY
    installed build. The Tk version shows whatever git prints to stderr, which
    for a missing repo is a sentence about ownership and safe directories that
    means nothing to the person reading it."""
    result = changelog_core.history(tmp_path)
    assert not result.ok
    assert "no git repository" in result.message
    assert "expected in an installed copy" in result.message


def test_a_commit_carries_its_sha_beside_its_label():
    result = changelog_core.history(ROOT, limit=3)
    first = result.commits[0]
    assert first.sha in first.label      # shown, but not recovered FROM it
    assert len(first.sha) >= 7


def test_filtering_touches_no_subprocess_and_no_disk():
    """THE DEFECT. Filtering in the Tk tab re-selects row 0 and immediately
    runs `git show --stat --patch` for it, synchronously on the GUI thread —
    so every keystroke spawns a subprocess."""
    import ast
    import inspect
    source = inspect.getsource(changelog_core.filter_commits)
    tree = ast.parse(source.strip())
    calls = [n.func for n in ast.walk(tree) if isinstance(n, ast.Call)]
    names = {getattr(c, "attr", getattr(c, "id", "")) for c in calls}
    assert not (names & {"run", "Popen", "check_output", "_git"}), (
        f"filtering reaches for a subprocess: {names}")


def test_filtering_matches_subject_date_and_sha():
    commits = [changelog_core.Commit("abc1234", "2026-01-01  abc1234  fix a thing"),
               changelog_core.Commit("def5678", "2026-02-02  def5678  add a feature")]
    assert len(changelog_core.filter_commits(commits, "fix")) == 1
    assert len(changelog_core.filter_commits(commits, "def5678")) == 1
    assert len(changelog_core.filter_commits(commits, "2026")) == 2
    assert len(changelog_core.filter_commits(commits, "")) == 2


def test_filtering_is_case_insensitive():
    commits = [changelog_core.Commit("a", "2026-01-01  a  Fix The Thing")]
    assert changelog_core.filter_commits(commits, "fix the") == commits


def test_asking_for_detail_of_nothing_is_harmless():
    assert changelog_core.detail(ROOT, "") == ""


def test_detail_in_a_build_with_no_repo_says_the_same_thing(tmp_path):
    assert "no git repository" in changelog_core.detail(tmp_path, "abc1234")


def test_the_qt_tab_fetches_detail_on_a_worker():
    """The Tk version runs `git show --stat --patch` on the GUI thread on
    every selection change."""
    import ast
    source = (ROOT / "council_qt" / "tabs" / "changelog.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    handler = next(node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef)
                   and node.name == "on_selected")
    body = ast.get_source_segment(source, handler) or ""
    assert "threading.Thread" in body
    assert "_to_ui" in body
