"""
Connected tasks: council_core.task_chain, its run through agent_jobs_runner,
and the Agent Jobs tab's option and live feed.

Model replies are scripted and workers are stand-ins, so every branch — a
failed hard check, a rejected step, an unreadable plan, a cancel — is driven
on purpose rather than hoped for.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from council_core import task_chain as tc


# ============================================================
# Parsing
# ============================================================

def test_a_plan_inside_prose_and_fences():
    reply = ('Sure! Here is the plan:\n```json\n{"steps": ['
             '{"task": "List the CSV files", "check": "names files"},'
             '{"task": "Total the amount column", "needs": [1]}]}\n```')
    steps = tc.parse_plan(reply)
    assert [s.task for s in steps] == ["List the CSV files",
                                       "Total the amount column"]
    assert steps[1].needs == [1] and steps[0].check == "names files"


def test_needs_may_only_point_backwards():
    steps = tc.parse_plan(json.dumps({"steps": [
        {"task": "a", "needs": [2, 1, "x"]}, {"task": "b", "needs": [1, 2]}]}))
    assert steps[0].needs == [] and steps[1].needs == [1]


def test_plans_are_capped_and_plain_strings_are_steps():
    steps = tc.parse_plan(json.dumps([f"t{i}" for i in range(9)]))
    assert len(steps) == tc.MAX_STEPS and steps[0].task == "t0"


@pytest.mark.parametrize("reply", ["no json here", '{"steps": []}',
                                   '{"steps": [{"needs": [1]}]}'])
def test_unusable_plans_say_why(reply):
    with pytest.raises(tc.PlanError):
        tc.parse_plan(reply)


@pytest.mark.parametrize("reply, verdict", [
    ('{"pass": true, "reason": "ok"}', True),
    ('Verdict: {"pass": false, "reason": "made up"}', False),
    ("PASS: fine", True), ("fail - wrong column", False)])
def test_verdicts(reply, verdict):
    assert tc.parse_verdict(reply)["pass"] is verdict


def test_an_unreadable_verdict_is_none():
    assert tc.parse_verdict("I think it is mostly right") is None


# ============================================================
# The chain
# ============================================================

class Script:
    """A scripted model: replies per role, in order, and a record of calls."""

    def __init__(self, **replies):
        self.replies = {k: list(v) for k, v in replies.items()}
        self.calls = []

    def __call__(self, role, messages, max_tokens):
        self.calls.append((role, messages))
        queue = self.replies.get(role) or ["{}"]
        return queue.pop(0) if len(queue) > 1 else queue[0]


class Workers:
    """Stand-in workers: answers per step, in order, with tool use."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, role, task, on_step):
        self.calls.append((role, task))
        on_step(None)
        ans = self.answers.pop(0) if self.answers else ("x" * 40, ["read"])
        text, tools = ans if isinstance(ans, tuple) else (ans, ["read"])
        return tc.WorkerResult(text, "done", tools)


PLAN2 = json.dumps({"steps": [
    {"task": "List the CSV files in the folder", "check": "names the files"},
    {"task": "Total the amount column of sales.csv", "needs": [1],
     "check": "gives a number"}]})
PASS = '{"pass": true, "reason": "ok"}'


def test_the_happy_path_uses_each_role_for_its_job():
    chat = Script(strategist=[PLAN2], judge=[PASS], writer=["Total is 42 [2]."])
    work = Workers(("Found sales.csv and costs.csv", ["list_files"]),
                   ("The amount column totals 42", ["run_pandas_analysis"]))
    events = []
    result = tc.TaskChain(chat, work, on_event=events.append).run("Sum sales")
    assert result.answer == "Total is 42 [2]." and result.verified_count == 2
    assert [c[0] for c in chat.calls] == ["strategist", "judge", "judge",
                                          "writer"]
    assert {role for role, _t in work.calls} == {"intern"}
    assert [e.stage for e in events][0] == "plan"
    assert events[-1].stage == "answer" and events[-1].ok


def test_a_step_sees_only_the_steps_it_needs():
    plan = json.dumps({"steps": [{"task": "list files"},
                                 {"task": "read notes"},
                                 {"task": "count rows", "needs": [1]}]})
    chat = Script(strategist=[plan], judge=[PASS], writer=["done"])
    work = Workers(("FILES: a.csv b.csv", ["list_files"]),
                   ("NOTES: meeting on monday", ["read_local_file"]),
                   ("3 rows", ["run_pandas_analysis"]))
    tc.TaskChain(chat, work).run("goal")
    third = work.calls[2][1]
    assert "RESULT OF STEP 1: FILES: a.csv b.csv" in third
    assert "meeting on monday" not in third            # step 2 not needed


