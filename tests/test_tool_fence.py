"""
Every other place the app runs model-written code is behind the analyst's
fence now — the one run_python got on 2026-10-15 (council_core.analyst_step:
vault_analyst's sandbox in a child process under child_proc, at most
TIME_LIMIT_S, a MEMORY_MB commit cap, the whole tree killed at the limit,
retries=0):

  * a Council member's APPROVED Forge tool (tool_review.approved_tools ->
    app_built_tools.run_tool), whose ToolSet call has no timeout of its own;
  * the FORGE tab: the test run after generation (tool_forge.generate_tool),
    Run Selected (forge_jobs.run_tool) and a tool's tests
    (tool_review.run_tests);
  * AGENT CREATOR agents (agent_profiles.run_profile): run_pandas_analysis
    and the tools attached to the agent;
  * JOBS-tab background jobs (agent_jobs_runner.JobRunner):
    run_pandas_analysis and run_app_tool.

Before, each ran the code IN THE APP'S PROCESS (vault_analyst.
execute_pandas_code called directly): a `while True:` never returned (the
agents' 60 s tool timeout only stopped the waiting — a thread cannot be
killed), a 3 GB allocation took the app's own memory, and a tool could
rebind the app's json.dumps or Path.home for the rest of the session
(measured 2026-10-15). A tool's arguments now reach it as JSON beside the
code (no pickle, nothing pasted into its source) and its return value comes
back exactly — text stays text.

Every call is made the way the app makes it, on a worker thread while this
thread — the "app" — keeps running. Every target is in pytest's temp
folder; nothing reaches a network.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

pytest.importorskip("PySide6", reason="the Council tab's actions need PySide6")

import app_built_tools as abt  # noqa: E402
import safe_agent  # noqa: E402
import tool_forge  # noqa: E402
import vault_analyst as va  # noqa: E402
from council_core import agent_profiles as ap  # noqa: E402
from council_core import analyst_step  # noqa: E402
from council_core import forge_jobs  # noqa: E402
from council_core import frame_codec  # noqa: E402
from council_core import tool_review as tr  # noqa: E402
from council_qt.tabs.council import CouncilActions  # noqa: E402

# The escapes the analyst fence refuses, and how run_python is checked
# against them: the same list, not a copy.
from tests.test_run_python_fence import (ENV_NAME, ENV_SECRET, ESCAPES,  # noqa: E402
                                         ROOT_SECRET, SECRET, fill, leaked)

#: The code budget in these tests (analyst_step.TIME_LIMIT_S, shortened)
#: and the sandbox's start-up grace beside it.
LIMIT_S, GRACE_S = 2, 3
#: How long a call may take before the test calls it hung. Far above the
#: fence's stop (LIMIT_S + GRACE_S); in-process, a hang never ended.
DEADLINE_S = 45.0

SPIN = ("def spin(n=1):\n"
        "    while n > 0:\n"
        "        pass\n"
        "    return n\n")
#: 3 GB — over the 2048 MB cap. Run in the app's process it succeeded.
GRAB = ("def grab(mb=3072):\n"
        "    return len(bytearray(mb * 1024 * 1024))\n")
#: Rebinds module attributes: the app's own json.dumps and Path.home when it
#: ran in the app's process.
TAMPER = ("def tamper(go=True):\n"
          "    import json\n"
          "    if go:\n"
          "        json.dumps = lambda *a, **k: 'HIJACKED'\n"
          "        Path.home = lambda: Path('HIJACKED')\n"
          "    return 'tampered'\n")
SPIN_SNIPPET = "while True:\n    pass\n"
GRAB_SNIPPET = "b = bytearray(3072 * 1024 * 1024)\nprint(len(b))\n"
TAMPER_SNIPPET = ("import json\n"
                  "json.dumps = lambda *a, **k: 'HIJACKED'\n"
                  "Path.home = lambda: Path('HIJACKED')\n"
                  "print('tampered')\n")


@pytest.fixture
def box(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    data = vault / "data_in"
    data.mkdir(parents=True)
    (data / "runs.csv").write_text(
        "pump,runtime\nA,10\nB,20\nA,30\nC,40\n", encoding="utf-8")
    (vault / "manual.md").write_text(ROOT_SECRET + "\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text(SECRET + "\n", encoding="utf-8")
    (outside / "private.csv").write_text("secret\n" + SECRET + "\n",
                                         encoding="utf-8")
    (outside / "keep.txt").write_text("keep me\n", encoding="utf-8")
    monkeypatch.setenv(ENV_NAME, ENV_SECRET)
    # The app's vault is this one (council_core.paths.vault_dir()).
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    return NS(vault=vault, data=data, outside=outside,
              canary=outside / "canary.txt", keep=outside / "keep.txt",
              routing=vault / "node_routing.json")


@pytest.fixture
def short_limits(monkeypatch):
    monkeypatch.setattr(analyst_step, "TIME_LIMIT_S", LIMIT_S)
    monkeypatch.setattr(analyst_step, "SANDBOX_STARTUP_S", GRACE_S)


@pytest.fixture
def app_state(monkeypatch):
    """Put json.dumps and Path.home back after the test, whatever the code
    under test did to them (it rebound them for good when it ran in this
    process)."""
    monkeypatch.setattr(json, "dumps", json.dumps)
    monkeypatch.setattr(Path, "home", Path.__dict__["home"])


# ============================================================
# The app's view: a worker thread, and this thread still running
# ============================================================

def in_worker(fn, *, deadline: float = DEADLINE_S):
    """(fn's result, seconds): `fn` on a worker thread, as every tab and
    the jobs runner call it, while this thread keeps running. Fails when
    the worker is still busy at `deadline` (the code was not stopped) or
    this thread was held."""
    out = {}

    def work():
        try:
            out["result"] = fn()
        except BaseException as exc:                      # noqa: BLE001
            out["error"] = exc

    worker = threading.Thread(target=work, name="fence-test", daemon=True)
    t0 = last = time.monotonic()
    worker.start()
    longest = 0.0
    while worker.is_alive() and time.monotonic() - t0 < deadline:
        time.sleep(0.02)
        now = time.monotonic()
        longest, last = max(longest, now - last), now
    seconds = time.monotonic() - t0
    assert not worker.is_alive(), (
        f"still running after {deadline:.0f} s: the code was not stopped")
    assert longest < 5.0, f"the app's own thread was held {longest:.1f} s"
    if "error" in out:
        raise out["error"]
    return out["result"], seconds


def peak_commit_mb():
    """This process's peak committed memory (MB); None off Windows."""
    if sys.platform != "win32":
        return None
    import ctypes
    import ctypes.wintypes as wt

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]
    k32 = ctypes.WinDLL("kernel32")
    psapi = ctypes.WinDLL("psapi")
    k32.GetCurrentProcess.restype = wt.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE,
                                           ctypes.POINTER(Counters), wt.DWORD]
    c = Counters()
    c.cb = ctypes.sizeof(c)
    if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(),
                                      ctypes.byref(c), c.cb):
        return None
    return int(c.PeakPagefileUsage // (1024 * 1024))


# ============================================================
# The paths, each called the way the app calls it
# ============================================================

def approve_as_user(box, code, roles=("intern",)):
    """Save `code` as a Forge tool and record the user's approval of it, as
    tool_review.approve does once the tool's tests pass (they are not run
    here: a tool can pass its tests and still misbehave on other
    arguments, and tests/test_tool_review.py covers approve)."""
    ok, msg, name = abt.save_tool(abt.entry_function(code), "probe", code,
                                  vault_dir=box.vault)
    assert ok, msg
    data = tr.approvals(box.vault)
    data[name] = {"fingerprint": tr.fingerprint(
        abt.get_tool_code(name, box.vault)), "roles": list(roles),
        "approved": "test", "tests_passed": 1}
    tr._save_approvals(data, box.vault)
    return name


def council_tool(box, code, args=None):
    """(ok, text, payload): the approved tool as an Intern calls it in a
    Council turn (the Council tab's ToolSet)."""
    name = approve_as_user(box, code)
    fn = CouncilActions(vault_dir=box.vault).tools().for_role(
        "intern")[f"app_{name}"]
    return in_worker(lambda: fn(dict(args or {})))[0]


def forge_run(box, code):
    """The Forge tab's Run Selected (no arguments)."""
    ok, msg, name = abt.save_tool(abt.entry_function(code), "probe", code,
                                  vault_dir=box.vault)
    assert ok, msg
    return in_worker(lambda: forge_jobs.run_tool(name, box.vault))[0]


class Scripted:
    """The agent's model: one call of `tool` with `args`, then an answer."""

    def __init__(self, tool, args):
        self.tool, self.args, self.n = tool, args, 0

    def chat(self, messages, max_tokens=None):
        self.n += 1
        if self.n == 1:
            return json.dumps({"action": "tool", "tool": self.tool,
                               "args": self.args})
        return json.dumps({"action": "final", "answer": "done"})


def agent_run(box, tool, args, attach=None):
    """An Agent Creator agent's run (agent_profiles.run_profile) that calls
    `tool` once; `attach` is tool code attached to the agent."""
    store = ap.ProfileStore(box.vault)
    prof = store.create("Fence probe", builtin_tools=["run_pandas_analysis"])
    if attach:
        prof.tools.append(ap.AttachedTool(
            name=tool, entry=abt.entry_function(attach), description="probe",
            code=attach, sha256=ap.sha256(attach),
            params=ap.entry_params(attach)))
        store.save(prof)
    run, _s = in_worker(lambda: ap.run_profile(store, prof.id, "probe",
                                               runner=Scripted(tool, args)))
    return run


def job_agent_run(box, tool, args):
    """A Jobs-tab job's agent (JobRunner._build_agent — the one _run_job
    runs) calling `tool` once; its AgentRun, with the full tool results."""
    from agent_jobs import AgentJob
    from agent_jobs_runner import JobRunner
    jr = JobRunner(vault_dir=box.vault, file_root=box.data,
                   runner=Scripted(tool, args), max_steps=3)
    agent = jr._build_agent(AgentJob(job_id="probe", goal="probe",
                                     max_steps=3))
    return in_worker(lambda: agent.run("probe"))[0]


def job_submit(box, tool, args):
    """(the finished job, seconds): a job submitted to the Jobs tab's runner
    and run on its background worker, while this thread polls the store."""
    from agent_jobs_runner import JobRunner
    jr = JobRunner(vault_dir=box.vault, file_root=box.data,
                   runner=Scripted(tool, args), max_steps=3)
    t0 = time.monotonic()
    job_id = jr.submit("probe the data")
    while time.monotonic() - t0 < DEADLINE_S:
        job = jr.store.get(job_id)
        if job is not None and job.status in ("done", "failed", "cancelled"):
            return job, time.monotonic() - t0
        time.sleep(0.05)
    pytest.fail(f"the job was still running after {DEADLINE_S:.0f} s: the "
                "code was not stopped")


def tool_result(run, name):
    """The full result of the first call of `name` in an AgentRun."""
    calls = [c for c in run.trace.calls if c.name == name]
    assert calls, [s.observation for s in run.steps]
    assert calls[0].error is None, calls[0].error
    return calls[0].result


def job_text(job):
    return "\n".join(str(s.observation) for s in job.steps)


# Each path, as (id, call(box, tool code, snippet) -> what the app shows).
def _p_council(box, code, _snippet):
    return council_tool(box, code)[1]


def _p_forge_run(box, code, _snippet):
    res = forge_run(box, code)
    return f"{res.status}\n{res.body}"


def _p_forge_generate(box, code, _snippet):
    ok, msg, _name, _code = in_worker(lambda: tool_forge.generate_tool(
        "probe", lambda _prompt: code, vault_dir=box.vault,
        max_attempts=1))[0]
    return msg


def _p_forge_tests(box, code, _snippet):
    name = abt.entry_function(code)
    abt.save_tool(name, "probe", code, vault_dir=box.vault)
    tr.save_tests(name, [{"args": {}, "expect": "any"}], box.vault)
    return "\n".join(w for _ok, w in in_worker(
        lambda: tr.run_tests(name, box.vault))[0])


def _p_agent_pandas(box, _code, snippet):
    return json.dumps(tool_result(agent_run(box, "run_pandas_analysis",
                                            {"code": snippet}),
                                  "run_pandas_analysis"))


def _p_agent_attached(box, code, _snippet):
    name = abt.entry_function(code)
    return json.dumps(tool_result(agent_run(box, name, {}, attach=code),
                                  name))


def _p_job_pandas(box, _code, snippet):
    job, _s = job_submit(box, "run_pandas_analysis", {"code": snippet})
    assert job.status == "done", job.status
    return job_text(job)


def _p_job_app_tool(box, code, _snippet):
    name = abt.entry_function(code)
    abt.save_tool(name, "probe", code, vault_dir=box.vault)
    job, _s = job_submit(box, "run_app_tool", {"name": name})
    assert job.status == "done", job.status
    return job_text(job)


PATHS = [("council-approved-tool", _p_council),
         ("forge-run-selected", _p_forge_run),
         ("forge-generate-test-run", _p_forge_generate),
         ("forge-tool-tests", _p_forge_tests),
         ("agent-run-pandas-analysis", _p_agent_pandas),
         ("agent-attached-tool", _p_agent_attached),
         ("job-run-pandas-analysis", _p_job_pandas),
         ("job-run-app-tool", _p_job_app_tool)]


# ============================================================
# Every path goes through the analyst's runner
# ============================================================

class _Recorder:
    """child_proc.run's stand-in: records each call, reports a time-out."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        call = None
        if len(argv) > 6:
            call = json.loads(Path(argv[6]).read_text(encoding="utf-8"))
        code = Path(argv[3]).read_text(encoding="utf-8")
        self.calls.append(NS(argv=list(argv), call=call, code=code, **kw))
        return NS(timed_out=True, stderr="", error="")


@pytest.mark.parametrize("pid,path", PATHS, ids=[p for p, _ in PATHS])
def test_every_path_runs_the_code_through_the_analyst_s_runner(
        box, monkeypatch, pid, path):
    """One runner, not a copy: council_core.analyst_child under child_proc,
    once (retries=0), with the memory cap and the time limit — the
    approved and Forge tools at TIME_LIMIT_S, the agents' and jobs' tools at
    safe_agent.CODE_TIME_S (their 60 s) — each plus the start-up grace."""
    from council_core import child_proc
    rec = _Recorder()
    monkeypatch.setattr(child_proc, "run", rec)
    text = path(box, "def scale(value=2, factor=3):\n"
                     "    return value * factor\n", "print(6 * 7)")
    assert rec.calls, f"{pid}: the code did not go through child_proc"
    budget = (safe_agent.CODE_TIME_S if pid.startswith(("agent", "job"))
              else analyst_step.TIME_LIMIT_S)
    for c in rec.calls:
        assert c.argv[1:3] == ["-m", "council_core.analyst_child"]
        assert c.retries == 0 and c.memory_limit_mb == analyst_step.MEMORY_MB
        assert c.timeout == budget + analyst_step.SANDBOX_STARTUP_S
    assert "stopped" in text, text
    if "pandas" not in pid:
        # A tool call: its function and arguments, as JSON beside the code.
        assert rec.calls[0].call["entry"] == "scale"
        assert rec.calls[0].call["args"] == {}


def test_a_tool_s_arguments_reach_it_as_json(box, monkeypatch):
    from council_core import child_proc
    rec = _Recorder()
    monkeypatch.setattr(child_proc, "run", rec)
    args = {"names": ["a", "b'c", 'd"e\nf'], "opts": {"k": 1.5, "on": True},
            "none": None}
    code = "def echo(**kw):\n    return kw\n"
    abt.run_code(code, args, vault_dir=box.vault)
    assert rec.calls[0].call == {"entry": "echo", "args": args,
                                 "folders": []}
    # the arguments are not pasted into the code the child runs
    assert rec.calls[0].code == code


def test_a_tool_s_arguments_and_value_make_the_round_trip(box):
    args = {"names": ["a", "b'c", 'd"e\nf'], "opts": {"k": 1.5, "on": True},
            "none": None}
    df, msg = abt.run_code("def echo(**kw):\n    return [kw]\n", args,
                           vault_dir=box.vault)
    assert df is not None, msg
    assert tr._value(df) == [args]


def test_arguments_that_are_not_plain_json_are_refused_in_plain_words(box):
    run = analyst_step.run_tool_code("def f(x=None):\n    return x\n", "f",
                                     {"x": {1, 2}}, [box.data])
    assert not run.ran and "plain values" in run.error


# ============================================================
# A hang, a huge allocation, the app's own state
# ============================================================

HANG_IDS = [p for p, _ in PATHS]


@pytest.mark.parametrize("pid,path", PATHS, ids=HANG_IDS)
def test_an_endless_loop_is_stopped_at_the_limit_and_the_app_keeps_going(
        box, short_limits, pid, path):
    t0 = time.monotonic()
    text = path(box, SPIN, SPIN_SNIPPET)
    seconds = time.monotonic() - t0
    assert f"ran longer than {LIMIT_S} s and was stopped" in text, text
    assert seconds < DEADLINE_S


@pytest.mark.parametrize("pid,path", PATHS, ids=HANG_IDS)
def test_a_huge_allocation_is_stopped_by_the_memory_cap(box, pid, path):
    """3 GB against the 2048 MB cap: the sandbox process is refused the
    memory and the code stops — this process's memory is not touched."""
    before = peak_commit_mb()
    text = path(box, GRAB, GRAB_SNIPPET)
    after = peak_commit_mb()
    assert "more memory than the sandbox allows" in text, text
    assert "3221225472" not in text, "the 3 GB allocation succeeded"
    if before is not None:
        assert after - before < 1024, (
            f"this process's peak commit grew {after - before} MB")


@pytest.mark.parametrize("pid,path", PATHS + [
    ("run_python", lambda box, _c, snippet: CouncilActions(
        vault_dir=box.vault).tools().for_role("coder")["run_python"](
            {"code": snippet})[1])], ids=HANG_IDS + ["run_python"])
def test_code_cannot_rebind_the_app_s_own_modules(box, app_state, pid, path):
    """json.dumps = …, Path.home = …: in the app's process these stayed
    rebound for the rest of the session — a later settings write, or the
    vault's location, would have gone through the model's code. In the
    fence they die with the sandbox's process (and the code's own result
    still comes back: the report is written with json as it was)."""
    real_home = Path.home()
    try:
        text = path(box, TAMPER, TAMPER_SNIPPET)
    except AssertionError as refused:
        # council_tool / forge_run assert that saving the tool worked; the
        # save is refused now, with the sandbox's reason.
        text = str(refused)
    assert json.dumps({"a": 1}) == '{"a": 1}'
    assert Path.home() == real_home
    # Since the Path-rebinding fix the sandbox refuses this code before it
    # runs ("code may not change pandas, numpy, pathlib or the other modules
    # and classes the sandbox provides"): the paths that run it say so, and
    # the paths that save a tool first never get one to run ("not found").
    # A path that ran it anyway must have returned the code's own result
    # (Forge's generation names the tool 'tamper'). Either way the app's
    # own json and Path are untouched, above.
    assert ("may not change" in text or "not found" in text
            or ("tamper" if pid == "forge-generate-test-run"
                else "tampered") in text), text


def test_the_sandbox_refuses_the_rebinding_tool_before_it_runs():
    for code in (TAMPER + "\nresult_df = tamper()\n", TAMPER_SNIPPET):
        ok, why = va.validate_generated_code(code)
        assert not ok and "may not change" in why, why


# ============================================================
# The escapes the analyst fence refuses, refused the same way
# ============================================================

def _first_of_each_kind():
    seen, out = set(), []
    for kind, code in ESCAPES:
        if kind not in seen:
            seen.add(kind)
            out.append((kind, code))
    # and the vault root (manual.md) read, beside reads outside the vault
    out += [e for e in ESCAPES if "manual.md').read()" in e[1]]
    return out


SNIPPET_ESCAPES = _first_of_each_kind()

#: A tool's code passed the sandbox's checks when it was saved, so these are
#: the escapes a saved tool can carry: the ones refused while it runs.
TOOL_ESCAPES = [
    ("write", "    split_csv_by_column('runs.csv', 'pump', r'{out}')\n"
              "    return 1\n"),
    ("native", "    import ctypes\n    return ctypes.sizeof(ctypes.c_int)\n"),
    ("read", "    return open(r'{private_txt}').read()\n"),
    ("read", "    return read_text(r'{private_txt}')\n"),
    ("read", "    return pd.read_csv('../manual.md')\n"),
]


def _harmless(box, text):
    assert not list(box.outside.glob("canary*")), \
        "the code wrote outside the vault"
    assert not box.routing.exists(), "the code wrote node_routing.json"
    assert box.keep.exists() and box.outside.is_dir(), "the code deleted"
    assert not leaked(text), text


def _analyst_refusal(box, code, monkeypatch):
    """The analyst sandbox's own last line for `code`, asked directly."""
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(box.vault))
    rep: dict = {}
    df, _log = va.execute_pandas_code(code, [box.data], report=rep)
    assert df is None and not rep["ran"]
    return rep["error"].strip().splitlines()[-1]


