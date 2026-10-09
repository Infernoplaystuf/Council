"""
The benchmark of the pipelines the user runs (llm_bench --pipeline new),
"Check this PC", and the child-process runner that keeps a long run from
being graded by the machine's failures (council_core.child_proc).

No real model anywhere: the engine's real Ollama path runs against
tests/fake_ollama, answered by tests/bench_fakes (a valid wireframe, a K1
function, docs_bench's answer key). Each test gets a SHORT scratch vault
(a generated project nests ~120 characters below it) and leaves the engine's
slot and routing caches as it found them.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from council_core import bench_engine as be  # noqa: E402
from council_core import child_proc as cp  # noqa: E402
from council_core import llm_bench as lb  # noqa: E402
from council_core import pc_check  # noqa: E402
from tests.bench_fakes import make_reply_fn  # noqa: E402
from tests.fake_ollama import DEFAULT_TAGS, FakeOllama  # noqa: E402

WINDOWS = sys.platform == "win32"
win_only = pytest.mark.skipif(not WINDOWS, reason="Job Objects are Windows")
CODE = {c["id"]: c for c in lb.load_code_cases()}
GUI = {c["id"]: c for c in lb.load_gui_cases()}
PHI = [t for t in DEFAULT_TAGS if t["name"].startswith("phi3.5")]


def _reset_caches():
    from council_core import local_models, model_slots
    model_slots.invalidate()
    local_models.invalidate_cache()
    be._reset_engine_routes()


@pytest.fixture
def short_vault(monkeypatch):
    vault = Path(tempfile.mkdtemp(prefix="lbt_"))
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    _reset_caches()
    yield vault
    _reset_caches()
    shutil.rmtree(vault, ignore_errors=True)


@pytest.fixture
def fake(short_vault, monkeypatch):
    """The fake Ollama (phi3.5 only), every role on it, the bench fakes
    answering."""
    log = []
    with FakeOllama(tags=PHI) as srv:
        monkeypatch.setenv("COUNCIL_OLLAMA_HOST", srv.url)
        monkeypatch.delenv("COUNCIL_BACKEND", raising=False)
        monkeypatch.delenv("COUNCIL_DESCRIBE_MODE", raising=False)
        srv.state.reply_fn = make_reply_fn(log=log)
        srv.log = log
        _reset_caches()
        be.use_model(short_vault, "ollama:phi3.5")
        srv.vault = short_vault
        yield srv
    _reset_caches()


# ============================================================
# child_proc — the 0xC0000142 fix
# ============================================================

def _alive(pid: int) -> bool:
    import ctypes
    k32 = ctypes.WinDLL("kernel32")
    h = k32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFO
    if not h:
        return False
    code = ctypes.c_ulong()
    try:
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        return code.value == 259                    # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


SPAWN_SLEEPER = ("import subprocess, sys; p = subprocess.Popen([sys.executable,"
                 " '-c', 'import time; time.sleep(60)']); print(p.pid, "
                 "flush=True)")


@win_only
def test_a_child_runs_in_a_job_and_leaves_nothing_behind():
    r = cp.run([sys.executable, "-c", "print('hi')"], timeout=30)
    assert r.ok and r.stdout.strip() == "hi"
    assert r.job and r.leaked == 0 and r.attempts == 1
    assert r.commit_free_mb is not None


@win_only
def test_a_grandchild_left_running_is_counted_and_killed():
    r = cp.run([sys.executable, "-c", SPAWN_SLEEPER], timeout=30)
    pid = int(r.stdout.split()[0])
    assert r.ok and r.leaked >= 1
    deadline = time.time() + 5
    while _alive(pid) and time.time() < deadline:
        time.sleep(0.1)
    assert not _alive(pid), "the leaked grandchild is still running"


@win_only
def test_a_timeout_kills_the_whole_tree():
    code = SPAWN_SLEEPER + "; import time; time.sleep(60)"
    t0 = time.time()
    r = cp.run([sys.executable, "-c", code], timeout=3)
    assert r.timed_out and time.time() - t0 < 20
    pid = int(r.stdout.split()[0])
    time.sleep(0.3)
    assert not _alive(pid)


@win_only
def test_an_exit_of_status_dll_init_failed_is_infra_and_is_retried():
    """The exact exit every probe and hidden test got late in the phi4 run
    (3221225794): the machine's failure, retried, never a verdict."""
    die = ("import ctypes; "
           "ctypes.windll.kernel32.ExitProcess(0xC0000142)")
    r = cp.run([sys.executable, "-c", die], timeout=30, retries=1)
    assert r.returncode == 3221225794
    assert "STATUS_DLL_INIT_FAILED" in r.infra and r.attempts == 2
    assert not r.ok
    assert cp.infra_reason(3221225794).startswith("exit 0xC0000142")
    assert cp.infra_reason(1) == "" and cp.infra_reason(0xC0000005) == ""


