"""Talking to a Pi over SSH: adopt it, provision it, register it.

What changes from the Apothecary wizard (apothecary_engine.py), and why:

  * HOST KEYS ARE PINNED. The old engine used AutoAddPolicy — it trusted
    whatever answered at that address, every time. Here the Council keeps its
    own known_hosts (in the app key folder). A Pi the Council imaged is
    expected to present the host key the Council generated for it; any other
    first-seen key raises HostKeyUnknown with its fingerprint for the user to
    confirm once; a changed key is refused (paramiko's BadHostKeyException).
  * THE PASSWORD IS USED ONCE. `adopt` logs in with it, installs the Council's
    public key, proves the key login works, and the node is registered with
    key auth and NO stored password (the old wizard wrote it in plain text to
    vault/node_registry.json).
  * OLLAMA IS NOT OPEN TO THE WHOLE NETWORK. The old wizard bound Ollama to
    0.0.0.0 and opened port 11434 to everyone. Here a firewall (nftables)
    lets only THIS PC reach port 11434 before Ollama is told to listen on the
    network; `refresh_firewall` re-applies it if this PC's address changes.
    (An SSH tunnel would be tighter still, but the engine treats loopback as
    "this PC", so a tunnel would need an engine routing change.)
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
import shlex
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import pi_secrets

_MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,80}$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


class HostKeyUnknown(Exception):
    def __init__(self, host: str, fingerprint: str, key=None):
        super().__init__(f"{host} presented a host key the Council has not seen: {fingerprint}")
        self.host, self.fingerprint, self.key = host, fingerprint, key


def fingerprint(key) -> str:
    """OpenSSH-style 'SHA256:...' fingerprint of a paramiko key."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def known_hosts_path(key_dir: Optional[Path] = None) -> Path:
    return Path(key_dir or pi_secrets.app_key_dir()) / "known_hosts"