def _e_council(box, code):
    ok, text, payload = council_tool(box, code)
    assert not ok
    return text + json.dumps(payload)


def _e_forge_run(box, code):
    res = forge_run(box, code)
    assert not res.ok
    return f"{res.status}\n{res.body}"


def _e_agent_attached(box, code):
    result = tool_result(agent_run(box, "probe", {}, attach=code), "probe")
    assert "preview" not in result
    return result["message"]


def _e_job_app_tool(box, code):
    abt.save_tool("probe", "probe", code, vault_dir=box.vault)
    result = tool_result(job_agent_run(box, "run_app_tool",
                                       {"name": "probe"}), "run_app_tool")
    assert "preview" not in result
    return result["message"]


TOOL_PATHS = [("council-approved-tool", _e_council),
              ("forge-run-selected", _e_forge_run),
              ("agent-attached-tool", _e_agent_attached),
              ("job-run-app-tool", _e_job_app_tool)]


@pytest.mark.parametrize("pid,path", TOOL_PATHS, ids=[p for p, _ in TOOL_PATHS])
@pytest.mark.parametrize("i", range(len(TOOL_ESCAPES)),
                         ids=[f"{k}-{i}" for i, (k, _c)
                              in enumerate(TOOL_ESCAPES)])
def test_a_tool_s_escape_is_refused_as_the_analyst_refuses_it(
        box, monkeypatch, pid, path, i):
    """Refused before it writes, deletes or reads anything — with the
    analyst sandbox's own words: the same fence, not a copy."""
    code = "def probe():\n" + fill(TOOL_ESCAPES[i][1], box)
    text = path(box, code)
    _harmless(box, text)
    refusal = _analyst_refusal(
        box, code + "\n\nresult_df = pd.DataFrame([{'r': probe()}])\n",
        monkeypatch)
    assert refusal in text, (refusal, text)


