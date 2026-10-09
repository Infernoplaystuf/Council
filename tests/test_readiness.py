"""The readiness check (council_core/readiness.py) and the off-switches."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import council_engine as ce  # noqa: E402
from council_core import readiness as rd  # noqa: E402


def test_the_quick_check_reports_every_part(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setattr(ce, "model_key", lambda role: ("local", "ollama:llama3.1:8b"))
    monkeypatch.setattr(ce, "native_tools", lambda role: role != "coder")
    monkeypatch.setattr(ce, "effective_n_ctx", lambda slot="main": 8192)
    checks = rd.run(tmp_path)
    names = {c.name: c for c in checks}
    assert names["git"].status == "OK"
    assert names["GUI check (offscreen)"].status == "OK", names[
        "GUI check (offscreen)"].detail
    coder = names["role coder"]
    assert coder.status == "WARN" and "native tool calling" in coder.fix
    assert "16k+" in coder.fix
    assert names["role judge"].status == "OK"
    assert any(c.name.startswith("panel ") for c in checks)
    text = rd.report(checks)
    assert text.startswith("READINESS — ") and "→" in text


def test_the_live_check(monkeypatch):
    monkeypatch.setattr(ce, "model_key", lambda role: (
        "local", "ollama:a" if role != "coder" else "ollama:b"))

    def local_chat(messages, **kw):
        if kw.get("json_schema"):
            return json.dumps({"answer": 3000, "unit": "m"})
        return "ready"
    monkeypatch.setattr(ce, "local_chat", local_chat)
    monkeypatch.setattr(ce, "chat_tools", lambda m, t, **kw: {
        "content": "", "tool_calls": [{"name": "calc",
                                       "arguments": {"expr": "1234*5678"}}]})
    said = []
    checks = rd._live(said.append)
    assert [c.status for c in checks] == ["OK"] * len(checks)
    assert sum("answers" in c.name for c in checks) == 2      # two models
    assert any(c.name == "coder tool call" for c in checks)


def test_a_coder_that_will_not_call_tools_fails(monkeypatch):
    monkeypatch.setattr(ce, "model_key", lambda role: ("local", ""))
    monkeypatch.setattr(ce, "chat_tools", lambda m, t, **kw: {
        "content": "It is 7006652.", "tool_calls": []})
    checks = rd._live(lambda s: None)
    assert checks[-1].status == "FAIL" and "llama3.1" in checks[-1].fix


def test_the_switches(monkeypatch):
    from council_core import council_schemas as cs
    monkeypatch.setenv("COUNCIL_STRUCTURED", "0")
    assert not cs.enabled()
    seen = {}

    def respond(self, prompt, **kw):
        seen.update(kw)
        return "Verdict: PASS"
    monkeypatch.setattr(ce.PersonalityModel, "respond", respond)
    j = ce.JudgeModel.__new__(ce.JudgeModel)
    j.name = "judge"
    j.critique("q", "a")
    assert "json_schema" not in seen
    j.rank_candidates("q", {"writer": {"answer": "a"}})
    assert seen.get("json_schema") is None
    monkeypatch.setenv("COUNCIL_NATIVE_TOOLS", "0")
    assert ce.native_tools("coder") is False


def test_a_code_job_keeps_its_transcript(tmp_path):
    import subprocess
    from council_core import code_agent as ca, project as pj
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "u@x"],
                 ["config", "user.name", "u"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "s"], cwd=repo, check=True)
    vault = tmp_path / "vault"
    project = pj.Project("p", str(repo))
    rec = ca.new_job(vault, project, "t", ca.plan_from_json({"goal": "g", "steps": [
        {"title": "s1", "files": [], "check": "none", "details": ""}]}))
    ca.run_job(vault, project, rec, chat=lambda m, **k: "NONE",
               chat_tools=lambda m, t, **k: {"content": "", "tool_calls": [
                   {"name": "step_done", "arguments": {"summary": "ok"}}]})
    lines = (pj.project_dir(vault, project) / "jobs"
             / f"{rec.id}.transcript.jsonl").read_text().splitlines()
    rows = [json.loads(x) for x in lines]
    assert rows[0]["role"] == "system" and rows[0]["step"] == 1
    assert any(r["calls"] for r in rows) and any(r["role"] == "tool" for r in rows)
