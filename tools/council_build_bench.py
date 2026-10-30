"""
tools/council_build_bench.py — the Build tab's Edit mode with a real model,
on small projects whose tests fail, scored by whether the tests pass after.

Each task is a fresh folder with a few files and pytest tests, as a user would
open it in the Build tab. The loop is the tab's own "Fix this" path, with the
tab's own pieces and without its window:

  1. the tests run in the Build sandbox (build_sandbox.run, pytest);
  2. the chat in Edit mode (build_chat.Conversation.ask, EDIT_SYSTEM, the
     "coder" role exactly as the tab calls it) gets the request, the open
     file, the other files and the failing output;
  3. a PLAN reply is answered with "Go ahead with the plan." (the Go button);
     edit blocks are matched (build_edits.propose); refused blocks are asked
     for once more (retry_note), as the tab does; every edit is accepted
     (Accept all) and written;
  4. the tests run again: at most build_sandbox.MAX_ROUNDS rounds, stopping
     early when two rounds fail the same way (build_sandbox.stuck).

Per task: solved, rounds, plan cards, refused edits, model calls, seconds.

    python tools\\council_build_bench.py --out C:\\council_test\\build
    (model: COUNCIL_BACKEND / COUNCIL_OLLAMA_MODEL / COUNCIL_GGUF_PATH, or the
     coder role of the vault in COUNCIL_VAULT_ROOT)
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent

GO_TEXT = "Go ahead with the plan."

TASKS: List[Dict[str, Any]] = [
    {"id": "median_even", "open": "stats.py",
     "request": "The tests fail. Fix median() so they pass.",
     "files": {
         "stats.py": (
             "def median(values):\n"
             "    \"\"\"The median of a non-empty list of numbers.\"\"\"\n"
             "    ordered = sorted(values)\n"
             "    return ordered[len(ordered) // 2]\n"),
         "tests/test_stats.py": (
             "import sys, pathlib\n"
             "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))\n"
             "from stats import median\n\n"
             "def test_odd():\n    assert median([3, 1, 2]) == 2\n\n"
             "def test_even():\n    assert median([4, 1, 3, 2]) == 2.5\n\n"
             "def test_one():\n    assert median([7]) == 7\n")}},
    {"id": "slugify_new", "open": "text_tools.py",
     "request": ("Add slugify(text) to text_tools.py: lower case, words joined "
                 "by '-', anything that is not a letter or digit dropped. The "
                 "tests show what is expected."),
     "files": {
         "text_tools.py": (
             "def title_case(text):\n"
             "    return \" \".join(w.capitalize() for w in text.split())\n"),
         "tests/test_text_tools.py": (
             "import sys, pathlib\n"
             "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))\n"
             "from text_tools import slugify, title_case\n\n"
             "def test_title():\n    assert title_case('a b') == 'A B'\n\n"
             "def test_slug():\n    assert slugify('Hello, World!') == 'hello-world'\n\n"
             "def test_spaces():\n    assert slugify('  Part  PN-1000 rev B ') == 'part-pn1000-rev-b'\n")}},
    {"id": "page_off_by_one", "open": "paging.py",
     "request": "Fix this: the paging tests fail.",
     "files": {
         "paging.py": (
             "def page(items, number, size):\n"
             "    \"\"\"Page `number` (1 = first) of `items`, `size` per page.\"\"\"\n"
             "    start = number * size\n"
             "    return items[start:start + size]\n\n"
             "def page_count(items, size):\n"
             "    return len(items) // size\n"),
         "tests/test_paging.py": (
             "import sys, pathlib\n"
             "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))\n"
             "from paging import page, page_count\n\n"
             "def test_first():\n    assert page(list(range(10)), 1, 3) == [0, 1, 2]\n\n"
             "def test_last():\n    assert page(list(range(10)), 4, 3) == [9]\n\n"
             "def test_count():\n    assert page_count(list(range(10)), 3) == 4\n"
             "    assert page_count(list(range(9)), 3) == 3\n")}},
    {"id": "two_files", "open": "report.py",
     "request": ("test_report fails: total() comes back wrong. Fix it where the "
                 "values are read, so every caller gets numbers."),
     "files": {
         "loader.py": (
             "import csv\n\n"
             "def load_rows(path):\n"
             "    with open(path, newline='', encoding='utf-8') as fh:\n"
             "        return [dict(r) for r in csv.DictReader(fh)]\n"),
         "report.py": (
             "from loader import load_rows\n\n"
             "def total(path, column):\n"
             "    return sum(r[column] for r in load_rows(path))\n"),
         "tests/test_report.py": (
             "import sys, pathlib\n"
             "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))\n"
             "from report import total\nfrom loader import load_rows\n\n"
             "def _csv(tmp_path):\n"
             "    p = tmp_path / 'r.csv'\n"
             "    p.write_text('part,power\\nA,1.5\\nB,2\\n', encoding='utf-8')\n"
             "    return p\n\n"
             "def test_total(tmp_path):\n    assert total(_csv(tmp_path), 'power') == 3.5\n\n"
             "def test_rows_are_numbers(tmp_path):\n"
             "    assert load_rows(_csv(tmp_path))[1]['power'] == 2.0\n")}},
]


def make_task(root: Path, task: Dict[str, Any]) -> Path:
    if root.exists():
        shutil.rmtree(root)
    for rel, text in task["files"].items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def run_tests(root: Path):
    from council_core import build_sandbox as bs
    lines: List[str] = []
    # pytest's tmp_path under the bench's own folder: in the shared
    # pytest-of-<user> folder its clean-up races with any other pytest
    # running at the time (the suite runs four at once).
    temp = root.parent / "_pytest_tmp"
    temp.mkdir(parents=True, exist_ok=True)
    out = bs.run(bs.test_argv(sys.executable), root, lines.append,
                 timeout=bs.TEST_TIMEOUT_S, memory_mb=bs.TEST_MEMORY_MB,
                 env=bs.sandbox_env({"QT_QPA_PLATFORM": "offscreen",
                                     "COUNCIL_NO_DIALOGS": "1",
                                     "PYTEST_DEBUG_TEMPROOT": str(temp)}))
    return bs.TestOutcome(out, "\n".join(lines))


def engine_chat(messages, on_token=None, should_stop=None) -> str:
    """The Build tab's model call (council_qt.tabs.build._chat)."""
    import council_engine
    from council_core import token_budget
    return council_engine.local_chat(
        messages, role="coder", temperature=0.2,
        num_predict=token_budget.reply("long", 1500), timeout=180,
        token_callback=on_token, should_stop=should_stop)