SNIPPET_PATHS = [("agent-run-pandas-analysis", "agent"),
                 ("job-run-pandas-analysis", "job")]


@pytest.mark.parametrize("pid,where", SNIPPET_PATHS,
                         ids=[p for p, _ in SNIPPET_PATHS])
@pytest.mark.parametrize("i", range(len(SNIPPET_ESCAPES)),
                         ids=[f"{k}-{i}" for i, (k, _c)
                              in enumerate(SNIPPET_ESCAPES)])
def test_an_agent_s_escape_is_refused_as_the_analyst_refuses_it(
        box, monkeypatch, pid, where, i):
    code = fill(SNIPPET_ESCAPES[i][1], box)
    run = (agent_run if where == "agent" else job_agent_run)(
        box, "run_pandas_analysis", {"code": code})
    result = tool_result(run, "run_pandas_analysis")
    _harmless(box, json.dumps(result))
    assert "preview" not in result
    assert "STDOUT" not in result["message"], result["message"]
    assert _analyst_refusal(box, code, monkeypatch) in result["message"]


# ============================================================
# What keeps working
# ============================================================

def test_an_approved_tool_returns_its_value_exactly(box):
    """Text stays text ("007", not 7), a list stays a list, a number a
    number — the value the tool returned, as it was in-process."""
    code = ("def pick(kind='text'):\n"
            "    if kind == 'text':\n        return '007'\n"
            "    if kind == 'list':\n        return [1, 'two', {'k': 3}]\n"
            "    return 2.5\n")
    name = approve_as_user(box, code)
    fn = CouncilActions(vault_dir=box.vault).tools().for_role(
        "intern")[f"app_{name}"]
    assert fn({"kind": "text"})[1].startswith("007\n")
    assert fn({"kind": "list"})[1].startswith("[1, 'two', {'k': 3}]")
    ok, text, payload = fn({"kind": "number"})
    assert ok and text.startswith("2.5") and payload == {"tool": name}
    df, msg = abt.run_tool(name, {"kind": "text"}, vault_dir=box.vault)
    assert tr._value(df) == "007" and msg.startswith(abt.LABEL)


