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

The user's decisions of 2026-10-07, built here:

  * OLLAMA IS ONE PINNED, CHECKED RELEASE (decision b). No 'curl | sh': the
    Pi downloads exactly OLLAMA_PIN_URL, and installs nothing unless its size
    and SHA-256 are the ones recorded below. Updating Ollama = updating those
    four values.
  * NO MODEL IS DOWNLOADED DURING SETUP (decision a). `pull_model` runs only
    when the user presses "Download on the Pi".
  * SSH TAKES KEYS ONLY once the Council's key login works (decision c): an
    sshd_config.d drop-in, checked with 'sshd -t' before the reload, and a
    NEW key login proven afterwards — or password login goes back on over
    the session still open. `password_login_help` says how to undo it.
  * THE WI-FI KEY LEAVES THE CARD (decision d) once NetworkManager keeps the
    Wi-Fi profile on the Pi itself and cloud-init is told not to render the
    network again.
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


def wipe_in_place_cmd(path_q: str) -> str:
    """Overwrite a file's bytes with zeros WHERE THEY ARE (``path_q`` is
    already shell-quoted), before it is replaced. The boot partition is FAT:
    a rename over a file, or rm, only frees its clusters, and the old bytes -
    the 64-hex Wi-Fi key beside 'password:', the host private key - stay on
    the card for anyone who reads it raw (grep -a on /dev/sdX1). FAT has no
    copy-on-write, so writing over the file's own range rewrites those same
    clusters. -x keeps the file's size (no rounding up to a block)."""
    return f"sudo shred -n 0 -z -x -- {path_q}"
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
    (the Wi-Fi key) is NOT touched here: it goes at the end of provisioning
    (`wifi_keep_cmd`), only once NetworkManager is shown to keep the Wi-Fi
    profile on the Pi itself — a Pi that loses its Wi-Fi cannot be reached
    to fix it. The old bytes are zeroed in place first (wipe_in_place_cmd):
    replaced on FAT, they stayed on the card."""
    q = shlex.quote
    checks = []
    for d in boot_dirs:
        f = f"{d}/user-data"
        checks.append(f"if [ -f {q(f)} ] && sudo grep -qF {q(FIRSTBOOT_MARKER)} {q(f)}; then "
                      f"{wipe_in_place_cmd(q(f))} && "
                      f"printf %s {q(_SCRUBBED_USER_DATA)} | sudo tee {q(f + '.council-tmp')} "
                      f">/dev/null && sudo mv -f {q(f + '.council-tmp')} {q(f)} || exit 1; fi")
    return "; ".join(checks) + "; sync"


# ── Ollama: ONE pinned, checked release (the user's decision b) ─────────
#: The release the Council installs on a Pi, and the proof it is that file.
#: v0.35.0 is the version this laptop runs ('ollama --version', 2026-10-07).
#: The SHA-256 is the release's sha256sum.txt line for this asset, which the
#: GitHub API's asset digest and the release page also give (three sources,
#: read 2026-10-07; the tarball itself was not downloaded here). The archive
#: is zstd-compressed tar: bin/ollama and lib/ollama (most of its 1.55 GB is
#: CUDA runners a Pi cannot use; they are installed anyway, as install.sh
#: does - leaving them out is unproven until a real Pi runs it).
#: UPDATING OLLAMA = CHANGING THESE FOUR VALUES, from the new release's
#: sha256sum.txt.
OLLAMA_PIN_VERSION = "0.35.0"
OLLAMA_PIN_URL = ("https://github.com/ollama/ollama/releases/download/v0.35.0/"
                  "ollama-linux-arm64.tar.zst")
OLLAMA_PIN_SHA256 = "cb627d332b1fe5055bd5485ca10d595da8429e447648209e375390ec3bd09374"
OLLAMA_PIN_SIZE = 1_550_231_393
#: Free space the download and unpack need under /var/tmp (the 1.55 GB file
#: plus what it unpacks to). The unpacked size is NOT measured yet: check it
#: on the first real Pi.
OLLAMA_MIN_FREE = 6_000_000_000
OLLAMA_UNIT = "/etc/systemd/system/ollama.service"


def _bash(script: str) -> str:
    """Run ``script`` under bash -c: the pinned install needs one shell for
    its temp file, its trap and its checks (the account's login shell may
    not be bash)."""
    return "bash -c " + shlex.quote(script)


def ollama_preflight_cmd(tmp_base: str = "/var/tmp") -> str:
    """64-bit OS, the zstd the .tar.zst needs (installed from the Pi's own
    apt if missing, as nftables is), github.com reachable (the asset is
    there, not on ollama.com), and room for the download. /var/tmp, not
    /tmp: /tmp is RAM on newer Pi OS, and 1.55 GB must not land in RAM."""
    q = shlex.quote
    return _bash(
        "set -eu\n"
        '[ "$(uname -m)" = aarch64 ] || { echo "this Pi does not run a 64-bit OS;'
        ' Ollama needs one" >&2; exit 1; }\n'
        "command -v zstd >/dev/null || sudo DEBIAN_FRONTEND=noninteractive"
        " apt-get install -y zstd\n"
        "curl -fsI --max-time 15 https://github.com >/dev/null || { echo \"the Pi cannot"
        " reach github.com, where the Ollama release is\" >&2; exit 1; }\n"
        f"avail=$(df --output=avail -B1 {q(tmp_base)} | tail -1 | tr -d ' ')\n"
        f'[ "$avail" -gt {int(OLLAMA_MIN_FREE)} ] || {{ echo "Ollama needs about'
        f' {OLLAMA_MIN_FREE / 1e9:.0f} GB free on the card; there is $avail bytes" >&2;'
        " exit 1; }\n"
        "echo ok\n")


def ollama_install_cmd(*, prefix: str = "/usr/local", tmp_base: str = "/var/tmp",
                       url: str = OLLAMA_PIN_URL, sha256: str = OLLAMA_PIN_SHA256,
                       size: int = OLLAMA_PIN_SIZE, version: str = OLLAMA_PIN_VERSION,
                       unit: str = OLLAMA_UNIT) -> str:
    """Download, check, THEN install — one shell, so the temp file and the
    checks live together and nothing is installed unless the size and the
    SHA-256 are the pinned ones. Replaces 'curl -fsSL
    https://ollama.com/install.sh | sh', which checks no hash at all.
    Skipped when the pinned version is already installed with its unit.
    ``prefix`` / ``tmp_base`` / ``unit`` exist so a test can run the real
    script in temporary folders."""
    if not re.fullmatch(r"[0-9a-f]{64}", sha256 or ""):
        raise ValueError("the pinned Ollama checksum is not a SHA-256")
    q = shlex.quote
    return _bash(
        "set -eu\n"
        f"V={q(version)}; P={q(prefix)}\n"
        f'if "$P/bin/ollama" --version 2>&1 | grep -qF "$V" && [ -f {q(unit)} ]; then\n'
        '  echo "Ollama $V is already installed"; exit 0\n'
        "fi\n"
        f"T=$(mktemp -d {q(tmp_base)}/council-ollama.XXXXXX)\n"
        "trap 'rm -rf \"$T\"' EXIT\n"
        'F="$T/ollama-linux-arm64.tar.zst"\n'
        "curl -fL --proto '=https' --proto-redir '=https' --tlsv1.2 --retry 3"
        f' --connect-timeout 20 -o "$F" {q(url)}\n'
        f'[ "$(stat -c %s "$F")" -eq {int(size)} ] || {{ echo "the download is not'
        f' {int(size)} bytes - refusing to install it" >&2; exit 1; }}\n'
        f'echo "{sha256}  $F" | sha256sum -c - >/dev/null 2>&1 || {{ echo "the'
        " download's SHA-256 is not the one the Council has for Ollama $V - refusing"
        ' to install it" >&2; exit 1; }\n'
        "# only now is anything installed:\n"
        'sudo rm -rf "$P/lib/ollama"\n'
        'sudo install -o0 -g0 -m755 -d "$P/bin" "$P/lib/ollama"\n'
        'sudo tar --zstd -xf "$F" -C "$P" --no-same-owner --no-overwrite-dir\n'
        '"$P/bin/ollama" --version 2>&1 | grep -qF "$V" || { echo "the installed'
        ' ollama is not version $V" >&2; exit 1; }\n'
        'echo "Ollama $V installed (size and SHA-256 checked)"\n')


def ollama_service_cmd(prefix: str = "/usr/local", unit: str = OLLAMA_UNIT) -> str:
    """The service account and unit install.sh would make (same flags), with
    a fixed PATH. Enabled here; started by the "listen" step, after its
    firewall."""
    q = shlex.quote
    text = "\n".join(["[Unit]", "Description=Ollama Service", "After=network-online.target",
                      "Wants=network-online.target", "", "[Service]",
                      f"ExecStart={prefix}/bin/ollama serve", "User=ollama", "Group=ollama",
                      "Restart=always", "RestartSec=3",
                      'Environment="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:'
                      '/usr/bin:/sbin:/bin"', "", "[Install]", "WantedBy=default.target", ""])
    return _bash(
        "set -eu\n"
        "id ollama >/dev/null 2>&1 || sudo useradd -r -s /bin/false -U -m"
        " -d /usr/share/ollama ollama\n"
        f"printf %s {q(text)} | sudo tee {q(unit)} >/dev/null\n"
        "sudo systemctl daemon-reload\n"
        "sudo systemctl enable ollama\n")


def ollama_check_cmd(port: int = 11434, version: str = OLLAMA_PIN_VERSION) -> str:
    """Ollama answers, as the pinned version (it takes a moment to start)."""
    return _bash(
        "set -eu\n"
        'v=""\n'
        "for i in $(seq 1 20); do\n"
        f"  v=$(curl -fs --max-time 5 http://127.0.0.1:{int(port)}/api/version) && break"
        " || true\n"
        "  sleep 2\n"
        "done\n"
        f'echo "$v" | grep -qF \'"{version}"\' || {{ echo "Ollama did not answer as'
        f' version {version}: $v" >&2; exit 1; }}\n'
        'echo "$v"\n')


# ── models: downloaded on the Pi only on the user's click (decision a) ──
def pull_model_cmd(model: str) -> str:
    if not _MODEL_RE.match(model or ""):
        raise ValueError(f"not a model name: {model!r}")
    return f"ollama pull {shlex.quote(model)}"


# ── keys only (decision c) ───────────────────────────────────────────────
#: Read first: sshd takes the FIRST value it meets for a setting, and
#: Debian's sshd_config includes sshd_config.d/*.conf in name order at its
#: top — so '00-' wins over cloud-init's 50-cloud-init.conf
#: ('PasswordAuthentication yes' when ssh_pwauth is true).
KEYS_ONLY_FILE = "/etc/ssh/sshd_config.d/00-council-keys-only.conf"
_KEYS_ONLY_TEXT = ("# Written by The Council's Pi setup: SSH accepts keys only.\n"
                   "# To allow passwords again: sudo rm this file && "
                   "sudo systemctl reload ssh\n"
                   "PasswordAuthentication no\nKbdInteractiveAuthentication no\n")


def _reload_ssh() -> str:
    # reload-or-restart: a plain reload fails when ssh.service is not running
    # (a socket-activated sshd), and that failure used to leave the drop-in.
    return ("(sudo systemctl reload-or-restart ssh 2>/dev/null"
            " || sudo systemctl reload-or-restart sshd)")


def keys_only_cmd(path: str = KEYS_ONLY_FILE, sshd: str = "/usr/sbin/sshd") -> str:
    """Turn SSH password login off. Run ONLY over a session that logged in
    with the Council's key (that is the proof key login works). The drop-in
    is checked by 'sshd -t' BEFORE the reload — a bad file is removed and
    nothing reloads — and the effective setting is read back ('sshd -T').

    ANY way out before 'keys-only' is printed removes the drop-in again (an
    EXIT trap), and reloads if a reload was tried. MEASURED (review,
    2026-10-07): a failed 'systemctl reload' left the file in place; the
    Council said password login was still ON - true until the next sshd
    restart or reboot, which made the Pi keys-only with no new key login
    ever proven. ``path`` / ``sshd`` exist so a test can run the real script."""
    q = shlex.quote
    f = path
    return _bash(
        "set -eu\n"
        "stage=writing\n"
        "undo() {\n"
        "  rc=$?\n"
        "  if [ \"$stage\" != done ]; then\n"
        f"    sudo rm -f {q(f)} {q(f + '.council-tmp')} || true\n"
        "    if [ \"$stage\" = reloading ]; then\n"
        f"      {_reload_ssh()} || true\n"
        "      echo \"sshd did not take the keys-only setting; it was removed and"
        " password login stays on\" >&2\n"
        "    fi\n"
        "  fi\n"
        "  exit $rc\n"
        "}\n"
        "trap undo EXIT\n"
        f"printf %s {q(_KEYS_ONLY_TEXT)} | sudo tee {q(f + '.council-tmp')} >/dev/null\n"
        f"sudo mv -f {q(f + '.council-tmp')} {q(f)}\n"
        f"if ! sudo {q(sshd)} -t; then echo \"sshd refused the"
        " setting; password login was left as it was\" >&2; exit 1; fi\n"
        "stage=reloading\n"
        f"{_reload_ssh()}\n"
        f"sudo {q(sshd)} -T 2>/dev/null | grep -qix 'passwordauthentication no' || "
        "{ echo \"another sshd setting still allows passwords\" >&2; exit 1; }\n"
        "stage=done\n"
        "echo keys-only\n")


def password_login_back_cmd() -> str:
    """Undo keys_only_cmd (used when a new key login fails afterwards)."""
    return f"sudo rm -f {shlex.quote(KEYS_ONLY_FILE)} && {_reload_ssh()}"


def password_login_help(username: str, host: str, key_dir: Optional[Path] = None) -> str:
    """What the wizard shows: password login is off, and how to turn it on."""
    try:
        priv, _pub = pi_secrets.council_key(key_dir)
        via = f'ssh -i "{priv}" {username}@{host}'
    except Exception:                                      # noqa: BLE001
        via = f"ssh with the Council's key to {username}@{host}"
    return ("SSH on the Pi now accepts keys only; password login is off. To turn it "
            "back on, log in with a keyboard and screen on the Pi, or from this PC "
            f"with {via}, and run: sudo rm {KEYS_ONLY_FILE} && sudo systemctl reload ssh")


def allowed_auths(host: str, port: int, username: str, timeout: float = 10.0) -> List[str]:
    """The login methods the Pi's running sshd offers ``username`` now (an
    SSH 'none' authentication; nothing secret is sent)."""
    import paramiko
    sock = socket.create_connection((host, port), timeout=timeout)
    t = paramiko.Transport(sock)
    try:
        t.start_client(timeout=timeout)
        try:
            t.auth_none(username)
            return ["none"]
        except paramiko.BadAuthenticationType as exc:
            return list(exc.allowed_types)
    finally:
        t.close()


def _prove_keys_only(host: str, username: str, port: int, key_dir: Optional[Path],
                     session) -> Tuple[bool, str]:
    """After keys_only_cmd: a NEW key login must work — or password login
    goes back on through ``session`` (still open), so the user is never
    locked out — and the running sshd must no longer offer passwords."""
    try:
        connect(host, username, port=port, key_dir=key_dir).close()
    except Exception as exc:                               # noqa: BLE001
        try:
            rc, _o, err = run(session, password_login_back_cmd(), timeout=60)
            back = "password login was turned back on" if rc == 0 else \
                f"turning password login back on failed too: {err.strip()[:200]}"
        except Exception as exc2:                          # noqa: BLE001
            back = f"turning password login back on failed too: {exc2}"
        return False, f"a new key login failed after passwords were turned off ({exc}); {back}"
    try:
        methods = allowed_auths(host, port, username)
    except Exception as exc:                               # noqa: BLE001
        return False, f"could not ask the Pi which logins it accepts: {exc}"
    if {"password", "keyboard-interactive"} & set(methods):
        return False, f"the Pi still offers password login ({', '.join(methods)})"
    return True, "SSH now accepts only keys (password login is off)."


# ── the Wi-Fi key off the card (decision d) ──────────────────────────────
_SCRUBBED_NETWORK = (
    "# First boot is done. The Council removed the network settings it had put\n"
    "# here (the Wi-Fi key). The Pi keeps its Wi-Fi in NetworkManager, and\n"
    "# cloud-init no longer renders the network (99-disable-network-config.cfg).\n")
CLOUD_NO_NETWORK = "network: {config: disabled}\n"


def wifi_keep_cmd(boot_dirs: Tuple[str, ...] = ("/boot/firmware", "/boot"), *,
                  cloud_cfg_dir: str = "/etc/cloud/cloud.cfg.d",
                  netplan_dir: str = "/etc/netplan") -> str:
    """After first boot and a key-login provision: take the Wi-Fi key off the
    card's FAT boot partition (readable by every account on the Pi and by
    anyone holding the card) — ONLY from a network-config the Council wrote,
    and only once the Pi itself keeps the Wi-Fi:

      1. NetworkManager shows a Wi-Fi profile (nmcli) that is stored on the
         Pi — a keyfile, or the netplan file cloud-init rendered (NM's copy
         of a netplan profile lives in /run and is re-made from it at boot);
      2. cloud-init is told not to render the network again
         (99-disable-network-config.cfg), so the emptied seed cannot wipe it;
      3. network-config's bytes are zeroed where they are on the card
         (wipe_in_place_cmd - a FAT rename-over left the key in the freed
         clusters), it becomes a comment-only file, and the Council's
         leftover *.council-tmp files go.

    No Wi-Fi profile on the Pi = the card is left alone and the step fails:
    a headless Pi that loses its Wi-Fi cannot be reached to fix it. A card
    whose network-config holds no Wi-Fi (Ethernet) is emptied too."""
    q = shlex.quote
    wipe_leftover = wipe_in_place_cmd('"$t"')
    lines = ["set -eu", "done_any=no"]
    for d in boot_dirs:
        f = f"{d}/network-config"
        lines += [
            f"if [ -f {q(f)} ] && sudo grep -qF {q(FIRSTBOOT_MARKER)} {q(f)}; then",
            f"  if sudo grep -q 'wifis:' {q(f)}; then",
            "    nmcli -t -f TYPE connection show 2>/dev/null | grep -qx 802-11-wireless || "
            "{ echo \"NetworkManager has no Wi-Fi profile; the Wi-Fi settings were left on"
            " the card\" >&2; exit 1; }",
            "    kept=no",
            "    nmcli -t -f TYPE,FILENAME connection show 2>/dev/null | grep '^802-11-wireless:'"
            " | grep -qv ':/run/' && kept=yes",
            f"    if [ $kept = no ] && sudo grep -qs 'wifis:' {q(netplan_dir)}/*.yaml; then"
            " kept=yes; fi",
            "    [ $kept = yes ] || { echo \"the Pi's Wi-Fi profile is not stored on the Pi;"
            " the Wi-Fi settings were left on the card\" >&2; exit 1; }",
            "  fi",
            f"  sudo mkdir -p {q(cloud_cfg_dir)}",
            f"  printf %s {q(CLOUD_NO_NETWORK)} | sudo tee "
            f"{q(cloud_cfg_dir + '/99-disable-network-config.cfg')} >/dev/null",
            f"  {wipe_in_place_cmd(q(f))}",
            f"  printf %s {q(_SCRUBBED_NETWORK)} | sudo tee {q(f + '.council-tmp')} >/dev/null",
            f"  sudo mv -f {q(f + '.council-tmp')} {q(f)}",
            "  done_any=yes",
            "fi",
            # A leftover from an interrupted write on Windows may be a whole
            # user-data or network-config: zeroed too, then removed.
            f"for t in {q(d)}/*.council-tmp; do [ -f \"$t\" ] && "
            f"{wipe_leftover} || true; done",
            f"sudo rm -f {q(d)}/*.council-tmp 2>/dev/null || true",
        ]
    lines += ["sync", 'echo "card network settings removed: $done_any"']
    return _bash("\n".join(lines) + "\n")


def provision_steps(pc_ip: str, port: int = 11434) -> List[Tuple[str, str, float]]:
    """(what the user sees, command, timeout s). The first-boot secrets come
    off the card first; the firewall goes up BEFORE Ollama listens on the
    network; Ollama is the pinned release, checked before it is installed;
    no model is downloaded (the user presses "Download on the Pi"); the
    Wi-Fi key leaves the card and password login goes off LAST, once
    everything else worked."""
    rules = shlex.quote(firewall_rules(pc_ip, port))
    unit = shlex.quote(
        "[Unit]\nDescription=Only The Council's PC may reach Ollama\n"
        "Before=ollama.service\nAfter=network-pre.target\n"
        f"[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/usr/sbin/nft -f {FIREWALL_FILE}\n"
        "[Install]\nWantedBy=multi-user.target\n")
    override = shlex.quote(f"[Service]\nEnvironment=\"OLLAMA_HOST=0.0.0.0:{int(port)}\"\n")
    return [
        ("Remove the Pi's first-boot secrets from its card", scrub_firstboot_cmd(), 60),
        ("Check the Pi: 64-bit, zstd, github.com reachable, room for Ollama",
         ollama_preflight_cmd(), 600),
        ("Install the firewall tool (nftables)",
         "command -v nft >/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y nftables",
         600),
        ("Let only this PC reach Ollama's port",
         f"sudo mkdir -p /etc/council && printf %s {rules} | sudo tee {FIREWALL_FILE} >/dev/null && "
         f"printf %s {unit} | sudo tee /etc/systemd/system/{FIREWALL_UNIT} >/dev/null && "
         f"sudo systemctl daemon-reload && sudo systemctl enable --now {FIREWALL_UNIT} && "
         f"sudo systemctl restart {FIREWALL_UNIT}", 60),
        (f"Install Ollama {OLLAMA_PIN_VERSION} (download {OLLAMA_PIN_SIZE / 1e9:.2f} GB from "
         "GitHub, check its SHA-256, then install)", ollama_install_cmd(), 3600),
        ("Set up Ollama's service", ollama_service_cmd(), 120),
        ("Let Ollama listen on the network (behind that firewall)",
         f"sudo mkdir -p /etc/systemd/system/ollama.service.d && printf %s {override} | "
         "sudo tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null && "
         "sudo systemctl daemon-reload && sudo systemctl enable ollama && "
         "sudo systemctl restart ollama", 60),
        (f"Check Ollama {OLLAMA_PIN_VERSION} answers", ollama_check_cmd(port), 90),
        ("Keep the Wi-Fi on the Pi and take its key off the card", wifi_keep_cmd(), 60),
        ("Turn SSH password login off (keys only)", keys_only_cmd(), 60),
    ]


@dataclass
class StepResult:
    label: str
    ok: bool
    output: str = ""


def provision(host: str, username: str, *, port: int = 22,
              pc_ip: Optional[str] = None, key_dir: Optional[Path] = None,
              on_step: Optional[Callable[[str], None]] = None,
              on_line: Optional[Callable[[str], None]] = None) -> List[StepResult]:
    """Run provision_steps over the Council's key; stop at the first failure.
    The session itself is a key login — the proof the keys-only step needs —
    and that step is then proven with a NEW key login (see _prove_keys_only)."""
    pc_ip = pc_ip or this_pc_ip_toward(host)
    results: List[StepResult] = []
    keys_only = keys_only_cmd()
    c = connect(host, username, port=port, key_dir=key_dir)
    try:
        for label, cmd, timeout in provision_steps(pc_ip):
            if on_step:
                on_step(label)
            try:
                rc, out, err = run(c, cmd, timeout=timeout, on_line=on_line)
                ok = rc == 0
                text = (out + err)[-2000:]
            except Exception as exc:                      # noqa: BLE001
                ok, text = False, str(exc)
            if ok and cmd == keys_only:
                ok, text = _prove_keys_only(host, username, port, key_dir, c)
            results.append(StepResult(label, ok, text))
            if not ok:
                break
    finally:
        c.close()
    return results


def keys_only(host: str, username: str, *, port: int = 22,
              key_dir: Optional[Path] = None) -> StepResult:
    """Turn password login off on its own (Apothecary → "Switch to key
    login"): over a key login, proven with a new one."""
    label = "Turn SSH password login off (keys only)"
    c = connect(host, username, port=port, key_dir=key_dir)
    try:
        rc, out, err = run(c, keys_only_cmd(), timeout=60)
        if rc != 0:
            return StepResult(label, False, (out + err)[-2000:])
        ok, text = _prove_keys_only(host, username, port, key_dir, c)
        return StepResult(label, ok, text)
    finally:
        c.close()


def pull_model(host: str, username: str, model: str, *, port: int = 22,
               key_dir: Optional[Path] = None,
               on_line: Optional[Callable[[str], None]] = None) -> StepResult:
    """Download ``model`` on the Pi — called ONLY from the user's "Download
    on the Pi" click (decision a), never by setup."""
    from . import pi_models
    if not pi_models.is_us_origin(model):
        raise ValueError(f"{model} is not a US-origin model")
    cmd = pull_model_cmd(model)
    label = f"Download {model} on the Pi"
    c = connect(host, username, port=port, key_dir=key_dir)
    try:
        try:
            rc, out, err = run(c, cmd, timeout=3600, on_line=on_line)
            return StepResult(label, rc == 0, (out + err)[-2000:])
        except Exception as exc:                          # noqa: BLE001
            return StepResult(label, False, str(exc))
    finally:
        c.close()


def refresh_firewall(host: str, username: str, *, port: int = 22,
                     key_dir: Optional[Path] = None) -> str:
    """Re-point the Ollama firewall at this PC's current address."""
    pc_ip = this_pc_ip_toward(host)
    label, cmd, timeout = next(s for s in provision_steps(pc_ip)
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