def test_a_data_step_that_looked_at_nothing_is_retried_with_the_reason():
    plan = json.dumps({"steps": [{"task": "Total the amount column"}]})
    chat = Script(strategist=[plan], judge=[PASS], writer=["42"])
    work = Workers(("The total is probably about 40", []),
                   ("The amount column totals 42", ["run_pandas_analysis"]))
    result = tc.TaskChain(chat, work).run("sum")
    assert result.steps[0].attempts == 2 and result.steps[0].verified
    assert "without looking at any data" in work.calls[1][1]
    assert result.steps[0].problems == []               # clean retry is clean


def test_a_step_rejected_twice_is_kept_unverified_and_the_chain_goes_on():
    chat = Script(strategist=[PLAN2],
                  judge=['{"pass": false, "reason": "invented a file"}',
                         '{"pass": false, "reason": "still invented"}', PASS],
                  writer=["answer"])
    work = Workers()
    result = tc.TaskChain(chat, work).run("goal")
    first, second = result.steps
    assert not first.verified and first.attempts == 2
    assert first.problems == ["still invented"]
    assert second.verified
    writer_prompt = chat.calls[-1][1][1]["content"]
    assert "(UNVERIFIED: still invented)" in writer_prompt
    assert "(UNVERIFIED" in work.calls[-1][1]            # step 2 told too


def test_an_unusable_plan_is_asked_for_again_then_falls_back():
    chat = Script(strategist=["no idea", "still prose"], judge=[PASS],
                  writer=["answer"])
    result = tc.TaskChain(chat, Workers()).run("Do the thing")
    assert result.plan_source == "fallback"
    assert [s.task for s in result.plan] == ["Do the thing"]
    assert "could not be used" in chat.calls[1][1][1]["content"]


def test_a_repaired_plan_is_used():
    chat = Script(strategist=["prose", PLAN2], judge=[PASS], writer=["a"])
    result = tc.TaskChain(chat, Workers()).run("g")
    assert result.plan_source == "model" and len(result.plan) == 2


def test_an_unreadable_verdict_is_kept_but_not_called_verified():
    plan = json.dumps({"steps": [{"task": "summarise the notes"}]})
    chat = Script(strategist=[plan], judge=["looks fine to me"],
                  writer=["a"])
    result = tc.TaskChain(chat, Workers()).run("g")
    step = result.steps[0]
    assert step.attempts == 1 and not step.verified
    assert "not independently verified" in step.problems[0]


def test_cancel_stops_between_calls():
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 1                           # after the plan

    chat = Script(strategist=[PLAN2], judge=[PASS], writer=["a"])
    with pytest.raises(tc.ChainCancelled):
        tc.TaskChain(chat, Workers(), should_stop=stop).run("g")


def test_the_report_marks_each_step():
    chat = Script(strategist=[PLAN2],
                  judge=['{"pass": false, "reason": "no"}',
                         '{"pass": false, "reason": "no"}', PASS],
                  writer=["The answer."])
    md = tc.report_markdown(tc.TaskChain(chat, Workers()).run("goal"))
    assert "**Verified:** 1 of 2" in md
    assert "### ⚠ 1." in md and "### ✓ 2." in md and "The answer." in md


# ============================================================
# Through the job runner
# ============================================================

class FakeAgent:
    def __init__(self, answers):
        self.answers = answers

    def run(self, task, on_step=None):
        on_step(SimpleNamespace(step=1), None)
        return SimpleNamespace(final_answer=self.answers.pop(0),
                               stopped_reason="done",
                               tools_used=["run_pandas_analysis"])


def test_a_chain_job_runs_persists_and_reports(tmp_path, monkeypatch):
    import agent_jobs_runner as ajr
    posted = []
    chat = Script(strategist=[PLAN2], judge=[PASS], writer=["Total 42 [2]."])
    runner = ajr.JobRunner(vault_dir=tmp_path, chat=chat,
                           ui_q=SimpleNamespace(put=posted.append))
    answers = ["Found sales.csv", "The amount totals 42"]
    roles = []

    def build(job, runner=None):
        roles.append(getattr(runner, "role", None))
        return FakeAgent(answers)

    monkeypatch.setattr(runner, "_build_agent", build)
    job_id = runner.submit("Sum the sales", mode="chain")
    runner._q.get_nowait()                     # run it here, not on a worker
    runner._run_job(job_id)
    job = runner.store.get(job_id)
    assert job.mode == "chain" and job.status == "done"
    assert job.stopped_reason == "2/2 steps verified"
    assert job.result_summary == "Total 42 [2]."
    assert all(s.kind.startswith("chain_") for s in job.steps)
    assert Path(job.report_path).read_text(encoding="utf-8").startswith(
        "# Connected-tasks report")
    assert roles == ["intern", "intern"]       # workers speak for the Intern
    assert posted[-1][:3] == ("job_done", job_id, "done")