def test_a_tool_that_returns_a_table_and_reads_the_data_folder(box):
    code = ("def by_pump(column='runtime'):\n"
            "    df = pd.read_csv('runs.csv')\n"
            "    return df.groupby('pump', as_index=False)[column].sum()\n")
    name = approve_as_user(box, code)
    ok, text, _p = CouncilActions(vault_dir=box.vault).tools().for_role(
        "intern")[f"app_{name}"]({})
    assert ok and "pump" in text and "40" in text, text
    res = forge_jobs.run_tool(name, box.vault)
    assert res.ok and res.status == f"Ran '{name}'." and "40" in res.body


def test_a_tool_s_tests_and_its_approval_still_work(box):
    code = "def scale(value=2, factor=3):\n    return value * factor\n"
    abt.save_tool("scale", "multiply", code, vault_dir=box.vault)
    tr.save_tests("scale", [{"args": {}, "expect": "number", "equals": 6},
                            {"args": {"value": 5, "factor": 2},
                             "expect": "number", "equals": 10}], box.vault)
    assert all(ok for ok, _ in tr.run_tests("scale", box.vault))
    assert tr.approve("scale", ["intern"], box.vault)[0]


def test_forge_generation_still_fixes_a_tool_that_errors(box):
    replies = iter(["def answer():\n    return undefined_name\n",
                    "def answer():\n    return 42\n"])
    ok, msg, name, _code = tool_forge.generate_tool(
        "the answer", lambda _p: next(replies), vault_dir=box.vault)
    assert ok and name == "answer" and "fixed in 2 attempts" in msg, msg