@win_only
def test_the_commit_cap_turns_a_runaway_into_a_memory_error():
    r = cp.run([sys.executable, "-c",
                "b = bytearray(400 * 1024 * 1024); print(len(b))"],
               timeout=30, memory_limit_mb=128)
    assert not r.ok and "MemoryError" in r.stderr and not r.infra


def test_a_flood_of_output_keeps_only_the_tail():
    r = cp.run([sys.executable, "-c",
                "import sys\nfor i in range(40):\n"
                "    sys.stdout.write('x' * 100000)\nprint('END')"],
               timeout=30, max_output=4096)
    assert r.ok and len(r.stdout) <= 4096 and r.stdout.endswith("END\n")


def test_stop_kills_the_child():
    flag = {"stop": False}
    threading.Timer(0.8, lambda: flag.__setitem__("stop", True)).start()
    t0 = time.time()
    r = cp.run([sys.executable, "-c", "import time; time.sleep(60)"],
               timeout=60, should_stop=lambda: flag["stop"])
    assert r.stopped and time.time() - t0 < 15


def test_waiting_for_commit_headroom():
    seq = iter([100, 200, 5000])
    got = cp.wait_for_commit(1000, max_wait=10, poll=0.01,
                             status=lambda: {"commit_free_mb": next(seq)})
    assert got["ok"] and got["free_mb"] == 5000
    low = cp.wait_for_commit(1000, max_wait=0.05, poll=0.01,
                             status=lambda: {"commit_free_mb": 10})
    assert not low["ok"] and low["free_mb"] == 10
    unknown = cp.wait_for_commit(1000, max_wait=0, status=lambda: {
        "commit_free_mb": None})
    assert unknown["ok"]


def _infra_run(*_a, **_k):
    return cp.ChildResult(returncode=3221225794, infra=(
        "exit 0xC0000142 STATUS_DLL_INIT_FAILED after 3 attempt(s); commit "
        "free 12 MB"), attempts=3, commit_free_mb=12)


def test_a_hidden_test_the_pc_could_not_start_is_not_graded(monkeypatch):
    monkeypatch.setattr(cp, "run", _infra_run)
    ref = ("```python\ndef on_btn_add(self, *args) -> None:\n"
           "    self.ports.result.set('6.5')\n```")
    chk = lb.check_code(ref, CODE["K1"])
    assert not chk.ok and chk.category == "harness_error"
    assert "STATUS_DLL_INIT_FAILED" in chk.detail
    rows = [{"id": "K1", "tier": "simple", "passed": False,
             "category": "harness_error"},
            {"id": "K5", "tier": "simple", "passed": True, "category": "ok"}]
    s = lb.summarise(rows)
    assert (s["graded"], s["invalid"], s["passed"], s["pass_rate"]) == \
        (1, 1, 1, 1.0)
    table = lb.format_table({"meta": {}, "code": {"passes": [rows]}})
    assert "INVALID: 1 case(s)" in table and "N/A" in table


def test_a_runtime_probe_the_pc_could_not_start_is_harness_error(
        monkeypatch, short_vault):
    import run_describe_prompts as rdp
    from tests.bench_fakes import S4_PIXEL
    monkeypatch.setattr(cp, "run", _infra_run)
    ran = rdp.run_generated(short_vault, sys.executable)
    assert not ran["ok"] and ran["infra"]
    r = lb.run_gui_case(GUI["S4"], lb.ScriptedBackend([json.dumps(S4_PIXEL)]),
                        vault=short_vault, runtime_python=sys.executable)
    assert r["category"] == "harness_error", r
    assert lb.summarise([r])["invalid"] == 1


def test_a_smoke_run_the_pc_could_not_start_is_skipped_not_blamed(
        monkeypatch):
    import gui_smoke
    monkeypatch.setattr(cp, "run", _infra_run)
    res = gui_smoke.smoke_function({"logic.py": "def f():\n    return {}\n"},
                                   "f", [], {}, python=sys.executable)
    assert res.skipped.startswith("this PC could not start the smoke run")
    assert not res.faults()


