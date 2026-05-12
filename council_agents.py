# ============================================================
# Conda env:
#   conda create -n council python=3.11 -y
#   conda activate council
# ============================================================

from __future__ import annotations

from typing import Dict, Any, Tuple
import csv
import json
import textwrap
from pathlib import Path

import council_engine as ce
from agent_core import ModelAgent, ToolFn


# -----------------------------
# Tools
# -----------------------------
def make_run_python_tool(runner: ce.LocalRunner) -> ToolFn:
    def _tool(args: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        code = str(args.get("code", ""))
        if not code.strip():
            return False, "No code provided.", {}

        filename = str(args.get("filename", "scratch.py"))
        timeout_s = int(args.get("timeout_s", 120))

        rc, out, err, path = runner.run_code(code, filename_hint=filename, timeout_s=timeout_s)
        msg = f"rc={rc}\n--- stdout ---\n{out}\n--- stderr ---\n{err}\nfile={path}"
        payload = {"rc": rc, "stdout": out, "stderr": err, "path": str(path)}
        return True, msg, payload

    return _tool


def make_read_file_tool() -> ToolFn:
    def _tool(args: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        path_str = str(args.get("path", "")).strip()
        if not path_str:
            return False, "Provide {'path': '/absolute/path/to/file'}", {}
        p = Path(path_str)
        if not p.exists():
            return False, f"File not found: {path_str}", {}
        if not p.is_file():
            return False, f"Not a file: {path_str}", {}
        try:
            suffix = p.suffix.lower()
            if suffix == ".csv":
                rows = []
                with open(p, newline="", encoding="utf-8", errors="replace") as fh:
                    reader = csv.reader(fh)
                    for i, row in enumerate(reader):
                        rows.append(", ".join(row))
                        if i >= 200:
                            rows.append("… (truncated after 200 rows)")
                            break
                content = "\n".join(rows)
            else:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    content = fh.read(40_000)
                if len(content) == 40_000:
                    content += "\n… (truncated)"
            return True, content, {"path": str(p), "bytes": p.stat().st_size}
        except Exception as exc:
            return False, f"Error reading file: {exc}", {}
    return _tool


def make_vault_save_tool(librarian: ce.Librarian) -> ToolFn:
    def _tool(args: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        name = str(args.get("name", "note.txt"))
        content = str(args.get("content", ""))
        if not content:
            return False, "No content provided.", {}
        librarian.save(name, content)
        return True, f"Saved to vault as '{name}'.", {"name": name, "bytes": len(content.encode("utf-8"))}
    return _tool


def make_vault_list_tool(librarian: ce.Librarian) -> ToolFn:
    def _tool(args: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        items = librarian.list_items()
        text = "\n".join(items) if items else "(empty)"
        return True, text, {"items": items}
    return _tool


def make_vault_read_tool(librarian: ce.Librarian) -> ToolFn:
    def _tool(args: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        name = str(args.get("name", ""))
        if not name:
            return False, "Provide {'name': 'file_in_vault.txt'}", {}
        data = librarian.read(name)
        return True, data, {"name": name, "bytes": len(data.encode("utf-8"))}
    return _tool


# -----------------------------
# Agent factory
# -----------------------------
def build_agents(*, runner: ce.LocalRunner, librarian: ce.Librarian, enable_tools: bool) -> Dict[str, ModelAgent]:
    tools = {
        "run_python": make_run_python_tool(runner),
        "read_file":  make_read_file_tool(),
        "vault_save": make_vault_save_tool(librarian),
        "vault_list": make_vault_list_tool(librarian),
        "vault_read": make_vault_read_tool(librarian),
    }

    # Under "Enable Tools", only empower the agents that should actually poke reality.
    # Writer stays mostly synth; Coder and Intern are your primary operators.
    writer = ModelAgent("writer", ce.WriterModel(), enable_tools=False)
    peasant = ModelAgent("peasant", ce.PeasantModel(), enable_tools=False)
    artist = ModelAgent("artist", ce.ArtistModel(), enable_tools=False)

    intern = ModelAgent(
        "intern",
        ce.InternModel(),
        tools=tools,
        enable_tools=enable_tools,
        max_tool_steps=3,
    )
    coder = ModelAgent(
        "coder",
        ce.CoderModel(),
        tools=tools,
        enable_tools=enable_tools,
        max_tool_steps=3,
    )

    return {
        "writer": writer,
        "peasant": peasant,
        "intern": intern,
        "coder": coder,
        "artist": artist,
    }