def test_a_forge_test_run_that_hangs_is_reported_for_the_model_to_fix(
        box, short_limits):
    """generate_tool feeds the stop back to the model, which fixes it."""
    prompts = []
    replies = iter([SPIN, "def spin(n=0):\n    return n\n"])

    def model(prompt):
        prompts.append(prompt)
        return next(replies)
    ok, msg, name, _code = tool_forge.generate_tool("spin", model,
                                                    vault_dir=box.vault)
    assert ok and name == "spin", msg
    assert "ran longer than" in prompts[1] and "stopped" in prompts[1]


def test_an_agent_s_attached_tool_and_pandas_give_their_answers(box):
    code = ("def total(column='runtime'):\n"
            "    return int(pd.read_csv('runs.csv')[column].sum())\n")
    run = agent_run(box, "total", {"column": "runtime"}, attach=code)
    assert tool_result(run, "total")["preview"] == [{"result": 100}]
    assert run.final_answer == "done" and run.tools_used == ["total"]
    run = agent_run(box, "run_pandas_analysis", {"code": (
        "print(pd.read_csv('runs.csv')['runtime'].mean())\n"
        "result_df = pd.read_csv('runs.csv').groupby('pump', "
        "as_index=False)['runtime'].sum()\n")})
    res = tool_result(run, "run_pandas_analysis")
    assert "25.0" in res["message"] and res["shape"] == [3, 2]
    assert {"pump": "A", "runtime": 40} in res["preview"]


def test_a_job_finishes_with_its_answer_and_report(box):
    job, _s = job_submit(box, "run_pandas_analysis", {"code": (
        "result_df = pd.read_csv('runs.csv')[['runtime']].sum()"
        ".to_frame('total')\n")})
    assert job.status == "done" and job.report_path, job.stopped_reason
    assert "100" in job_text(job)


def test_a_job_s_app_tool_gives_its_answer(box):
    code = ("def count_rows(name='runs.csv'):\n"
            "    return len(pd.read_csv(name))\n")
    abt.save_tool("count_rows", "rows", code, vault_dir=box.vault)
    job, _s = job_submit(box, "run_app_tool",
                         {"name": "count_rows", "args": {"name": "runs.csv"}})
    assert job.status == "done"
    assert '"result": 4' in job_text(job), job_text(job)


