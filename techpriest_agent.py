# ============================================================
# techpriest_agent.py  —  Self-correcting coding agent
# ============================================================
# Replaces the single-shot Tech-Priest with a ReAct loop:
#   write code → execute → read error → reflect → fix → repeat
#   Up to MAX_ATTEMPTS before giving up.
#
# Install:
#   pip install langgraph langchain-ollama
#
# Falls back gracefully to single-shot if langgraph not installed.
# ============================================================

from __future__ import annotations

import re
import sys
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# ── LangGraph (optional) ─────────────────────────────────────
# Catch OSError/ImportError broadly — on Windows, torch DLL load
# failures surface as OSError even though the import chain starts
# with langchain_ollama. The council doesn't need torch at all;
# this is a transitive dependency in langchain_core.
_LANGGRAPH_OK = False
try:
    from langgraph.graph import StateGraph, END
    from langchain_ollama import ChatOllama
    from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
    _LANGGRAPH_OK = True
except (ImportError, OSError) as _e:
    # OSError on Windows = DLL load failure (usually torch/CUDA mismatch)
    if isinstance(_e, OSError):
        print(f"[TechPriest] LangGraph skipped — DLL load error: {_e}")
        print("[TechPriest] Tip: run  pip uninstall torch torchvision torchaudio -y")
        print("             then reinstall matching your CUDA version, or use CPU-only:")
        print("             pip install torch --index-url https://download.pytorch.org/whl/cpu")


# ── Fallback: use council_engine directly ────────────────────
import council_engine as ce


# ============================================================
# State schema (works without LangGraph too)
# ============================================================

@dataclass
class AgentState:
    task: str                          # original user request
    code: str = ""                     # latest generated code
    filename: str = "solution.py"
    stdout: str = ""
    stderr: str = ""
    returncode: int = -1
    attempt: int = 0
    max_attempts: int = 8
    passed: bool = False
    history: List[Dict[str, str]] = field(default_factory=list)
    final_code: str = ""
    explanation: str = ""
    event_log: List[str] = field(default_factory=list)

    def log(self, msg: str) -> None:
        self.event_log.append(msg)


# ============================================================
# Prompts
# ============================================================

SYSTEM_PROMPT = """\
You are the TECH-PRIEST — an elite software engineer focused on robust, production-quality code.

Rules:
1. Output ONLY a fenced Python code block. No prose before it, no prose after.
2. The code must be completely self-contained and runnable as a standalone script.
3. Include all imports. Handle edge cases. Add clear error messages.
4. Never use placeholder comments like "# implement this". Write real code.
5. If the task is ambiguous, make the most reasonable assumption and note it in a comment.

Output format — STRICTLY:
```python
# your code here
```
"""

FIX_PROMPT_TEMPLATE = """\
Your previous attempt failed. Fix it.

ORIGINAL TASK:
{task}

YOUR PREVIOUS CODE (attempt {attempt}/{max_attempts}):
```python
{code}
```

EXECUTION RESULT:
Return code: {returncode}
stdout:
{stdout}
stderr:
{stderr}

REFLECTION:
- What went wrong?
- What is the root cause?
- What is the minimal fix?

Now output the corrected code. ONLY a fenced Python code block. No other text.
```python
"""

FIRST_ATTEMPT_PROMPT = """\
Write a Python script that solves the following task completely.

TASK:
{task}

Output ONLY a fenced Python code block. No explanations, no prose.
```python
"""

EXPLAIN_PROMPT = """\
The following Python code solves this task:

TASK:
{task}

CODE:
```python
{code}
```

Write a brief (3-5 sentence) explanation of how the code works and any important design decisions.
Do NOT repeat the code. Plain prose only.
"""


# ============================================================
# Code extraction
# ============================================================

def _extract_code(text: str) -> str:
    """Extract first Python code block from model output."""
    # Try fenced block first
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Try unfenced — everything that looks like Python
    lines = text.splitlines()
    code_lines = []
    in_code = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("import ", "from ", "def ", "class ", "#!", "if __name__")):
            in_code = True
        if in_code:
            code_lines.append(line)
    if code_lines:
        return "\n".join(code_lines).strip()
    # Last resort: return the whole text
    return text.strip()


def _make_filename(task: str) -> str:
    words = re.sub(r"[^a-z0-9 ]", "", task.lower()).split()[:5]
    return "_".join(words) or "solution"


# ============================================================
# Execution
# ============================================================

def _run_code(code: str, filename: str, workspace: Path, timeout: int = 30) -> Tuple[int, str, str]:
    """Write code to file and execute it. Returns (returncode, stdout, stderr)."""
    path = workspace / filename
    path.write_text(code, encoding="utf-8")
    try:
        result = subprocess.run(
            [sys.executable, str(path)],
            capture_output=True, text=True,
            cwd=str(workspace), timeout=timeout,
        )
        return result.returncode, result.stdout[:4000], result.stderr[:4000]
    except subprocess.TimeoutExpired:
        return -1, "", f"TimeoutExpired: script exceeded {timeout}s"
    except Exception as e:
        return -1, "", str(e)