def test_a_docs_code_test_the_pc_could_not_start_is_not_graded(monkeypatch):
    from council_core import docs_bench
    monkeypatch.setattr(cp, "run", _infra_run)
    item = docs_bench.load_bench()["code_tasks"][0]
    got = docs_bench.run_code_test("x = 1\n", item)
    assert not got["passed"] and got["infra"]
    rep = docs_bench.BenchReport(items=[
        docs_bench.ItemResult("c01", "code", False, infra=got["infra"]),
        docs_bench.ItemResult("q01", "question", True)])
    assert rep.rate == 1.0 and rep.summary()["not_graded"] == 1


# ============================================================
# The new pipeline, through the engine and the fake Ollama
# ============================================================

def test_use_model_puts_every_role_on_it_in_the_scratch_vault(fake):
    from council_core import model_slots
    data = json.loads((fake.vault / "model_slots.json").read_text())
    assert data["slots"]["main"]["path"] == "ollama:phi3.5:latest"
    cfg = model_slots.current()
    for role in ("coder", "docs", "writer"):
        assert cfg.slots[cfg.slot_for(role)].path == "ollama:phi3.5:latest"


def test_the_tap_records_every_engine_call_and_puts_the_engine_back(fake):
    import council_engine
    real = council_engine.local_chat
    with lb.EngineTap() as tap:
        assert council_engine.local_chat is not real
        out = council_engine.local_chat(
            [{"role": "user", "content": "Reply with the one word: ready"}],
            role="coder", num_predict=8)
    assert council_engine.local_chat is real
    assert out == "ready"
    c = tap.calls[0]
    assert (c.prompt_tokens, c.output_tokens, c.gen_tok_s) == (120, 40, 40.0)
    assert c.model == "phi3.5:latest" and c.backend == "ollama"
    assert c.constrained is False and c.role == "coder"


def test_the_new_gui_pipeline_uses_the_designers_own_profile(fake):
    """No model call of ours: Describe sizes the profile from the coder
    role's model (phi3.5, 3.8B -> tree mode, best of 3) and sends its
    json_schema through local_chat."""
    with lb.EngineTap() as tap:
        r = lb.run_gui_case(GUI["S2"], tap, vault=fake.vault,
                            runtime_python=sys.executable, pipeline="new")
    assert r["passed"], r
    prof = r["profile"]
    assert (prof["mode"], prof["n_best"], prof["constrained"]) == \
        ("tree", 3, True)
    assert prof["params_b"] == 3.8
    assert r["model_calls"] == 1 and r["constrained_calls"] == 1
    assert r["gen_tok_s"] == 40.0 and r["served_by"] == ["phi3.5:latest"]
    sent = fake.state.chats[-1]
    assert isinstance(sent.get("format"), dict)
    assert "layout" in sent["format"]["properties"]
    assert r["runtime"]["ok"] and "BUILT" in r["runtime"]["detail"]
    assert fake.log == ["describe-tree"]


def test_the_baseline_gui_pipeline_is_still_the_plain_call(fake):
    backend = lb.EngineBackend(model="ollama:phi3.5")
    r = lb.run_gui_case(GUI["S4"], backend, vault=fake.vault,
                        pipeline="baseline")
    assert r["passed"], r
    assert "profile" not in r
    sent = fake.state.chats[-1]
    assert sent.get("format") is None              # no schema, as before
    assert r["prompt_tokens"] == 120 and r["gen_tok_s"] == 40.0


def test_the_codebehind_strategy_writes_wires_and_passes_k1(fake):
    with lb.EngineTap() as tap:
        r = lb.run_code_case(CODE["K1"], tap, strategy="codebehind",
                             vault=fake.vault)
    assert r["passed"], r
    assert r["mode"] == "function" and r["category"] == "ok"
    assert r["link"]["module"] == "logic"
    assert r["link"]["inputs"] == ["first_number", "second_number"]
    assert r["link"]["outputs"] == {"result": "result"}
    assert any(g.startswith("smoke run: ok") for g in r["gates"])
    assert r["model_calls"] == 1 and r["n_best"] == 3
    assert "def " in r["code"]


def test_a_wrong_function_fails_the_hidden_test(fake):
    fake.state.reply_fn = make_reply_fn(wrong_code=True)
    with lb.EngineTap() as tap:
        r = lb.run_code_case(CODE["K1"], tap, strategy="codebehind",
                             vault=fake.vault)
    assert not r["passed"] and r["category"] == "test_failure"
    assert "should show 6.5" in r["detail"]


