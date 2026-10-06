"""Agent profiles (council_core/agent_profiles.py): the council builds a tool,
reviews it, and attaches it by the user's setting; an agent runs only its own,
pinned tools. Models are replaced by a scripted council; every vault is a
tmp_path.
"""
from __future__ import annotations

import json

import pytest

from council_core import agent_profiles as ap

GOOD_TOOL = '''```python
def count_rows(name: str = "ok.csv"):
    """Rows in a CSV in the data folder."""
    return pd.DataFrame({"rows": [len(pd.read_csv(name))]})
```'''

FIXED_TOOL = GOOD_TOOL.replace('"""Rows in a CSV in the data folder."""',
                               '"""Rows in a CSV (header excluded)."""')

ESCAPING_TOOL = '''```python
def sneaky():
    import os
    return os.listdir("C:/")
```'''


class Council:
    """A scripted council: the drafter's replies in order, and each
    reviewer's verdicts in order."""

    def __init__(self, drafts, verdicts):
        self.drafts = list(drafts)
        self.verdicts = {r: list(v) for r, v in verdicts.items()}
        self.calls = []

    def __call__(self, role, messages, *, max_tokens=700, json_schema=None):
        self.calls.append(role)
        if role == ap.DRAFTER_ROLE:
            return self.drafts.pop(0) if len(self.drafts) > 1 else self.drafts[0]
        v = self.verdicts[role]
        verdict = v.pop(0) if len(v) > 1 else v[0]
        if isinstance(verdict, str):
            return verdict
        return json.dumps({"approve": verdict, "concerns": [] if verdict
                           else [f"{role} is not convinced"]})


@pytest.fixture
def store(tmp_path):
    vault = tmp_path / "vault"
    (vault / "data_in").mkdir(parents=True)
    (vault / "data_in" / "ok.csv").write_text("a\n1\n2\n3\n", encoding="utf-8")
    return ap.ProfileStore(vault)


@pytest.fixture
def profile(store):
    return store.create("Row counter", builtin_tools=["list_files"])


def test_default_setting_is_approve(store):
    assert store.tool_attach_mode() == "approve"
    store.set_tool_attach_mode("automatic")
    assert ap.ProfileStore(store.vault).tool_attach_mode() == "automatic"
    with pytest.raises(ValueError):
        store.set_tool_attach_mode("sometimes")


def test_approve_mode_waits_even_when_everyone_approves(store, profile):
    council = Council([GOOD_TOOL], {"judge": [True], "skeptic": [True]})
    req = ap.build_tool(store, profile.id, "count the rows of a csv", chat=council)
    assert req.status == "waiting" and req.all_approved and req.test == "passed"
    assert store.get(profile.id).tools == []
    store.approve(req.id)
    tools = store.get(profile.id).tools
    assert [t.name for t in tools] == ["count_rows"]
    assert tools[0].approved_by == "user"
    assert tools[0].sha256 == ap.sha256(tools[0].code)
    assert store.get_request(req.id).status == "attached"


def test_automatic_mode_attaches_when_all_approve(store, profile):
    store.set_tool_attach_mode("automatic")
    council = Council([GOOD_TOOL], {"judge": [True], "skeptic": [True]})
    req = ap.build_tool(store, profile.id, "count the rows of a csv", chat=council)
    assert req.status == "attached" and req.decided_by == "automatic"
    assert store.get(profile.id).tools[0].approved_by == "automatic"
    assert council.calls == ["coder", "judge", "skeptic"]


def test_automatic_mode_never_attaches_over_an_objection(store, profile):
    store.set_tool_attach_mode("automatic")
    council = Council([GOOD_TOOL, FIXED_TOOL], {"judge": [True], "skeptic": [False]})
    req = ap.build_tool(store, profile.id, "count the rows of a csv", chat=council)
    assert req.status == "waiting"
    assert store.get(profile.id).tools == []
    # It revised once with the concerns, and was reviewed again.
    assert council.calls == ["coder", "judge", "skeptic", "coder", "judge", "skeptic"]
    assert {r.round for r in req.reviews} == {1, 2}


def test_a_revision_that_satisfies_the_reviewers_attaches(store, profile):
    store.set_tool_attach_mode("automatic")
    council = Council([GOOD_TOOL, FIXED_TOOL],
                      {"judge": [True, True], "skeptic": [False, True]})
    req = ap.build_tool(store, profile.id, "count the rows of a csv", chat=council)
    assert req.status == "attached"
    assert "header excluded" in store.get(profile.id).tools[0].code


def test_an_unreadable_review_is_not_an_approval(store, profile):
    store.set_tool_attach_mode("automatic")
    council = Council([GOOD_TOOL], {"judge": [True], "skeptic": ["looks fine to me!"]})
    req = ap.build_tool(store, profile.id, "count rows", chat=council)
    assert req.status == "waiting"
    assert any(r.error for r in req.reviews if r.role == "skeptic")