def test_the_sandbox_helpers_still_write_only_the_vault_s_data_out(box):
    """split_csv_by_column may write — into the app's vault's data_out, as
    when the code ran in the app's process."""
    run = agent_run(box, "run_pandas_analysis", {"code": (
        "r = split_csv_by_column('runs.csv', 'pump', 'split')\n"
        "print(int(r['rows'].sum()))\n")})
    assert "4" in tool_result(run, "run_pandas_analysis")["message"]
    written = sorted(p.name for p in (box.vault / "data_out" / "split")
                     .glob("*.csv"))
    assert written == ["runs_A.csv", "runs_B.csv", "runs_C.csv"]


# ============================================================
# What comes back is what the code returned (review of the fence,
# 2026-10-15): exact floats, the table's real size, the same types, a
# damaged report as a plain error, a bounded result, a plain memory stop
# ============================================================

TINY = ("def tiny(n=1):\n"
        "    return pd.DataFrame({'eps': [1.2345678901234e-12] * n,\n"
        "                         'third': [1 / 3] * n})\n")
TINY_SNIPPET = ("result_df = pd.DataFrame({'eps': [1.2345678901234e-12],\n"
                "                          'third': [1 / 3]})\n")
EXACT_ROWS = [{"eps": 1.2345678901234e-12, "third": 1 / 3}]


def test_a_tool_s_floats_come_back_exactly(box):
    """Through pandas' JSON a tool's 1.2345678901234e-12 came back as 0.0,
    1/3 as 0.33333333330000003 and 2.123456789012345 as 2.123456789; in
    the app's own process they were exact, and are again."""
    df, msg = abt.run_code(TINY, {}, vault_dir=box.vault)
    assert df is not None, msg
    assert df.to_dict(orient="records") == EXACT_ROWS
    run = analyst_step.run_tool_code(
        "def d():\n    return {'a': 1, 'b': 2.123456789012345,\n"
        "            'pi': 3.141592653589793}\n", "d", {}, [box.data])
    assert run.df.to_dict(orient="records") == [
        {"a": 1, "b": 2.123456789012345, "pi": 3.141592653589793}]


@pytest.mark.parametrize("where", ["council", "forge"])
def test_a_tiny_value_is_shown_not_zero(box, where):
    """The Council's approved-tool text and Forge's Run Selected showed
    the 1e-12 column as 0.0."""
    text = (council_tool(box, TINY)[1] if where == "council"
            else forge_run(box, TINY).body)
    assert "1.234568e-12" in text and "0.333333" in text, text


def _obs_agent_pandas(box, code, snippet):
    return tool_result(agent_run(box, "run_pandas_analysis",
                                 {"code": snippet}), "run_pandas_analysis")


def _obs_agent_attached(box, code, snippet):
    name = abt.entry_function(code)
    return tool_result(agent_run(box, name, {}, attach=code), name)


def _obs_job_pandas(box, code, snippet):
    return tool_result(job_agent_run(box, "run_pandas_analysis",
                                     {"code": snippet}),
                       "run_pandas_analysis")


def _obs_job_app_tool(box, code, snippet):
    name = abt.entry_function(code)
    abt.save_tool(name, "probe", code, vault_dir=box.vault)
    return tool_result(job_agent_run(box, "run_app_tool", {"name": name}),
                       "run_app_tool")


OBS_PATHS = [("agent-run-pandas-analysis", _obs_agent_pandas),
             ("agent-attached-tool", _obs_agent_attached),
             ("job-run-pandas-analysis", _obs_job_pandas),
             ("job-run-app-tool", _obs_job_app_tool)]


@pytest.mark.parametrize("pid,path", OBS_PATHS, ids=[p for p, _ in OBS_PATHS])
def test_agents_and_jobs_get_exact_floats(box, pid, path):
    """An agent's preview was [{'eps': 0.0, 'third': 0.33333333330000003}]."""
    result = path(box, TINY, TINY_SNIPPET)
    assert result["preview"] == EXACT_ROWS, result


ROWS = ("def rows(n=10000):\n"
        "    return pd.DataFrame({'i': range(n), 'x': [0.5] * n})\n")
ROWS_SNIPPET = ("result_df = pd.DataFrame({'i': range(10000),\n"
                "                          'x': [0.5] * 10000})\n")
CUT = "showing the first 5000 of 10000 rows"


def test_a_10000_row_table_is_described_as_10000_rows(box):
    """Only the first frame_codec.MAX_ROWS rows come back; the Council's
    text, Forge's Run Selected and the tool's tests showed the 5000 as the
    whole table, and nothing said rows were dropped."""
    assert frame_codec.MAX_ROWS == 5000
    ok, text, _p = council_tool(box, ROWS)
    assert ok and CUT in text, text[-300:]
    res = forge_run(box, ROWS)
    assert res.ok and CUT in res.body, res.body[:300]
    tr.save_tests("rows", [{"args": {}, "expect": "table"}], box.vault)
    assert tr.run_tests("rows", box.vault) == [
        (True, "rows(**{}) → a table (10000, 2)")]
    df, msg = abt.run_tool("rows", {}, vault_dir=box.vault)
    assert len(df) == 5000 and CUT in msg


@pytest.mark.parametrize("pid,path", OBS_PATHS, ids=[p for p, _ in OBS_PATHS])
def test_agents_and_jobs_are_told_the_table_s_real_size(box, pid, path):
    result = path(box, ROWS, ROWS_SNIPPET)
    assert result["shape"] == [10000, 2] and CUT in result["message"], result


def test_run_python_reports_the_table_s_real_size(box):
    ok, text, payload = CouncilActions(vault_dir=box.vault).tools().for_role(
        "coder")["run_python"]({"code": ROWS_SNIPPET})
    assert ok and payload["rows"] == 10000 and payload["cols"] == 2
    assert CUT in text