def test_state_kept_on_self_is_refused_by_the_handler_mode_writer(fake):
    """A FINDING, pinned: K2 and K6 need state between presses, and the
    writer's handler mode refuses every self.<attribute> that is not a port
    or a handler — so a correct stopwatch is never offered."""
    with lb.EngineTap() as tap:
        r = lb.run_code_case(CODE["K6"], tap, strategy="codebehind",
                             vault=fake.vault)
    assert r["mode"] == "handler" and not r["passed"]
    assert r["category"] == "wrong_port", r
    assert "self._started" in r["detail"]


# ---- 2026-10-05 replies, replayed through the real writer ----------------

RECORDED = json.loads((ROOT / "tests" / "data" / "llm_bench" /
                       "recorded_2026-10-05.json").read_text(encoding="utf-8"))


def replay_code(cid, vault, *keys):
    """One code case through the real writer, project, smoke run and hidden
    test, the engine answering with these recorded replies in order."""
    tap = lb.EngineTap([RECORDED["code"][k] for k in keys])
    with tap:
        row = lb.run_code_case(CODE[cid], tap, strategy="codebehind",
                               vault=vault, n_best=1)
    return row, tap


def test_k2_filtering_what_the_last_press_left_is_sent_back(short_vault):
    """llama3.1:8b's K2 passed every gate and the ONE-press smoke run, then
    failed the hidden test. Now the smoke run presses again and the repair
    is told to keep the original — replayed here with phi4's reply, the
    one that kept it."""
    row, tap = replay_code("K2", short_vault, "llama3.1:8b K2 1",
                           "phi4:14b K2 1")
    assert row["passed"], row
    assert row["attempts"] == 2 and row["repair_rounds"] == 1
    repair = tap.prompts[1]
    assert "each press must start from the ORIGINAL data" in repair
    assert "self._ai_original = self.ports.fruits.items()" in repair
    assert "Repeated presses start from the ORIGINAL data" in tap.prompts[0]


def test_k2_qwen_coder_is_caught_by_the_repeated_presses(short_vault):
    row, _tap = replay_code("K2", short_vault, "qwen2.5-coder K2 1")
    assert not row["passed"] and row["category"] != "test_failure", row
    assert any("ORIGINAL data" in g for g in row["gates"]), row["gates"]


def test_k4_module_dot_function_now_reaches_the_hidden_test_and_passes(
        short_vault):
    """qwen2.5-coder's K4: recorded undefined_name after 5 calls."""
    row, _tap = replay_code("K4", short_vault, "qwen2.5-coder K4 1")
    assert row["passed"], row
    assert row["attempts"] == 1
    assert "added `import image_stats`" in row["notes"]


def test_k7_a_crash_relabelled_as_a_refusal_is_not_accepted(short_vault):
    """qwen2.5's K7: the smoke run called the relabelled crash deliberate,
    the writer accepted it, and the hidden test read an empty file."""
    row, _tap = replay_code("K7", short_vault, "qwen2.5 K7 4")
    assert not row["passed"] and row["category"] != "test_failure", row
    assert any("raised again as ValueError" in g for g in row["gates"]), \
        row["gates"]


def test_the_docs_suite_answers_through_the_docs_role(fake):
    with lb.EngineTap() as tap:
        got = lb.run_docs(tap, items=("q01", "q04"))
    rows = got["rows"]
    assert [r["id"] for r in rows] == ["q01", "q04"]
    assert all(r["passed"] and r["citation_ok"] for r in rows), rows
    assert all(r["model_calls"] == 2 and r["gen_tok_s"] == 40.0
               for r in rows)
    s = got["summary"]
    assert s["questions"] == "2/2" and s["citations_right"] == "2/2"
    assert s["served_by"] == ["phi3.5:latest"] and s["role"] == "docs"


def test_one_run_measures_gui_code_and_docs_and_saves_as_it_goes(fake):
    updates = []
    rep = lb.run(lb.SUITES, lb.EngineTap(), only="S2,K1,q01",
                 pipeline="new", vault=fake.vault,
                 on_update=lambda r: updates.append(
                     json.loads(json.dumps(r, default=str))))
    assert [r["passed"] for s in lb.SUITES
            for r in rep[s]["passes"][0]] == [True, True, True]
    assert len(updates) == 3
    assert "code" not in updates[0] and "code" in updates[1]
    assert rep["meta"]["pipeline"] == "new"
    assert rep["meta"]["code"]["strategy"] == "codebehind"
    table = lb.format_table(rep)
    for head in ("GUI pass 1", "CODE pass 1", "DOCS pass 1",
                 "citations right 1/1"):
        assert head in table
    assert rep["meta"]["commit_free_mb"]["start"] is not None or not WINDOWS


