# ============================================================
# apothecary_engine.py  —  v2
# Conda env:
#   conda create -n council python=3.11 -y
#   conda activate council
# Optional (SSH): pip install paramiko
# ============================================================
#
# New in v2:
#   - Pi provisioning wizard (OS check, ethernet keepalive; Ollama and models
#     moved to council_core.pi_setup - see WIZARD_SEQUENCE)
#   - Connection health monitor (background thread, auto-recovers)
#   - Static IP assignment helper for ethernet stability
#   - Auto-register provisioned Pi into council dispatcher
#   - Per-node status badges in the console UI
#   - Task runner: named sequences of SSH commands with pass/fail
# ============================================================

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Optional, List, Dict, Callable, Tuple

try:
    import paramiko  # type: ignore
    _PARAMIKO_OK = True
except Exception:
    paramiko = None
    _PARAMIKO_OK = False


# ============================================================
# Helpers
# ============================================================

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


# ============================================================
# Pi provisioning task definitions
# ============================================================

@dataclass
class TaskStep:
    cmd: str
    label: str
    ok_rc: List[int] = field(default_factory=lambda: [0])
    check_out: str = ""
    timeout: int = 60
    warn_only: bool = False


PI_MODEL_RECOMMENDATIONS = {
    "Pi 5 (16GB)":          ["qwen2.5:14b-instruct-q4_K_M", "qwen2.5:7b-instruct-q4_K_M", "phi4"],
    "Pi 5 (16GB) + AI HAT+":["qwen2.5:14b-instruct-q4_K_M", "qwen2.5:7b-instruct-q4_K_M", "phi4"],
    "Pi 5 (8GB)":           ["phi4", "qwen2.5:7b-instruct-q4_K_M", "qwen2.5:3b"],
    "Pi 5 (8GB) + AI HAT+": ["phi4", "qwen2.5:7b-instruct-q4_K_M", "qwen2.5:3b"],
    "Pi 5 (4GB)":           ["qwen2.5:1.5b", "phi3.5:3.8b-mini"],
    "Pi 4 (8GB)":           ["qwen2.5:1.5b", "llama3.2:1b"],
    "Pi 4 (4GB)":           ["qwen2.5:0.5b", "llama3.2:1b"],
    "Other / 2GB":          ["qwen2.5:0.5b"],
}

# Council role assignment by Pi model
PI_COUNCIL_ROLE = {
    "Pi 5 (16GB)":           "heavy",   # Sage, Strategist
    "Pi 5 (16GB) + AI HAT+": "heavy",   # Sage, Strategist (HAT accelerates inference)
    "Pi 5 (8GB)":            "fast",    # Peasant, Intern, Artist
    "Pi 5 (8GB) + AI HAT+":  "fast",    # Peasant, Intern, Artist (HAT helps)
    "Pi 5 (4GB)":            "fast",
    "Pi 4 (8GB)":            "fast",
    "Pi 4 (4GB)":            "fast",
    "Other / 2GB":           "fast",
}