def run_task(task: Dict[str, Any], work: Path, vault: Path,
             chat: Callable[..., str]) -> Dict[str, Any]:
    from council_core import build_chat as bc
    from council_core import build_edits as be
    from council_core import build_sandbox as bs
    root = make_task(work / task["id"], task)
    conv = bc.Conversation(vault, str(root))
    conv.clear()
    rec: Dict[str, Any] = {"id": task["id"], "solved": False, "rounds": 0,
                           "plans": 0, "refused": 0, "applied": 0,
                           "calls": 0, "stuck": False, "error": ""}
    t0 = time.monotonic()
    calls = {"n": 0}

    def counted(messages, **kw):
        calls["n"] += 1
        return chat(messages, **kw)

    def ctx_for(output: str) -> "bc.Context":
        main = root / task["open"]
        others = [(str(p), p.read_text(encoding="utf-8"))
                  for p in sorted(root.rglob("*.py")) if p != main]
        return bc.Context(path=str(main), text=main.read_text(encoding="utf-8"),
                          language="python", folder=str(root), root=str(root),
                          numbered=False, others=others,
                          output=bc.tail(output))

    def read(rel: str) -> Optional[str]:
        p = root / rel
        return p.read_text(encoding="utf-8") if p.is_file() else None

    signatures: List[str] = []
    first = run_tests(root)
    rec["tests_before"] = first.headline()
    output, request = first.output, task["request"]
    try:
        while rec["rounds"] < bs.MAX_ROUNDS:
            rec["rounds"] += 1
            reply = conv.ask(request, ctx_for(output), counted,
                             system=be.EDIT_SYSTEM, mode="edit")
            if be.parse_plan(reply) is not None and not be.parse_blocks(
                    reply, task["open"]):
                rec["plans"] += 1
                reply = conv.ask(GO_TEXT, ctx_for(output), counted,
                                 system=be.EDIT_SYSTEM, mode="edit", kind="go")
            blocks = be.parse_blocks(reply, task["open"])
            prop = be.propose(blocks, root, read)
            refused = prop.refused()
            if refused:
                rec["refused"] += len(refused)
                again = conv.ask(be.retry_note(refused), ctx_for(output),
                                 counted, system=be.EDIT_SYSTEM, mode="edit",
                                 kind="retry")
                more = be.propose(be.parse_blocks(again, task["open"]), root,
                                  read)
                prop.changes += [c for c in more.changes
                                 if any(e.ok for e in c.edits)]
            prop.accept_all()
            for change in prop.accepted_changes():
                p = root / change.path
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(change.after(), encoding="utf-8")
                rec["applied"] += sum(1 for e in change.edits if e.ok)
            result = run_tests(root)
            rec["tests_after"] = result.headline()
            if result.passed:
                rec["solved"] = True
                break
            signatures.append(result.signature())
            if bs.stuck(signatures):
                rec["stuck"] = True
                break
            output = result.output
            request = "The tests still fail. Fix this."
    except Exception as exc:                              # noqa: BLE001
        rec["error"] = repr(exc)
    rec["calls"] = calls["n"]
    rec["seconds"] = round(time.monotonic() - t0, 1)
    return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="council_build_bench",
                                 description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="")
    a = ap.parse_args(argv)
    sys.path.insert(0, str(REPO))
    import os
    from council_core import paths
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    vault = paths.vault_dir() if os.environ.get("COUNCIL_VAULT_ROOT") \
        else out / "vault"
    want = {x for x in a.only.split(",") if x}
    results = []
    for task in TASKS:
        if want and task["id"] not in want:
            continue
        rec = run_task(task, out / "projects", vault, engine_chat)
        results.append(rec)
        print(f"  {task['id']}: {'solved' if rec['solved'] else 'NOT solved'}"
              f" in {rec['rounds']} round(s), {rec['calls']} model calls, "
              f"{rec['seconds']} s; plan cards {rec['plans']}, refused edits "
              f"{rec['refused']}{', stuck' if rec['stuck'] else ''}"
              f"{', error ' + rec['error'] if rec['error'] else ''}",
              flush=True)
    solved = sum(r["solved"] for r in results)
    print(f"Build tab, Edit mode: {solved}/{len(results)} solved", flush=True)
    (out / "build_bench.json").write_text(json.dumps(results, indent=1),
                                          encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