def test_a_new_pipeline_run_replays_to_the_same_verdicts(fake):
    rep = lb.run(lb.SUITES, lb.EngineTap(), only="S2,K1,q01",
                 pipeline="new", vault=fake.vault)
    old = json.loads(json.dumps(rep, default=str))
    fake.state.reply_fn = lambda body: "the server must not be asked"
    n = len(fake.state.chats)
    again = lb.replay(old, vault=fake.vault)
    assert len(fake.state.chats) == n            # no model was asked
    assert again["docs"]["passes"][0][0]["passed"]
    for suite in lb.SUITES:
        assert [(r["id"], r["passed"], r["category"])
                for r in again[suite]["passes"][0]] == \
            [(r["id"], r["passed"], r["category"])
             for r in rep[suite]["passes"][0]]


def test_the_cli_runs_one_model_unattended_and_cleans_up(fake, tmp_path):
    out = tmp_path / "bench.json"
    rc = lb.main(["--model", "ollama:phi3.5", "--suites", "gui,code,docs",
                  "--only", "S2,K1,q01", "--out", str(out)])
    assert rc == 0
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["meta"]["model"] == "ollama:phi3.5"
    assert rep["meta"]["pipeline"] == "new"
    assert rep["meta"]["warmup"]["model"] == "phi3.5:latest"
    assert [rep[s]["summary"][0]["passed"] for s in lb.SUITES] == [1, 1, 1]
    assert not Path(rep["meta"]["vault"]).exists()      # scratch removed
    assert "phi3.5:latest" in fake.state.unloads        # model unloaded


# ============================================================
# "Check this PC"
# ============================================================

HW = {"summary": "fake", "gpu": "NVIDIA GeForce RTX 4070 Laptop GPU",
      "raw": {"gpu_name": "NVIDIA GeForce RTX 4070 Laptop GPU",
              "ram_speed_mts": 5600}}


def test_check_this_pc_measures_every_installed_model(fake, monkeypatch,
                                                      tmp_path):
    """The whole path, offline: each model in its own child process
    (llm_bench --check) against the fake server — speed with the cold call
    set apart, placement from /api/ps, the quick probe through the real
    pipelines, the estimates — then the report in the vault."""
    monkeypatch.setattr(pc_check, "_hardware", lambda: HW)
    fake.state.vram_fraction = 0.5
    lines = []
    app_vault = tmp_path / "vault"
    res = lb.check_this_pc(vault_dir=app_vault, on_progress=lines.append)
    assert res["ok"], res["message"]
    (row,) = res["rows"]
    assert row["model"] == "ollama:phi3.5:latest" and row["origin"] == "US"
    assert (row["placement"], row["vram_pct"]) == ("part GPU", 50.0)
    assert row["gen_tok_s"] == 40.0 and row["cold_s"] is not None
    assert len(row["speed"]["warm"]) == 2
    for suite, n in (("gui", 1), ("code", 1), ("docs", 2)):
        assert (row[suite]["passed"], row[suite]["graded"]) == (n, n), suite
    assert row["estimates"]["gui"]["basis"] == "this probe's calls"
    assert row["predicted_gen_tok_s"] is not None
    saved = json.loads((app_vault / "model_bench.json").read_text())
    assert saved["ranking"]["coder"][0]["model"] == row["model"]
    assert res["report_path"] == app_vault / "model_bench.json"
    assert "Best — GUIs and code: phi3.5:latest" in res["message"]
    assert "phi3.5:latest" in fake.state.unloads
    assert any("quick probe" in ln for ln in lines)
    # Through the Models tab's hook, the same runner.
    from council_core import model_jobs
    assert model_jobs.bench_runner() is lb.check_this_pc