def _policy(expected_public: Optional[str], approved_fp: Optional[str], kh_path: Path):
    import paramiko

    class Pinned(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            line = f"{key.get_name()} {key.get_base64()}"
            fp = fingerprint(key)
            ok = ((expected_public and " ".join(expected_public.split()[:2]) == line)
                  or (approved_fp and approved_fp == fp))
            if not ok:
                raise HostKeyUnknown(hostname, fp, key)
            client.get_host_keys().add(hostname, key.get_name(), key)
            kh_path.parent.mkdir(parents=True, exist_ok=True)
            client.save_host_keys(str(kh_path))
    return Pinned()


def connect(host: str, username: str, *, password: Optional[str] = None,
            port: int = 22, expected_host_key: Optional[str] = None,
            approved_fingerprint: Optional[str] = None, timeout: float = 15.0,
            key_dir: Optional[Path] = None):
    """A connected paramiko SSHClient. With no password, the Council's key."""
    import paramiko
    kh = known_hosts_path(key_dir)
    client = paramiko.SSHClient()
    if kh.exists():
        client.load_host_keys(str(kh))
    client.set_missing_host_key_policy(_policy(expected_host_key, approved_fingerprint, kh))
    kw = dict(hostname=host, port=port, username=username, timeout=timeout,
              auth_timeout=timeout, banner_timeout=timeout,
              look_for_keys=False, allow_agent=False)
    if password is not None:
        kw["password"] = password
    else:
        priv, _pub = pi_secrets.council_key(key_dir)
        kw["pkey"] = paramiko.Ed25519Key.from_private_key_file(str(priv))
    client.connect(**kw)
    return client


def run(client, command: str, timeout: float = 120.0,
        on_line: Optional[Callable[[str], None]] = None) -> Tuple[int, str, str]:
    """Run ``command``; stream stdout lines to ``on_line``; (rc, out, err)."""
    chan = client.get_transport().open_session(timeout=timeout)
    chan.settimeout(timeout)
    chan.exec_command(command)
    out, err, buf = [], [], ""
    start = time.time()
    while True:
        while chan.recv_ready():
            piece = chan.recv(4096).decode("utf-8", "replace")
            out.append(piece)
            if on_line:
                buf += piece
                *lines, buf = buf.split("\n")
                for ln in lines:
                    on_line(ln)
        while chan.recv_stderr_ready():
            err.append(chan.recv_stderr(4096).decode("utf-8", "replace"))
        if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
            break
        if time.time() - start > timeout:
            chan.close()
            raise TimeoutError(f"'{command[:60]}' took longer than {timeout:.0f} s")
        time.sleep(0.05)
    if on_line and buf:
        on_line(buf)
    return chan.recv_exit_status(), "".join(out), "".join(err)


@dataclass
class PiFacts:
    hostname: str = ""
    arch: str = ""
    ram_gb: float = 0.0
    os_name: str = ""
    model: str = ""

    @property
    def is_64bit(self) -> bool:
        return self.arch in ("aarch64", "arm64", "x86_64")


FACTS_CMD = ("hostname; uname -m; awk '/MemTotal/ {print $2}' /proc/meminfo; "
             ". /etc/os-release && echo \"$PRETTY_NAME\"; "
             "tr -d '\\0' < /proc/device-tree/model 2>/dev/null; echo")


def facts(client) -> PiFacts:
    rc, out, _ = run(client, FACTS_CMD, timeout=20)
    lines = (out.splitlines() + [""] * 5)[:5]
    try:
        ram = round(int(lines[2]) / 1024 / 1024, 1)
    except ValueError:
        ram = 0.0
    return PiFacts(hostname=lines[0].strip(), arch=lines[1].strip(), ram_gb=ram,
                   os_name=lines[3].strip(), model=lines[4].strip())


def install_key_cmd(public_key: str) -> str:
    q = shlex.quote(public_key.strip())
    return ("umask 077 && mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
            f"(grep -qxF {q} ~/.ssh/authorized_keys || echo {q} >> ~/.ssh/authorized_keys)")


@dataclass
class Adopted:
    host: str
    username: str
    fingerprint: str
    facts: PiFacts


def adopt(host: str, username: str, password: str, *, port: int = 22,
          approved_fingerprint: Optional[str] = None,
          expected_host_key: Optional[str] = None,
          key_dir: Optional[Path] = None) -> Adopted:
    """Log in once with the password, install the Council's key, prove the
    key works. Raises HostKeyUnknown (show the fingerprint, ask, call again
    with ``approved_fingerprint``) or paramiko's auth errors."""
    if not _USER_RE.match(username or ""):
        raise ValueError("that is not a valid Linux username")
    _priv, pub = pi_secrets.council_key(key_dir)
    c = connect(host, username, password=password, port=port, key_dir=key_dir,
                approved_fingerprint=approved_fingerprint,
                expected_host_key=expected_host_key)
    try:
        rc, _o, err = run(c, install_key_cmd(pub), timeout=20)
        if rc != 0:
            raise RuntimeError(f"could not install the Council's key: {err.strip()[:200]}")
    finally:
        c.close()
    c = connect(host, username, port=port, key_dir=key_dir)      # by key now
    try:
        fp = fingerprint(c.get_transport().get_remote_server_key())
        return Adopted(host, username, fp, facts(c))
    finally:
        c.close()


def this_pc_ip_toward(host: str) -> str:
    """The address this PC uses to reach ``host`` (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    finally:
        s.close()


FIREWALL_FILE = "/etc/council/ollama-firewall.nft"
FIREWALL_UNIT = "council-ollama-firewall.service"


def firewall_rules(pc_ip: str, port: int = 11434) -> str:
    ip = ipaddress.ip_address(pc_ip)
    fam = "ip" if ip.version == 4 else "ip6"
    return ("table inet council\n"
            "delete table inet council\n"
            "table inet council {\n"
            "  chain input {\n"
            "    type filter hook input priority 0; policy accept;\n"
            "    iif lo accept\n"
            f"    tcp dport {int(port)} {fam} saddr {ip} accept\n"
            f"    tcp dport {int(port)} drop\n"
            "  }\n"
            "}\n")


#: Every first-boot file the Council writes carries this line (firstboot.py).
FIRSTBOOT_MARKER = "Written by The Council's Pi setup."
_SCRUBBED_USER_DATA = (
    "#cloud-config\n"
    "# First boot is done. The Council removed what it had put here (the Pi's\n"
    "# SSH host private key and the password hash). meta-data is unchanged, so\n"
    "# cloud-init does not run first-boot setup again.\n")


def scrub_firstboot_cmd(boot_dirs: Tuple[str, ...] = ("/boot/firmware", "/boot")) -> str:
    """Rewrite the card's cloud-init user-data — ONLY a file the Council
    wrote (it carries FIRSTBOOT_MARKER) — as a comment-only file once the
    Pi is up. It held the Pi's SSH HOST PRIVATE key and the password hash
    on the FAT boot partition, which every local account on the Pi (and
    anyone holding the card) can read: enough to impersonate the Pi to the
    Council, defeating the pinned host key. meta-data keeps its instance id,
    so cloud-init's first-boot modules do not run again. network-config
    (the Wi-Fi key) is left alone: whether cloud-init re-renders the network
    from it on a later boot is not verified on a real Pi yet, and a Pi that
    loses its Wi-Fi cannot be reached to fix it."""
    q = shlex.quote
    checks = []
    for d in boot_dirs:
        f = f"{d}/user-data"
        checks.append(f"if [ -f {q(f)} ] && sudo grep -qF {q(FIRSTBOOT_MARKER)} {q(f)}; then "
                      f"printf %s {q(_SCRUBBED_USER_DATA)} | sudo tee {q(f + '.council-tmp')} "
                      f">/dev/null && sudo mv -f {q(f + '.council-tmp')} {q(f)} || exit 1; fi")
    return "; ".join(checks) + "; sync"


def provision_steps(model: str, pc_ip: str, port: int = 11434
                    ) -> List[Tuple[str, str, float]]:
    """(what the user sees, command, timeout s). The first-boot secrets come
    off the card first; the firewall goes up BEFORE Ollama listens on the
    network."""
    if not _MODEL_RE.match(model or ""):
        raise ValueError(f"not a model name: {model!r}")
    rules = shlex.quote(firewall_rules(pc_ip, port))
    unit = shlex.quote(
        "[Unit]\nDescription=Only The Council's PC may reach Ollama\n"
        "Before=ollama.service\nAfter=network-pre.target\n"
        f"[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/usr/sbin/nft -f {FIREWALL_FILE}\n"
        "[Install]\nWantedBy=multi-user.target\n")
    override = shlex.quote(f"[Service]\nEnvironment=\"OLLAMA_HOST=0.0.0.0:{int(port)}\"\n")
    return [
        ("Remove the Pi's first-boot secrets from its card", scrub_firstboot_cmd(), 60),
        ("Check the Pi can reach the internet (to install Ollama)",
         "curl -fsI --max-time 15 https://ollama.com >/dev/null && echo ok", 30),
        ("Install the firewall tool (nftables)",
         "command -v nft >/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y nftables",
         600),
        ("Let only this PC reach Ollama's port",
         f"sudo mkdir -p /etc/council && printf %s {rules} | sudo tee {FIREWALL_FILE} >/dev/null && "
         f"printf %s {unit} | sudo tee /etc/systemd/system/{FIREWALL_UNIT} >/dev/null && "
         f"sudo systemctl daemon-reload && sudo systemctl enable --now {FIREWALL_UNIT} && "
         f"sudo systemctl restart {FIREWALL_UNIT}", 60),
        ("Install Ollama (from ollama.com)",
         "command -v ollama >/dev/null || (curl -fsSL https://ollama.com/install.sh | sh)", 1200),
        ("Let Ollama listen on the network (behind that firewall)",
         f"sudo mkdir -p /etc/systemd/system/ollama.service.d && printf %s {override} | "
         "sudo tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null && "
         "sudo systemctl daemon-reload && sudo systemctl enable ollama && "
         "sudo systemctl restart ollama", 60),
        (f"Download the model {model} on the Pi", f"ollama pull {shlex.quote(model)}", 3600),
        ("Check Ollama answers", f"curl -fs --max-time 10 http://127.0.0.1:{int(port)}/api/tags", 30),
    ]


@dataclass
class StepResult:
    label: str
    ok: bool
    output: str = ""


def provision(host: str, username: str, model: str, *, port: int = 22,
              pc_ip: Optional[str] = None, key_dir: Optional[Path] = None,
              on_step: Optional[Callable[[str], None]] = None,
              on_line: Optional[Callable[[str], None]] = None) -> List[StepResult]:
    """Run provision_steps over the Council's key; stop at the first failure."""
    pc_ip = pc_ip or this_pc_ip_toward(host)
    results: List[StepResult] = []
    c = connect(host, username, port=port, key_dir=key_dir)
    try:
        for label, cmd, timeout in provision_steps(model, pc_ip):
            if on_step:
                on_step(label)
            try:
                rc, out, err = run(c, cmd, timeout=timeout, on_line=on_line)
                ok = rc == 0
                text = (out + err)[-2000:]
            except Exception as exc:                      # noqa: BLE001
                ok, text = False, str(exc)
            results.append(StepResult(label, ok, text))
            if not ok:
                break
    finally:
        c.close()
    return results


def refresh_firewall(host: str, username: str, *, port: int = 22,
                     key_dir: Optional[Path] = None) -> str:
    """Re-point the Ollama firewall at this PC's current address."""
    pc_ip = this_pc_ip_toward(host)
    label, cmd, timeout = next(s for s in provision_steps("llama3.2:1b", pc_ip)
                               if FIREWALL_FILE in s[1] and FIREWALL_UNIT in s[1])
    c = connect(host, username, port=port, key_dir=key_dir)
    try:
        rc, _o, err = run(c, cmd, timeout=timeout)
    finally:
        c.close()
    if rc != 0:
        raise RuntimeError(err.strip()[:300] or "could not update the firewall")
    return pc_ip


def register(vault, *, name: str, host: str, username: str, model: str,
             facts_: Optional[PiFacts] = None, key_dir: Optional[Path] = None):
    """Add or update the node — key auth, NO password stored."""
    from council_core import apothecary as apoth
    priv, _pub = pi_secrets.council_key(key_dir)
    reg = apoth._ae.NodeRegistry(str(apoth.registry_path(Path(vault))))
    existing = next((n for n in reg.list_nodes() if n.name == name), None)
    node = existing or apoth.NodeEntry(name=name, host=host)
    node.host, node.username, node.auth_method = host, username, "key"
    node.key_path, node.password, node.model = str(priv), "", model
    node.active_model = model
    if facts_:
        node.ram_gb = int(round(facts_.ram_gb))
        node.pi_model = facts_.model or node.pi_model
    node.notes = (node.notes or "") if existing else "Set up by The Council (key login, firewalled Ollama)."
    reg.upsert(node)
    return node
