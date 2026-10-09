"""The two ways the Council sets up a Pi, start to finish.

EXISTING PI — `setup_existing`:
    connect with the user's password ONCE (host key confirmed by the user, or
    already pinned) -> install the Council's key -> read the Pi's facts ->
    provision (firewall, the pinned Ollama, Wi-Fi key off the card, SSH keys
    only) -> register with key auth, no password. The model is NOT downloaded:
    `download_model` runs only when the user presses "Download on the Pi".

NEW PI — `prepare_new_pi`, then the elevated flash_helper, then `finish_new_pi`:
    1. the user picks an image, a card (disks.py — erasable cards only), and
       the Pi's hostname / login / Wi-Fi;
    2. the Council makes the Pi's SSH HOST key and puts it, with the
       Council's own public key, in the first-boot files; a "pending" record
       (hostname, username, host public key — no secrets) is saved so the Pi
       can be found even after the Council restarts;
    3. flash_helper (UAC) erases, writes, verifies, adds and verifies the
       first-boot files;
    4. the user moves the card to the Pi and powers it on; `wait_for_pi`
       looks for it (mDNS name, then every SSH host on this PC's subnet) and
       accepts ONLY the host presenting the key the Council generated — the
       right Pi, never "whichever SSH host answered";
    5. provision and register as above. No password is needed at all: the
       Council's key was on the card.

`secure_existing_node` upgrades a node the OLD wizard registered (password in
plain text, Ollama open to the LAN): it uses that stored password one last
time to install the key, re-registers with key auth and an empty password,
puts up the firewall and turns SSH password login off. NodePrimus on this
network is that case.

A card is erased only after a second, explicit confirmation when it already
holds files (disks.Disk.contents); the image's checksum is checked before the
card is erased (flash_helper); a file that is not in the official list is
written with no assumed checksum or first-boot format (the helper reads the
format off the card; `note_written` records which).
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import socket
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from . import disks as dk
from . import firstboot as fb
from . import flash_helper
from . import pi_models
from . import pi_secrets
from . import remote

Say = Optional[Callable[[str], None]]


def state_dir() -> Path:
    override = os.environ.get("COUNCIL_PI_STATE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) / "Council" if base else Path.home() / ".council_app") / "pi_setup"


# ── finding a Pi ─────────────────────────────────────────────────────────
def server_key_line(host: str, port: int = 22, timeout: float = 3.0) -> Optional[str]:
    """The SSH host key ``host`` presents, as 'ssh-ed25519 AAAA…' — read from
    the key exchange, no login attempted. None when nothing answers."""
    import paramiko
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return None
    t = paramiko.Transport(sock)
    try:
        opts = t.get_security_options()
        # Ask for ed25519 first: the key the Council generated is ed25519.
        keys = list(opts.key_types)
        if "ssh-ed25519" in keys:
            keys.remove("ssh-ed25519")
            opts.key_types = ["ssh-ed25519"] + keys
        t.start_client(timeout=timeout)
        k = t.get_remote_server_key()
        return f"{k.get_name()} {k.get_base64()}"
    except Exception:                                      # noqa: BLE001
        return None
    finally:
        t.close()


def _subnet_ssh_hosts(timeout: float = 0.35) -> List[str]:
    from apothecary_engine import _get_local_subnet, _probe_port22
    subnet = _get_local_subnet()
    if not subnet:
        return []
    ips = [f"{subnet}.{i}" for i in range(1, 255)]
    with cf.ThreadPoolExecutor(max_workers=64) as ex:
        return [ip for ip, ok in zip(ips, ex.map(lambda i: _probe_port22(i, timeout), ips)) if ok]


def _mdns(hostname: str) -> List[str]:
    try:
        return sorted({r[4][0] for r in socket.getaddrinfo(hostname + ".local", 22,
                                                           socket.AF_INET, socket.SOCK_STREAM)
                       if not r[4][0].startswith("127.")})
    except OSError:
        return []


def find_by_host_key(expected_public: str, hostname: str, *, port: int = 22,
                     candidates: Optional[Callable[[], List[str]]] = None,
                     key_of: Callable[[str, int], Optional[str]] = server_key_line
                     ) -> Optional[str]:
    """The address whose SSH host key IS ``expected_public``, or None."""
    want = " ".join(expected_public.split()[:2])
    pool = candidates() if candidates else (_mdns(hostname) + _subnet_ssh_hosts())
    seen = set()
    for ip in pool:
        if ip in seen:
            continue
        seen.add(ip)
        if key_of(ip, port) == want:
            return ip
    return None


def wait_for_pi(expected_public: str, hostname: str, *, timeout_s: float = 900,
                poll_s: float = 15, say: Say = None, cancelled=lambda: False,
                finder=find_by_host_key) -> str:
    """Poll until the Pi with this host key is on the network. First boot
    takes a few minutes and reboots once."""
    say = say or (lambda _m: None)
    start = time.time()
    while time.time() - start < timeout_s:
        if cancelled():
            raise TimeoutError("stopped looking for the Pi")
        ip = finder(expected_public, hostname)
        if ip:
            say(f"Found the Pi at {ip} (its host key matches the one on its card).")
            return ip
        say(f"Not on the network yet ({int(time.time() - start)} s) — first boot takes "
            "a few minutes…")
        time.sleep(poll_s)
    raise TimeoutError(
        "The Pi did not appear. Check: it has power and its green light flickered; "
        "Wi-Fi details and country were right (or try an Ethernet cable for first boot); "
        "this PC is on the same network.")


# ── existing Pi ──────────────────────────────────────────────────────────
@dataclass
class Outcome:
    ok: bool
    host: str = ""
    name: str = ""
    model: str = ""
    message: str = ""
    steps: List[remote.StepResult] = field(default_factory=list)
    username: str = ""
    #: The model is chosen but NOT on the Pi yet: the wizard offers
    #: "Download on the Pi" (the user's decision a, 2026-10-07).
    model_pending: bool = False


def _provision_and_register(vault, *, name, host, username, model, facts, port, key_dir,
                            say: Say, on_line) -> Outcome:
    say = say or (lambda _m: None)
    if not pi_models.is_us_origin(model):
        return Outcome(False, host, name, model,
                       f"{model} is not a US-origin model; choose one of "
                       f"{', '.join(pi_models.for_ram(facts.ram_gb))}")
    steps = remote.provision(host, username, port=port, key_dir=key_dir,
                             on_step=say, on_line=on_line)
    if not all(s.ok for s in steps):
        bad = steps[-1]
        return Outcome(False, host, name, model, f"'{bad.label}' failed: "
                       f"{bad.output.strip()[-300:]}", steps, username=username)
    remote.register(vault, name=name, host=host, username=username, model=model,
                    facts_=facts, key_dir=key_dir)
    return Outcome(True, host, name, model,
                   f"{name} is set up on {host}: Ollama {remote.OLLAMA_PIN_VERSION}, reachable "
                   f"only from this PC. {remote.password_login_help(username, host, key_dir)} "
                   f"Its model is not downloaded yet: {pi_models.describe(model)}. Press "
                   "\"Download on the Pi\" when you want it.", steps,
                   username=username, model_pending=True)


def download_model(*, host: str, username: str, model: str, port: int = 22,
                   key_dir: Optional[Path] = None, on_line=None) -> Outcome:
    """The ONE model download the Council does, and only from the user's
    "Download on the Pi" click (decision a): US-origin models only."""
    if not pi_models.is_us_origin(model):
        return Outcome(False, host, "", model, f"{model} is not a US-origin model",
                       username=username, model_pending=True)
    step = remote.pull_model(host, username, model, port=port, key_dir=key_dir,
                             on_line=on_line)
    if not step.ok:
        return Outcome(False, host, "", model, f"the download failed: "
                       f"{step.output.strip()[-300:]}", [step], username=username,
                       model_pending=True)
    return Outcome(True, host, "", model, f"{model} is on the Pi at {host}.", [step],
                   username=username)


def setup_existing(vault, *, host: str, username: str, password: str, name: str,
                   model: Optional[str] = None, port: int = 22,
                   approved_fingerprint: Optional[str] = None,
                   key_dir: Optional[Path] = None, say: Say = None, on_line=None) -> Outcome:
    """Raises remote.HostKeyUnknown when the user must confirm the Pi's key."""
    say = say or (lambda _m: None)
    say(f"Connecting to {host} with your password (used once)…")
    adopted = remote.adopt(host, username, password, port=port, key_dir=key_dir,
                           approved_fingerprint=approved_fingerprint)
    say(f"The Council's key is installed. {adopted.facts.model or 'Pi'}, "
        f"{adopted.facts.ram_gb} GB, {adopted.facts.os_name}.")
    if not adopted.facts.is_64bit:
        say("Note: this Pi runs a 32-bit OS; Ollama needs 64-bit.")
    model = model or pi_models.for_ram(adopted.facts.ram_gb)[0]
    return _provision_and_register(vault, name=name, host=host, username=username,
                                   model=model, facts=adopted.facts, port=port,
                                   key_dir=key_dir, say=say, on_line=on_line)