def test_check_this_pc_stops_when_asked(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(pc_check, "_hardware", lambda: HW)
    fake.state.first_delay = 30.0                # the cold call hangs
    stop = {"now": False}
    threading.Timer(4.0, lambda: stop.__setitem__("now", True)).start()
    t0 = time.time()
    res = lb.check_this_pc(vault_dir=tmp_path, should_stop=lambda: stop["now"])
    assert time.time() - t0 < 30
    assert res["rows"] and res["rows"][0].get("stopped")
    assert res["message"].startswith("Stopped after")


def test_check_this_pc_says_when_no_ollama_answers(short_vault, monkeypatch):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setenv("COUNCIL_OLLAMA_HOST", f"http://127.0.0.1:{port}")
    _reset_caches()
    res = lb.check_this_pc(vault_dir=short_vault)
    assert not res["ok"] and "No Ollama server answers" in res["message"]
    assert not (short_vault / "model_bench.json").exists()


def test_the_ranking_is_us_only_and_reliability_first():
    def row(name, origin, gui, code, docs, tok, est):
        return {"model": f"ollama:{name}", "name": name, "origin": origin,
                "gen_tok_s": tok, "placement": "GPU",
                "gui": {"passed": gui, "graded": 1},
                "code": {"passed": code, "graded": 1},
                "docs": {"passed": docs, "graded": 2},
                "estimates": {"gui": {"seconds": est},
                              "function": {"seconds": est / 3},
                              "docs_answer": {"seconds": est / 4}}}
    rows = [row("phi3.5", "US", 0, 1, 1, 93, 10),
            row("llama3.1:8b", "US", 1, 1, 2, 47, 30),
            row("qwen2.5:7b", "non-US", 1, 1, 2, 51, 20)]
    ranking = pc_check.rank(rows)
    for role in ("coder", "docs", "writer"):
        assert all("qwen" not in e["name"] for e in ranking[role])
    assert ranking["coder"][0]["name"] == "llama3.1:8b"
    assert ranking["docs"][0]["name"] == "llama3.1:8b"
    assert ranking["writer"][0]["name"] == "phi3.5"


def test_the_hardware_prediction_and_the_estimates():
    phi = {"size_bytes": 2_176_178_843, "family": "phi3", "name": "phi3.5"}
    gpu = pc_check.predict_gen_tok_s(phi, 1.0, HW["raw"])
    assert 85 < gpu < 100                           # measured here: 92.6
    cpu = pc_check.predict_gen_tok_s(phi, 0.0, HW["raw"])
    assert 15 < cpu < 25                            # measured here: 17.5
    assert pc_check.predict_gen_tok_s(phi, 1.0, {"gpu_name": "?"}) is None
    est = pc_check.estimate_seconds(
        {"gen_tok_s": 50.0, "prompt_tok_s": 1000.0},
        [{"model_calls": 2, "prompt_tokens": 4000, "output_tokens": 1000}],
        "gui")
    assert est["seconds"] == 2 * (500 / 50 + 2000 / 1000)
    typ = pc_check.estimate_seconds({"gen_tok_s": 50.0}, [], "function")
    assert typ["basis"] == "typical token counts"
    assert pc_check.estimate_seconds({}, [], "gui") is None


# ============================================================
# The Models tab button, end to end (offscreen)
# ============================================================

def test_the_models_tab_check_button_runs_the_real_check(fake, monkeypatch,
                                                         tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from council_core import model_jobs
    from council_qt.tabs.models import ModelsActions, ModelsTab
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(pc_check, "_hardware", lambda: HW)

    class Quiet(ModelsActions):            # no hardware probe, no ranking
        def detect(self):
            return model_jobs.Hardware()

        def find(self, *a, **k):
            return model_jobs.FindResult(True, "")

        def upgrade(self, *a, **k):
            return "", None

    vault = tmp_path / "vault"


    tab = ModelsTab(actions=Quiet(vault), role_actions=None)
    tab.roles.reload = lambda: None
    tab.on_check_pc()
    assert tab.check_btn.text().startswith("■")
    deadline = time.time() + 240
    while "Saved to" not in tab.status.text() and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()
    assert "Saved to" in tab.status.text(), tab.status.text()
    assert "phi3.5:latest" in tab.status.text()
    assert (vault / "model_bench.json").is_file()
    assert "Check this PC" in tab.check_btn.text()
    tab.deleteLater()
    app.processEvents()


# ============================================================
# The soak: many children back to back
# ============================================================

def test_a_short_soak_has_no_failures_and_no_leaks(short_vault):
    stats = lb.soak(6, short_vault)
    assert stats["failed"] == 0, stats["failures"]
    assert (stats["probe_ok"], stats["test_ok"]) == (3, 3)
    assert stats["infra"] == 0 and stats["leaked"] == 0