#: What a tool returned in the app's own process (the wrapper
#: app_built_tools used until 2026-10-15) — the reference for each case.
def in_process(box, code):
    call = ("\n\n_ret = f()\nresult_df = _ret if isinstance(_ret, "
            "(pd.DataFrame, pd.Series, dict)) else "
            "pd.DataFrame([{'result': _ret}])\n")
    df, log = va.execute_pandas_code(code + call, [box.data])
    assert df is not None, log
    return df


RETURNS = {
    "numeric-text-labels": "return pd.DataFrame({'001': [1], '2024': [2]}, "
                           "index=['007'])",
    "timestamp": "return pd.Timestamp('2026-10-15 12:00')",
    "datetime64": "return np.datetime64('2026-10-15T12:00:00.000000000')",
    "bytes": "return b'abc'",
    "tuple": "return (1, 2)",
    "complex": "return complex(1, 2)",
    "timedelta-column": "return pd.DataFrame({'d': pd.to_timedelta([1, 2], "
                        "unit='s')})",
    "period": "return pd.Period('2026-10', freq='M')",
    "set": "return {1, 2}",
    "grouped-columns": "return pd.DataFrame({'g': ['a', 'b'], 'x': [1, 2], "
                       "'y': [3, 4]}).groupby('g').agg(['sum', 'mean'])",
    "text-007": "return '007'",
    "floats": "return [1 / 3, 1.2345678901234e-12]",
}


@pytest.mark.parametrize("case", list(RETURNS))
def test_a_tool_s_return_comes_back_as_it_was_in_process(box, case):
    """Columns '001' came back as 1 (df['001'] raised KeyError), a
    Timestamp or bytes as text, a tuple as a list, complex as
    {'real', 'imag'}, a timedelta column as ISO text, and a Period not at
    all ("Maximum recursion level reached")."""
    code = f"def f():\n    {RETURNS[case]}\n"
    want = in_process(box, code)
    df, msg = abt.run_code(code, {}, vault_dir=box.vault)
    assert df is not None, msg
    pd.testing.assert_frame_equal(df, want)
    if list(want.columns) == ["result"]:
        a, b = want["result"].iloc[0], df["result"].iloc[0]
        assert type(a) is type(b) and a == b, (a, b)


# A damaged report — model code can rewrite what its own process reports.

class _Reporter:
    """child_proc.run's stand-in: writes `report` as the child's out.json
    (argv[5]) and exits 0."""

    def __init__(self, report):
        self.report = report

    def __call__(self, argv, **kw):
        text = (self.report if isinstance(self.report, str)
                else json.dumps(self.report))
        Path(argv[5]).write_text(text, encoding="utf-8")
        return NS(timed_out=False, returncode=0, stderr="", error="",
                  infra="", peak_mb=900)


DAMAGED_REPORTS = [
    {"ok": True, "ran": True, "value": 1, "shape": ["many", 1], "log": "ok"},
    {"ok": True, "ran": True, "shape": [1, 1],
     "frame": {"format": frame_codec.FORMAT, "index": "x"}},
    {"ok": True, "ran": "yes", "stdout": ["a"], "error": {"x": 1}, "log": 5,
     "frame": {"$": "pickle", "v": "gASV"}},
    {"ok": True, "ran": True, "table": 12345, "shape": [1, -1]},
    [1, 2, 3],
    "{ not json",
]


@pytest.mark.parametrize("i", range(len(DAMAGED_REPORTS)))
def test_a_damaged_report_is_a_plain_error_not_an_exception(box, i):
    """run_tool_code, run_script and run_code say "Never raises"; a report
    with a non-numeric shape raised ValueError into the app."""
    fake = _Reporter(DAMAGED_REPORTS[i])
    run = analyst_step.run_tool_code("def f():\n    return 1\n", "f", {},
                                     [box.data], runner=fake)
    assert run.df is None
    assert "could not be read" in run.brief() or "could not be read" in \
        run.message, (run.brief(), run.message)
    run = analyst_step.run_script("print(1)", box.data, runner=fake,
                                  exact=True)
    assert run.df is None
    df, log = analyst_step.run_code("result_df = 1", box.data, runner=fake)
    assert df is None and isinstance(log, str)


#: Model code that rewrites its own report: json's encoder class (not
#: json.dumps, which the child keeps) — the review's probe, verbatim.
FORGER = ("def forge():\n"
          "    import json\n"
          "    json.encoder.JSONEncoder.encode = lambda self, o: "
          + repr(json.dumps({"ok": True, "ran": True, "value": 1,
                             "shape": ["many", 1], "log": "ok"})) + "\n"
          "    return 1\n")


def test_code_that_rewrites_its_report_is_refused_on_every_path(box):
    """FORGER rewrites json's encoder so the child's report would lie.
    Since the Path-rebinding fix the sandbox refuses it before it runs;
    no path reports it as a success."""
    ok, why = va.validate_generated_code(FORGER + "\nresult_df = forge()\n")
    assert not ok and "may not change" in why, why
    run = analyst_step.run_tool_code(FORGER, "forge", {}, [box.data])
    assert run.df is None and "may not change" in run.brief(), run.brief()
    df, msg = abt.run_code(FORGER, {}, vault_dir=box.vault)
    assert df is None and "may not change" in msg, msg
    abt.save_tool("forge", "probe", FORGER, vault_dir=box.vault)
    res = forge_jobs.run_tool("forge", box.vault)
    assert not res.ok


# A huge result: refused in plain words, never read into the app.