def secure_existing_node(vault, node_name: str, *, approved_fingerprint: Optional[str] = None,
                         key_dir: Optional[Path] = None, say: Say = None) -> Outcome:
    """Move a node the old wizard registered to key login and a firewalled
    Ollama, using its stored password ONE last time, then clearing it."""
    from council_core import apothecary as apoth
    reg = apoth._ae.NodeRegistry(str(apoth.registry_path(Path(vault))))
    node = next((n for n in reg.list_nodes() if n.name == node_name), None)
    if node is None:
        raise KeyError(node_name)
    if not node.password:
        raise ValueError("this node has no stored password; use Set up a Pi -> existing Pi")
    adopted = remote.adopt(node.host, node.username, node.password, port=node.port,
                           key_dir=key_dir, approved_fingerprint=approved_fingerprint)
    pc_ip = remote.refresh_firewall(node.host, node.username, port=node.port, key_dir=key_dir)
    remote.register(vault, name=node.name, host=node.host, username=node.username,
                    model=node.model or node.active_model, facts_=adopted.facts,
                    key_dir=key_dir)
    # adopt() proved the key login; only now does SSH stop taking passwords
    # (decision c) - checked by a new key login, undone if that fails.
    keys = remote.keys_only(node.host, node.username, port=node.port, key_dir=key_dir)
    tail = (remote.password_login_help(node.username, node.host, key_dir) if keys.ok else
            f"Password login is still ON: {keys.output}")
    return Outcome(keys.ok, node.host, node.name, node.model,
                   f"{node.name} now uses the Council's key; its stored password was "
                   f"removed; Ollama answers only {pc_ip}. {tail} Change the Pi's password "
                   "too, since it was stored in plain text before.", [keys],
                   username=node.username)