PROVISION_TASKS: Dict[str, List[TaskStep]] = {

    "check_os": [
        TaskStep("uname -a", "Check OS"),
        TaskStep("cat /etc/os-release | grep PRETTY", "Read OS version"),
        TaskStep("free -h", "Check RAM"),
        TaskStep("df -h /", "Check disk space"),
        TaskStep(
            "ip addr show eth0 2>/dev/null || ip addr show end0 2>/dev/null || ip addr",
            "Check network interfaces",
        ),
    ],

    "keepalive_setup": [
        TaskStep(
            "(crontab -l 2>/dev/null; echo '*/5 * * * * ping -c 1 {desktop_ip} > /dev/null 2>&1') | crontab -",
            "Install keepalive ping cron (Pi pings desktop every 5 min)",
        ),
        TaskStep(
            "sudo ethtool -s eth0 wol g 2>/dev/null || true",
            "Enable Wake-on-LAN",
            warn_only=True,
        ),
        TaskStep(
            # Disable ethernet power saving - the #1 cause of Pi ethernet drops
            "sudo bash -c 'mkdir -p /etc/network/if-up.d && "
            "cat > /etc/network/if-up.d/disable-eth-power-save << HEREDOC\n"
            "#!/bin/sh\n"
            "ethtool -K \\$IFACE rx off tx off 2>/dev/null || true\n"
            "HEREDOC\n"
            "chmod +x /etc/network/if-up.d/disable-eth-power-save'",
            "Disable Ethernet power management (prevents link drops)",
            warn_only=True,
        ),
        TaskStep(
            # Also set it immediately for eth0/end0
            "iface=$(ip link | grep -o 'eth[0-9]\\|end[0-9]' | head -1); "
            "[ -n \"$iface\" ] && sudo ethtool -K $iface rx off tx off 2>/dev/null || true",
            "Apply Ethernet power save disable immediately",
            warn_only=True,
        ),
    ],

    "set_static_ip_eth": [
        TaskStep(
            "ip route | grep default | awk '{print $3}' | head -1",
            "Detect current gateway",
        ),
        TaskStep(
            "grep -q 'static ip_address={static_ip}' /etc/dhcpcd.conf 2>/dev/null "
            "&& echo 'Already set' || echo 'Not yet set'",
            "Check existing static config",
            warn_only=True,
        ),
        TaskStep(
            "IFACE=$(ip link | grep -o 'eth[0-9]\\|end[0-9]' | head -1 || echo eth0); "
            "sudo bash -c \"echo '' >> /etc/dhcpcd.conf && "
            "echo 'interface $IFACE' >> /etc/dhcpcd.conf && "
            "echo 'static ip_address={static_ip}/24' >> /etc/dhcpcd.conf && "
            "echo 'static routers={gateway}' >> /etc/dhcpcd.conf && "
            "echo 'static domain_name_servers=8.8.8.8 1.1.1.1' >> /etc/dhcpcd.conf\"",
            "Write static IP {static_ip} to /etc/dhcpcd.conf",
        ),
        TaskStep(
            "sudo systemctl restart dhcpcd 2>/dev/null || sudo service dhcpcd restart 2>/dev/null || true",
            "Restart DHCP client",
            warn_only=True,
        ),
    ],
}

# The old wizard also installed Ollama with 'curl -fsSL https://ollama.com/install.sh | sh'
# (no checksum), bound it to 0.0.0.0 with port 11434 open to everyone, and
# pulled a model (qwen2.5:3b, not US-origin) with no click of its own. Those
# steps are gone (review, 2026-10-07: they broke the user's decisions a and b,
# and "Run Full Setup" replaced the pinned, checked Ollama on a Pi the new
# setup had made). Ollama and models: Apothecary -> "Set up a Pi (new or
# existing)" (council_core.pi_setup) - ONE pinned release checked before it
# is installed, a firewall that lets only this PC in, and a US-origin model
# downloaded only on the user's "Download on the Pi" click.
WIZARD_SEQUENCE = [
    "check_os",
    "keepalive_setup",
]


def _render_steps(task_key: str, vars: Dict[str, str]) -> List[TaskStep]:
    steps = []
    for s in PROVISION_TASKS.get(task_key, []):
        cmd = s.cmd
        label = s.label
        for k, v in vars.items():
            cmd   = cmd.replace("{" + k + "}", v)
            label = label.replace("{" + k + "}", v)
        steps.append(TaskStep(
            cmd=cmd, label=label, ok_rc=list(s.ok_rc),
            check_out=s.check_out, timeout=s.timeout, warn_only=s.warn_only,
        ))
    return steps


# ============================================================
# Pi auto-discovery
# ============================================================
# Resolution chain (tried in order, first success wins):
#   1. socket.getaddrinfo(name.local)  — Windows Bonjour / Linux avahi
#   2. ping name.local, parse IP       — works when mDNS answers ping but not DNS
#   3. ARP table lookup                — catches recently-seen hosts
#   4. Subnet port-22 scan             — brute-force /24 fallback
#
# After an IP is found, SSH in and run `hostname -I` to confirm
# the real assigned address before saving to the registry.
# ============================================================

import re as _re
import subprocess as _sp
import concurrent.futures as _cf


def _resolve_mdns(hostname: str) -> Optional[str]:
    """Try socket.getaddrinfo(hostname.local) — works with Bonjour / avahi."""
    import socket as _sock
    try:
        results = _sock.getaddrinfo(hostname + ".local", 22, type=_sock.SOCK_STREAM)
        for r in results:
            ip = r[4][0]
            if ip and not ip.startswith("127.") and ":" not in ip:
                return ip
    except Exception:
        pass
    return None