def test_a_huge_returned_value_is_refused_and_not_held_by_the_app(box):
    """An 80 MB return raised the app's peak memory by ~320-400 MB (the
    report read and parsed several times over). It is refused in the
    sandbox's process, in plain words."""
    code = "def big(mb=80):\n    return 'x' * (mb * 1024 * 1024)\n"
    before = peak_commit_mb()
    (df, msg), _s = in_worker(lambda: abt.run_code(code, {},
                                                   vault_dir=box.vault))
    after = peak_commit_mb()
    assert df is None
    assert ("the result was too large to bring back (80 MB; at most "
            f"{frame_codec.MAX_RESULT_MB} MB)") in msg, msg[:300]
    if before is not None:
        assert after - before < 150, f"the app's peak grew {after - before} MB"
    ok, text, _p = council_tool(box, code)
    assert not ok and "too large to bring back" in text


def test_the_app_does_not_read_a_report_over_the_limit(box, monkeypatch):
    """Whatever the child wrote: the size is checked before reading."""
    monkeypatch.setattr(frame_codec, "MAX_RESULT_MB", 1)
    report = {"ok": True, "ran": True, "log": "x" * (2 * 2 ** 20),
              "frame": frame_codec.encode(pd.DataFrame({"result": [1]}))}
    run = analyst_step.run_tool_code("def f():\n    return 1\n", "f", {},
                                     [box.data], runner=_Reporter(report))
    assert run.df is None
    assert run.message.startswith(
        "the result was too large to bring back (2 MB; at most 1 MB)")


def test_a_large_value_under_the_limit_still_comes_back(box):
    code = ("def big(mb=4):\n"
            "    return 'x' * (mb * 1024 * 1024) + '\\u00e9'\n")
    df, msg = abt.run_code(code, {}, vault_dir=box.vault)
    assert df is not None, msg[:300]
    assert tr._value(df) == "x" * (4 * 2 ** 20) + "\u00e9"


# A memory-cap death inside a C library.

def _dies(returncode, peak_mb, stderr=""):
    def run(argv, **kw):
        return NS(timed_out=False, returncode=returncode, stderr=stderr,
                  error="", infra="", peak_mb=peak_mb)
    return run


MEMORY = ("the code needed more memory than the sandbox allows "
          f"({analyst_step.MEMORY_MB} MB) and was stopped")


def test_a_silent_abort_under_the_cap_reads_as_the_memory_cap(box,
                                                             monkeypatch):
    """pandas' to_json failing an allocation under the 2048 MB cap ends the
    process with 0xC0000409 and nothing on stderr; it read only "the
    analysis process ended without a result", and Forge said "failed — see
    the output"."""
    run = analyst_step.run_tool_code("def f():\n    return 1\n", "f", {},
                                     [box.data],
                                     runner=_dies(0xC0000409, 1019))
    assert run.stopped_by_memory
    assert run.message == (f"{MEMORY} (the sandbox process ended at 1019 MB, "
                           "exit 0xC0000409)")
    # STATUS_NO_MEMORY / STATUS_COMMITMENT_LIMIT, whatever the peak
    for code in (0xC0000017, 0xC000012D):
        assert analyst_step.run_script(
            "print(1)", box.data, runner=_dies(code, 300)).stopped_by_memory
    # not every crash: a small process, or one that said why
    for runner in (_dies(0xC0000409, 300), _dies(0xC0000005, 1500),
                   _dies(0xC0000409, 1500, stderr="Fatal Python error: x")):
        run = analyst_step.run_script("print(1)", box.data, runner=runner)
        assert not run.stopped_by_memory, run.message
    assert "exit 0xC0000005" in analyst_step.run_script(
        "print(1)", box.data, runner=_dies(0xC0000005, 1500)).message
    # the Forge tab's status
    from council_core import child_proc
    monkeypatch.setattr(child_proc, "run", _dies(0xC0000409, 1019))
    ok, why, name = abt.save_tool("one", "probe",
                                  "def one():\n    return 1\n",
                                  vault_dir=box.vault)
    assert ok, why
    res = forge_jobs.run_tool(name, box.vault)
    assert res.status.startswith(f"⚠ {name}: the code needed more memory"), \
        res.status


def test_a_result_too_big_for_pandas_json_is_a_plain_stop(box):
    """The real thing, on the run_python / analyst path that still writes
    pandas' JSON: a 200 MB result_df died in to_json at ~1 GB of the cap
    with no reason given."""
    run, _s = in_worker(lambda: analyst_step.run_script(
        "result_df = pd.DataFrame({'s': ['x' * (200 * 1024 * 1024)]})\n",
        box.data, vault=box.vault))
    first = run.message.splitlines()[0]
    assert "ended without a result" not in run.message, run.message
    assert first.startswith((MEMORY, "the result was too large")), first


# ============================================================
# The Forge tab itself
# ============================================================

@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def test_the_forge_tab_answers_while_a_hanging_tool_is_stopped(
        box, short_limits, qapp):
    """Run Selected on a tool that never returns: the tab keeps processing
    events, the run is stopped at the limit, and the status says so in
    plain words (it said "Ran 'spin'." whatever happened)."""
    from PySide6.QtCore import QTimer
    from council_qt.tabs.forge import ForgeActions, ForgeTab
    abt.save_tool("spin", "never returns", SPIN, vault_dir=box.vault)
    tab = ForgeTab(actions=ForgeActions(box.vault))
    try:
        tab.tools.setCurrentRow(0)
        ticks = []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start(50)
        t0 = time.monotonic()
        tab.on_run()
        while tab._busy and time.monotonic() - t0 < DEADLINE_S:
            qapp.processEvents()
            time.sleep(0.01)
        seconds = time.monotonic() - t0
        timer.stop()
        assert not tab._busy, "Run Selected never finished"
        assert len(ticks) >= seconds / 0.05 * 0.5, (len(ticks), seconds)
        assert "stopped" in tab.status.text(), tab.status.text()
        assert "spin" in tab.status.text()
    finally:
        tab.close()
        tab.deleteLater()
        qapp.processEvents()