def test_code_the_sandbox_rejects_never_becomes_a_tool(store, profile):
    store.set_tool_attach_mode("automatic")
    council = Council([ESCAPING_TOOL], {"judge": [True], "skeptic": [True]})
    req = ap.build_tool(store, profile.id, "list the C drive", chat=council)
    assert req.status == "failed"
    assert store.get(profile.id).tools == []
    with pytest.raises(ValueError):
        store.approve(req.id)


def test_the_agent_runs_its_pinned_code_not_the_file(store, profile):
    import app_built_tools as abt
    council = Council([GOOD_TOOL], {"judge": [True], "skeptic": [True]})
    req = ap.build_tool(store, profile.id, "count rows", chat=council)
    store.approve(req.id)
    # Someone later saves a different tool under the same name in the Forge.
    abt.save_tool("count_rows", "changed",
                  'def count_rows(name="ok.csv"):\n    return pd.DataFrame({"rows": [-1]})',
                  vault_dir=store.vault)
    tool = store.get(profile.id).tools[0]
    out = ap.run_attached_tool(tool, {}, [store.vault / "data_in"])
    assert out["preview"] == [{"rows": 3}]
    # Tampering with the pinned copy itself is refused.
    tool.code += "\n# changed"
    assert "no longer matches" in ap.run_attached_tool(tool, {}, [store.vault])["error"]


def test_registry_holds_only_the_profiles_tools(store, profile):
    council = Council([GOOD_TOOL], {"judge": [True], "skeptic": [True]})
    store.approve(ap.build_tool(store, profile.id, "count rows", chat=council).id)
    reg = ap.build_registry(store.get(profile.id), store.vault, store.vault / "data_in")
    assert set(reg.names()) == {"list_files", "count_rows"}
    assert reg.frozen


def test_write_and_run_app_tool_can_never_be_picked(store):
    for bad in ("write_tool", "run_app_tool", "list_app_tools"):
        with pytest.raises(ValueError):
            store.create("x", builtin_tools=[bad])


def test_run_profile_uses_the_attached_tool(store, profile):
    council = Council([GOOD_TOOL], {"judge": [True], "skeptic": [True]})
    store.approve(ap.build_tool(store, profile.id, "count rows", chat=council).id)

    class Runner:
        def __init__(self):
            self.n = 0

        def chat(self, messages, max_tokens=None):
            self.n += 1
            if self.n == 1:
                return json.dumps({"action": "tool", "tool": "count_rows", "args": {}})
            return json.dumps({"action": "final", "answer": "3 rows"})

    run = ap.run_profile(store, profile.id, "how many rows in ok.csv?", runner=Runner())
    assert run.final_answer == "3 rows"
    assert run.tools_used == ["count_rows"]
    obs = [s.observation for s in run.steps if s.tool == "count_rows"][0]
    assert "'rows': 3" in obs or '"rows": 3' in obs


def test_an_unattached_tool_is_refused(store, profile):
    import app_built_tools as abt
    abt.save_tool("secret_tool", "not attached",
                  'def secret_tool():\n    return pd.DataFrame([1])', vault_dir=store.vault)

    class Runner:
        def __init__(self):
            self.n = 0

        def chat(self, messages, max_tokens=None):
            self.n += 1
            if self.n == 1:
                return json.dumps({"action": "tool", "tool": "secret_tool", "args": {}})
            return json.dumps({"action": "final", "answer": "done"})

    run = ap.run_profile(store, profile.id, "use secret_tool", runner=Runner())
    assert "secret_tool" not in run.tools_used
    assert "secret_tool" in run.tools_missing


def test_damaged_store_is_left_alone(store):
    store.path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ap.ProfileStoreDamaged):
        store.profiles()
    with pytest.raises(ap.ProfileStoreDamaged):
        store.create("x")
    assert store.path.read_text(encoding="utf-8") == "{not json"


def test_store_is_hidden_from_vault_searches(store, profile):
    import conversation_logger as cl
    import data_index
    assert cl.is_protected_path(store.path, store.vault)
    assert ap.STORE_NAME in data_index._APP_INTERNAL_FILENAMES


def test_graph_tools_read_the_knowledge_graph(tmp_path):
    from council_core import kg_corpus, knowledge_graph as kgm
    v = tmp_path / "v"
    kg_corpus.build(v)
    with kgm.KnowledgeGraph(v) as kg:
        kg.confirm_all_rules()
        kg.seed()
    tools = ap._graph_tools(v)
    found = tools["graph_find"].fn({"text": "Carol"}, None)
    assert found["matches"][0]["name"] == "Carol Lee"
    links = tools["graph_neighbors"].fn({"name": "PN-0088"}, None)
    assert any(l["other"] == "Carol Lee" and l["link"] == "OWNS" for l in links["links"])
    assert all(l["sources"] for l in links["links"])


def test_entry_params_schema():
    assert ap.entry_params('def f(column: str, limit=10):\n    pass') == {
        "column": "str (required)", "limit": "value (optional, default 10)"}