def test_a_cancelled_chain_job(tmp_path, monkeypatch):
    import agent_jobs_runner as ajr
    runner = ajr.JobRunner(vault_dir=tmp_path,
                           chat=Script(strategist=[PLAN2]))
    job_id = runner.submit("g", mode="chain")
    runner._q.get_nowait()
    runner.cancel(job_id)
    runner._run_job(job_id)
    assert runner.store.get(job_id).status == "cancelled"


def test_single_jobs_are_unchanged_and_old_records_load(tmp_path):
    import agent_jobs
    import agent_jobs_runner as ajr
    runner = ajr.JobRunner(vault_dir=tmp_path)
    job_id = runner.submit("g")
    assert runner.store.get(job_id).mode == "single"
    legacy = {"job_id": "old", "goal": "x", "status": "done"}
    assert agent_jobs.AgentJob.from_dict(legacy).mode == "single"


def test_a_workers_model_runner_speaks_for_its_role():
    import agent_jobs_runner as ajr
    runner = ajr.JobRunner(vault_dir=None)
    assert runner._runner_for("intern").role == "intern"
    custom = object()
    assert ajr.JobRunner(runner=custom)._runner_for("intern") is custom


# ============================================================
# The job list reads real AgentJobs
# ============================================================

def test_the_listing_reads_real_job_ids_and_steps(tmp_path):
    """It read `job.id` and `job.steps_done`, which AgentJob does not have:
    every id was "", so Cancel / Open report / Remove finished found nothing."""
    import agent_jobs
    from council_core import jobs as jobs_core
    store = agent_jobs.JobStore(tmp_path)
    store.upsert(agent_jobs.AgentJob(job_id="job_1", goal="a", status="done",
                                     steps=[agent_jobs.JobStep(1)],
                                     max_steps=6))
    store.upsert(agent_jobs.AgentJob(job_id="job_2", goal="b",
                                     status="running", mode="chain"))
    runner = SimpleNamespace(store=store)
    listing = jobs_core.listing(runner)
    assert listing.ids == ["job_1", "job_2"]
    assert listing.rows[0] == ("done", "a", "1/6")
    assert listing.rows[1][1] == jobs_core.CHAIN_MARK + "b"
    assert jobs_core.finished_ids(runner) == ["job_1"]


# ============================================================
# The Agent Jobs tab
# ============================================================