# ── new Pi ───────────────────────────────────────────────────────────────
@dataclass
class Pending:
    id: str
    hostname: str
    username: str
    host_public: str
    model: str
    created_ts: float = field(default_factory=time.time)


def _pending_path() -> Path:
    return state_dir() / "pending.json"


def pending() -> List[Pending]:
    try:
        return [Pending(**p) for p in json.loads(_pending_path().read_text(encoding="utf-8"))]
    except (OSError, ValueError, TypeError):
        return []


def _save_pending(items: List[Pending]) -> None:
    p = _pending_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps([asdict(i) for i in items], indent=1), encoding="utf-8")
    tmp.replace(p)


def prepare_new_pi(*, disk: dk.Disk, typed_confirm: str, image: Path, init_format: str,
                   extract_sha256: str, extract_size: int, cfg: fb.FirstBoot,
                   model: str, key_dir: Optional[Path] = None,
                   files_confirmed: Sequence[str] = ()) -> Dict[str, object]:
    """Check everything, build the first-boot files and the helper's job.
    Returns {"job": path, "pending": Pending}. Nothing is erased here.

    ``init_format`` '' = not known (a file that is not in the official list,
    or a list entry without one): the job carries the first-boot files for
    BOTH formats and the helper uses the one it reads off the written card -
    nothing is assumed from a file name. ``files_confirmed``: the user's
    second confirmation for a card that already holds files (decision f) -
    the contents (Disk.contents) that question named. A bare yes was tied to
    the reader, not the card: a card swapped in the same reader was erased
    without its own files ever being named (review, 2026-10-07)."""
    if not disk.eligible:
        raise ValueError(f"that disk cannot be used: {disk.why_not}")
    if typed_confirm.strip() != disk.confirm_code:
        raise ValueError(f"type exactly: {disk.confirm_code}")
    agreed = ([str(x) for x in files_confirmed]
              if isinstance(files_confirmed, (list, tuple)) else [])
    if disk.holds_files and agreed != disk.contents():
        raise ValueError("that card holds files (" + "; ".join(disk.contents())
                         + ") - confirm that they may be erased first")
    if init_format not in fb.FORMATS + ("",):
        raise ValueError(f"unknown first-boot format {init_format!r}")
    if not pi_models.is_us_origin(model):
        raise ValueError(f"{model} is not a US-origin model")
    _priv, council_pub = pi_secrets.council_key(key_dir)
    if council_pub not in cfg.ssh_public_keys:
        cfg.ssh_public_keys = list(cfg.ssh_public_keys) + [council_pub]
    host_public = ""
    if init_format in (fb.CLOUDINIT, ""):
        cfg.host_key_private, host_public = pi_secrets.host_keypair()
        cfg.host_key_public = host_public
    if init_format:
        files: Dict[str, str] = fb.build(cfg, init_format)
        by_format: Dict[str, Dict[str, str]] = {}
    else:
        files, by_format = {}, {fmt: fb.build(cfg, fmt) for fmt in fb.FORMATS}
    cfg.password = ""                          # the hash is in `files`; drop the original
    cfg.wifi_password = ""
    cfg.host_key_private = ""
    job_dir = state_dir() / "jobs" / str(uuid.uuid4())
    job = flash_helper.write_job(job_dir, disk=disk, image=image, init_format=init_format,
                                 firstboot_files=files, extract_sha256=extract_sha256,
                                 extract_size=extract_size, firstboot_by_format=by_format,
                                 files_confirmed=agreed if disk.holds_files else [])
    item = Pending(id=job_dir.name, hostname=cfg.hostname, username=cfg.username,
                   host_public=host_public, model=model)
    _save_pending([p for p in pending() if p.hostname != cfg.hostname] + [item])
    return {"job": job, "pending": item}