# ============================================================
# LangGraph agent (when available)
# ============================================================

def _build_langgraph_agent(
    host: str,
    model: str,
    workspace: Path,
    max_attempts: int = 8,
    event_callback: Optional[Callable[[str, str], None]] = None,
) -> Any:
    """
    Build a LangGraph StateGraph for the coding agent.
    event_callback(phase, message) fires on each state transition.
    """
    if not _LANGGRAPH_OK:
        raise RuntimeError("langgraph not installed")

    llm = ChatOllama(model=model, base_url=host, temperature=0.15, num_predict=3000)

    def _emit(phase: str, msg: str):
        if event_callback:
            event_callback(phase, msg)

    # ── Node: generate (first attempt) ──────────────────────
    def node_generate(state: dict) -> dict:
        task = state["task"]
        attempt = state.get("attempt", 0) + 1
        _emit("generate", f"Attempt {attempt}/{max_attempts} — writing code…")

        if attempt == 1:
            prompt = FIRST_ATTEMPT_PROMPT.format(task=task)
        else:
            prompt = FIX_PROMPT_TEMPLATE.format(
                task=task,
                attempt=attempt - 1,
                max_attempts=max_attempts,
                code=state.get("code", ""),
                returncode=state.get("returncode", -1),
                stdout=state.get("stdout", "")[:1500],
                stderr=state.get("stderr", "")[:1500],
            )

        messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)]
        response = llm.invoke(messages)
        code = _extract_code(response.content)
        filename = _make_filename(task) + f"_v{attempt}.py"

        history = state.get("history", [])
        history.append({"attempt": attempt, "code": code})

        return {**state, "code": code, "filename": filename,
                "attempt": attempt, "history": history}

    # ── Node: execute ────────────────────────────────────────
    def node_execute(state: dict) -> dict:
        attempt = state["attempt"]
        _emit("execute", f"Attempt {attempt} — running code…")

        # Dream3D static validation before execution
        try:
            from dream3d_primer import PipelineValidator
            valid, issues = PipelineValidator.validate(state["code"])
            if not valid:
                feedback = PipelineValidator.format_feedback(issues)
                _emit("validate", f"Static analysis issues:\n{feedback}")
                # Return as failed so generate node fixes it
                return {**state, "returncode": -2, "stdout": "",
                        "stderr": feedback, "passed": False}
        except ImportError:
            pass

        rc, stdout, stderr = _run_code(state["code"], state["filename"], workspace)
        passed = rc == 0

        status = "✓ PASSED" if passed else f"✗ FAILED (rc={rc})"
        _emit("result", f"Attempt {attempt} {status}\nstdout: {stdout[:300]}\nstderr: {stderr[:300]}")

        return {**state, "returncode": rc, "stdout": stdout,
                "stderr": stderr, "passed": passed}

    # ── Node: explain ────────────────────────────────────────
    def node_explain(state: dict) -> dict:
        _emit("explain", "Generating explanation…")
        prompt = EXPLAIN_PROMPT.format(task=state["task"], code=state["code"])
        messages = [HumanMessage(content=prompt)]
        response = llm.invoke(messages)
        return {**state, "final_code": state["code"], "explanation": response.content}

    # ── Node: give_up ────────────────────────────────────────
    def node_give_up(state: dict) -> dict:
        _emit("give_up", f"Exhausted {max_attempts} attempts. Returning best effort.")
        # Pick the attempt with lowest returncode, fallback to last
        best = state.get("code", "")
        for h in state.get("history", []):
            if h.get("passed"):
                best = h["code"]
                break
        return {**state, "final_code": best, "explanation":
                f"Could not produce passing code in {max_attempts} attempts. "
                "Returning last generated version — review stderr for details."}

    # ── Routing ──────────────────────────────────────────────
    def should_continue(state: dict) -> str:
        if state.get("passed"):
            return "explain"
        if state.get("attempt", 0) >= max_attempts:
            return "give_up"
        return "generate"

    # ── Build graph ──────────────────────────────────────────
    graph = StateGraph(dict)
    graph.add_node("generate", node_generate)
    graph.add_node("execute",  node_execute)
    graph.add_node("explain",  node_explain)
    graph.add_node("give_up",  node_give_up)

    graph.set_entry_point("generate")
    graph.add_edge("generate", "execute")
    graph.add_conditional_edges("execute", should_continue, {
        "generate": "generate",
        "explain":  "explain",
        "give_up":  "give_up",
    })
    graph.add_edge("explain",  END)
    graph.add_edge("give_up",  END)

    return graph.compile()


# ============================================================
# Fallback: pure-Python ReAct loop (no LangGraph)
# ============================================================