def _resolve_ping(hostname: str) -> Optional[str]:
    """Ping hostname.local and parse the IP from the first output line."""
    import sys
    cmd = (
        ["ping", "-n", "1", "-w", "2000", hostname + ".local"]
        if sys.platform == "win32"
        else ["ping", "-c", "1", "-W", "2", hostname + ".local"]
    )
    try:
        out = _sp.run(cmd, capture_output=True, timeout=5, text=True).stdout
        m = _re.search(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b", out)
        if m:
            ip = m.group(1)
            if not ip.startswith("127."):
                return ip
    except Exception:
        pass
    return None


def _resolve_arp(hostname: str) -> Optional[str]:
    """Check ARP table for a recent entry matching the hostname."""
    try:
        out = _sp.run(["arp", "-a"], capture_output=True, timeout=5, text=True).stdout
        for line in out.splitlines():
            if hostname.lower() in line.lower():
                m = _re.search(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b", line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return None


def _probe_port22(ip: str, timeout_s: float = 0.4) -> bool:
    """Return True if port 22 is open on ip."""
    import socket as _sock
    try:
        with _sock.create_connection((ip, 22), timeout=timeout_s):
            return True
    except Exception:
        return False


def _get_local_subnet() -> Optional[str]:
    """Return local subnet prefix, e.g. '192.168.1'."""
    import socket as _sock
    try:
        with _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            parts = ip.split(".")
            if len(parts) == 4:
                return ".".join(parts[:3])
    except Exception:
        pass
    return None


def _resolve_subnet_scan(
    progress_cb: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """Scan the local /24 for hosts with port 22 open. Returns IP only if unambiguous."""
    subnet = _get_local_subnet()
    if not subnet:
        return None
    if progress_cb:
        progress_cb(f"  Scanning {subnet}.1-254 for SSH ...")
    candidates = [f"{subnet}.{i}" for i in range(1, 255)]
    found: List[str] = []
    with _cf.ThreadPoolExecutor(max_workers=64) as ex:
        futures = {ex.submit(_probe_port22, ip, 0.35): ip for ip in candidates}
        for fut in _cf.as_completed(futures):
            if fut.result():
                found.append(futures[fut])
    if progress_cb:
        progress_cb(f"  SSH open on: {', '.join(sorted(found)) if found else 'none'}")
    # Only auto-select when exactly one host found (avoids picking the wrong machine)
    return found[0] if len(found) == 1 else None


def discover_pi(
    hostname: str,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """
    Try all resolution strategies in order. Returns IP string or None.
    hostname — Pi hostname without .local, e.g. 'raspberrypi' or 'pi-01'
    """
    steps = [
        (f"mDNS  ({hostname}.local)",  lambda: _resolve_mdns(hostname)),
        (f"ping  ({hostname}.local)",  lambda: _resolve_ping(hostname)),
        (f"ARP   ({hostname})",        lambda: _resolve_arp(hostname)),
        ("subnet scan (fallback)",     lambda: _resolve_subnet_scan(progress_cb)),
    ]
    for label, fn in steps:
        if progress_cb:
            progress_cb(f"  Trying {label} ...")
        ip = fn()
        if ip:
            if progress_cb:
                progress_cb(f"  \u2713 Found: {ip}  (via {label.strip()})")
            return ip
        if progress_cb:
            progress_cb(f"  \u2717 {label.strip()}: no result")
    return None


def confirm_and_get_real_ip(
    engine: "ApothecaryEngine",
    node: "NodeEntry",
    discovered_ip: str,
    password_override: Optional[str],
    progress_cb: Optional[Callable[[str], None]] = None,
) -> str:
    """
    SSH into discovered_ip, run `hostname -I` to get the real assigned IP.
    Updates node.host in-place. Returns confirmed IP (falls back to discovered_ip).
    """
    node.host = discovered_ip
    try:
        rc, out, _ = engine.run_ssh(node, "hostname -I", password_override, timeout_s=10)
        if rc == 0 and out.strip():
            ips = [ip for ip in out.split() if not ip.startswith("127.")]
            if ips:
                real_ip = ips[0]
                if progress_cb:
                    progress_cb(f"  \u2713 Pi reports IP: {real_ip}")
                node.host = real_ip
                return real_ip
    except Exception as e:
        if progress_cb:
            progress_cb(f"  \u26a0 SSH confirm failed: {e}")
    return discovered_ip


# ============================================================
# Data model
# ============================================================

@dataclass
class NodeEntry:
    name: str
    host: str
    port: int = 22
    username: str = "pi"
    auth_method: str = "password"
    password: str = ""
    key_path: str = ""
    notes: str = ""
    last_seen: str = ""
    created_at: str = ""
    ollama_port: int = 11434
    model: str = ""
    status: str = "unknown"
    last_status_check: str = ""
    # ── Hardware metadata ────────────────────────────────────────
    pi_model: str = ""          # e.g. "Pi 5 (16GB)", "Pi 5 (8GB)"
    has_ai_hat: bool = False    # Raspberry Pi AI HAT+ attached
    ai_hat_tops: float = 0.0    # TOPS rating (26.0 for AI HAT+)
    ram_gb: int = 0             # RAM in GB (4, 8, 16)
    # ── Model inventory ─────────────────────────────────────────
    installed_models: list = field(default_factory=list)   # from ollama list
    active_model: str = ""      # currently loaded model
    model_log: list = field(default_factory=list)          # [{ts, model, role}] last 50 calls
    council_role: str = ""      # assigned council role: "heavy" | "fast" | "unassigned"


class NodeRegistry:
    def __init__(self, path: str):
        self.path = path
        self.data = safe_read_json(self.path, {"nodes": []})

    def list_nodes(self) -> List[NodeEntry]:
        nodes = []
        valid_fields = set(NodeEntry.__dataclass_fields__.keys())
        for d in self.data.get("nodes", []):
            try:
                nodes.append(NodeEntry(**{k: v for k, v in d.items() if k in valid_fields}))
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
        self.data["nodes"] = [d for d in self.data.get("nodes", []) if d.get("name") != name]
        safe_write_json(self.path, self.data)


# ============================================================
# SSH engine
# ============================================================

class ApothecaryEngine:

    def __init__(self, registry: NodeRegistry, log_path: str):
        self.registry = registry
        self.log_path = log_path

    @staticmethod
    def require_paramiko():
        if not _PARAMIKO_OK:
            raise RuntimeError("Paramiko not installed. Run: pip install paramiko")

    def run_ssh(
        self,
        node: NodeEntry,
        remote_command: str,
        password_override: Optional[str],
        timeout_s: int,
    ) -> Tuple[int, str, str]:
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
                        key = loader.from_private_key_file(node.key_path)
                        break
                    except Exception:
                        continue
                if key is None:
                    raise RuntimeError("Failed to load private key.")
                client.connect(
                    hostname=node.host, port=node.port, username=node.username,
                    pkey=key, timeout=timeout_s, auth_timeout=timeout_s,
                    banner_timeout=timeout_s,
                )
            else:
                if not password:
                    raise RuntimeError("Password auth but no password provided.")
                client.connect(
                    hostname=node.host, port=node.port, username=node.username,
                    password=password, timeout=timeout_s, auth_timeout=timeout_s,
                    banner_timeout=timeout_s, look_for_keys=False, allow_agent=False,
                )

            transport = client.get_transport()
            if transport is None or not transport.is_active():
                raise RuntimeError("SSH transport not active.")

            chan = transport.open_session(timeout=timeout_s)
            chan.settimeout(timeout_s)
            chan.exec_command(remote_command)

            stdout_chunks: List[str] = []
            stderr_chunks: List[str] = []
            start = time.time()

            while True:
                if chan.recv_ready():
                    stdout_chunks.append(chan.recv(4096).decode("utf-8", errors="replace"))
                if chan.recv_stderr_ready():
                    stderr_chunks.append(chan.recv_stderr(4096).decode("utf-8", errors="replace"))
                if chan.exit_status_ready():
                    break
                if time.time() - start > timeout_s:
                    raise TimeoutError(f"Command timed out after {timeout_s}s.")
                time.sleep(0.05)

            # Drain what is still buffered. The loop reads at most one 4 KB
            # chunk per pass, so a command that had already exited got one
            # read and everything past 4096 bytes was silently dropped.
            while chan.recv_ready():
                stdout_chunks.append(chan.recv(4096).decode("utf-8", errors="replace"))
            while chan.recv_stderr_ready():
                stderr_chunks.append(chan.recv_stderr(4096).decode("utf-8", errors="replace"))

            rc = chan.recv_exit_status()
            stdout = "".join(stdout_chunks)
            stderr = "".join(stderr_chunks)

            append_log(self.log_path, f"[{now_iso()}] {node.name}@{node.host}: rc={rc}")
            return rc, stdout, stderr
        finally:
            try:
                client.close()
            except Exception:
                pass

    def run_task_sequence(
        self,
        node: NodeEntry,
        steps: List[TaskStep],
        password_override: Optional[str],
        progress_cb: Optional[Callable[[str, bool], None]] = None,
    ) -> Tuple[bool, List[Dict]]:
        results = []
        all_passed = True
        for step in steps:
            if progress_cb:
                progress_cb(f"  \u25b6 {step.label}", False)
            try:
                rc, out, err = self.run_ssh(node, step.cmd, password_override, step.timeout)
                combined = (out + err).strip()
                passed = rc in step.ok_rc
                if step.check_out and step.check_out not in combined:
                    passed = False
                icon = "\u2713" if passed else ("\u26a0" if step.warn_only else "\u2717")
                msg  = f"    {icon} {step.label}"
                if combined and not passed:
                    msg += "\n      " + combined.splitlines()[0][:120]
                if progress_cb:
                    progress_cb(msg, not passed and not step.warn_only)
                results.append({"step": step.label, "rc": rc, "passed": passed})
                if not passed and not step.warn_only:
                    all_passed = False
                    break
            except Exception as e:
                msg = f"    \u2717 {step.label}: {e}"
                if progress_cb:
                    progress_cb(msg, not step.warn_only)
                results.append({"step": step.label, "rc": -1, "passed": False, "error": str(e)})
                if not step.warn_only:
                    all_passed = False
                    break
        return all_passed, results

    def test_connection(
        self, node: NodeEntry, password_override: Optional[str], timeout_s: int
    ) -> Tuple[bool, str]:
        try:
            rc, out, err = self.run_ssh(node, "echo COUNCIL_OK", password_override, timeout_s)
            if rc == 0 and "COUNCIL_OK" in out:
                return True, "SSH OK"
            return False, f"rc={rc} out={out.strip()[:80]}"
        except Exception as e:
            return False, str(e)

    def check_ollama(
        self, node: NodeEntry, password_override: Optional[str], timeout_s: int = 8
    ) -> Tuple[bool, str]:
        try:
            rc, out, err = self.run_ssh(
                node,
                f"curl -s --connect-timeout 5 http://localhost:{node.ollama_port}/api/tags",
                password_override, timeout_s,
            )
            if rc == 0 and "models" in out:
                return True, "Ollama OK"
            return False, f"Ollama not responding (rc={rc})"
        except Exception as e:
            return False, str(e)


# ============================================================
# Health monitor — background thread
# ============================================================

class PiHealthMonitor:
    """
    Polls every Pi every 60s via SSH.
    On status change: calls status_cb(name, status, message).
    If Ollama is down but SSH is up: automatically issues restart.

    status values: "online" | "offline" | "degraded"
    """

    def __init__(
        self,
        registry: NodeRegistry,
        engine: ApothecaryEngine,
        check_interval_s: int = 60,
        status_cb: Optional[Callable[[str, str, str], None]] = None,
    ):
        self.registry          = registry
        self.engine            = engine
        self.check_interval_s  = check_interval_s
        self.status_cb         = status_cb
        self._stop             = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_status: Dict[str, str] = {}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="pi-health-monitor"
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(self.check_interval_s):
            for node in self.registry.list_nodes():
                if self._stop.is_set():
                    return
                self._check_node(node)

    def log_model_call(self, node_name: str, model: str, role: str) -> None:
        """
        Log a model call against a node for the model inventory.
        Called by the dispatcher whenever a Pi node handles a request.
        Keeps the last 50 entries per node.
        """
        # This called self._get / self.upsert_node — Apothecary's methods, not
        # this class's — inside `except Exception: pass`, so every call was a
        # silent no-op and the Model Inventory call log was always empty.
        try:
            node = next((n for n in self.registry.list_nodes()
                         if n.name == node_name), None)
            if node is None:
                return
            entry = {"ts": now_iso(), "model": model, "role": role}
            if not isinstance(node.model_log, list):
                node.model_log = []
            node.model_log.append(entry)
            node.model_log = node.model_log[-50:]  # keep last 50
            node.active_model = model
            self.registry.upsert(node)
        except Exception:
            pass

    def refresh_installed_models(self, node: NodeEntry,
                                  password_override: Optional[str] = None,
                                  strict: bool = False) -> List[str]:
        """
        SSH into the node, run `ollama list`, parse the model names,
        and store them in node.installed_models. Returns the list.

        strict=True raises instead of returning the stored list, so a caller
        that shows the result can tell "refreshed" from "could not refresh".
        """
        try:
            rc, out, _ = self.engine.run_ssh(
                node, "ollama list 2>/dev/null", password_override, timeout_s=15
            )
            if rc != 0 or not out.strip():
                if strict:
                    raise RuntimeError(f"`ollama list` failed (rc={rc})")
                return node.installed_models or []
            models = []
            for line in out.strip().splitlines()[1:]:  # skip header
                parts = line.split()
                if parts:
                    models.append(parts[0])
            node.installed_models = models
            node.last_status_check = now_iso()
            # Was self.upsert_node — not a method of this class — so the
            # AttributeError was swallowed below and the STALE list returned.
            self.registry.upsert(node)
            return models
        except Exception:
            if strict:
                raise
            return node.installed_models or []

    def _check_node(self, node: NodeEntry):
        pw = node.password or None
        ssh_ok, ssh_msg = self.engine.test_connection(node, pw, timeout_s=8)

        if not ssh_ok:
            status = "offline"
            msg    = f"SSH unreachable: {ssh_msg}"
        else:
            oll_ok, oll_msg = self.engine.check_ollama(node, pw, timeout_s=8)
            if oll_ok:
                status = "online"
                msg    = "SSH \u2713  Ollama \u2713"
            else:
                status = "degraded"
                msg    = f"SSH \u2713  Ollama \u2717 ({oll_msg}) \u2014 attempting restart"
                try:
                    self.engine.run_ssh(
                        node,
                        "sudo systemctl restart ollama 2>/dev/null || ollama serve &",
                        pw, timeout_s=15,
                    )
                    msg += " \u2014 restart issued"
                except Exception as e:
                    msg += f" \u2014 restart failed: {e}"

        node.status = status
        node.last_status_check = now_iso()
        try:
            self.registry.upsert(node)
        except Exception:
            pass

        prev = self._last_status.get(node.name)
        if prev != status:
            self._last_status[node.name] = status
            if self.status_cb:
                self.status_cb(node.name, status, msg)


# ============================================================
# Façade
# ============================================================

class Apothecary:

    def __init__(
        self,
        *,
        registry_path: str,
        store_passwords: bool = True,
        log_path: Optional[str] = None,
        status_cb: Optional[Callable[[str, str, str], None]] = None,
    ):
        self.registry_path   = registry_path
        self.store_passwords = store_passwords
        self.log_path = log_path or os.path.join(
            os.path.dirname(registry_path), "apothecary.log"
        )
        self.registry = NodeRegistry(registry_path)
        self.engine   = ApothecaryEngine(self.registry, self.log_path)
        self.monitor  = PiHealthMonitor(
            self.registry, self.engine,
            check_interval_s=60,
            status_cb=status_cb,
        )
        if self.registry.list_nodes():
            self.monitor.start()

    def list_nodes(self) -> List[NodeEntry]:
        return self.registry.list_nodes()

    def upsert_node(self, entry: NodeEntry) -> None:
        if not entry.created_at:
            entry.created_at = now_iso()
        if not self.store_passwords:
            entry.password = ""
        self.registry.upsert(entry)
        self.monitor.start()

    def delete_node(self, name: str) -> None:
        self.registry.delete(name)

    def save_node(self, entry: NodeEntry,
                  previous_name: Optional[str] = None) -> None:
        """Upsert, and on a rename remove the old record. The registry is
        keyed by name, so an edit that changed the name used to leave the
        original behind as a duplicate."""
        if previous_name and previous_name != entry.name:
            self.registry.delete(previous_name)
        self.upsert_node(entry)

    def refresh_installed_models(self, node: NodeEntry,
                                 password_override: Optional[str] = None
                                 ) -> List[str]:
        """`ollama list` on the node. Raises when it cannot be refreshed.

        The Model Inventory's Refresh button called this on Apothecary, where
        it did not exist — it lives on the monitor — so the AttributeError
        killed the worker and the button did nothing.
        """
        return self.monitor.refresh_installed_models(
            node, password_override, strict=True)

    def test(
        self, name: str, password_override: Optional[str] = None, timeout_s: int = 10
    ) -> Tuple[bool, str]:
        node = self._get(name)
        ok, msg = self.engine.test_connection(node, password_override, timeout_s)
        if ok:
            node.last_seen = now_iso()
            self.upsert_node(node)
        return ok, msg

    def run(
        self, name: str, cmd: str,
        password_override: Optional[str] = None, timeout_s: int = 30
    ) -> Tuple[int, str, str]:
        node = self._get(name)
        rc, out, err = self.engine.run_ssh(node, cmd, password_override, timeout_s)
        node.last_seen = now_iso()
        self.upsert_node(node)
        return rc, out, err

    def provision_pi(
        self,
        name: str,
        model: str,
        desktop_ip: str,
        static_ip: str = "",
        password_override: Optional[str] = None,
        progress_cb: Optional[Callable[[str, bool], None]] = None,
    ) -> Tuple[bool, str]:
        """Check the Pi's OS and fix its Ethernet link (WIZARD_SEQUENCE).
        It no longer installs Ollama or downloads a model - ``model`` is
        kept for older callers and ignored; see WIZARD_SEQUENCE."""
        node = self._get(name)
        pw   = password_override or node.password or None
        vars = {"host": node.host, "desktop_ip": desktop_ip}

        if progress_cb:
            sep = "=" * 50
            progress_cb(f"\n{sep}", False)
            progress_cb("  Council Pi Setup Wizard (OS check and Ethernet keepalive)", False)
            progress_cb(f"  Node: {name}  ({node.username}@{node.host})", False)
            progress_cb(sep, False)

        all_ok = True
        for task_key in WIZARD_SEQUENCE:
            task_name = task_key.replace("_", " ").title()
            if progress_cb:
                progress_cb(f"\n[ {task_name} ]", False)
            if task_key == "set_static_ip_eth" and not static_ip:
                if progress_cb:
                    progress_cb("  \u21b7 Skipped (no static IP requested)", False)
                continue
            steps = _render_steps(task_key, {**vars, "static_ip": static_ip})
            ok, _ = self.engine.run_task_sequence(node, steps, pw, progress_cb)
            if not ok:
                all_ok = False
                if progress_cb:
                    progress_cb(f"\n  \u2717 Setup stopped at: {task_name}", True)
                    progress_cb("  Fix the error above and re-run.", True)
                break

        if all_ok:
            node.last_seen = now_iso()
            node.status    = "online"
            self.upsert_node(node)
            if progress_cb:
                sep = "=" * 50
                progress_cb(f"\n{sep}", False)
                progress_cb("  \u2713 OS checked and Ethernet keepalive set.", False)
                progress_cb("  Ollama and models are installed by 'Set up a Pi (new or "
                            "existing)' in the Apothecary: one pinned, checked Ollama "
                            "release, and a model only when you press 'Download on the Pi'.",
                            False)
                progress_cb(sep, False)
            return True, f"Checked {name} and set its Ethernet keepalive."

        return False, f"Setup failed for {name}. See output."

    def _get(self, name: str) -> NodeEntry:
        for n in self.registry.list_nodes():
            if n.name == name:
                return n
        raise KeyError(f"Node not found: {name}")


# Pure data, so it lives outside the `if _TK_OK:` block — inside it, a
# Qt-only build lost both the badge colours and the icons.
STATUS_COLOR = {
    "online":  "#a6e3a1",
    "offline": "#f38ba8",
    "degraded":"#fab387",
    "unknown": "#6c7086",
}
STATUS_ICON = {
    "online":  "●",   # filled circle
    "offline": "○",   # empty circle
    "degraded":"◐",   # half circle
    "unknown": "?",
}
