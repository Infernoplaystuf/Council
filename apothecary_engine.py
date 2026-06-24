# ============================================================
# apothecary_engine.py  —  v2
# Conda env:
#   conda create -n council python=3.11 -y
#   conda activate council
# Optional (SSH): pip install paramiko
# ============================================================
#
# New in v2:
#   - Pi provisioning wizard (install Ollama, pull model, enable service)
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

    "install_ollama": [
        TaskStep(
            "curl -fsSL https://ollama.com/install.sh | sh",
            "Download and install Ollama",
            timeout=300,
        ),
        TaskStep("ollama --version", "Verify Ollama installed"),
        TaskStep(
            "sudo systemctl enable ollama 2>/dev/null || true",
            "Enable Ollama service on boot",
            warn_only=True,
        ),
        TaskStep(
            "sudo systemctl start ollama 2>/dev/null || (ollama serve &); sleep 2",
            "Start Ollama service",
            warn_only=True,
        ),
        TaskStep(
            "curl -s --connect-timeout 8 http://localhost:11434/api/tags",
            "Verify Ollama API responding",
            timeout=20,
        ),
    ],

    "configure_ollama_service": [
        TaskStep(
            "sudo mkdir -p /etc/systemd/system/ollama.service.d",
            "Create systemd override dir",
        ),
        TaskStep(
            "printf '[Service]\\nEnvironment=\"OLLAMA_HOST=0.0.0.0:11434\"\\n"
            "Environment=\"OLLAMA_FLASH_ATTENTION=1\"\\n' | "
            "sudo tee /etc/systemd/system/ollama.service.d/override.conf",
            "Bind Ollama to 0.0.0.0 (allow remote connections)",
        ),
        TaskStep("sudo systemctl daemon-reload", "Reload systemd"),
        TaskStep("sudo systemctl restart ollama", "Restart Ollama"),
        TaskStep(
            "sleep 3 && curl -s --connect-timeout 8 http://localhost:11434/api/tags",
            "Verify Ollama API responding after restart",
            timeout=20,
        ),
    ],

    "open_firewall": [
        TaskStep(
            "sudo ufw allow 11434/tcp 2>/dev/null && sudo ufw reload 2>/dev/null || true",
            "Open port 11434 in ufw",
            warn_only=True,
        ),
        TaskStep(
            "sudo iptables -I INPUT -p tcp --dport 11434 -j ACCEPT 2>/dev/null || true",
            "Allow port 11434 in iptables",
            warn_only=True,
        ),
    ],

    "pull_model": [
        TaskStep("ollama pull {model}", "Pull model {model}", timeout=3600),  # 1hr — large models take time
        TaskStep("ollama list | grep {model_base}", "Verify model present"),
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

WIZARD_SEQUENCE = [
    "check_os",
    "install_ollama",
    "configure_ollama_service",
    "open_firewall",
    "pull_model",
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
        try:
            node = self._get(node_name)
            entry = {"ts": now_iso(), "model": model, "role": role}
            if not isinstance(node.model_log, list):
                node.model_log = []
            node.model_log.append(entry)
            node.model_log = node.model_log[-50:]  # keep last 50
            node.active_model = model
            self.upsert_node(node)
        except Exception:
            pass

    def refresh_installed_models(self, node: NodeEntry,
                                  password_override: Optional[str] = None) -> List[str]:
        """
        SSH into the node, run `ollama list`, parse the model names,
        and store them in node.installed_models. Returns the list.
        """
        try:
            rc, out, _ = self.engine.run_ssh(
                node, "ollama list 2>/dev/null", password_override, timeout_s=15
            )
            if rc != 0 or not out.strip():
                return node.installed_models or []
            models = []
            for line in out.strip().splitlines()[1:]:  # skip header
                parts = line.split()
                if parts:
                    models.append(parts[0])
            node.installed_models = models
            node.last_status_check = now_iso()
            self.upsert_node(node)
            return models
        except Exception:
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
        node = self._get(name)
        pw   = password_override or node.password or None
        model_base = model.split(":")[0]
        vars = {
            "model": model, "model_base": model_base,
            "host": node.host, "desktop_ip": desktop_ip,
        }

        if progress_cb:
            sep = "=" * 50
            progress_cb(f"\n{sep}", False)
            progress_cb("  Council Pi Setup Wizard", False)
            progress_cb(f"  Node: {name}  ({node.username}@{node.host})", False)
            progress_cb(f"  Model: {model}", False)
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
            node.model     = model
            node.last_seen = now_iso()
            node.status    = "online"
            self.upsert_node(node)
            if progress_cb:
                sep = "=" * 50
                progress_cb(f"\n{sep}", False)
                progress_cb("  \u2713 Pi setup complete!", False)
                progress_cb(f"  Ollama running at http://{node.host}:{node.ollama_port}", False)
                progress_cb(f"  Model: {model}", False)
                progress_cb(f"\n  Add to Council: COUNCIL_PI_HOSTS=http://{node.host}:{node.ollama_port}", False)
                progress_cb(sep, False)
            return True, f"Provisioned {name}. Ollama at http://{node.host}:{node.ollama_port}"

        return False, f"Setup failed for {name}. See output."

    def _get(self, name: str) -> NodeEntry:
        for n in self.registry.list_nodes():
            if n.name == name:
                return n
        raise KeyError(f"Node not found: {name}")


# ============================================================
# GUI Console Widget
# ============================================================

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, simpledialog
    _TK_OK = True
except Exception:
    tk = None
    ttk = None
    messagebox = None
    simpledialog = None
    _TK_OK = False


if _TK_OK:
    _STATUS_COLOR = {
        "online":  "#a6e3a1",
        "offline": "#f38ba8",
        "degraded":"#fab387",
        "unknown": "#6c7086",
    }
    _STATUS_ICON = {
        "online":  "\u25cf",   # filled circle
        "offline": "\u25cb",   # empty circle
        "degraded":"\u25d0",   # half circle
        "unknown": "?",
    }

    class ApothecaryConsole(ttk.Frame):
        def __init__(self, parent, apothecary: Apothecary, ui_queue=None):
            super().__init__(parent)
            self.apoth    = apothecary
            self.ui_queue = ui_queue
            self._build()
            self.apoth.monitor.status_cb = self._on_status_change
            self.apoth.monitor.start()

        # ── output ──────────────────────────────────────────────

        def _emit(self, text: str, error: bool = False):
            self._log_append(text, "err" if error else "info")

        def _log_append(self, msg: str, tag: str = "info"):
            self.log.configure(state="normal")
            if "\u2713" in msg:
                tag = "ok"
            elif "\u2717" in msg or tag == "err":
                tag = "err"
            elif "\u26a0" in msg:
                tag = "warn"
            self.log.insert("end", msg.rstrip() + "\n", tag)
            self.log.see("end")
            self.log.configure(state="disabled")

        def _on_status_change(self, name: str, status: str, msg: str):
            self.after(0, lambda: self._handle_status_update(name, status, msg))

        def _handle_status_update(self, name: str, status: str, msg: str):
            icon = _STATUS_ICON.get(status, "?")
            self._emit(f"{icon} [{name}] {msg}")
            self._refresh()

        # ── build ────────────────────────────────────────────────

        def _build(self):
            # ── Local-machine panel ─────────────────────────────────
            # "This machine" header — what the user has locally plus
            # which curated US-origin models will run on it. Sits at
            # the top so users see the local picture before they go
            # adding Pi nodes. Lazily imports hardware_detect +
            # model_catalog so a missing module degrades to a one-
            # line "couldn't probe" message instead of a build crash.
            self._build_local_panel()

            top = ttk.Frame(self)
            top.pack(fill="both", expand=True, padx=10, pady=(10, 4))

            left = ttk.Frame(top)
            left.pack(side="left", fill="both", expand=True)
            ttk.Label(left, text="Pi Nodes").pack(anchor="w")
            lb_f = ttk.Frame(left)
            lb_f.pack(fill="both", expand=True, pady=4)
            self.lb = tk.Listbox(lb_f, bg="#181825", fg="#cdd6f4",
                                 selectbackground="#585b70", relief="flat",
                                 font=("Consolas", 10), height=8)
            sb = ttk.Scrollbar(lb_f, command=self.lb.yview)
            self.lb.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            self.lb.pack(side="left", fill="both", expand=True)
            self.lb.bind("<<ListboxSelect>>", self._on_select)

            right = ttk.Frame(top)
            right.pack(side="right", fill="y", padx=(10, 0))

            ttk.Button(right, text="Refresh",      command=self._refresh).pack(fill="x")
            ttk.Button(right, text="Add Node",     command=self._add_edit).pack(fill="x", pady=(4,0))
            ttk.Button(right, text="\U0001f50d Discover Pi", command=self._open_discover).pack(fill="x", pady=(2,0))
            ttk.Button(right, text="Edit Node",    command=lambda: self._add_edit(edit=True)).pack(fill="x", pady=(2,0))
            ttk.Button(right, text="Delete",       command=self._delete).pack(fill="x", pady=(2,0))
            ttk.Separator(right, orient="horizontal").pack(fill="x", pady=8)
            ttk.Button(right, text="Test SSH",     command=self._test_ssh).pack(fill="x")
            ttk.Button(right, text="Check Ollama", command=self._check_ollama).pack(fill="x", pady=(2,0))
            ttk.Button(right, text="Run Command",  command=self._run_cmd).pack(fill="x", pady=(2,0))
            ttk.Separator(right, orient="horizontal").pack(fill="x", pady=8)
            ttk.Button(right, text="\U0001f527 Setup Pi Wizard", command=self._open_wizard).pack(fill="x")
            ttk.Button(right, text="Set Static IP",  command=self._open_static_ip).pack(fill="x", pady=(2,0))
            ttk.Button(right, text="Fix Keepalive",  command=self._run_keepalive).pack(fill="x", pady=(2,0))
            ttk.Button(right, text="Restart Ollama", command=self._restart_ollama).pack(fill="x", pady=(2,0))
            ttk.Separator(right, orient="horizontal").pack(fill="x", pady=8)
            ttk.Button(right, text="Copy Ollama URL", command=self._copy_ollama_url).pack(fill="x")
            ttk.Separator(right, orient="horizontal").pack(fill="x", pady=8)
            ttk.Button(right, text="\U0001f4e6 Model Inventory",
                       command=self._show_model_inventory).pack(fill="x")

            self._detail_var = tk.StringVar(value="Select a node to see details")
            ttk.Label(self, textvariable=self._detail_var,
                      foreground="#89b4fa", wraplength=600).pack(anchor="w", padx=10, pady=(0,4))

            ttk.Label(self, text="Output:").pack(anchor="w", padx=10)
            log_f = ttk.Frame(self)
            log_f.pack(fill="both", expand=True, padx=10, pady=(0,8))
            self.log = tk.Text(log_f, bg="#11111b", fg="#cdd6f4", relief="flat",
                               font=("Consolas", 9), state="disabled", height=14, wrap="word")
            lsb = ttk.Scrollbar(log_f, command=self.log.yview)
            self.log.configure(yscrollcommand=lsb.set)
            lsb.pack(side="right", fill="y")
            self.log.pack(side="left", fill="both", expand=True)
            self.log.tag_config("ok",   foreground="#a6e3a1")
            self.log.tag_config("err",  foreground="#f38ba8")
            self.log.tag_config("warn", foreground="#fab387")
            self.log.tag_config("info", foreground="#cdd6f4")
            self._refresh()

        # ── Local panel — this machine + recommended US-origin models ──

        def _build_local_panel(self) -> None:
            """Top-of-tab "This machine" section.

            Shows the local hardware probe and the curated US-origin
            models that fit. Clicking a model swatch surfaces a one-
            line download instruction in the console log.
            """
            outer = ttk.LabelFrame(self, text="This machine")
            outer.pack(fill="x", padx=10, pady=(8, 0))
            row1 = ttk.Frame(outer)
            row1.pack(fill="x", padx=8, pady=(6, 2))
            self._hw_var = tk.StringVar(value="Probing hardware…")
            ttk.Label(row1, textvariable=self._hw_var,
                      foreground="#94e2d5",
                      font=("Consolas", 9)
                      ).pack(side="left", fill="x", expand=True)
            ttk.Button(row1, text="↻ Re-probe",
                       command=self._refresh_local_panel,
                       width=10).pack(side="right")

            row2 = ttk.Frame(outer)
            row2.pack(fill="x", padx=8, pady=(2, 8))
            ttk.Label(row2, text="Recommended (US-origin):",
                      foreground="#cdd6f4").pack(side="left")
            self._model_btn_frame = ttk.Frame(row2)
            self._model_btn_frame.pack(side="left", fill="x",
                                        expand=True, padx=(6, 0))

            self._refresh_local_panel()

        def _refresh_local_panel(self) -> None:
            """Re-run hardware detection and recompute the recommended
            models. Cheap (<200 ms) so it's safe to call from a button."""
            hw, recs, err = self._probe_local_models()
            if err:
                self._hw_var.set(f"⚠ Hardware probe failed: {err}")
            else:
                # Compact one-liner: CPU / RAM / GPU / VRAM / CUDA
                cpu = (hw.get("cpu_brand") or "CPU unknown")[:40]
                ram = hw.get("ram_gb") or 0
                gpu = hw.get("gpu_name") or "no GPU"
                vram = hw.get("vram_gb")
                cuda = hw.get("cuda_max")
                vram_part = f", {vram:.1f} GB VRAM" if vram else ""
                cuda_part = f", CUDA {cuda}" if cuda else ""
                self._hw_var.set(
                    f"{cpu} | {ram:.1f} GB RAM | {gpu}"
                    f"{vram_part}{cuda_part}"
                )

            # Rebuild the model swatches
            for child in self._model_btn_frame.winfo_children():
                child.destroy()
            if not recs:
                ttk.Label(self._model_btn_frame,
                          text="(no fitting models found)",
                          foreground="#7a7575").pack(side="left")
                return
            for spec in recs[:6]:
                lbl = f"{spec.id}  ({spec.size_gb:.1f} GB)"
                ttk.Button(
                    self._model_btn_frame, text=lbl,
                    command=lambda s=spec: self._on_pick_local_model(s),
                ).pack(side="left", padx=2)

        def _probe_local_models(self):
            """Run hardware_detect + model_catalog. Returns (hw_dict,
            list_of_ModelSpec, error_str). All three are safe to consume
            even on failure — hw is an empty dict, recs is empty list,
            error is the diagnostic string."""
            try:
                import hardware_detect as _hwd
                hw = _hwd.detect() or {}
            except Exception as exc:
                return ({}, [], f"hardware_detect failed: {exc!r}")
            try:
                import model_catalog as _mc
            except Exception as exc:
                return (hw, [], f"model_catalog import failed: {exc!r}")
            vram = hw.get("vram_gb")
            ram = hw.get("ram_gb") or 0
            # GPU-equipped boxes prefer for_vram. CPU-only / mystery-GPU
            # fall back to for_ram (added in phase 4) when present,
            # else use a generous RAM budget against for_vram.
            try:
                if vram and vram > 0:
                    recs = _mc.for_vram(float(vram))
                elif hasattr(_mc, "for_ram"):
                    recs = _mc.for_ram(float(ram))
                else:
                    # Cheap heuristic: treat each GB of RAM as ~0.5 GB
                    # of effective VRAM budget on a CPU-only system.
                    recs = _mc.for_vram(max(2.0, float(ram) * 0.5))
            except Exception as exc:
                return (hw, [], f"model_catalog query failed: {exc!r}")
            return (hw, list(recs), "")

        def _on_pick_local_model(self, spec) -> None:
            """User clicked a recommended-model swatch."""
            try:
                import model_catalog as _mc
                cmd = _mc.download_command(spec)
            except Exception:
                cmd = (f"# install {spec.id} from "
                        f"https://huggingface.co/{spec.hf_repo}")
            self._emit(
                f"📥 {spec.name}  ({spec.params_b:.1f}B, {spec.quant}, "
                f"~{spec.size_gb:.1f} GB)"
            )
            self._emit(f"   {spec.blurb}")
            self._emit(f"   {cmd}")
            self._emit(
                "   (Run that line in a shell to download. Then point "
                "COUNCIL_GGUF_PATH at the resulting file.)"
            )

        # ── list helpers ─────────────────────────────────────────

        def _refresh(self):
            self.lb.delete(0, "end")
            for n in self.apoth.list_nodes():
                icon  = _STATUS_ICON.get(n.status, "?")
                color = _STATUS_COLOR.get(n.status, "#6c7086")
                lbl   = f"{icon} {n.name:<16} {n.username}@{n.host}:{n.port}"
                if n.model:
                    lbl += f"  [{n.model}]"
                if n.last_seen:
                    lbl += f"  seen:{n.last_seen[:10]}"
                self.lb.insert("end", lbl)
                self.lb.itemconfigure(self.lb.size()-1, foreground=color)

        def _selected_node(self) -> Optional[NodeEntry]:
            sel = self.lb.curselection()
            if not sel:
                return None
            name = self.lb.get(sel[0]).split()[1].strip()
            try:
                return self.apoth._get(name)
            except Exception:
                return None

        def _on_select(self, event=None):
            node = self._selected_node()
            if node:
                url = f"http://{node.host}:{node.ollama_port}"
                hw = node.pi_model or "unknown hardware"
                hat = "  AI HAT+" if node.has_ai_hat else ""
                role = f"  role:{node.council_role}" if node.council_role else ""
                installed = f"  models:{len(node.installed_models)}" if node.installed_models else ""
                self._detail_var.set(
                    f"{node.name}  |  {hw}{hat}{role}  |  "
                    f"{node.username}@{node.host}:{node.port}  |  "
                    f"status:{node.status}{installed}  |  "
                    f"active:{node.active_model or chr(0x2014)}"
                )

        # ── node management ──────────────────────────────────────

        def _open_discover(self):
            win = tk.Toplevel(self)
            win.title("Discover Pi")
            win.configure(bg="#1e1e2e")
            win.geometry("560x460")
            win.resizable(True, True)

            ttk.Label(win,
                text="Auto-discover a Raspberry Pi by hostname",
                foreground="#89b4fa", font=("", 10, "bold"),
            ).pack(anchor="w", padx=12, pady=(12, 2))
            ttk.Label(win,
                text=(
                    "Enter the Pi hostname and password.\n"
                    "The Apothecary tries mDNS, ping, ARP, then a subnet scan."
                ),
                foreground="#6c7086", justify="left",
            ).pack(anchor="w", padx=12, pady=(0, 8))
            ttk.Separator(win, orient="horizontal").pack(fill="x", padx=12, pady=4)

            # ── Input fields ──────────────────────────────────────
            f1 = ttk.Frame(win)
            f1.pack(fill="x", padx=12, pady=4)
            ttk.Label(f1, text="Hostname:", width=16).pack(side="left")
            name_var = tk.StringVar(value="raspberrypi")
            ttk.Entry(f1, textvariable=name_var, width=22).pack(side="left", padx=4)
            ttk.Label(f1, text="(without .local)", foreground="#6c7086").pack(side="left")

            f2 = ttk.Frame(win)
            f2.pack(fill="x", padx=12, pady=4)
            ttk.Label(f2, text="Save as:", width=16).pack(side="left")
            label_var = tk.StringVar()
            ttk.Entry(f2, textvariable=label_var, width=22).pack(side="left", padx=4)
            ttk.Label(f2, text="(blank = same as hostname)", foreground="#6c7086").pack(side="left")

            f3 = ttk.Frame(win)
            f3.pack(fill="x", padx=12, pady=4)
            ttk.Label(f3, text="SSH username:", width=16).pack(side="left")
            user_var = tk.StringVar(value="pi")
            ttk.Entry(f3, textvariable=user_var, width=18).pack(side="left", padx=4)

            f4 = ttk.Frame(win)
            f4.pack(fill="x", padx=12, pady=4)
            ttk.Label(f4, text="SSH password:", width=16).pack(side="left")
            pw_var = tk.StringVar()
            ttk.Entry(f4, textvariable=pw_var, show="*", width=22).pack(side="left", padx=4)

            ttk.Separator(win, orient="horizontal").pack(fill="x", padx=12, pady=6)

            # ── Progress log ──────────────────────────────────────
            ttk.Label(win, text="Discovery log:").pack(anchor="w", padx=12)
            logf = ttk.Frame(win)
            logf.pack(fill="both", expand=True, padx=12, pady=(0, 4))
            dlog = tk.Text(
                logf, bg="#11111b", fg="#cdd6f4", font=("Consolas", 9),
                state="disabled", relief="flat", wrap="word", height=9,
            )
            dlsb = ttk.Scrollbar(logf, command=dlog.yview)
            dlog.configure(yscrollcommand=dlsb.set)
            dlsb.pack(side="right", fill="y")
            dlog.pack(side="left", fill="both", expand=True)
            dlog.tag_config("ok",   foreground="#a6e3a1")
            dlog.tag_config("err",  foreground="#f38ba8")
            dlog.tag_config("warn", foreground="#fab387")

            def _dlog(msg: str):
                dlog.configure(state="normal")
                tag = (
                    "ok"   if "\u2713" in msg else
                    "err"  if "\u2717" in msg else
                    "warn" if "\u26a0" in msg else ""
                )
                dlog.insert("end", msg + "\n", tag)
                dlog.see("end")
                dlog.configure(state="disabled")
                win.update_idletasks()

            # ── Buttons ───────────────────────────────────────────
            bf = ttk.Frame(win)
            bf.pack(fill="x", padx=12, pady=(0, 10))

            _result = {"ip": None}

            def _run():
                hostname = name_var.get().strip()
                user     = user_var.get().strip() or "pi"
                password = pw_var.get().strip()
                if not hostname:
                    messagebox.showerror("Missing", "Enter a hostname.", parent=win)
                    return
                disc_btn.configure(state="disabled")
                save_btn.configure(state="disabled")
                dlog.configure(state="normal")
                dlog.delete("1.0", "end")
                dlog.configure(state="disabled")
                _result["ip"] = None

                def _thread():
                    _dlog(f"Searching for '{hostname}' on the network ...")

                    temp = NodeEntry(
                        name=hostname, host="", port=22,
                        username=user, auth_method="password",
                        password=password,
                    )

                    ip = discover_pi(hostname, progress_cb=_dlog)

                    if not ip:
                        win.after(0, lambda: (
                            _dlog("\u2717 Pi not found."),
                            _dlog("  Check: Pi is on, ethernet connected, SSH enabled."),
                            disc_btn.configure(state="normal"),
                        ))
                        return

                    _dlog(f"Confirming via SSH at {ip} ...")
                    real_ip = confirm_and_get_real_ip(
                        self.apoth.engine, temp, ip, password or None, _dlog,
                    )

                    try:
                        temp.host = real_ip
                        rc2, hout, _ = self.apoth.engine.run_ssh(
                            temp, "hostname", password or None, timeout_s=8,
                        )
                        if rc2 == 0 and hout.strip():
                            _dlog(f"  Pi hostname: {hout.strip().splitlines()[0]}")
                    except Exception:
                        pass

                    _result["ip"] = real_ip

                    def _done():
                        disc_btn.configure(state="normal")
                        save_btn.configure(state="normal")
                        _dlog(f"\n\u2713 Ready to save as '{label_var.get().strip() or hostname}' at {real_ip}")
                    win.after(0, _done)

                threading.Thread(target=_thread, daemon=True).start()

            def _save():
                ip = _result.get("ip")
                if not ip:
                    messagebox.showerror("Not found", "Run discovery first.", parent=win)
                    return
                hostname = name_var.get().strip()
                node_label = label_var.get().strip() or hostname
                user     = user_var.get().strip() or "pi"
                password = pw_var.get().strip()
                entry = NodeEntry(
                    name=node_label, host=ip, port=22,
                    username=user, auth_method="password",
                    password=password, status="online",
                    last_seen=now_iso(),
                )
                self.apoth.upsert_node(entry)
                self._refresh()
                self._emit(f"\u2713 Saved '{node_label}' at {ip}")
                win.destroy()
                if messagebox.askyesno(
                    "Run Setup Wizard?",
                    f"Node saved at {ip}.\n\nRun the Pi Setup Wizard now to install Ollama?",
                    parent=self,
                ):
                    self._open_wizard()

            disc_btn = ttk.Button(bf, text="\U0001f50d  Find Pi", command=_run)
            disc_btn.pack(side="left")
            save_btn = ttk.Button(bf, text="\u2713  Save Node", command=_save, state="disabled")
            save_btn.pack(side="left", padx=6)
            ttk.Button(bf, text="Close", command=win.destroy).pack(side="right")

        def _add_edit(self, edit: bool = False):
            """Proper form dialog for adding or editing a node with hardware metadata."""
            if edit:
                node = self._selected_node()
                if not node:
                    messagebox.showinfo("No selection", "Select a node to edit.", parent=self)
                    return
                existing = node
            else:
                existing = None

            win = tk.Toplevel(self)
            win.title("Edit Node" if edit else "Add Node")
            win.configure(bg="#1e1e2e")
            win.geometry("520x620")
            win.resizable(False, True)

            def _lbl(parent, text, **kw):
                ttk.Label(parent, text=text, **kw).pack(anchor="w", padx=12, pady=(6, 1))

            def _row(parent):
                f = ttk.Frame(parent)
                f.pack(fill="x", padx=12, pady=2)
                return f

            def _entry(parent, var, width=32, show=None):
                kw = {"textvariable": var, "width": width}
                if show:
                    kw["show"] = show
                e = ttk.Entry(parent, **kw)
                e.pack(side="left", padx=4)
                return e

            ttk.Label(win, text="Node Configuration",
                      foreground="#89b4fa", font=("", 11, "bold")).pack(anchor="w", padx=12, pady=(12, 4))
            ttk.Separator(win, orient="horizontal").pack(fill="x", padx=12, pady=4)

            # ── Connection fields ──────────────────────────────────
            ttk.Label(win, text="CONNECTION", foreground="#cba6f7",
                      font=("", 9, "bold")).pack(anchor="w", padx=12, pady=(4, 0))

            r = _row(win)
            ttk.Label(r, text="Name:", width=14).pack(side="left")
            v_name = tk.StringVar(value=getattr(existing, "name", ""))
            _entry(r, v_name)

            r = _row(win)
            ttk.Label(r, text="Host / IP:", width=14).pack(side="left")
            v_host = tk.StringVar(value=getattr(existing, "host", ""))
            _entry(r, v_host)

            r = _row(win)
            ttk.Label(r, text="SSH port:", width=14).pack(side="left")
            v_port = tk.StringVar(value=str(getattr(existing, "port", 22)))
            _entry(r, v_port, width=8)
            ttk.Label(r, text="  Ollama port:").pack(side="left")
            v_oll = tk.StringVar(value=str(getattr(existing, "ollama_port", 11434)))
            _entry(r, v_oll, width=8)

            r = _row(win)
            ttk.Label(r, text="Username:", width=14).pack(side="left")
            v_user = tk.StringVar(value=getattr(existing, "username", "pi"))
            _entry(r, v_user, width=16)

            r = _row(win)
            ttk.Label(r, text="Auth method:", width=14).pack(side="left")
            v_auth = tk.StringVar(value=getattr(existing, "auth_method", "password"))
            ttk.Combobox(r, textvariable=v_auth, values=["password", "key"],
                         state="readonly", width=12).pack(side="left", padx=4)

            r = _row(win)
            ttk.Label(r, text="Password:", width=14).pack(side="left")
            v_pw = tk.StringVar(value=getattr(existing, "password", ""))
            _entry(r, v_pw, show="*")

            r = _row(win)
            ttk.Label(r, text="Key path:", width=14).pack(side="left")
            v_key = tk.StringVar(value=getattr(existing, "key_path", ""))
            _entry(r, v_key)

            ttk.Separator(win, orient="horizontal").pack(fill="x", padx=12, pady=8)

            # ── Hardware metadata ──────────────────────────────────
            ttk.Label(win, text="HARDWARE", foreground="#cba6f7",
                      font=("", 9, "bold")).pack(anchor="w", padx=12, pady=(0, 0))

            r = _row(win)
            ttk.Label(r, text="Pi model:", width=14).pack(side="left")
            v_pimodel = tk.StringVar(value=getattr(existing, "pi_model", ""))
            pi_models = list(PI_MODEL_RECOMMENDATIONS.keys())
            ttk.Combobox(r, textvariable=v_pimodel, values=pi_models,
                         state="readonly", width=28).pack(side="left", padx=4)

            r = _row(win)
            v_aihat = tk.BooleanVar(value=getattr(existing, "has_ai_hat", False))
            ttk.Checkbutton(r, text="AI HAT+ attached (26 TOPS)",
                            variable=v_aihat).pack(side="left", padx=16)

            r = _row(win)
            ttk.Label(r, text="Council role:", width=14).pack(side="left")
            v_role = tk.StringVar(value=getattr(existing, "council_role", "unassigned"))
            ttk.Combobox(r, textvariable=v_role,
                         values=["heavy", "fast", "unassigned"],
                         state="readonly", width=14).pack(side="left", padx=4)
            ttk.Label(r, text="  heavy=Sage/Strategist  fast=Intern/Peasant",
                      foreground="#6c7086").pack(side="left")

            # Auto-fill council role when Pi model selected
            def _auto_role(*_):
                pm = v_pimodel.get()
                role = PI_COUNCIL_ROLE.get(pm, "")
                if role:
                    v_role.set(role)
                # Auto-set AI HAT checkbox based on model name
                if "AI HAT" in pm:
                    v_aihat.set(True)
            v_pimodel.trace_add("write", _auto_role)

            ttk.Separator(win, orient="horizontal").pack(fill="x", padx=12, pady=8)

            # ── Notes ─────────────────────────────────────────────
            _lbl(win, "Notes (optional):")
            v_notes = tk.StringVar(value=getattr(existing, "notes", ""))
            ttk.Entry(win, textvariable=v_notes, width=52).pack(anchor="w", padx=16)

            # ── Model recommendations hint ─────────────────────────
            hint_var = tk.StringVar(value="Select a Pi model above to see recommendations")
            hint_lbl = ttk.Label(win, textvariable=hint_var, foreground="#6c7086",
                                  font=("", 8), wraplength=460)
            hint_lbl.pack(anchor="w", padx=12, pady=(6, 0))

            def _update_hint(*_):
                pm = v_pimodel.get()
                recs = PI_MODEL_RECOMMENDATIONS.get(pm, [])
                if recs:
                    hint_var.set("Recommended models: " + ", ".join(recs))
                else:
                    hint_var.set("Select a Pi model above to see recommendations")
            v_pimodel.trace_add("write", _update_hint)

            # ── Save button ───────────────────────────────────────
            def _save():
                nm = v_name.get().strip()
                hs = v_host.get().strip()
                if not nm or not hs:
                    messagebox.showerror("Required", "Name and Host are required.", parent=win)
                    return
                try:
                    port_v = int(v_port.get() or 22)
                    oll_v  = int(v_oll.get() or 11434)
                except ValueError:
                    messagebox.showerror("Invalid", "Ports must be integers.", parent=win)
                    return
                entry = NodeEntry(
                    name=nm, host=hs, port=port_v, username=v_user.get().strip() or "pi",
                    auth_method=v_auth.get(), password=v_pw.get(),
                    key_path=v_key.get().strip(), ollama_port=oll_v,
                    notes=v_notes.get().strip(),
                    pi_model=v_pimodel.get(),
                    has_ai_hat=bool(v_aihat.get()),
                    ai_hat_tops=26.0 if v_aihat.get() else 0.0,
                    ram_gb=int(v_pimodel.get().replace("GB)", "").split("(")[-1])
                           if "GB)" in v_pimodel.get() else 0,
                    council_role=v_role.get(),
                    model=getattr(existing, "model", "") if existing else "",
                    installed_models=getattr(existing, "installed_models", []) if existing else [],
                    model_log=getattr(existing, "model_log", []) if existing else [],
                    active_model=getattr(existing, "active_model", "") if existing else "",
                    status=getattr(existing, "status", "unknown") if existing else "unknown",
                    last_seen=getattr(existing, "last_seen", "") if existing else "",
                    created_at=getattr(existing, "created_at", now_iso()) if existing else now_iso(),
                )
                self.apoth.upsert_node(entry)
                self._refresh()
                self._emit(f"\u2713 Node '{nm}' saved  [{entry.pi_model or 'unknown hardware'}]")
                win.destroy()

            bf = ttk.Frame(win)
            bf.pack(fill="x", padx=12, pady=10)
            ttk.Button(bf, text="\u2713  Save", command=_save).pack(side="left")
            ttk.Button(bf, text="Cancel", command=win.destroy).pack(side="right")

        def _show_model_inventory(self):
            """Show installed models and call log for the selected node."""
            node = self._selected_node()
            if not node:
                messagebox.showinfo("No selection", "Select a node first.", parent=self)
                return

            win = tk.Toplevel(self)
            win.title(f"Model Inventory — {node.name}")
            win.configure(bg="#1e1e2e")
            win.geometry("580x500")

            ttk.Label(win, text=f"{node.name}  [{node.pi_model or 'unknown'}]"
                      + ("  AI HAT+" if node.has_ai_hat else ""),
                      foreground="#89b4fa", font=("", 10, "bold")).pack(anchor="w", padx=12, pady=(10, 2))
            ttk.Label(win, text=f"Council role: {node.council_role or 'unassigned'}  |  "
                      f"Active model: {node.active_model or '—'}",
                      foreground="#6c7086").pack(anchor="w", padx=12)

            ttk.Separator(win, orient="horizontal").pack(fill="x", padx=12, pady=6)

            # Installed models
            ttk.Label(win, text="INSTALLED MODELS", foreground="#cba6f7",
                      font=("", 9, "bold")).pack(anchor="w", padx=12)
            inst_box = tk.Text(win, height=6, bg="#11111b", fg="#cdd6f4",
                               font=("Consolas", 9), state="disabled", relief="flat")
            inst_box.pack(fill="x", padx=12, pady=(2, 6))
            inst_box.configure(state="normal")
            if node.installed_models:
                for m in node.installed_models:
                    tag = "active" if m == node.active_model else ""
                    inst_box.insert("end", f"  {'▶ ' if tag else '  '}{m}\n", tag)
                inst_box.tag_config("active", foreground="#a6e3a1", font=("Consolas", 9, "bold"))
            else:
                inst_box.insert("end", "  (no models recorded — click Refresh below)\n", "")
            inst_box.configure(state="disabled")

            # Call log
            ttk.Label(win, text="RECENT CALL LOG (last 50)", foreground="#cba6f7",
                      font=("", 9, "bold")).pack(anchor="w", padx=12)
            log_box = tk.Text(win, height=12, bg="#11111b", fg="#cdd6f4",
                              font=("Consolas", 9), state="disabled", relief="flat")
            sb = ttk.Scrollbar(win, command=log_box.yview)
            log_box.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y", padx=(0, 12))
            log_box.pack(fill="both", expand=True, padx=(12, 0), pady=(2, 6))
            log_box.configure(state="normal")
            if node.model_log:
                for entry in reversed(node.model_log[-50:]):
                    ts = entry.get("ts", "")[:16]
                    model = entry.get("model", "?")
                    role = entry.get("role", "?")
                    log_box.insert("end", f"  {ts}  {role:<14} {model}\n")
            else:
                log_box.insert("end", "  (no calls logged yet)\n")
            log_box.configure(state="disabled")

            bf = ttk.Frame(win)
            bf.pack(fill="x", padx=12, pady=8)

            def _refresh_models():
                self._emit(f"Refreshing model list from {node.name}...")
                def _t():
                    models = self.apoth.refresh_installed_models(node)
                    def _done():
                        inst_box.configure(state="normal")
                        inst_box.delete("1.0", "end")
                        if models:
                            for m in models:
                                tag = "active" if m == node.active_model else ""
                                inst_box.insert("end", f"  {'▶ ' if tag else '  '}{m}\n", tag)
                        else:
                            inst_box.insert("end", "  (no models found)\n")
                        inst_box.configure(state="disabled")
                        self._emit(f"\u2713 {node.name}: {len(models)} model(s) found")
                    win.after(0, _done)
                import threading
                threading.Thread(target=_t, daemon=True).start()

            ttk.Button(bf, text="\u21ba  Refresh Model List", command=_refresh_models).pack(side="left")
            ttk.Button(bf, text="Close", command=win.destroy).pack(side="right")

        def _delete(self):
            node = self._selected_node()
            if not node:
                return
            if messagebox.askyesno("Confirm", f"Delete node '{node.name}'?", parent=self):
                self.apoth.delete_node(node.name)
                self._refresh()
                self._emit(f"Deleted '{node.name}'.")

        # ── SSH actions ──────────────────────────────────────────

        def _pw_override(self) -> Optional[str]:
            pw = simpledialog.askstring("Password override",
                "Password (blank = use stored):", show="*", parent=self)
            return pw.strip() if (pw and pw.strip()) else None

        def _test_ssh(self):
            node = self._selected_node()
            if not node:
                return
            ok, msg = self.apoth.test(node.name, password_override=self._pw_override())
            self._emit(("\u2713" if ok else "\u2717") + f" SSH test [{node.name}]: {msg}", not ok)

        def _check_ollama(self):
            node = self._selected_node()
            if not node:
                return
            ok, msg = self.apoth.engine.check_ollama(node, self._pw_override())
            self._emit(("\u2713" if ok else "\u2717") + f" Ollama [{node.name}]: {msg}", not ok)

        def _run_cmd(self):
            node = self._selected_node()
            if not node:
                return
            cmd = simpledialog.askstring("Remote command", "Shell command:", parent=self)
            if not cmd:
                return
            try:
                rc, out, err = self.apoth.run(node.name, cmd,
                    password_override=self._pw_override(), timeout_s=30)
                self._emit(f"rc={rc}")
                if out.strip():
                    self._emit(out.strip())
                if err.strip():
                    self._emit(err.strip(), True)
            except Exception as e:
                self._emit(f"\u2717 {e}", True)

        def _restart_ollama(self):
            node = self._selected_node()
            if not node:
                return
            self._emit(f"Restarting Ollama on {node.name}...")
            try:
                rc, _, _ = self.apoth.run(
                    node.name,
                    "sudo systemctl restart ollama 2>/dev/null "
                    "|| (pkill ollama; sleep 1; ollama serve &)",
                    password_override=self._pw_override(), timeout_s=20,
                )
                self._emit(("\u2713" if rc==0 else "\u26a0") + f" Ollama restart rc={rc}")
            except Exception as e:
                self._emit(f"\u2717 {e}", True)

        def _copy_ollama_url(self):
            node = self._selected_node()
            if not node:
                return
            url = f"http://{node.host}:{node.ollama_port}"
            try:
                self.clipboard_clear()
                self.clipboard_append(url)
                self._emit(f"Copied: {url}")
            except Exception:
                self._emit(f"Ollama URL: {url}")

        # ── Pi Setup Wizard ──────────────────────────────────────

        def _open_wizard(self):
            node = self._selected_node()
            if not node:
                messagebox.showinfo("No selection",
                    "Add a node first (name, host/IP, SSH credentials),\n"
                    "then click Setup Pi Wizard.", parent=self)
                return

            win = tk.Toplevel(self)
            win.title(f"Pi Setup Wizard — {node.name}")
            win.configure(bg="#1e1e2e")
            win.geometry("680x600")
            win.resizable(True, True)

            ttk.Label(win,
                text=f"Setting up:  {node.name}  ({node.username}@{node.host})",
                foreground="#89b4fa", font=("", 10, "bold")).pack(anchor="w", padx=12, pady=(10,2))
            ttk.Label(win,
                text="This wizard will: check OS, install Ollama, configure it to accept\n"
                     "remote connections, pull your chosen model, and fix ethernet stability.",
                foreground="#6c7086", justify="left").pack(anchor="w", padx=12, pady=(0,8))
            ttk.Separator(win, orient="horizontal").pack(fill="x", padx=12, pady=4)

            # ── Model row ─────────────────────────────────────────
            mf = ttk.Frame(win)
            mf.pack(fill="x", padx=12, pady=4)
            ttk.Label(mf, text="Model:").pack(side="left")
            model_var = tk.StringVar(value=node.model or "qwen2.5:3b")
            ttk.Entry(mf, textvariable=model_var, width=28).pack(side="left", padx=6)
            rec_var = tk.StringVar(value="Hardware suggestions \u25be")
            rec_cb  = ttk.Combobox(mf, textvariable=rec_var, width=26, state="readonly")
            rec_cb["values"] = list(PI_MODEL_RECOMMENDATIONS.keys())
            def _on_rec(event=None):
                models = PI_MODEL_RECOMMENDATIONS.get(rec_var.get(), [])
                if models:
                    model_var.set(models[0])
            rec_cb.bind("<<ComboboxSelected>>", _on_rec)
            rec_cb.pack(side="left")

            # ── Desktop IP row ────────────────────────────────────
            df = ttk.Frame(win)
            df.pack(fill="x", padx=12, pady=4)
            ttk.Label(df, text="Your desktop IP:").pack(side="left")
            desktop_ip_var = tk.StringVar()
            try:
                import socket as _s
                with _s.socket(_s.AF_INET, _s.SOCK_DGRAM) as _sock:
                    _sock.connect(("8.8.8.8", 80))
                    desktop_ip_var.set(_sock.getsockname()[0])
            except Exception:
                desktop_ip_var.set("")
            ttk.Entry(df, textvariable=desktop_ip_var, width=18).pack(side="left", padx=6)
            ttk.Label(df,
                text="Pi will ping this every 5 min to keep ethernet link alive",
                foreground="#6c7086").pack(side="left")

            # ── Password row ──────────────────────────────────────
            pf = ttk.Frame(win)
            pf.pack(fill="x", padx=12, pady=4)
            ttk.Label(pf, text="SSH password override:").pack(side="left")
            pw_var = tk.StringVar()
            ttk.Entry(pf, textvariable=pw_var, show="*", width=20).pack(side="left", padx=6)
            ttk.Label(pf, text="(blank = use stored)", foreground="#6c7086").pack(side="left")

            ttk.Separator(win, orient="horizontal").pack(fill="x", padx=12, pady=6)

            # ── Progress log ──────────────────────────────────────
            ttk.Label(win, text="Progress:").pack(anchor="w", padx=12)
            pf2 = ttk.Frame(win)
            pf2.pack(fill="both", expand=True, padx=12, pady=(0,4))
            prog = tk.Text(pf2, bg="#11111b", fg="#cdd6f4", font=("Consolas", 9),
                           state="disabled", relief="flat", wrap="word", height=10)
            psb  = ttk.Scrollbar(pf2, command=prog.yview)
            prog.configure(yscrollcommand=psb.set)
            psb.pack(side="right", fill="y")
            prog.pack(side="left", fill="both", expand=True)
            prog.tag_config("ok",   foreground="#a6e3a1")
            prog.tag_config("err",  foreground="#f38ba8")
            prog.tag_config("warn", foreground="#fab387")
            prog.tag_config("hdr",  foreground="#89b4fa")

            def _prog(msg: str, error: bool = False):
                prog.configure(state="normal")
                tag = "err" if error else ("ok" if "\u2713" in msg else
                      ("warn" if "\u26a0" in msg else
                       ("hdr" if (msg.startswith("[") or msg.startswith("=")) else "")))
                prog.insert("end", msg + "\n", tag)
                prog.see("end")
                prog.configure(state="disabled")
                win.update_idletasks()

            # ── Buttons packed with side=bottom so they are never hidden ─
            bf = ttk.Frame(win)
            bf.pack(fill="x", padx=12, pady=(0,10), side="bottom")

            def _run():
                model   = model_var.get().strip()
                desk_ip = desktop_ip_var.get().strip()
                pw_over = pw_var.get().strip() or None
                if not model:
                    messagebox.showerror("Missing model", "Enter a model name.", parent=win)
                    return
                run_btn.configure(state="disabled")
                prog.configure(state="normal")
                prog.delete("1.0", "end")
                prog.configure(state="disabled")

                def _thread():
                    ok, msg = self.apoth.provision_pi(
                        node.name, model, desk_ip,
                        password_override=pw_over,
                        progress_cb=lambda m, e: win.after(0, lambda m=m,e=e: _prog(m,e)),
                    )
                    def _done():
                        run_btn.configure(state="normal")
                        self._refresh()
                        if ok:
                            ollama_url = f"http://{node.host}:{node.ollama_port}"
                            if messagebox.askyesno(
                                "Register with Council?",
                                f"\u2713 Pi setup complete!\n\n"
                                f"Add {node.name} to the Council dispatcher?\n"
                                f"URL: {ollama_url}\n\n"
                                "I'll help you update launch_council.bat.",
                                parent=win,
                            ):
                                self._register_with_council(node, ollama_url)
                    win.after(0, _done)

                threading.Thread(target=_thread, daemon=True).start()

            run_btn = ttk.Button(bf, text="\u25b6  Run Full Setup", command=_run)
            run_btn.pack(side="left")
            ttk.Button(bf, text="Close", command=win.destroy).pack(side="right")

        def _register_with_council(self, node: NodeEntry, ollama_url: str):
            import pathlib, re
            bat_candidates = [
                pathlib.Path.home() / "council_ai" / "launch_council.bat",
                pathlib.Path.home() / "Desktop"    / "launch_council.bat",
                pathlib.Path("launch_council.bat"),
            ]
            bat_path = next((p for p in bat_candidates if p.exists()), None)

            win = tk.Toplevel(self)
            win.title("Register Pi with Council")
            win.configure(bg="#1e1e2e")
            win.geometry("580x320")

            ttk.Label(win,
                text="Add this to your launch_council.bat:",
                foreground="#89b4fa", font=("", 10)).pack(anchor="w", padx=12, pady=(12,4))
            ttk.Label(win,
                text=f"  set COUNCIL_PI_HOSTS={ollama_url}",
                foreground="#a6e3a1", font=("Consolas", 10)).pack(anchor="w", padx=12)
            ttk.Label(win,
                text="\nMultiple Pis? Comma-separate:\n"
                     "  set COUNCIL_PI_HOSTS=http://192.168.1.50:11434,http://192.168.1.51:11434",
                foreground="#6c7086", justify="left").pack(anchor="w", padx=12, pady=4)

            if bat_path:
                ttk.Label(win,
                    text=f"\nFound: {bat_path}",
                    foreground="#a6e3a1").pack(anchor="w", padx=12)

                def _patch():
                    try:
                        text    = bat_path.read_text(encoding="utf-8")
                        new_ln  = f"set COUNCIL_PI_HOSTS={ollama_url}"
                        if "COUNCIL_PI_HOSTS" in text:
                            text = re.sub(r"set COUNCIL_PI_HOSTS=[^\r\n]*", new_ln, text)
                        else:
                            text = text.replace(
                                "set OLLAMA_MAX_LOADED_MODELS",
                                new_ln + "\nset OLLAMA_MAX_LOADED_MODELS", 1,
                            )
                        bat_path.write_text(text, encoding="utf-8")
                        self._emit(f"\u2713 Updated {bat_path.name} with COUNCIL_PI_HOSTS")
                        win.destroy()
                    except Exception as e:
                        messagebox.showerror("Error", f"Could not update .bat:\n{e}", parent=win)

                ttk.Button(win, text=f"Write to {bat_path.name}", command=_patch).pack(padx=12, pady=8)
            else:
                ttk.Label(win,
                    text="launch_council.bat not found in expected locations.\n"
                         "Add the line above manually.",
                    foreground="#f38ba8").pack(anchor="w", padx=12, pady=8)

            ttk.Button(win, text="Close", command=win.destroy).pack(pady=4)

        # ── Static IP helper ─────────────────────────────────────

        def _open_static_ip(self):
            node = self._selected_node()
            if not node:
                messagebox.showinfo("No selection", "Select a node first.", parent=self)
                return
            win = tk.Toplevel(self)
            win.title(f"Set Static IP — {node.name}")
            win.configure(bg="#1e1e2e")
            win.geometry("420x210")
            ttk.Label(win,
                text="Assigning a static IP prevents the Pi's address from\n"
                     "changing on reboot, which is the #1 cause of lost connections.",
                foreground="#6c7086", justify="left").pack(anchor="w", padx=12, pady=(12,6))
            ttk.Label(win, text="Static IP to assign (e.g. 192.168.1.50):").pack(anchor="w", padx=12)
            ip_var = tk.StringVar(value=node.host)
            ttk.Entry(win, textvariable=ip_var, width=22).pack(padx=12, anchor="w", pady=2)
            ttk.Label(win, text="Gateway (e.g. 192.168.1.1):").pack(anchor="w", padx=12, pady=(6,0))
            gw_var = tk.StringVar(value="192.168.1.1")
            ttk.Entry(win, textvariable=gw_var, width=22).pack(padx=12, anchor="w", pady=2)

            def _run():
                static_ip = ip_var.get().strip()
                gateway   = gw_var.get().strip()
                if not static_ip or not gateway:
                    messagebox.showerror("Missing", "Fill in both fields.", parent=win)
                    return
                steps = _render_steps("set_static_ip_eth", {
                    "static_ip": static_ip, "gateway": gateway,
                    "host": node.host, "model": "", "model_base": "", "desktop_ip": "",
                })
                win.destroy()
                def _thread():
                    ok, _ = self.apoth.engine.run_task_sequence(
                        node, steps, node.password or None,
                        progress_cb=lambda m,e: self.after(0, lambda m=m,e=e: self._emit(m,e)),
                    )
                    if ok:
                        node.host = static_ip
                        self.apoth.upsert_node(node)
                    self.after(0, lambda: (
                        self._emit(("\u2713" if ok else "\u2717") + f" Static IP {'set' if ok else 'FAILED'}: {static_ip}", not ok),
                        self._refresh(),
                    ))
                threading.Thread(target=_thread, daemon=True).start()

            ttk.Button(win, text="Apply Static IP", command=_run).pack(pady=10)

        # ── Keepalive fix ────────────────────────────────────────

        def _run_keepalive(self):
            node = self._selected_node()
            if not node:
                return
            desktop_ip = simpledialog.askstring(
                "Desktop IP",
                "Your desktop's LAN IP address:\n"
                "(Pi will ping this every 5 min to keep the ethernet link alive)",
                parent=self,
            )
            if not desktop_ip:
                return
            steps = _render_steps("keepalive_setup", {
                "desktop_ip": desktop_ip,
                "host": node.host, "model": "", "model_base": "",
            })
            self._emit(f"Installing ethernet keepalive on {node.name}...")

            def _thread():
                ok, _ = self.apoth.engine.run_task_sequence(
                    node, steps, node.password or None,
                    progress_cb=lambda m,e: self.after(0, lambda m=m,e=e: self._emit(m,e)),
                )
                self.after(0, lambda: self._emit(
                    ("\u2713" if ok else "\u2717") +
                    f" Keepalive {'installed' if ok else 'FAILED'} on {node.name}", not ok))
            threading.Thread(target=_thread, daemon=True).start()