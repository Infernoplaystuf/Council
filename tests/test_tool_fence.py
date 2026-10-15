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
    text = path(box, TAMPER, TAMPER_SNIPPET)
    assert json.dumps({"a": 1}) == '{"a": 1}'
    assert Path.home() == real_home
    # what the code returned or printed (Forge's generation says it saved
    # the tool 'tamper', whose test run passed)
    assert ("tamper" if pid == "forge-generate-test-run" else "tampered") \
        in text, text


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
