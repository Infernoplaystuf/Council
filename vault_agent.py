# ============================================================
# vault_agent.py  —  Council Vault Agent
# ============================================================
# A ReAct-loop file agent that operates EXCLUSIVELY within
# the Council vault directory. It cannot read, write, delete,
# or access anything outside VAULT_DIR — the sandbox is
# enforced on every operation regardless of what the model says.
#
# Uses the same PersonalityModel interface as Coder/Intern
# so it runs on whatever local models Council already has loaded.
# No external API calls, no internet access, no subprocess.
#
# Tools available to the agent:
#   list_files    — list files in a vault subdirectory
#   read_file     — read a vault file (text or partial)
#   write_file    — write/append to a vault file
#   delete_file   — delete a vault file (requires confirm=true)
#   create_dir    — create a directory inside vault
#   search_vault  — keyword search across all vault text files
#   move_file     — move/rename within vault
#   done          — signal task complete, return final answer
# ============================================================

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


# ============================================================
# Sandbox enforcement
# ============================================================

class SandboxViolation(Exception):
    pass


def _safe_resolve(vault_dir: Path, raw_path: str) -> Path:
    """
    Resolve raw_path relative to vault_dir.
    Raises SandboxViolation if the resolved path escapes the vault.
    Strips leading slashes / drive letters so the model can't root-escape.
    """
    # Strip absolute prefixes so the model can use paths like "docs/file.txt"
    # or "/docs/file.txt" without escaping
    stripped = raw_path.lstrip("/\\")
    # Remove Windows-style drive letters
    stripped = re.sub(r"^[A-Za-z]:\\?", "", stripped)
    # Resolve relative to vault
    candidate = (vault_dir / stripped).resolve()
    vault_resolved = vault_dir.resolve()
    try:
        candidate.relative_to(vault_resolved)
    except ValueError:
        raise SandboxViolation(
            f"Path '{raw_path}' resolves outside the vault. "
            "The agent can only operate within the vault directory."
        )
    return candidate


# ============================================================
# Tool definitions
# ============================================================

MAX_READ_BYTES = 12_000   # chars returned to model per read
MAX_LIST_FILES = 80       # max files shown in list_files
BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
    ".mp3", ".mp4", ".wav", ".ogg", ".flac",
    ".zip", ".tar", ".gz", ".7z",
    ".pdf", ".db", ".sqlite", ".bin", ".pkl", ".npy",
    ".chromadb",
}


@dataclass
class ToolResult:
    ok: bool
    output: str
    metadata: Dict[str, Any] = field(default_factory=dict)


