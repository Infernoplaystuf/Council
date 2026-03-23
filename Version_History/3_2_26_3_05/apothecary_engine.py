# ============================================================
# Conda env:
#   conda create -n council python=3.11 -y
#   conda activate council
# Optional (SSH): pip install paramiko
# ============================================================

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional, Tuple

# Optional dependency: Paramiko
try:
    import paramiko  # type: ignore
except Exception:
    paramiko = None


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_read_json(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default


def safe_write_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def append_log(path: str, line: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")


@dataclass
class NodeEntry:
    name: str
    host: str
    port: int = 22
    username: str = "pi"
    auth_method: str = "password"  # "password" or "key"
    password: str = ""            # may be blank if not stored
    key_path: str = ""
    notes: str = ""
    last_seen: str = ""
    created_at: str = ""


class NodeRegistry:
    def __init__(self, path: str):
        self.path = path
        self.data = safe_read_json(self.path, {"nodes": []})

    def list_nodes(self) -> list[NodeEntry]:
        nodes = []
        for d in self.data.get("nodes", []):
            try:
                nodes.append(NodeEntry(**d))
            except Exception:
                continue
        return nodes

    def upsert(self, entry: NodeEntry):
        entry_dict = asdict(entry)
        nodes = self.data.get("nodes", [])
        for i, d in enumerate(nodes):
            if d.get("name") == entry.name:
                nodes[i] = entry_dict
                break
        else:
            nodes.append(entry_dict)
        self.data["nodes"] = nodes
        safe_write_json(self.path, self.data)

    def delete(self, name: str):
        nodes = self.data.get("nodes", [])
        nodes = [d for d in nodes if d.get("name") != name]
        self.data["nodes"] = nodes
        safe_write_json(self.path, self.data)


class ApothecaryEngine:
    """
    Executes remote commands via Paramiko (supports password or key auth).
    """

    def __init__(self, registry: NodeRegistry, log_path: str):
        self.registry = registry
        self.log_path = log_path

    @staticmethod
    def require_paramiko():
        if paramiko is None:
            raise RuntimeError("Paramiko not installed. Install with: pip install paramiko")

    def test_connection(self, node: NodeEntry, password_override: str | None, timeout_s: int) -> tuple[bool, str]:
        try:
            rc, out, err = self.run_ssh(node, "echo OK", password_override, timeout_s)
            if rc == 0 and "OK" in out:
                return True, "Connected."
            return False, f"Nonzero/Unexpected output. rc={rc}, out={out.strip()}, err={err.strip()}"
        except Exception as e:
            return False, str(e)

    def run_ssh(self, node: NodeEntry, remote_command: str, password_override: str | None, timeout_s: int) -> tuple[int, str, str]:
        self.require_paramiko()

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        password = password_override if password_override is not None else node.password

        try:
            if node.auth_method == "key":
                if not node.key_path:
                    raise RuntimeError("Key auth selected but key_path is empty.")

                key = None
                for loader in (
                    getattr(paramiko, "RSAKey", None),
                    getattr(paramiko, "Ed25519Key", None),
                    getattr(paramiko, "ECDSAKey", None),
                    getattr(paramiko, "DSSKey", None),
                ):
                    if loader is None:
                        continue
                    try:
                        key = loader.from_private_key_file(node.key_path)  # type: ignore[attr-defined]
                        break
                    except Exception:
                        continue
                if key is None:
                    raise RuntimeError("Failed to load private key (unsupported format or bad key).")

                client.connect(
                    hostname=node.host,
                    port=node.port,
                    username=node.username,
                    pkey=key,
                    timeout=timeout_s,
                    auth_timeout=timeout_s,
                    banner_timeout=timeout_s,
                )
            else:
                if not password:
                    raise RuntimeError("Password auth selected but no password provided.")
                client.connect(
                    hostname=node.host,
                    port=node.port,
                    username=node.username,
                    password=password,
                    timeout=timeout_s,
                    auth_timeout=timeout_s,
                    banner_timeout=timeout_s,
                    look_for_keys=False,
                    allow_agent=False,
                )

            transport = client.get_transport()
            if transport is None or not transport.is_active():
                raise RuntimeError("SSH transport not active after connect.")

            chan = transport.open_session(timeout=timeout_s)
            chan.settimeout(timeout_s)
            chan.exec_command(remote_command)

            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []

            start = datetime.now().timestamp()
            while True:
                if chan.recv_ready():
                    stdout_chunks.append(chan.recv(4096).decode("utf-8", errors="replace"))
                if chan.recv_stderr_ready():
                    stderr_chunks.append(chan.recv_stderr(4096).decode("utf-8", errors="replace"))
                if chan.exit_status_ready():
                    break
                if datetime.now().timestamp() - start > timeout_s:
                    raise TimeoutError(f"Remote command timed out after {timeout_s} seconds.")
                import time
                time.sleep(0.05)

            rc = chan.recv_exit_status()
            stdout = "".join(stdout_chunks)
            stderr = "".join(stderr_chunks)

            append_log(self.log_path, f"[{now_iso()}] {node.name}@{node.host}: {remote_command} (rc={rc})")
            if stdout.strip():
                append_log(self.log_path, "STDOUT:\n" + stdout)
            if stderr.strip():
                append_log(self.log_path, "STDERR:\n" + stderr)

            return rc, stdout, stderr

        finally:
            try:
                client.close()
            except Exception:
                pass


# ============================================================
# Compatibility façade expected by council_gui_engine.py
# ============================================================

class Apothecary:
    """
    Convenience façade:
      - holds NodeRegistry + ApothecaryEngine
      - optionally strips passwords before persisting (store_passwords=False)
    """
    def __init__(self, *, registry_path: str, store_passwords: bool = True, log_path: Optional[str] = None):
        self.registry_path = registry_path
        self.store_passwords = store_passwords
        self.log_path = log_path or os.path.join(os.path.dirname(registry_path), "apothecary.log")
        self.registry = NodeRegistry(registry_path)
        self.engine = ApothecaryEngine(self.registry, self.log_path)

    def list_nodes(self) -> list[NodeEntry]:
        return self.registry.list_nodes()

    def upsert_node(self, entry: NodeEntry) -> None:
        if not entry.created_at:
            entry.created_at = now_iso()
        if not self.store_passwords:
            entry.password = ""
        self.registry.upsert(entry)

    def delete_node(self, name: str) -> None:
        self.registry.delete(name)

    def test(self, name: str, password_override: Optional[str] = None, timeout_s: int = 10) -> tuple[bool, str]:
        node = self._get(name)
        ok, msg = self.engine.test_connection(node, password_override, timeout_s)
        if ok:
            node.last_seen = now_iso()
            self.upsert_node(node)
        return ok, msg

    def run(self, name: str, cmd: str, password_override: Optional[str] = None, timeout_s: int = 30) -> tuple[int, str, str]:
        node = self._get(name)
        rc, out, err = self.engine.run_ssh(node, cmd, password_override, timeout_s)
        node.last_seen = now_iso()
        self.upsert_node(node)
        return rc, out, err

    def _get(self, name: str) -> NodeEntry:
        for n in self.registry.list_nodes():
            if n.name == name:
                return n
        raise KeyError(f"Node not found: {name}")


# ============================================================
# Minimal Tkinter console widget (optional, but used by GUI)
# ============================================================

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, simpledialog
except Exception:
    tk = None
    ttk = None
    messagebox = None
    simpledialog = None


if tk is not None:
    class ApothecaryConsole(ttk.Frame):
        def __init__(self, parent, apothecary: Apothecary, ui_queue=None):
            super().__init__(parent)
            self.apoth = apothecary
            self.ui_queue = ui_queue
            self._build()

        def _emit(self, text: str):
            if self.ui_queue is not None:
                self.ui_queue.put(("apoth_out", text))
            else:
                # fallback: print
                print(text)

        def _build(self):
            top = ttk.Frame(self)
            top.pack(fill="both", expand=True, padx=10, pady=10)

            left = ttk.Frame(top)
            left.pack(side="left", fill="both", expand=True)

            right = ttk.Frame(top)
            right.pack(side="right", fill="y")

            ttk.Label(left, text="Registered Nodes").pack(anchor="w")
            self.lb = tk.Listbox(left, height=18)
            self.lb.pack(fill="both", expand=True)

            btns = ttk.Frame(right)
            btns.pack(fill="x")

            ttk.Button(btns, text="Refresh", command=self._refresh).pack(fill="x")
            ttk.Button(btns, text="Add / Edit", command=self._add_edit).pack(fill="x", pady=4)
            ttk.Button(btns, text="Delete", command=self._delete).pack(fill="x")
            ttk.Separator(right).pack(fill="x", pady=10)
            ttk.Button(right, text="Test Connection", command=self._test).pack(fill="x")
            ttk.Button(right, text="Run Command", command=self._run_cmd).pack(fill="x", pady=4)

            self._refresh()

        def _selected(self) -> Optional[str]:
            sel = self.lb.curselection()
            if not sel:
                return None
            return self.lb.get(sel[0]).split(" | ", 1)[0].strip()

        def _refresh(self):
            self.lb.delete(0, "end")
            for n in self.apoth.list_nodes():
                self.lb.insert("end", f"{n.name} | {n.username}@{n.host}:{n.port} | {n.auth_method}")
            self._emit("Apothecary: registry refreshed.")

        def _add_edit(self):
            name = simpledialog.askstring("Node Name", "Name (unique):", parent=self)
            if not name:
                return

            # If exists, prefill
            existing = None
            for n in self.apoth.list_nodes():
                if n.name == name:
                    existing = n
                    break

            host = simpledialog.askstring("Host", "Host/IP:", initialvalue=(existing.host if existing else ""), parent=self)
            if not host:
                return

            port_s = simpledialog.askstring("Port", "Port:", initialvalue=str(existing.port if existing else 22), parent=self)
            try:
                port = int(port_s) if port_s else 22
            except Exception:
                messagebox.showerror("Error", "Port must be an integer.")
                return

            user = simpledialog.askstring("Username", "SSH username:", initialvalue=(existing.username if existing else "pi"), parent=self)
            if not user:
                return

            auth = simpledialog.askstring(
                "Auth Method",
                "Auth method: 'password' or 'key'",
                initialvalue=(existing.auth_method if existing else "password"),
                parent=self,
            )
            auth = (auth or "password").strip().lower()
            if auth not in ("password", "key"):
                messagebox.showerror("Error", "Auth method must be 'password' or 'key'.")
                return

            password = ""
            key_path = ""

            if auth == "password":
                password = simpledialog.askstring(
                    "Password",
                    "Password (leave blank to keep existing / or not store):",
                    show="*",
                    parent=self,
                ) or ""
                if existing and not password:
                    password = existing.password
            else:
                key_path = simpledialog.askstring(
                    "Key Path",
                    "Path to private key file:",
                    initialvalue=(existing.key_path if existing else ""),
                    parent=self,
                ) or ""
                if not key_path:
                    messagebox.showerror("Error", "Key path required for key auth.")
                    return

            notes = simpledialog.askstring("Notes", "Notes (optional):", initialvalue=(existing.notes if existing else ""), parent=self) or ""

            entry = NodeEntry(
                name=name,
                host=host,
                port=port,
                username=user,
                auth_method=auth,
                password=password,
                key_path=key_path,
                notes=notes,
                last_seen=(existing.last_seen if existing else ""),
                created_at=(existing.created_at if existing else ""),
            )

            self.apoth.upsert_node(entry)
            self._refresh()
            self._emit(f"Apothecary: upserted node '{name}'.")

        def _delete(self):
            name = self._selected()
            if not name:
                return
            if messagebox.askyesno("Confirm", f"Delete node '{name}'?"):
                self.apoth.delete_node(name)
                self._refresh()
                self._emit(f"Apothecary: deleted node '{name}'.")

        def _test(self):
            name = self._selected()
            if not name:
                return
            pw = simpledialog.askstring("Password Override", "Password override (blank = use stored):", show="*", parent=self)
            pw = pw if (pw is not None and pw.strip() != "") else None
            ok, msg = self.apoth.test(name, password_override=pw, timeout_s=10)
            self._emit(f"Apothecary test {name}: {'OK' if ok else 'FAIL'} - {msg}")

        def _run_cmd(self):
            name = self._selected()
            if not name:
                return
            cmd = simpledialog.askstring("Remote Command", "Command to run:", parent=self)
            if not cmd:
                return
            pw = simpledialog.askstring("Password Override", "Password override (blank = use stored):", show="*", parent=self)
            pw = pw if (pw is not None and pw.strip() != "") else None
            try:
                rc, out, err = self.apoth.run(name, cmd, password_override=pw, timeout_s=30)
                self._emit(f"Apothecary run {name}: rc={rc}\n--- stdout ---\n{out}\n--- stderr ---\n{err}")
            except Exception as e:
                self._emit(f"Apothecary run ERROR: {e}")