pytest.importorskip("PySide6", reason="the Jobs tab needs PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def pump(qapp, until, seconds=5.0):
    deadline = time.time() + seconds
    while not until() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    assert until(), "timed out"


def test_the_tab_starts_a_chain_and_shows_its_steps_live(qapp, tmp_path):
    import agent_jobs
    from council_core import jobs as jobs_core
    from council_qt.tabs.jobs import JobsActions, JobsTab

    started = []

    class Actions(JobsActions):
        def start(self, goal, steps, chain=False):
            started.append((goal, chain))
            return jobs_core.JobResult(True, "Started job_9.", job_id="job_9")

        def listing(self):
            return jobs_core.JobListResult(True, "")

    actions = Actions(vault_dir=tmp_path)
    tab = JobsTab(actions=actions)
    assert actions.feed is not None
    tab.goal.setPlainText("Sum the sales")
    tab.chain.setChecked(True)
    tab.on_start()
    pump(qapp, lambda: started)
    assert started == [("Sum the sales", True)]
    step = agent_jobs.JobStep(3, kind="chain_check",
                              label="check: step 1: passed").to_dict()
    t = threading.Thread(target=actions.feed.put,
                         args=(("job_step", "job_9", step),))
    t.start()
    t.join(3)
    pump(qapp, lambda: "check: step 1: passed" in tab.log.toPlainText())
    tab.deleteLater()
    qapp.processEvents()


def test_remove_finished_removes_them(qapp, tmp_path):
    import agent_jobs
    from council_qt.tabs.jobs import JobsActions, JobsTab
    actions = JobsActions(vault_dir=tmp_path)
    store = actions.runner().store
    store.upsert(agent_jobs.AgentJob(job_id="a", goal="x", status="done"))
    store.upsert(agent_jobs.AgentJob(job_id="b", goal="y", status="running"))
    tab = JobsTab(actions=actions)
    tab.on_remove_finished()
    pump(qapp, lambda: "Removed" in tab.status.text())
    assert [j.job_id for j in store.all()] == ["b"]
    tab.deleteLater()
    qapp.processEvents()


# ============================================================
# Found on the first real run
# ============================================================

def test_the_tool_name_in_action_shorthand_is_a_tool_call():
    """Granite 3.1 8B writes {"action": "read_local_file", "path": ...};
    it used to come back as the FINAL ANSWER, so no step read anything."""
    import safe_agent
    names = ["read_local_file", "list_files"]
    a = safe_agent.parse_action('{"action": "read_local_file", '
                                '"path": "sales.csv"}', tool_names=names)
    assert a == {"action": "tool", "tool": "read_local_file",
                 "args": {"path": "sales.csv"}}
    b = safe_agent.parse_action('{"action": "list_files", "args": {"x": 1}}',
                                tool_names=names)
    assert b["tool"] == "list_files" and b["args"] == {"x": 1}


def test_an_unknown_action_name_is_not_a_tool_call():
    import safe_agent
    a = safe_agent.parse_action('{"action": "rm_rf", "path": "/"}',
                                tool_names=["read_local_file"])
    assert a["action"] == "final"
    # and without names the old behaviour holds
    assert safe_agent.parse_action(
        '{"action": "read_local_file", "path": "x"}')["action"] == "final"


def test_the_data_overview_names_files_columns_and_rows(tmp_path):
    import agent_jobs_runner as ajr
    (tmp_path / "sales.csv").write_text(
        "date,region,amount\n2026-01-01,North,10\n2026-01-02,South,20\n")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.md").write_text("hello")
    (tmp_path / ".hidden").write_text("secret")
    text = ajr.data_overview(tmp_path)
    assert "- sales.csv" in text
    assert "columns: date,region,amount — 2 data rows" in text
    assert "notes/a.md" in text and ".hidden" not in text
    assert ajr.data_overview(tmp_path / "missing") == ""


def test_the_overview_reaches_the_planner_and_every_worker():
    chat = Script(strategist=[PLAN2], judge=[PASS], writer=["a"])
    work = Workers()
    tc.TaskChain(chat, work,
                 data_overview=lambda: "- sales.csv — columns: region,amount"
                 ).run("goal")
    planner_prompt = chat.calls[0][1][1]["content"]
    assert planner_prompt.startswith("DATA AVAILABLE:\n- sales.csv")
    assert all("FILES YOU CAN READ:\n- sales.csv" in task
               for _role, task in work.calls)


def test_a_report_too_long_for_the_path_falls_back_to_the_id(tmp_path,
                                                              monkeypatch):
    import agent_jobs_runner as ajr
    chat = Script(strategist=['{"steps": [{"task": "summarise the notes"}]}'],
                  judge=[PASS], writer=["done"])
    runner = ajr.JobRunner(vault_dir=tmp_path, chat=chat)
    monkeypatch.setattr(runner, "_build_agent",
                        lambda job, runner=None: FakeAgent(["the notes say x"]))
    real_write = Path.write_text

    def write(self, *a, **k):
        if self.name.startswith("chain__") and len(self.name) > 40:
            raise OSError("path too long")
        return real_write(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", write)
    job_id = runner.submit("a goal long enough to make a long name", mode="chain")
    runner._q.get_nowait()
    runner._run_job(job_id)
    assert Path(runner.store.get(job_id).report_path).name == \
        f"chain__{job_id}.md"


def test_the_pandas_sandbox_reads_a_bare_file_name_from_the_data_folder(
        tmp_path):
    """`pd.read_csv('sales.csv')` resolved against the app's working folder
    and failed; the worker then reported the file as missing."""
    from vault_analyst import execute_pandas_code
    data = tmp_path / "data_in"
    data.mkdir()
    (data / "sales.csv").write_text("region,amount\nNorth,10\nSouth,20\n")
    (tmp_path / "secret.csv").write_text("x\n1\n")
    df, msg = execute_pandas_code(
        "result_df = pd.read_csv('sales.csv').groupby('region').sum()",
        [data])
    assert df is not None, msg
    assert int(df["amount"].sum()) == 30
    # A relative path out of the folder is NOT rewritten into reach.
    df2, msg2 = execute_pandas_code(
        "result_df = pd.read_csv('../secret.csv')", [data])
    assert df2 is None


def test_steps_that_only_open_a_file_are_dropped_and_needs_renumbered():
    steps = tc.parse_plan(json.dumps({"steps": [
        {"task": "Read the sales.csv file"},
        {"task": "Group sales.csv by region and sum amount", "needs": [1]},
        {"task": "Load 'costs.xlsx'"},
        {"task": "Name the top region", "needs": [2, 3]}]}))
    pruned = tc.prune_trivial_steps(steps)
    assert [s.task for s in pruned] == [
        "Group sales.csv by region and sum amount", "Name the top region"]
    assert [s.number for s in pruned] == [1, 2]
    assert pruned[0].needs == [] and pruned[1].needs == [1]


def test_a_plan_that_is_only_a_read_is_kept():
    only = tc.parse_plan('{"steps": [{"task": "Read the notes.md file"}]}')
    assert tc.prune_trivial_steps(only) == only