class VaultTools:
    """
    All file operations available to the agent.
    Every method enforces the vault sandbox.
    """

    def __init__(self, vault_dir: Path, confirm_deletes: bool = True):
        self.vault_dir       = vault_dir.resolve()
        self.confirm_deletes = confirm_deletes
        self._pending_delete: Optional[Path] = None

    # ── list_files ────────────────────────────────────────────

    def list_files(self, path: str = "", recursive: bool = False) -> ToolResult:
        """
        List files and directories at path (relative to vault).
        If path is empty, lists the vault root.
        """
        try:
            target = _safe_resolve(self.vault_dir, path) if path else self.vault_dir
            if not target.exists():
                return ToolResult(False, f"Path does not exist: {path!r}")
            if not target.is_dir():
                return ToolResult(False, f"Not a directory: {path!r}")

            if recursive:
                items = sorted(target.rglob("*"))
            else:
                items = sorted(target.iterdir())

            lines = []
            for item in items[:MAX_LIST_FILES]:
                rel = item.relative_to(self.vault_dir)
                tag = "/" if item.is_dir() else ""
                size = f"  ({item.stat().st_size:,} bytes)" if item.is_file() else ""
                lines.append(f"  {rel}{tag}{size}")

            if not lines:
                lines = ["  (empty)"]

            truncated = ""
            if len(list(target.iterdir())) > MAX_LIST_FILES:
                truncated = f"\n  ... (showing first {MAX_LIST_FILES})"

            base_rel = str(target.relative_to(self.vault_dir)) if target != self.vault_dir else "(vault root)"
            return ToolResult(True, f"Contents of {base_rel}:\n" + "\n".join(lines) + truncated)
        except SandboxViolation as e:
            return ToolResult(False, str(e))
        except Exception as e:
            return ToolResult(False, f"list_files error: {e}")

    # ── read_file ─────────────────────────────────────────────

    def read_file(self, path: str, offset: int = 0) -> ToolResult:
        """
        Read a vault text file. Returns up to MAX_READ_BYTES characters.
        Use offset to page through large files.
        Binary files are refused with a helpful message.
        """
        try:
            target = _safe_resolve(self.vault_dir, path)
            if not target.exists():
                return ToolResult(False, f"File not found: {path!r}")
            if not target.is_file():
                return ToolResult(False, f"Not a file: {path!r}")
            if target.suffix.lower() in BINARY_EXTENSIONS:
                return ToolResult(False,
                    f"Binary file ({target.suffix}) — cannot read as text. "
                    "Use list_files to see what's in the directory instead.")

            content = target.read_text(encoding="utf-8", errors="replace")
            total = len(content)
            chunk = content[offset: offset + MAX_READ_BYTES]
            more  = total - offset - len(chunk)
            note  = f"\n[... {more} more chars — use offset={offset+len(chunk)}]" if more > 0 else ""

            rel = str(target.relative_to(self.vault_dir))
            header = f"=== {rel} ({total} chars)"
            if offset:
                header += f" [offset {offset}]"
            header += " ===\n"

            return ToolResult(True, header + chunk + note,
                              metadata={"path": rel, "total": total, "offset": offset})
        except SandboxViolation as e:
            return ToolResult(False, str(e))
        except Exception as e:
            return ToolResult(False, f"read_file error: {e}")

    # ── write_file ────────────────────────────────────────────

    def write_file(self, path: str, content: str, append: bool = False) -> ToolResult:
        """
        Write content to a vault file.
        If append=True, adds to the end of an existing file.
        Creates parent directories as needed.
        """
        try:
            target = _safe_resolve(self.vault_dir, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            mode   = "a" if append else "w"
            action = "Appended to" if append else "Wrote"
            with open(target, mode, encoding="utf-8") as f:
                f.write(content)
            rel = str(target.relative_to(self.vault_dir))
            return ToolResult(True,
                f"{action} {rel} ({len(content)} chars, {target.stat().st_size} bytes total).")
        except SandboxViolation as e:
            return ToolResult(False, str(e))
        except Exception as e:
            return ToolResult(False, f"write_file error: {e}")

    # ── delete_file ───────────────────────────────────────────

    def delete_file(self, path: str, confirm: bool = False) -> ToolResult:
        """
        Delete a vault file.
        MUST pass confirm=true — otherwise returns a warning asking for confirmation.
        This two-step prevents accidental deletions.
        """
        try:
            target = _safe_resolve(self.vault_dir, path)
            rel    = str(target.relative_to(self.vault_dir))

            if not target.exists():
                return ToolResult(False, f"File not found: {path!r}")

            if not confirm:
                self._pending_delete = target
                size = target.stat().st_size
                return ToolResult(False,
                    f"Confirm required to delete '{rel}' ({size:,} bytes). "
                    "Call delete_file again with confirm=true to proceed.")

            if target.is_dir():
                return ToolResult(False,
                    f"'{rel}' is a directory. Cannot delete directories through the agent.")

            target.unlink()
            self._pending_delete = None
            return ToolResult(True, f"Deleted: {rel}")
        except SandboxViolation as e:
            return ToolResult(False, str(e))
        except Exception as e:
            return ToolResult(False, f"delete_file error: {e}")

    # ── move_file ─────────────────────────────────────────────

    def move_file(self, src: str, dst: str) -> ToolResult:
        """Move or rename a file within the vault."""
        try:
            src_path = _safe_resolve(self.vault_dir, src)
            dst_path = _safe_resolve(self.vault_dir, dst)
            if not src_path.exists():
                return ToolResult(False, f"Source not found: {src!r}")
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            src_path.rename(dst_path)
            rel_src = str(src_path.relative_to(self.vault_dir))
            rel_dst = str(dst_path.relative_to(self.vault_dir))
            return ToolResult(True, f"Moved: {rel_src}  →  {rel_dst}")
        except SandboxViolation as e:
            return ToolResult(False, str(e))
        except Exception as e:
            return ToolResult(False, f"move_file error: {e}")

    # ── create_dir ────────────────────────────────────────────

    def create_dir(self, path: str) -> ToolResult:
        """Create a directory (and any parents) inside the vault."""
        try:
            target = _safe_resolve(self.vault_dir, path)
            target.mkdir(parents=True, exist_ok=True)
            rel = str(target.relative_to(self.vault_dir))
            return ToolResult(True, f"Directory ready: {rel}/")
        except SandboxViolation as e:
            return ToolResult(False, str(e))
        except Exception as e:
            return ToolResult(False, f"create_dir error: {e}")

    # ── search_vault ──────────────────────────────────────────

    def search_vault(self, query: str, path: str = "", case_sensitive: bool = False) -> ToolResult:
        """
        Keyword search across vault text files.
        Returns file paths and matching lines (up to 5 lines per file).
        """
        try:
            root = _safe_resolve(self.vault_dir, path) if path else self.vault_dir
            q    = query if case_sensitive else query.lower()
            results: List[str] = []
            scanned = 0

            for fpath in sorted(root.rglob("*")):
                if not fpath.is_file():
                    continue
                if fpath.suffix.lower() in BINARY_EXTENSIONS:
                    continue
                try:
                    text  = fpath.read_text(encoding="utf-8", errors="replace")
                    lines = text.splitlines()
                    hits  = []
                    for i, line in enumerate(lines, 1):
                        target_line = line if case_sensitive else line.lower()
                        if q in target_line:
                            hits.append(f"    line {i}: {line[:120]}")
                        if len(hits) >= 5:
                            hits.append("    ...")
                            break
                    if hits:
                        rel = str(fpath.relative_to(self.vault_dir))
                        results.append(f"  {rel}:\n" + "\n".join(hits))
                    scanned += 1
                except Exception:
                    continue

            if not results:
                return ToolResult(True,
                    f"No matches for {query!r} in {scanned} files scanned.")
            return ToolResult(True,
                f"Found {len(results)} file(s) matching {query!r} "
                f"(scanned {scanned} text files):\n\n" + "\n\n".join(results[:20]))
        except SandboxViolation as e:
            return ToolResult(False, str(e))
        except Exception as e:
            return ToolResult(False, f"search_vault error: {e}")

    # ── dispatch ──────────────────────────────────────────────

    def dispatch(self, tool_name: str, args: Dict[str, Any]) -> ToolResult:
        """Route a tool call from the model to the right method."""
        dispatch_map = {
            "list_files":   lambda a: self.list_files(
                a.get("path", ""), a.get("recursive", False)),
            "read_file":    lambda a: self.read_file(
                a["path"], int(a.get("offset", 0))),
            "write_file":   lambda a: self.write_file(
                a["path"], a["content"], bool(a.get("append", False))),
            "delete_file":  lambda a: self.delete_file(
                a["path"], bool(a.get("confirm", False))),
            "move_file":    lambda a: self.move_file(a["src"], a["dst"]),
            "create_dir":   lambda a: self.create_dir(a["path"]),
            "search_vault": lambda a: self.search_vault(
                a["query"], a.get("path", ""), bool(a.get("case_sensitive", False))),
        }
        handler = dispatch_map.get(tool_name)
        if not handler:
            return ToolResult(False, f"Unknown tool: {tool_name!r}. "
                f"Available: {list(dispatch_map.keys())}")
        try:
            return handler(args)
        except KeyError as e:
            return ToolResult(False, f"Missing required argument for {tool_name}: {e}")
        except Exception as e:
            return ToolResult(False, f"Tool error ({tool_name}): {e}")


# ============================================================
# System prompt
# ============================================================

SYSTEM_PROMPT = """\
You are the Vault Agent — a file management assistant for the Council AI system.
You operate EXCLUSIVELY within the vault directory. You cannot access, read, write,
or delete anything outside the vault. This is a hard constraint.

You have access to these tools. Always use them by emitting a JSON tool call.

TOOLS:
  list_files    path (optional), recursive (optional bool)
  read_file     path (required), offset (optional int)
  write_file    path (required), content (required), append (optional bool)
  delete_file   path (required), confirm (required bool — must be true to actually delete)
  move_file     src (required), dst (required)
  create_dir    path (required)
  search_vault  query (required), path (optional), case_sensitive (optional bool)
  done          answer (required string — your final response to the user)

FORMAT: To use a tool, output EXACTLY this and nothing else on that turn:
<tool>
{
  "tool": "tool_name",
  "args": { ... }
}
</tool>

RULES:
- Always start by listing or searching to understand what exists before writing.
- Never overwrite important files without reading them first.
- For delete_file, always call it once without confirm to see the warning, then confirm.
- If a task is complete or you cannot proceed, call done with your answer.
- Paths are relative to the vault root. Never use absolute paths or "../".
- Keep write_file content clean and well-formatted.
- Maximum 20 tool calls per task to prevent runaway loops.
"""

TOOL_CALL_RE = re.compile(r"<tool>\s*(\{.*?\})\s*</tool>", re.DOTALL)


# ============================================================
# ReAct loop
# ============================================================

@dataclass
class AgentStep:
    kind: str          # "thought" | "tool_call" | "tool_result" | "done" | "error"
    content: str
    tool_name: str = ""
    tool_args: Dict = field(default_factory=dict)
    ok: bool = True
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class VaultAgent:
    """
    ReAct agent that operates on vault files using the Council's local models.
    Safe to run: cannot touch anything outside VAULT_DIR.

    Usage:
        agent = VaultAgent(personality_model, vault_dir)
        for step in agent.run(task):
            print(step.kind, step.content)
    """

    MAX_STEPS = 20

    def __init__(
        self,
        personality_model: Any,   # council_engine.PersonalityModel
        vault_dir: Path,
        event_callback: Optional[Callable[[str, str], None]] = None,
    ):
        self.model     = personality_model
        self.tools     = VaultTools(vault_dir)
        self.vault_dir = vault_dir
        self.event_cb  = event_callback  # (phase, message)

    def _emit(self, phase: str, msg: str):
        if self.event_cb:
            try:
                self.event_cb(phase, msg)
            except Exception:
                pass

    def run(self, task: str) -> List[AgentStep]:
        """
        Execute the task. Returns list of AgentSteps (thoughts, calls, results).
        This is synchronous — call from a background thread if using in the GUI.
        """
        steps: List[AgentStep] = []
        messages: List[Dict[str, str]] = []

        # Build vault context summary for the first message
        vault_context = self._vault_summary()
        first_message = (
            f"VAULT CONTEXT:\n{vault_context}\n\n"
            f"TASK: {task}"
        )
        messages.append({"role": "user", "content": first_message})

        self._emit("agent_start", f"Vault Agent starting: {task[:80]}")

        for step_num in range(self.MAX_STEPS):
            # ── Model call ──────────────────────────────────────
            self._emit("thinking", f"Step {step_num + 1}/{self.MAX_STEPS}")
            try:
                response = self.model.respond(
                    messages[-1]["content"] if messages[-1]["role"] == "user"
                    else "[continue]",
                    extra_context="\n".join(
                        f"{m['role'].upper()}: {m['content']}"
                        for m in messages[:-1]
                    ) if len(messages) > 1 else "",
                    max_tokens=600,
                )
            except Exception as e:
                err_step = AgentStep(kind="error", content=f"Model call failed: {e}", ok=False)
                steps.append(err_step)
                self._emit("error", str(e))
                break

            # ── Parse response ──────────────────────────────────
            match = TOOL_CALL_RE.search(response)

            if not match:
                # No tool call — treat as thought/observation
                thought = AgentStep(kind="thought", content=response.strip())
                steps.append(thought)
                self._emit("thought", response.strip()[:120])
                # Prompt model to emit a tool call
                messages.append({"role": "assistant", "content": response})
                messages.append({"role": "user",
                    "content": "Please emit a tool call to continue, or call done{} if complete."})
                continue

            # ── Execute tool call ───────────────────────────────
            raw_json = match.group(1)
            try:
                parsed    = json.loads(raw_json)
                tool_name = parsed.get("tool", "")
                tool_args = parsed.get("args", {})
            except json.JSONDecodeError as e:
                err = AgentStep(kind="error",
                    content=f"Invalid tool JSON: {e}\nRaw: {raw_json[:200]}", ok=False)
                steps.append(err)
                messages.append({"role": "assistant", "content": response})
                messages.append({"role": "user",
                    "content": f"JSON parse error: {e}. Please fix your tool call format."})
                continue

            call_step = AgentStep(kind="tool_call", content=raw_json,
                                   tool_name=tool_name, tool_args=tool_args)
            steps.append(call_step)
            self._emit("tool_call", f"→ {tool_name}({', '.join(f'{k}={repr(v)[:40]}' for k,v in tool_args.items())})")

            # ── done ────────────────────────────────────────────
            if tool_name == "done":
                answer = tool_args.get("answer", response)
                done_step = AgentStep(kind="done", content=answer)
                steps.append(done_step)
                self._emit("done", answer[:120])
                return steps

            # ── dispatch to VaultTools ──────────────────────────
            result = self.tools.dispatch(tool_name, tool_args)
            result_step = AgentStep(
                kind="tool_result",
                content=result.output,
                tool_name=tool_name,
                ok=result.ok,
            )
            steps.append(result_step)
            status = "OK" if result.ok else "FAILED"
            self._emit("tool_result", f"[{status}] {result.output[:120]}")

            # Feed result back into conversation
            messages.append({"role": "assistant", "content": response})
            messages.append({"role": "user",
                "content": f"Tool result [{status}]:\n{result.output}"})

        # ── Max steps reached ───────────────────────────────────
        timeout_step = AgentStep(kind="error",
            content=f"Reached maximum steps ({self.MAX_STEPS}). Task may be incomplete.",
            ok=False)
        steps.append(timeout_step)
        self._emit("error", "Max steps reached.")
        return steps

    def _vault_summary(self) -> str:
        """Quick summary of vault root for the model's orientation."""
        try:
            items = sorted(self.vault_dir.iterdir())
            lines = []
            for item in items[:30]:
                tag = "/" if item.is_dir() else f" ({item.stat().st_size:,}B)"
                lines.append(f"  {item.name}{tag}")
            return "vault/\n" + "\n".join(lines) + (
                f"\n  ... ({len(items)-30} more)" if len(items) > 30 else ""
            )
        except Exception:
            return "(vault contents unavailable)"
