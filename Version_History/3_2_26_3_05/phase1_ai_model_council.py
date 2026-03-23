# phase1_ai_model_council_paramiko.py
# Phase 1: Apothecary + Judge + Writer
# Upgraded: Apothecary SSH uses Paramiko so per-node PASSWORD auth works inside the GUI.
#
# SECURITY NOTE:
# - Passwords are stored in plaintext in the registry JSON by default (fast, but risky).
# - You can disable password saving by toggling STORE_PASSWORDS = False (then it asks each time).
#
# Run:
#   pip install paramiko
#   python phase1_ai_model_council_paramiko.py

import json
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog

# ---- Optional dependency: Paramiko ----
try:
    import paramiko
except Exception:
    paramiko = None


# ============================
# CONFIG: Security + Defaults
# ============================

STORE_PASSWORDS = True   # If False, Apothecary asks for password at execution time (per command).


# ----------------------------
# Utilities / Persistence
# ----------------------------

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
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

def append_log(path: str, line: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")


# ----------------------------
# Node Registry (Apothecary)
# ----------------------------

@dataclass
class NodeEntry:
    name: str
    host: str
    port: int = 22
    username: str = "pi"
    auth_method: str = "password"  # "password" or "key"
    password: str = ""            # stored only if STORE_PASSWORDS True
    key_path: str = ""            # private key path if auth_method="key"
    notes: str = ""
    last_seen: str = ""
    created_at: str = ""


class NodeRegistry:
    def __init__(self, path: str):
        self.path = path
        self.data = safe_read_json(self.path, {"nodes": []})

    def list_nodes(self):
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


# ----------------------------
# “Model” Backends (stubs)
# ----------------------------

class WriterModel:
    def respond(self, user_text: str) -> str:
        return (
            "Writer — Response Rite\n"
            "----------------------\n"
            f"I received: {user_text}\n\n"
            "Draft output:\n"
            "- I can explain concepts, draft code, or structure a plan.\n"
            "- If you want me to generate scripts, I will provide complete code blocks.\n"
        )

class JudgeModel:
    ROUTE_APOTHECARY_PAT = re.compile(
        r"\b(ssh|node|nodes|raspberry\s*pi|pi\b|cluster|switch|ip\b|host\b|port\b|deploy|install|provision)\b",
        re.IGNORECASE
    )
    ROUTE_WRITER_PAT = re.compile(
        r"\b(write|draft|explain|latex|code|script|python|r|sql|excel|analysis|model|train|plot|chart)\b",
        re.IGNORECASE
    )

    def route(self, user_text: str) -> str:
        if self.ROUTE_APOTHECARY_PAT.search(user_text):
            return "apothecary"
        if self.ROUTE_WRITER_PAT.search(user_text):
            return "writer"
        return "writer"

    def critique(self, user_text: str, writer_text: str) -> str:
        flags = []
        if len(writer_text.strip()) < 40:
            flags.append("Response is very short; may lack detail.")
        verdict = "PASS" if not flags else "NEEDS_WORK"
        critique = (
            "Judge — Appraisal Rite\n"
            "----------------------\n"
            f"Verdict: {verdict}\n"
        )
        if flags:
            critique += "Findings:\n" + "\n".join(f"- {f}" for f in flags) + "\n"
        return critique


# ----------------------------
# Apothecary Engine (Paramiko)
# ----------------------------

class ApothecaryEngine:
    """
    Executes remote commands via Paramiko (supports password or key auth).
    Keeps dependencies lean; no sshpass, no external prompts.
    """

    def __init__(self, registry: NodeRegistry, log_path: str):
        self.registry = registry
        self.log_path = log_path

    def _require_paramiko(self):
        if paramiko is None:
            raise RuntimeError(
                "Paramiko not installed. Install it with: pip install paramiko"
            )

    def test_connection(self, node: NodeEntry, password_override: str | None, timeout_s: int):
        self._require_paramiko()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        password = password_override if password_override is not None else node.password

        try:
            if node.auth_method == "key":
                if not node.key_path:
                    raise RuntimeError("Key auth selected but key_path is empty.")
                key = None
                # Paramiko can load many key types via RSAKey/Ed25519Key/etc.
                # We'll let it auto-detect by filename using from_private_key_file in a best-effort way.
                # If it fails, user should provide a compatible key or use password auth.
                try:
                    key = paramiko.RSAKey.from_private_key_file(node.key_path)
                except Exception:
                    try:
                        key = paramiko.Ed25519Key.from_private_key_file(node.key_path)
                    except Exception:
                        try:
                            key = paramiko.ECDSAKey.from_private_key_file(node.key_path)
                        except Exception:
                            key = paramiko.DSSKey.from_private_key_file(node.key_path)

                client.connect(
                    hostname=node.host,
                    port=node.port,
                    username=node.username,
                    pkey=key,
                    timeout=timeout_s,
                    auth_timeout=timeout_s,
                    banner_timeout=timeout_s
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
                    allow_agent=False
                )

            return True, "Connection successful."
        finally:
            try:
                client.close()
            except Exception:
                pass

    def run_ssh(self, node: NodeEntry, remote_command: str, password_override: str | None, timeout_s: int):
        self._require_paramiko()
        append_log(self.log_path, f"[{now_iso()}] SSH TARGET: {node.name} {node.host}:{node.port} user={node.username} auth={node.auth_method}")
        append_log(self.log_path, f"[{now_iso()}] SSH CMD: {remote_command}")

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        password = password_override if password_override is not None else node.password

        try:
            if node.auth_method == "key":
                if not node.key_path:
                    raise RuntimeError("Key auth selected but key_path is empty.")
                key = None
                try:
                    key = paramiko.RSAKey.from_private_key_file(node.key_path)
                except Exception:
                    try:
                        key = paramiko.Ed25519Key.from_private_key_file(node.key_path)
                    except Exception:
                        try:
                            key = paramiko.ECDSAKey.from_private_key_file(node.key_path)
                        except Exception:
                            key = paramiko.DSSKey.from_private_key_file(node.key_path)

                client.connect(
                    hostname=node.host,
                    port=node.port,
                    username=node.username,
                    pkey=key,
                    timeout=timeout_s,
                    auth_timeout=timeout_s,
                    banner_timeout=timeout_s
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
                    allow_agent=False
                )

            # Run command
            transport = client.get_transport()
            if transport is None or not transport.is_active():
                raise RuntimeError("SSH transport not active after connect.")

            chan = transport.open_session(timeout=timeout_s)
            chan.settimeout(timeout_s)
            chan.exec_command(remote_command)

            # Read streams
            stdout_chunks = []
            stderr_chunks = []

            # Paramiko channels need polling
            start = time.time()
            while True:
                if chan.recv_ready():
                    stdout_chunks.append(chan.recv(4096).decode("utf-8", errors="replace"))
                if chan.recv_stderr_ready():
                    stderr_chunks.append(chan.recv_stderr(4096).decode("utf-8", errors="replace"))
                if chan.exit_status_ready():
                    break
                if time.time() - start > timeout_s:
                    raise TimeoutError(f"Remote command timed out after {timeout_s} seconds.")
                time.sleep(0.05)

            rc = chan.recv_exit_status()
            stdout = "".join(stdout_chunks)
            stderr = "".join(stderr_chunks)

            return rc, stdout, stderr
        finally:
            try:
                client.close()
            except Exception:
                pass


# ----------------------------
# GUI: Council Console
# ----------------------------

class CouncilConsole(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AI Model Council — Phase I (Council Console)")
        self.geometry("1050x720")

        # Paths / persistence
        self.base_dir = os.path.join(os.path.expanduser("~"), ".ai_model_council")
        os.makedirs(self.base_dir, exist_ok=True)
        self.registry_path = os.path.join(self.base_dir, "node_registry.json")
        self.council_log = os.path.join(self.base_dir, "logs", "council.log")
        self.apoth_log = os.path.join(self.base_dir, "logs", "apothecary.log")

        # Models
        self.writer = WriterModel()
        self.judge = JudgeModel()

        # Apothecary components
        self.registry = NodeRegistry(self.registry_path)
        self.apothecary_engine = ApothecaryEngine(self.registry, self.apoth_log)

        # Threading
        self.ui_q = queue.Queue()

        self._build_ui()
        self._build_apothecary_window()

        self.after(100, self._poll_ui_queue)

    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(side="top", fill="x", padx=10, pady=8)

        ttk.Label(top, text="Council Console (Judge-routed)").pack(side="left")

        ttk.Button(top, text="Apothecary Console", command=self._show_apothecary).pack(side="right", padx=4)
        ttk.Button(top, text="Show Paths", command=self._show_paths).pack(side="right", padx=4)

        main = ttk.Frame(self)
        main.pack(fill="both", expand=True, padx=10, pady=8)

        left = ttk.Frame(main)
        left.pack(side="left", fill="both", expand=True)

        ttk.Label(left, text="Transcript").pack(anchor="w")
        self.transcript = tk.Text(left, wrap="word", height=25)
        self.transcript.pack(fill="both", expand=True)
        self.transcript.configure(state="disabled")

        right = ttk.Frame(main)
        right.pack(side="right", fill="both", expand=False, padx=(10, 0))

        ttk.Label(right, text="Judge Routing + Critique").pack(anchor="w")
        self.judge_box = tk.Text(right, wrap="word", width=42, height=25)
        self.judge_box.pack(fill="both", expand=True)
        self.judge_box.configure(state="disabled")

        bottom = ttk.Frame(self)
        bottom.pack(side="bottom", fill="x", padx=10, pady=10)

        ttk.Label(bottom, text="User Input").pack(anchor="w")
        self.input = tk.Text(bottom, wrap="word", height=4)
        self.input.pack(fill="x", expand=False)

        btns = ttk.Frame(bottom)
        btns.pack(fill="x", pady=(6, 0))

        ttk.Button(btns, text="Send to Council", command=self._send).pack(side="left")
        ttk.Button(btns, text="Clear Input", command=lambda: self._set_text(self.input, "")).pack(side="left", padx=6)

        self.status = ttk.Label(btns, text="Status: idle")
        self.status.pack(side="right")

    def _show_paths(self):
        msg = (
            f"Registry: {self.registry_path}\n"
            f"Council Log: {self.council_log}\n"
            f"Apothecary Log: {self.apoth_log}\n"
            f"STORE_PASSWORDS: {STORE_PASSWORDS}\n"
            f"Paramiko installed: {paramiko is not None}\n"
        )
        messagebox.showinfo("Paths / Status", msg)

    def _append_transcript(self, who: str, text: str):
        self.transcript.configure(state="normal")
        self.transcript.insert("end", f"\n[{now_iso()}] {who}\n{text}\n")
        self.transcript.see("end")
        self.transcript.configure(state="disabled")
        append_log(self.council_log, f"[{now_iso()}] {who}: {text}")

    def _set_judge(self, text: str):
        self.judge_box.configure(state="normal")
        self.judge_box.delete("1.0", "end")
        self.judge_box.insert("end", text)
        self.judge_box.see("end")
        self.judge_box.configure(state="disabled")

    @staticmethod
    def _set_text(widget: tk.Text, text: str):
        widget.delete("1.0", "end")
        widget.insert("end", text)

    def _send(self):
        user_text = self.input.get("1.0", "end").strip()
        if not user_text:
            return
        self._set_text(self.input, "")
        self._append_transcript("User", user_text)

        route = self.judge.route(user_text)
        self._set_judge(f"Judge Routing Decision:\n- Route: {route}\n")
        self.status.configure(text=f"Status: processing ({route})")

        if route == "apothecary":
            self._append_transcript("Judge", "Routing to Apothecary. Use the Apothecary Console to perform node rites safely.")
            self._show_apothecary()
            self.status.configure(text="Status: idle")
            return

        def worker():
            try:
                writer_text = self.writer.respond(user_text)
                critique = self.judge.critique(user_text, writer_text)
                self.ui_q.put(("writer_done", writer_text, critique))
            except Exception as e:
                self.ui_q.put(("error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_ui_queue(self):
        try:
            while True:
                item = self.ui_q.get_nowait()
                kind = item[0]
                if kind == "writer_done":
                    _, writer_text, critique = item
                    self._append_transcript("Writer", writer_text)
                    self._append_transcript("Judge", critique)
                    self._set_judge(critique)
                    self.status.configure(text="Status: idle")
                elif kind == "apoth_out":
                    _, text = item
                    self._apoth_print(text)
                elif kind == "error":
                    _, msg = item
                    messagebox.showerror("Error", msg)
                    self.status.configure(text="Status: idle")
        except queue.Empty:
            pass
        self.after(100, self._poll_ui_queue)

    # ----------------------------
    # Apothecary Window
    # ----------------------------

    def _build_apothecary_window(self):
        self.apoth_win = tk.Toplevel(self)
        self.apoth_win.title("AI Model Council — Phase I (Apothecary Console)")
        self.apoth_win.geometry("1150x760")
        self.apoth_win.withdraw()

        root = ttk.Frame(self.apoth_win)
        root.pack(fill="both", expand=True, padx=10, pady=10)

        # Registry controls
        reg_frame = ttk.LabelFrame(root, text="Node Registry (persistent)")
        reg_frame.pack(side="top", fill="x")

        ttk.Button(reg_frame, text="Refresh", command=self._refresh_nodes).pack(side="left", padx=4, pady=6)
        ttk.Button(reg_frame, text="Add / Edit Node", command=self._add_edit_node).pack(side="left", padx=4, pady=6)
        ttk.Button(reg_frame, text="Delete Node", command=self._delete_node).pack(side="left", padx=4, pady=6)
        ttk.Button(reg_frame, text="Test Connection", command=self._test_connection).pack(side="left", padx=14, pady=6)

        self.paramiko_status = ttk.Label(reg_frame, text=f"Paramiko: {'OK' if paramiko is not None else 'MISSING'}")
        self.paramiko_status.pack(side="right", padx=6)

        # Node tree
        self.node_tree = ttk.Treeview(
            root,
            columns=("host", "port", "username", "auth", "last_seen", "notes"),
            show="headings",
            height=8
        )
        for col, w in [("host", 170), ("port", 60), ("username", 120), ("auth", 90), ("last_seen", 160), ("notes", 450)]:
            self.node_tree.heading(col, text=col)
            self.node_tree.column(col, width=w, stretch=(col in ("notes",)))
        self.node_tree.pack(fill="x", pady=(8, 10))

        # Command / Recipes
        cmd_frame = ttk.LabelFrame(root, text="SSH Rite (plan-before-act + CONFIRM)")
        cmd_frame.pack(side="top", fill="both", expand=True)

        top_row = ttk.Frame(cmd_frame)
        top_row.pack(fill="x", padx=8, pady=8)

        ttk.Label(top_row, text="Target Node:").pack(side="left")
        self.target_node_var = tk.StringVar(value="")
        self.target_node_combo = ttk.Combobox(top_row, textvariable=self.target_node_var, state="readonly", width=28)
        self.target_node_combo.pack(side="left", padx=6)

        ttk.Label(top_row, text="Timeout (s):").pack(side="left", padx=(16, 4))
        self.timeout_var = tk.StringVar(value="120")
        ttk.Entry(top_row, textvariable=self.timeout_var, width=8).pack(side="left")

        # Recipes dropdown
        ttk.Label(top_row, text="Recipe:").pack(side="left", padx=(16, 4))
        self.recipe_var = tk.StringVar(value="(none)")
        self.recipe_combo = ttk.Combobox(top_row, textvariable=self.recipe_var, state="readonly", width=30)
        self.recipe_combo["values"] = ["(none)", "Base Setup (apt + python + git)", "Council Node Setup (dirs + venv)"]
        self.recipe_combo.pack(side="left", padx=6)
        ttk.Button(top_row, text="Load Recipe", command=self._load_recipe).pack(side="left", padx=6)

        # Command box
        ttk.Label(cmd_frame, text="Remote command:").pack(anchor="w", padx=8)
        self.cmd_text = tk.Text(cmd_frame, wrap="word", height=7)
        self.cmd_text.pack(fill="x", padx=8, pady=(0, 8))

        btn_row = ttk.Frame(cmd_frame)
        btn_row.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btn_row, text="Explain Plan", command=self._explain_plan).pack(side="left")
        ttk.Button(btn_row, text="EXECUTE (requires CONFIRM)", command=self._execute_plan).pack(side="left", padx=6)
        ttk.Button(btn_row, text="Clear Command", command=lambda: self._set_text(self.cmd_text, "")).pack(side="left", padx=6)

        # Output
        ttk.Label(cmd_frame, text="Output:").pack(anchor="w", padx=8)
        self.apoth_output = tk.Text(cmd_frame, wrap="word")
        self.apoth_output.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.apoth_output.configure(state="disabled")

        warn = (
            "Safety Protocol:\n"
            "- This Apothecary uses Paramiko for SSH so password-per-node works inside the GUI.\n"
            "- Before execution, you must type CONFIRM to proceed.\n"
            "- Ensure the selected node matches the physical Pi you intend.\n"
        )
        ttk.Label(root, text=warn).pack(anchor="w", pady=(6, 0))

        self._refresh_nodes()

    def _show_apothecary(self):
        self.apoth_win.deiconify()
        self.apoth_win.lift()

    def _apoth_print(self, text: str):
        self.apoth_output.configure(state="normal")
        self.apoth_output.insert("end", text.rstrip() + "\n")
        self.apoth_output.see("end")
        self.apoth_output.configure(state="disabled")
        append_log(self.apoth_log, f"[{now_iso()}] {text}".rstrip())

    def _refresh_nodes(self):
        for iid in self.node_tree.get_children():
            self.node_tree.delete(iid)

        nodes = self.registry.list_nodes()
        for n in nodes:
            self.node_tree.insert(
                "", "end", iid=n.name,
                values=(n.host, n.port, n.username, n.auth_method, n.last_seen, n.notes)
            )
        self.target_node_combo["values"] = [n.name for n in nodes]
        if nodes and not self.target_node_var.get():
            self.target_node_var.set(nodes[0].name)

    def _selected_node(self):
        name = self.target_node_var.get().strip()
        if not name:
            return None
        for n in self.registry.list_nodes():
            if n.name == name:
                return n
        return None

    def _add_edit_node(self):
        name = simpledialog.askstring("Node Name", "Name (unique key):", parent=self.apoth_win)
        if not name:
            return

        existing = None
        for n in self.registry.list_nodes():
            if n.name == name:
                existing = n
                break

        def ask(prompt, initial=""):
            return simpledialog.askstring("Node Field", prompt, initialvalue=initial, parent=self.apoth_win)

        host = ask("Host/IP:", existing.host if existing else "")
        if not host:
            return
        port_s = ask("Port:", str(existing.port if existing else 22)) or "22"
        username = ask("Username:", existing.username if existing else "pi") or "pi"
        auth_method = ask("Auth method (password/key):", existing.auth_method if existing else "password") or "password"
        auth_method = auth_method.strip().lower()
        if auth_method not in ("password", "key"):
            messagebox.showwarning("Auth Method", "Invalid auth method; using 'password'.")
            auth_method = "password"

        password = existing.password if existing else ""
        key_path = existing.key_path if existing else ""

        if auth_method == "password":
            if STORE_PASSWORDS:
                password = ask("Password (stored in plaintext; fast but risky):", password) or ""
            else:
                password = ""  # never store
            key_path = ""
        else:
            pick = messagebox.askyesno("Key Path", "Select a private key file now?")
            if pick:
                kp = filedialog.askopenfilename(parent=self.apoth_win, title="Select Private Key")
                if kp:
                    key_path = kp
            password = ""

        notes = ask("Notes:", existing.notes if existing else "") or ""

        entry = NodeEntry(
            name=name,
            host=host.strip(),
            port=int(port_s),
            username=username.strip(),
            auth_method=auth_method,
            password=password,
            key_path=key_path,
            notes=notes,
            last_seen=existing.last_seen if existing else "",
            created_at=existing.created_at if existing else now_iso(),
        )
        self.registry.upsert(entry)
        self._refresh_nodes()

    def _delete_node(self):
        sel = self.node_tree.selection()
        if not sel:
            messagebox.showinfo("Delete Node", "Select a node first.")
            return
        name = sel[0]
        if messagebox.askyesno("Confirm Delete", f"Delete node '{name}' from registry?"):
            self.registry.delete(name)
            self._refresh_nodes()

    def _maybe_get_password(self, node: NodeEntry) -> str | None:
        """
        Returns a password override or None if not needed.
        If STORE_PASSWORDS is False, ask each time for password-auth nodes.
        """
        if node.auth_method != "password":
            return None
        if STORE_PASSWORDS and node.password:
            return None  # use stored
        # ask user
        pw = simpledialog.askstring(
            "Password Required",
            f"Enter SSH password for {node.username}@{node.host}:",
            parent=self.apoth_win,
            show="*"
        )
        return pw or ""

    def _test_connection(self):
        node = self._selected_node()
        if not node:
            messagebox.showinfo("Test Connection", "Select a node first.")
            return
        timeout = int(self.timeout_var.get() or "120")

        pw_override = self._maybe_get_password(node)

        def worker():
            try:
                ok, msg = self.apothecary_engine.test_connection(node, pw_override, timeout)
                if ok:
                    node.last_seen = now_iso()
                    self.registry.upsert(node)
                    self.ui_q.put(("apoth_out", f"[{now_iso()}] TEST OK: {msg}\n"))
                    self.ui_q.put(("apoth_out", "-" * 60 + "\n"))
                else:
                    self.ui_q.put(("apoth_out", f"[{now_iso()}] TEST FAIL: {msg}\n"))
            except Exception as e:
                self.ui_q.put(("apoth_out", f"[{now_iso()}] TEST ERROR: {e}\n"))
                self.ui_q.put(("apoth_out", "-" * 60 + "\n"))

        threading.Thread(target=worker, daemon=True).start()

    def _load_recipe(self):
        r = self.recipe_var.get()
        if r == "Base Setup (apt + python + git)":
            cmd = (
                "sudo apt-get update -y && "
                "sudo apt-get install -y python3 python3-venv python3-pip git"
            )
            self._set_text(self.cmd_text, cmd)
        elif r == "Council Node Setup (dirs + venv)":
            cmd = (
                "mkdir -p ~/council_node && "
                "python3 -m venv ~/council_node/venv && "
                "~/council_node/venv/bin/pip install --upgrade pip"
            )
            self._set_text(self.cmd_text, cmd)
        else:
            messagebox.showinfo("Recipe", "No recipe selected.")

    def _explain_plan(self):
        node = self._selected_node()
        cmd = self.cmd_text.get("1.0", "end").strip()
        timeout = int(self.timeout_var.get() or "120")

        if not cmd:
            messagebox.showinfo("Explain Plan", "Enter a command first.")
            return
        if not node:
            messagebox.showinfo("Explain Plan", "Select a node.")
            return

        plan = (
            "Apothecary — Plan Before Act\n"
            "-----------------------------\n"
            f"Target: {node.name} ({node.host}:{node.port})\n"
            f"User: {node.username}\n"
            f"Auth: {node.auth_method}\n"
            f"Timeout: {timeout}s\n"
            f"Command:\n{cmd}\n\n"
            "Warnings:\n"
            "- Verify the node is the intended physical Pi.\n"
            "- Review the command carefully; this will execute remotely.\n"
        )
        self._apoth_print(plan)

    def _execute_plan(self):
        node = self._selected_node()
        cmd = self.cmd_text.get("1.0", "end").strip()
        timeout = int(self.timeout_var.get() or "120")

        if not cmd:
            messagebox.showinfo("Execute", "Enter a command first.")
            return
        if not node:
            messagebox.showinfo("Execute", "Select a node.")
            return

        confirm = simpledialog.askstring(
            "Safety Confirmation",
            "Type CONFIRM to execute the planned action:",
            parent=self.apoth_win
        )
        if (confirm or "").strip() != "CONFIRM":
            self._apoth_print("Execution aborted (confirmation not provided).")
            return

        pw_override = self._maybe_get_password(node)

        def worker():
            try:
                start = time.time()
                rc, out, err = self.apothecary_engine.run_ssh(node, cmd, pw_override, timeout)
                node.last_seen = now_iso()
                self.registry.upsert(node)

                self.ui_q.put(("apoth_out", f"[{now_iso()}] SSH return code: {rc}\n"))
                if out.strip():
                    self.ui_q.put(("apoth_out", "STDOUT:\n" + out + "\n"))
                if err.strip():
                    self.ui_q.put(("apoth_out", "STDERR:\n" + err + "\n"))
                self.ui_q.put(("apoth_out", f"Elapsed: {time.time()-start:.2f}s\n"))
                self.ui_q.put(("apoth_out", "-" * 60 + "\n"))
            except Exception as e:
                self.ui_q.put(("apoth_out", f"[{now_iso()}] ERROR: {e}\n"))
                self.ui_q.put(("apoth_out", "-" * 60 + "\n"))

        threading.Thread(target=worker, daemon=True).start()


# ----------------------------
# Entry Point
# ----------------------------

def main():
    app = CouncilConsole()
    app.mainloop()

if __name__ == "__main__":
    main()
