# ============================================================
# Conda env:
#   conda create -n council python=3.11 -y
#   conda activate council
# ============================================================

from __future__ import annotations

from typing import Dict, Any, Tuple
import json
import textwrap

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