def _run_fallback_loop(
    task: str,
    personality_model: Any,          # ce.PersonalityModel
    runner: ce.LocalRunner,
    max_attempts: int = 8,
    event_callback: Optional[Callable[[str, str], None]] = None,
) -> AgentState:
    """
    Self-correcting loop using council_engine directly.
    No LangGraph required.
    """
    def _emit(phase: str, msg: str):
        if event_callback:
            event_callback(phase, msg)

    state = AgentState(task=task, max_attempts=max_attempts)
    state.filename = _make_filename(task) + ".py"

    for attempt in range(1, max_attempts + 1):
        state.attempt = attempt

        # Build prompt
        if attempt == 1:
            prompt = FIRST_ATTEMPT_PROMPT.format(task=task)
        else:
            prompt = FIX_PROMPT_TEMPLATE.format(
                task=task,
                attempt=attempt - 1,
                max_attempts=max_attempts,
                code=state.code,
                returncode=state.returncode,
                stdout=state.stdout[:1500],
                stderr=state.stderr[:1500],
            )

        _emit("generate", f"Attempt {attempt}/{max_attempts} — writing code…")
        raw = personality_model.respond(prompt)
        state.code = _extract_code(raw)
        state.history.append({"attempt": attempt, "code": state.code})

        # Execute
        _emit("execute", f"Attempt {attempt} — running…")

        # Dream3D static validation before execution
        try:
            from dream3d_primer import PipelineValidator
            valid, issues = PipelineValidator.validate(state.code)
            if not valid:
                feedback = PipelineValidator.format_feedback(issues)
                _emit("validate", f"Static validation issues found")
                state.returncode = -2
                state.stdout     = ""
                state.stderr     = feedback
                state.passed     = False
                _emit("result", f"Attempt {attempt} ✗ STATIC FAIL\n{feedback[:400]}")
                continue
        except ImportError:
            pass

        rc, stdout, stderr = _run_code(
            state.code,
            f"{_make_filename(task)}_v{attempt}.py",
            runner.workspace,
        )
        state.returncode = rc
        state.stdout = stdout
        state.stderr = stderr
        state.passed = rc == 0

        status = "✓ PASSED" if state.passed else f"✗ FAILED (rc={rc})"
        _emit("result",
              f"Attempt {attempt} {status}\n"
              f"stdout: {stdout[:400]}\nstderr: {stderr[:400]}")

        if state.passed:
            _emit("explain", "Generating explanation…")
            explain_prompt = EXPLAIN_PROMPT.format(task=task, code=state.code)
            state.explanation = personality_model.respond(explain_prompt)
            state.final_code = state.code
            return state

    # Exhausted
    _emit("give_up", f"Exhausted {max_attempts} attempts. Returning best effort.")
    state.final_code = state.code
    state.explanation = (
        f"Could not produce passing code in {max_attempts} attempts. "
        "Returning last generated version — review stderr above."
    )
    return state


# ============================================================
# Public API
# ============================================================

class TechPriestAgent:
    """
    Drop-in replacement for the single-shot Tech-Priest ModelAgent.
    Uses LangGraph if available, falls back to a pure-Python ReAct loop.
    """

    def __init__(
        self,
        personality_model: Any,        # ce.PersonalityModel
        runner: ce.LocalRunner,
        max_attempts: int = 8,
        event_callback: Optional[Callable[[str, str], None]] = None,
    ):
        self.model = personality_model
        self.runner = runner
        self.max_attempts = max_attempts
        self.event_callback = event_callback
        self._langgraph_agent = None

        if _LANGGRAPH_OK:
            try:
                spec = personality_model.registry.get(
                    personality_model.backend_key or "local_coder_primary"
                )
                self._langgraph_agent = _build_langgraph_agent(
                    host=spec.host,
                    model=spec.model,
                    workspace=runner.workspace,
                    max_attempts=max_attempts,
                    event_callback=event_callback,
                )
                print("[TechPriestAgent] Using LangGraph backend")
            except Exception as e:
                print(f"[TechPriestAgent] LangGraph init failed ({e}), using fallback loop")
        else:
            print("[TechPriestAgent] langgraph not installed — using fallback ReAct loop")

    @property
    def uses_langgraph(self) -> bool:
        return self._langgraph_agent is not None

    def run(self, task: str) -> AgentState:
        """
        Run the coding agent on a task.
        Returns AgentState with .final_code, .explanation, .passed, .event_log
        """
        if self._langgraph_agent is not None:
            result = self._langgraph_agent.invoke({
                "task": task,
                "attempt": 0,
                "max_attempts": self.max_attempts,
                "history": [],
                "passed": False,
                "code": "", "filename": "", "stdout": "", "stderr": "",
                "returncode": -1, "final_code": "", "explanation": "",
                "event_log": [],
            })
            state = AgentState(task=task, max_attempts=self.max_attempts)
            state.final_code  = result.get("final_code", "")
            state.explanation = result.get("explanation", "")
            state.passed      = result.get("passed", False)
            state.attempt     = result.get("attempt", 0)
            state.code        = result.get("code", "")
            state.stdout      = result.get("stdout", "")
            state.stderr      = result.get("stderr", "")
            state.history     = result.get("history", [])
            return state
        else:
            return _run_fallback_loop(
                task=task,
                personality_model=self.model,
                runner=self.runner,
                max_attempts=self.max_attempts,
                event_callback=self.event_callback,
            )