def note_written(pending_id: str, init_format: str) -> Optional[Pending]:
    """The helper wrote the card in ``init_format`` (it read it off the card
    when the image's format was not known). A legacy (systemd) card carries
    no pre-made host key, so the Pi cannot be found by one: its record drops
    the key and finishing asks for 'A Pi that is already set up' instead of
    waiting for a key that never appears."""
    items = pending()
    item = next((p for p in items if p.id == pending_id), None)
    if item is None:
        return None
    if init_format != fb.CLOUDINIT and item.host_public:
        item.host_public = ""
        _save_pending(items)
    return item


_JOB_FILES = ("job.json", "cancel", "status.json", "status.json.tmp")


def _remove_job_dir(job_dir: Path) -> None:
    """Delete one job folder the Council made (only its own file names)."""
    for name in _JOB_FILES:
        try:
            (job_dir / name).unlink()
        except FileNotFoundError:
            pass
    try:
        job_dir.rmdir()
    except OSError:
        pass


def abandon_job(job: Path, pending_id: Optional[str] = None) -> None:
    """The card writer never ran (UAC declined, the helper did not start or
    never reported back): delete the job — job.json carries the first-boot
    secrets (the Pi's SSH host private key, the password hash, the WPA key)
    and only the helper deleted it, so a declined prompt left them on disk
    for good — and drop its pending record, so nothing offers to finish a
    Pi whose card was never written."""
    _remove_job_dir(Path(job).parent)
    if pending_id:
        _save_pending([p for p in pending() if p.id != pending_id])


#: A job.json this old was never read: the helper reads it within seconds of
#: the UAC prompt being approved (and deletes it when it finishes).
STALE_JOB_S = 15 * 60


def sweep_jobs(now: Optional[float] = None) -> List[str]:
    """Run when the dialog opens. Deletes every job.json older than
    STALE_JOB_S (its secrets), and drops the job and its pending record when
    the card was not written: the helper reported an error, or there is no
    status and no job left to run. Returns the job ids removed."""
    now = time.time() if now is None else now
    jobs = state_dir() / "jobs"
    removed: List[str] = []
    if not jobs.is_dir():
        return removed
    for d in sorted(p for p in jobs.iterdir() if p.is_dir()):
        job = d / "job.json"
        try:
            if job.is_file() and now - job.stat().st_mtime > STALE_JOB_S:
                job.unlink()
        except OSError:
            pass
        st = flash_helper.read_status(d)
        if (st and st.get("phase") == "error") or (not st and not job.exists()):
            _remove_job_dir(d)
            removed.append(d.name)
    if removed:
        _save_pending([p for p in pending() if p.id not in removed])
    return removed


def finish_new_pi(vault, item: Pending, *, key_dir: Optional[Path] = None, say: Say = None, port: int = 22,
                  on_line=None, cancelled=lambda: False, finder=find_by_host_key,
                  timeout_s: float = 900) -> Outcome:
    say = say or (lambda _m: None)
    if not item.host_public:
        raise ValueError("this Pi's image has no pre-made host key (legacy image); "
                         "use 'A Pi that is already set up' with its address")
    host = wait_for_pi(item.host_public, item.hostname, say=say, cancelled=cancelled,
                       finder=finder, timeout_s=timeout_s)
    c = remote.connect(host, item.username, port=port, key_dir=key_dir,
                       expected_host_key=item.host_public)
    try:
        facts = remote.facts(c)
    finally:
        c.close()
    out = _provision_and_register(vault, name=item.hostname, host=host, username=item.username,
                                  model=item.model, facts=facts, port=port, key_dir=key_dir,
                                  say=say, on_line=on_line)
    if out.ok:
        _save_pending([p for p in pending() if p.id != item.id])
    return out
