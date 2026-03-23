# ============================================================
# council_engine.py  —  v2
# ============================================================
# Conda env:
#   conda create -n council python=3.11 -y
#   conda activate council
# Optional (SSH in Apothecary): pip install paramiko
# Optional (Phase 3 STT mic): pip install sounddevice soundfile
# Optional (Phase 3 transcription): pip install faster-whisper
# ============================================================

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Generator, Iterator, List, Optional, Tuple
import urllib.request
import urllib.error


# Optional dependencies for Phase 3
try:
    import sounddevice as sd  # type: ignore
except Exception:
    sd = None

try:
    import soundfile as sf  # type: ignore
except Exception:
    sf = None

try:
    from faster_whisper import WhisperModel  # type: ignore
except Exception:
    WhisperModel = None


# ============================================================
# Utils
# ============================================================

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def append_log(path: str, line: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")


def safe_name(s: str, maxlen: int = 128) -> str:
    return (re.sub(r"[^A-Za-z0-9._-]+", "_", s.strip())[:maxlen] or "item")


# ============================================================
# Ollama backend — streaming + non-streaming (stdlib only)
# ============================================================

def _ensure_localhost(url: str, *, allow_remote: bool = False) -> None:
    """Guard against accidental remote calls unless explicitly opted in."""
    u = url.lower().strip()
    is_local = u.startswith("http://localhost") or u.startswith("http://127.0.0.1")
    if not is_local and not allow_remote:
        raise RuntimeError(
            f"Refusing non-local model endpoint: {url}\n"
            "Pass allow_remote=True to LocalBackendSpec to enable Pi/remote hosts."
        )


def _ollama_chat(
    host: str,
    model: str,
    messages: List[Dict[str, str]],
    *,
    temperature: float,
    num_predict: int,
    allow_remote: bool = False,
) -> str:
    """Blocking (non-streaming) Ollama /api/chat call. Returns full response text."""
    _ensure_localhost(host, allow_remote=allow_remote)
    url = host.rstrip("/") + "/api/chat"

    payload = {
        "model": model,
        "messages": messages,
        "options": {
            "temperature": float(temperature),
            "num_predict": int(num_predict),
        },
        "stream": False,
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
            return data.get("message", {}).get("content", "") or ""
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        raise RuntimeError(f"Ollama HTTPError {e.code}: {txt[:800]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            "Failed to reach Ollama. Is it installed and running?\n"
            "Try: start Ollama, then `ollama list`.\n"
            f"Underlying error: {e}"
        ) from e


def _ollama_chat_stream(
    host: str,
    model: str,
    messages: List[Dict[str, str]],
    *,
    temperature: float,
    num_predict: int,
    allow_remote: bool = False,
    token_callback: Optional[Callable[[str], None]] = None,
) -> str:
    """
    Streaming Ollama /api/chat call.

    Yields tokens to `token_callback(token_str)` as they arrive.
    Returns the full assembled response string when done.

    Falls back to non-streaming if streaming fails.
    """
    _ensure_localhost(host, allow_remote=allow_remote)
    url = host.rstrip("/") + "/api/chat"

    payload = {
        "model": model,
        "messages": messages,
        "options": {
            "temperature": float(temperature),
            "num_predict": int(num_predict),
        },
        "stream": True,
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    full_text: List[str] = []

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                token = obj.get("message", {}).get("content", "")
                if token:
                    full_text.append(token)
                    if token_callback:
                        token_callback(token)
                if obj.get("done", False):
                    break
        return "".join(full_text)

    except Exception:
        # Fallback: non-streaming
        return _ollama_chat(
            host, model, messages,
            temperature=temperature,
            num_predict=num_predict,
            allow_remote=allow_remote,
        )


@dataclass
class LocalBackendSpec:
    key: str
    host: str
    model: str
    tags: Dict[str, float]
    default_temperature: float = 0.3
    default_max_tokens: int = 1400
    allow_remote: bool = False  # set True for Pi/network hosts

    def generate(
        self,
        *,
        developer_instructions: str,
        user_text: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        trace: bool = True,
        token_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        temp = self.default_temperature if temperature is None else float(temperature)
        mtok = self.default_max_tokens if max_tokens is None else int(max_tokens)

        if trace:
            print(
                f"[MODEL_CALL] backend={self.key} model={self.model} "
                f"host={self.host} temp={temp} max_tokens={mtok} "
                f"sys_len={len(developer_instructions)} user_len={len(user_text)}"
            )

        messages = [
            {"role": "system", "content": developer_instructions},
            {"role": "user", "content": user_text},
        ]

        if token_callback is not None:
            return _ollama_chat_stream(
                self.host, self.model, messages,
                temperature=temp, num_predict=mtok,
                allow_remote=self.allow_remote,
                token_callback=token_callback,
            )
        return _ollama_chat(
            self.host, self.model, messages,
            temperature=temp, num_predict=mtok,
            allow_remote=self.allow_remote,
        )


class BackendRegistry:
    def __init__(self):
        self._specs: Dict[str, LocalBackendSpec] = {}

    def register(self, spec: LocalBackendSpec) -> None:
        self._specs[spec.key] = spec

    def get(self, key: str) -> LocalBackendSpec:
        if key not in self._specs:
            raise KeyError(f"Backend not found: {key}")
        return self._specs[key]

    def best_for(self, *, weights: Dict[str, float], fallback_key: str) -> LocalBackendSpec:
        best_key = fallback_key
        best_score = float("-inf")
        for k, spec in self._specs.items():
            score = 0.0
            for cap, w in weights.items():
                score += float(w) * float(spec.tags.get(cap, 0.0))
            if score > best_score:
                best_key = k
                best_score = score
        return self._specs[best_key]


# ============================================================
# Pi / Remote node probing
# ============================================================

def _ollama_ps(host: str, timeout_s: int = 5) -> Optional[Dict[str, Any]]:
    url = host.rstrip("/") + "/api/ps"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def _ollama_tags(host: str, timeout_s: int = 5) -> Optional[Dict[str, Any]]:
    url = host.rstrip("/") + "/api/tags"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


@dataclass
class NodeStatus:
    host: str
    reachable: bool
    active_models: int
    installed_models: List[str]
    latency_ms: float
    active_model_names: List[str] = field(default_factory=list)


def probe_node(host: str, timeout_s: int = 5) -> NodeStatus:
    import time
    t0 = time.monotonic()
    ps = _ollama_ps(host, timeout_s=timeout_s)
    latency_ms = (time.monotonic() - t0) * 1000.0

    if ps is None:
        return NodeStatus(host=host, reachable=False, active_models=0,
                          installed_models=[], latency_ms=latency_ms)

    active_entries = ps.get("models", [])
    active = len(active_entries)
    active_names = [m.get("name", "") for m in active_entries if m.get("name")]

    tags = _ollama_tags(host, timeout_s=timeout_s)
    installed: List[str] = []
    if tags:
        installed = [m.get("name", "") for m in tags.get("models", []) if m.get("name")]

    return NodeStatus(
        host=host, reachable=True, active_models=active,
        installed_models=installed, latency_ms=latency_ms,
        active_model_names=active_names,
    )


class LoadAwareDispatcher:
    """
    Picks the best available Ollama host for a given model at call time.
    Caches probe results for `cache_ttl_s` seconds.
    """

    def __init__(self, hosts: List[str], cache_ttl_s: float = 10.0, probe_timeout_s: int = 4):
        self.hosts = list(hosts)
        self.cache_ttl_s = cache_ttl_s
        self.probe_timeout_s = probe_timeout_s
        self._cache: Dict[str, tuple] = {}

    def _get_status(self, host: str) -> NodeStatus:
        import time
        now = time.monotonic()
        cached = self._cache.get(host)
        if cached:
            status, ts = cached
            if now - ts < self.cache_ttl_s:
                return status
        status = probe_node(host, timeout_s=self.probe_timeout_s)
        self._cache[host] = (status, now)
        return status

    def probe_all(self) -> List[NodeStatus]:
        return [self._get_status(h) for h in self.hosts]

    def best_host_for(self, model: str) -> str:
        statuses = self.probe_all()
        reachable = [s for s in statuses if s.reachable]
        if not reachable:
            print(f"[DISPATCHER] No reachable hosts — falling back to localhost:11434")
            return "http://localhost:11434"
        has_model = [s for s in reachable if any(model in m for m in s.installed_models)]
        candidates = has_model if has_model else reachable
        candidates.sort(key=lambda s: (s.active_models, s.latency_ms))
        chosen = candidates[0].host
        print(
            f"[DISPATCHER] model={model} → {chosen} "
            f"(active={candidates[0].active_models}, latency={candidates[0].latency_ms:.0f}ms)"
        )
        return chosen

    def invalidate(self, host: Optional[str] = None) -> None:
        if host:
            self._cache.pop(host, None)
        else:
            self._cache.clear()


# ============================================================
# Conversation saving + role memory
# ============================================================

class ConversationStore:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def session_path(self, session_id: str) -> Path:
        return self.base_dir / f"{safe_name(session_id, 64)}.jsonl"

    def append(self, session_id: str, record: Dict[str, Any]) -> None:
        p = self.session_path(session_id)
        line = json.dumps(record, ensure_ascii=False)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def load_last(self, session_id: str, n: int = 12) -> List[Dict[str, Any]]:
        p = self.session_path(session_id)
        if not p.exists():
            return []
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        out: List[Dict[str, Any]] = []
        for ln in lines[-n:]:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
        return out

    def list_sessions(self) -> List[str]:
        """Return session IDs sorted newest-first."""
        files = sorted(self.base_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [p.stem for p in files]

    def load_session_summary(self, session_id: str, max_turns: int = 6) -> str:
        """
        Return a condensed text summary of a past session suitable for
        injecting into role context, keeping token count bounded.
        """
        turns = self.load_last(session_id, n=max_turns)
        if not turns:
            return ""
        lines = [f"[Past session: {session_id}]"]
        for t in turns:
            who = t.get("who", "?")
            text = t.get("text", "")
            # Truncate very long turns
            if len(text) > 400:
                text = text[:400] + "…"
            lines.append(f"{who}: {text}")
        return "\n".join(lines)


class RoleMemoryManager:
    def __init__(self, mem_dir: Path):
        self.mem_dir = mem_dir
        self.mem_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, role: str) -> Path:
        return self.mem_dir / f"memory_{safe_name(role.lower(), 64)}.md"

    def read(self, role: str) -> str:
        p = self.path_for(role)
        return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""

    def update(self, role: str, new_summary: str) -> None:
        p = self.path_for(role)
        p.write_text(new_summary.strip() + "\n", encoding="utf-8")

    def all_roles(self) -> List[str]:
        return [p.stem.replace("memory_", "") for p in self.mem_dir.glob("memory_*.md")]


# ============================================================
# Personalities  (improved system prompts)
# ============================================================

ROLE_PROMPTS: Dict[str, str] = {
    "judge": """\
You are the JUDGE of the Council — the final arbiter of quality.
Your only job is evaluation and routing. Never produce the actual answer yourself.

ROUTING: When given a user message, output EXACTLY one word — the role name — with no other text.
Valid route targets: writer | techpriest | intern | peasant | artist | apothecary | speech | librarian | ide

CRITIQUE: When critiquing a response, use EXACTLY this format and nothing else:
=== Judge Critique ===
Verdict: PASS | NEEDS_WORK
Findings:
- <finding 1>
- <finding 2>
(up to 5 findings)
Suggestions:
- <suggestion 1>
========================

RANKING: When ranking candidates, output ONLY valid JSON, no markdown fences:
{"winner":"<role>","scores":{"<role>":0..10,...},"rationale":"<one sentence>"}

Rules:
- Be ruthless about incomplete or unsafe answers.
- Prefer answers that are specific, runnable, and verifiable over vague prose.
- Catch missing error handling, hardcoded values, race conditions, security issues.
- If the verdict is NEEDS_WORK, the council deliberates again.
""",

    "writer": """\
You are the WRITER of the Council — the voice that the user actually hears.
You receive the full deliberation context and synthesize it into one clear, polished response.

Rules:
- Never repeat the deliberation back to the user. Only give the final answer.
- If code is needed: include ONE complete, runnable code block. No partial snippets.
- If a filename is needed: output a short safe snake_case name ending in .py
- Structure: brief intro sentence → answer/code → brief closing note if useful.
- Maximum length: be thorough but not bloated. Cut fluff.
- If council members disagreed, pick the best-supported position and note why briefly.
""",

    "techpriest": """\
You are the TECH-PRIEST of the Council — the engineer who makes things robust.
You think in systems, failure modes, and long-term maintainability.

For every answer you give, you MUST consider:
1. Edge cases and error handling (what happens when input is None, empty, or malformed?)
2. Security (no hardcoded secrets, no shell injection, no unchecked user input)
3. Performance (will this scale? any obvious bottlenecks?)
4. Maintainability (clear naming, minimal magic, easy to modify later)
5. Dependencies (fewest possible; prefer stdlib when reasonable)

Output format:
APPROACH: <one sentence summary of your recommended approach>
IMPLEMENTATION:
<code or detailed steps>
RISKS:
- <risk 1>
- <risk 2>
MUST-DO:
- <mandatory safeguard 1>
""",

    "intern": """\
You are the INTERN of the Council — fast, pragmatic, and unafraid to just try things.
You produce the first working draft. It doesn't have to be perfect, just functional and testable.

Rules:
- Get something working first. Optimize later.
- Keep it small: prefer 20-line solutions over 200-line ones when both work.
- Always include a simple test or usage example at the bottom.
- If you're unsure about something, make your assumption explicit with a comment.
- Output: working code or a concrete step-by-step plan. No essays.
""",

    "peasant": """\
You are the PEASANT of the Council — the voice of the confused user and the devil's advocate.
Your job is to ask the questions no one else is asking and catch hidden assumptions.

Rules:
- You MUST ask at least 2 clarifying questions per response. Format them as:
  Q1: <question>
  Q2: <question>
  Q3: <optional third question>
- After your questions, briefly note the ASSUMPTION you think is most dangerous.
- Use plain language. If you can't explain it simply, it's too complex.
- You are allowed (encouraged) to disagree with other council members.
- Never just accept the premise of the question at face value.
""",

    "artist": """\
You are the ARTIST of the Council — focused on user experience, clarity, and visual thinking.
You think about how things look, feel, and communicate to a human reader.

For any task you consider:
- Layout and information hierarchy (what should the user see first?)
- Naming and labeling (are labels intuitive or jargon-heavy?)
- Flow and sequence (does the UX make sense step by step?)
- Output formats (tables > lists > prose when comparing things; diagrams > text for processes)
- Error messages and user feedback (are they friendly and actionable?)

Output format:
UX ASSESSMENT: <one sentence on the biggest UX issue or strength>
RECOMMENDATION:
<your concrete suggestion — redesign, diagram, improved label set, etc.>
VISUAL STRUCTURE (if applicable):
<ASCII diagram or structured layout>
""",
}


@dataclass
class PersonalityModel:
    name: str
    system_prompt: str
    weights: Dict[str, float]
    registry: BackendRegistry
    backend_key: Optional[str] = None
    temperature: float = 0.3
    max_output_tokens: int = 1400
    memory_manager: Optional[RoleMemoryManager] = None
    conversation_store: Optional[ConversationStore] = None
    session_id: Optional[str] = None
    prior_session_id: Optional[str] = None  # inject context from a past session
    trace: bool = True

    def respond(
        self,
        user_text: str,
        *,
        extra_context: str = "",
        token_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        mem = self.memory_manager.read(self.name) if self.memory_manager else ""

        # Current session history
        history_txt = ""
        if self.conversation_store and self.session_id:
            turns = self.conversation_store.load_last(self.session_id, n=10)
            if turns:
                chunks = []
                for t in turns:
                    who = t.get("who", "unknown")
                    text = t.get("text", "")
                    chunks.append(f"{who}: {text}")
                history_txt = "\n".join(chunks)

        # Prior session context (cross-session memory)
        prior_txt = ""
        if self.conversation_store and self.prior_session_id:
            prior_txt = self.conversation_store.load_session_summary(
                self.prior_session_id, max_turns=6
            )

        prefix_parts = []
        if mem.strip():
            prefix_parts.append("ROLE MEMORY (maintain consistency):\n" + mem.strip())
        if prior_txt.strip():
            prefix_parts.append("PRIOR SESSION CONTEXT:\n" + prior_txt.strip())
        if history_txt.strip():
            prefix_parts.append("RECENT CONVERSATION:\n" + history_txt.strip())
        if extra_context.strip():
            prefix_parts.append("COUNCIL CONTEXT:\n" + extra_context.strip())

        stitched_user = "\n\n".join(prefix_parts + ["USER TASK:\n" + user_text])

        spec = (
            self.registry.get(self.backend_key)
            if self.backend_key
            else self.registry.best_for(weights=self.weights, fallback_key="local_fast")
        )

        return spec.generate(
            developer_instructions=self.system_prompt,
            user_text=stitched_user,
            temperature=self.temperature,
            max_tokens=self.max_output_tokens,
            trace=self.trace,
            token_callback=token_callback,
        )


# ============================================================
# Judge (routing + critique + ranking)
# ============================================================

# Routing scored by keyword presence — more robust than simple string matching
_ROUTE_PATTERNS: List[Tuple[str, List[str], int]] = [
    # (route, keywords, base_score)
    ("apothecary",  ["ssh", "node", "raspberry", "pi", "apothecary", "provision", "remote node", "deploy to"], 10),
    ("speech",      ["speech", "transcribe", "whisper", "microphone", "voice input", "dictate"],                10),
    ("librarian",   ["list vault", "show vault", "open vault", "browse vault", "commit vault",
                     "vault commit", "git commit", "read vault", "load from vault"],                            10),
    ("ide",         ["python", "script", "code", "function", "class ", "def ", "bug", "refactor",
                     "implement", "write a program", "write a script", "debug"],                               6),
    ("artist",      ["draw", "image", "diagram", "plot", "visual", "ui ", "ux ", "layout",
                     "interface", "wireframe", "chart"],                                                       6),
    ("peasant",     ["explain like", "eli5", "simple", "what is", "what does", "how does",
                     "what are", "for a beginner"],                                                            5),
    ("intern",      ["plan", "todo", "steps", "outline", "checklist", "what should i", "how to start"],       5),
    ("techpriest",  ["robust", "secure", "architecture", "design pattern", "engineer", "scalab",
                     "production", "best practice", "maintainab"],                                            5),
    ("writer",      [],                                                                                        1),  # default
]


def route_message(user_text: str) -> str:
    """Score-based routing — returns the winning route label."""
    t = user_text.lower()
    scores: Dict[str, int] = {}
    for route, keywords, base in _ROUTE_PATTERNS:
        score = base if not keywords else 0
        for kw in keywords:
            if kw in t:
                score += base
        if score > 0:
            scores[route] = scores.get(route, 0) + score

    if not scores:
        return "writer"
    return max(scores, key=lambda r: scores[r])


class JudgeModel(PersonalityModel):

    def route(self, user_text: str) -> str:
        return route_message(user_text)

    def critique(self, user_text: str, candidate_text: str, *, extra_context: str = "") -> str:
        prompt = (
            "Critique the candidate response using EXACTLY this format:\n"
            "=== Judge Critique ===\n"
            "Verdict: PASS | NEEDS_WORK\n"
            "Findings:\n"
            "- <finding>\n"
            "Suggestions:\n"
            "- <suggestion>\n"
            "========================\n\n"
            f"User request:\n{user_text}\n\n"
            f"Candidate response:\n{candidate_text}\n"
        )
        return self.respond(prompt, extra_context=extra_context)

    def rank_candidates(
        self,
        user_text: str,
        candidates: Dict[str, Dict[str, str]],
        *,
        extra_context: str = "",
    ) -> str:
        parts = [
            "Rank candidates. Output ONLY valid JSON (no markdown), format:\n"
            '{"winner":"<role>","scores":{"<role>":0..10},"rationale":"<one sentence>"}\n\n'
            f"USER REQUEST:\n{user_text}\n\nCANDIDATES:\n"
        ]
        for role, data in candidates.items():
            parts.append(
                f"--- {role} ---\nANSWER:\n{data.get('answer','')}\n\n"
                f"PEASANT QUESTIONS:\n{data.get('peasant_q','')}\n\n"
                f"REBUTTAL:\n{data.get('rebuttal','')}\n\n"
                f"DISCUSSION:\n{data.get('discussion','')}\n"
            )
        return self.respond("\n".join(parts), extra_context=extra_context)


# ============================================================
# Librarian (Vault + Logging)
# ============================================================

class Librarian:
    def __init__(self, vault_dir: Path, log_path: Path):
        self.vault_dir = vault_dir
        self.log_path = log_path
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log_event(self, who: str, text: str):
        append_log(str(self.log_path), f"[{now_iso()}] {who}: {text}")

    def snapshot_code(self, code_text: str, *, label: str = "council_code") -> Path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.vault_dir / f"{safe_name(label, 64)}_{ts}.py"
        path.write_text(code_text, encoding="utf-8")
        return path

    def ensure_git_repo(self) -> Tuple[bool, str]:
        if shutil.which("git") is None:
            return False, "git not found on PATH."
        git_dir = self.vault_dir / ".git"
        if git_dir.exists():
            return True, "git repo already present."
        p = subprocess.run(["git", "init"], cwd=str(self.vault_dir), capture_output=True, text=True)
        if p.returncode != 0:
            return False, p.stderr.strip() or "git init failed."
        return True, p.stdout.strip() or "git init OK."

    def git_commit_all(self, message: str) -> Tuple[bool, str]:
        ok, msg = self.ensure_git_repo()
        if not ok:
            return False, msg
        add = subprocess.run(["git", "add", "-A"], cwd=str(self.vault_dir), capture_output=True, text=True)
        if add.returncode != 0:
            return False, add.stderr.strip() or "git add failed."
        commit = subprocess.run(["git", "commit", "-m", message], cwd=str(self.vault_dir), capture_output=True, text=True)
        if commit.returncode != 0:
            out = (commit.stdout + "\n" + commit.stderr).strip()
            return False, out or "git commit failed."
        return True, commit.stdout.strip() or "commit OK."

    def save_text(self, name: str, content: str) -> Path:
        path = self.vault_dir / safe_name(name, 128)
        path.write_text(content, encoding="utf-8")
        return path

    def list_items(self) -> List[str]:
        items: List[str] = []
        for p in self.vault_dir.iterdir():
            if p.is_file() and p.name != ".gitignore":
                items.append(p.name)
        items.sort()
        return items

    def read_text(self, name: str) -> str:
        path = self.vault_dir / safe_name(name, 128)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Vault item not found: {path.name}")
        return path.read_text(encoding="utf-8", errors="replace")


# ============================================================
# Local IDE/Runner
# ============================================================

class LocalRunner:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)

    def run_code(
        self,
        code: str,
        *,
        filename_hint: str = "scratch.py",
        timeout_s: int = 120,
        env_extra: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, str, str, Path]:
        fname = safe_name(filename_hint, 64)
        if not fname.endswith(".py"):
            fname += ".py"
        path = self.workspace / fname
        path.write_text(code, encoding="utf-8")

        env = os.environ.copy()
        if env_extra:
            env.update(env_extra)

        p = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(self.workspace),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
        )
        return p.returncode, p.stdout, p.stderr, path

    def run_code_streaming(
        self,
        code: str,
        *,
        filename_hint: str = "scratch.py",
        timeout_s: int = 120,
        stdout_callback: Optional[Callable[[str], None]] = None,
        stderr_callback: Optional[Callable[[str], None]] = None,
    ) -> Tuple[int, Path]:
        """
        Run code and stream stdout/stderr line by line via callbacks.
        Returns (returncode, path).
        """
        import threading as _threading
        fname = safe_name(filename_hint, 64)
        if not fname.endswith(".py"):
            fname += ".py"
        path = self.workspace / fname
        path.write_text(code, encoding="utf-8")

        proc = subprocess.Popen(
            [sys.executable, "-u", str(path)],
            cwd=str(self.workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def _drain(stream, cb):
            for line in stream:
                if cb:
                    cb(line)
            stream.close()

        t_out = _threading.Thread(target=_drain, args=(proc.stdout, stdout_callback), daemon=True)
        t_err = _threading.Thread(target=_drain, args=(proc.stderr, stderr_callback), daemon=True)
        t_out.start()
        t_err.start()

        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        t_out.join()
        t_err.join()
        return proc.returncode, path


# ============================================================
# Speech-to-Text (Phase 3)
# ============================================================

class SpeechToText:
    def __init__(self, model_size: str = "base"):
        self.model_size = model_size
        self.model = None

    def ready(self) -> Tuple[bool, str]:
        if sd is None or sf is None:
            return False, "sounddevice/soundfile not installed."
        if WhisperModel is None:
            return False, "faster-whisper not installed."
        return True, "OK"

    def load(self) -> Tuple[bool, str]:
        ok, msg = self.ready()
        if not ok:
            return ok, msg
        if self.model is None:
            self.model = WhisperModel(
                self.model_size,
                device="cuda" if os.environ.get("CUDA_VISIBLE_DEVICES") else "cpu",
            )
        return True, "Loaded."

    def record_wav(self, out_path: Path, seconds: int = 5, samplerate: int = 16000) -> Tuple[bool, str]:
        ok, msg = self.ready()
        if not ok:
            return ok, msg
        audio = sd.rec(int(seconds * samplerate), samplerate=samplerate, channels=1, dtype="float32")
        sd.wait()
        sf.write(str(out_path), audio, samplerate)
        return True, f"Recorded {seconds}s to {out_path}"

    def transcribe(self, wav_path: Path) -> Tuple[bool, str]:
        ok, msg = self.load()
        if not ok:
            return ok, msg
        segments, _info = self.model.transcribe(str(wav_path))
        text = "".join([s.text for s in segments]).strip()
        return True, text


# ============================================================
# Backend config + registry + dispatcher
# ============================================================

DEFAULT_OLLAMA_HOST = os.environ.get("COUNCIL_OLLAMA_HOST", "http://localhost:11434")

_PI_HOSTS_RAW = os.environ.get("COUNCIL_PI_HOSTS", "")
DEFAULT_PI_HOSTS: List[str] = [h.strip() for h in _PI_HOSTS_RAW.split(",") if h.strip()]

DEFAULT_MODELS = {
    "general_primary": os.environ.get("COUNCIL_MODEL_GENERAL_PRIMARY", "llama3.1:8b"),
    "general_alt":     os.environ.get("COUNCIL_MODEL_GENERAL_ALT",     "mistral:7b"),
    "coder_primary":   os.environ.get("COUNCIL_MODEL_CODER_PRIMARY",   "qwen2.5-coder:7b"),
    "coder_fast":      os.environ.get("COUNCIL_MODEL_CODER_FAST",      "phi3.5"),
    "judge_fast":      os.environ.get("COUNCIL_MODEL_JUDGE_FAST",      "mistral:7b"),
    "peasant_fast":    os.environ.get("COUNCIL_MODEL_PEASANT_FAST",    "llama3.1:8b"),
}


def load_personality_pins(path: Path) -> Dict[str, str]:
    try:
        if path.exists():
            obj = json.loads(path.read_text(encoding="utf-8"))
            return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    return {}


def build_dispatcher(extra_hosts: Optional[List[str]] = None) -> LoadAwareDispatcher:
    hosts = [DEFAULT_OLLAMA_HOST] + (DEFAULT_PI_HOSTS if extra_hosts is None else extra_hosts)
    seen: set = set()
    unique: List[str] = []
    for h in hosts:
        if h not in seen:
            seen.add(h)
            unique.append(h)
    return LoadAwareDispatcher(unique)


class _DispatchedBackendSpec(LocalBackendSpec):
    """LocalBackendSpec that resolves its host at call time via LoadAwareDispatcher."""
    _dispatcher: LoadAwareDispatcher

    def generate(self, *, developer_instructions: str, user_text: str,
                 temperature: Optional[float] = None, max_tokens: Optional[int] = None,
                 trace: bool = True,
                 token_callback: Optional[Callable[[str], None]] = None) -> str:
        self.host = self._dispatcher.best_host_for(self.model)
        self.allow_remote = True
        return super().generate(
            developer_instructions=developer_instructions,
            user_text=user_text,
            temperature=temperature,
            max_tokens=max_tokens,
            trace=trace,
            token_callback=token_callback,
        )


def build_registry(dispatcher: Optional[LoadAwareDispatcher] = None) -> BackendRegistry:
    reg = BackendRegistry()

    def _spec(key: str, model_key: str, tags: Dict[str, float], temp: float, max_tok: int) -> LocalBackendSpec:
        if dispatcher is not None:
            spec = _DispatchedBackendSpec(
                key=key, host=DEFAULT_OLLAMA_HOST,
                model=DEFAULT_MODELS[model_key], tags=tags,
                default_temperature=temp, default_max_tokens=max_tok, allow_remote=True,
            )
            spec._dispatcher = dispatcher
            return spec
        return LocalBackendSpec(
            key=key, host=DEFAULT_OLLAMA_HOST,
            model=DEFAULT_MODELS[model_key], tags=tags,
            default_temperature=temp, default_max_tokens=max_tok, allow_remote=False,
        )

    reg.register(_spec("local_general_primary", "general_primary",
        {"general": 1.0, "reasoning": 0.9, "coding": 0.7, "latency": 0.55}, 0.35, 1800))
    reg.register(_spec("local_general_alt", "general_alt",
        {"general": 0.85, "reasoning": 0.8, "coding": 0.6, "latency": 0.9}, 0.55, 1500))
    reg.register(_spec("local_coder_primary", "coder_primary",
        {"general": 0.65, "reasoning": 0.85, "coding": 1.0, "latency": 0.5}, 0.18, 2200))
    reg.register(_spec("local_coder_fast", "coder_fast",
        {"general": 0.6, "reasoning": 0.75, "coding": 0.9, "latency": 0.95}, 0.25, 1600))
    reg.register(_spec("local_judge_fast", "judge_fast",
        {"general": 0.55, "reasoning": 0.95, "coding": 0.55, "latency": 1.0}, 0.12, 1300))
    reg.register(_spec("local_peasant_fast", "peasant_fast",
        {"general": 0.8, "reasoning": 0.6, "coding": 0.45, "latency": 0.9}, 0.35, 1200))
    reg.register(_spec("local_fast", "general_alt",
        {"general": 0.75, "reasoning": 0.7, "coding": 0.55, "latency": 1.0}, 0.4, 1400))

    return reg


def build_personalities(
    *,
    pins: Dict[str, str],
    vault_dir: Path,
    session_id: str,
    trace: bool = True,
    dispatcher: Optional[LoadAwareDispatcher] = None,
    prior_session_id: Optional[str] = None,
) -> Dict[str, PersonalityModel]:
    reg = build_registry(dispatcher=dispatcher)
    convo = ConversationStore(vault_dir / "conversations")
    memmgr = RoleMemoryManager(vault_dir / "memory")

    weights = {
        "judge":     {"reasoning": 0.7, "latency": 0.3},
        "writer":    {"general": 0.6, "reasoning": 0.4},
        "techpriest":{"coding": 0.7, "reasoning": 0.3},
        "intern":    {"coding": 0.5, "latency": 0.5},
        "peasant":   {"general": 0.6, "latency": 0.4},
        "artist":    {"general": 0.7, "reasoning": 0.3},
    }

    models: Dict[str, PersonalityModel] = {}

    for name in ("writer", "peasant", "intern", "techpriest", "artist"):
        models[name] = PersonalityModel(
            name=name,
            system_prompt=ROLE_PROMPTS[name],
            weights=weights[name],
            registry=reg,
            backend_key=pins.get(name),
            memory_manager=memmgr,
            conversation_store=convo,
            session_id=session_id,
            prior_session_id=prior_session_id,
            trace=trace,
        )

    models["judge"] = JudgeModel(
        name="judge",
        system_prompt=ROLE_PROMPTS["judge"],
        weights=weights["judge"],
        registry=reg,
        backend_key=pins.get("judge", "local_judge_fast"),
        temperature=0.1,
        max_output_tokens=1200,
        memory_manager=memmgr,
        conversation_store=convo,
        session_id=session_id,
        prior_session_id=prior_session_id,
        trace=trace,
    )

    # Default diversity pins
    defaults = {
        "writer": "local_general_primary",
        "techpriest": "local_coder_primary",
        "intern": "local_coder_fast",
        "peasant": "local_peasant_fast",
        "artist": "local_general_alt",
    }
    for role, default_key in defaults.items():
        if role not in pins:
            models[role].backend_key = default_key

    return models


def update_role_memory_after_pass(
    *,
    role_name: str,
    role_model: PersonalityModel,
    memory_manager: RoleMemoryManager,
    user_text: str,
    final_answer: str,
    judge_critique: str,
    max_bullets: int = 7,
) -> None:
    prompt = (
        f"You are updating your ROLE MEMORY for role '{role_name}'.\n"
        "Write a concise, durable memory for future tasks.\n"
        f"Use at most {max_bullets} bullet points.\n"
        "Focus on: what worked, what failed, what to always do, what to avoid.\n"
        "No long prose. Bullets only.\n\n"
        f"USER TASK:\n{user_text}\n\n"
        f"FINAL ANSWER:\n{final_answer}\n\n"
        f"JUDGE CRITIQUE:\n{judge_critique}\n"
    )
    summary = role_model.respond(prompt)
    memory_manager.update(role_name, summary)
