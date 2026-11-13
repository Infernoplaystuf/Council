"""
council_core.node_setup — "Add a PC": make another Windows PC a model
machine for this one, and check that it answers.

    ips   = this_pc_addresses()                 # this PC's IPv4 addresses
    text  = setup_script("gaming-pc", "192.168.1.20", ["gemma3:12b"])
    path  = write_setup_script(vault, "gaming-pc", text)
    found = probe("http://192.168.1.31:11434")  # reachable? which models?
    notes = check(found, wanted=["gemma3:12b"])

THE SETUP FILE RUNS ON THE OTHER PC, BY THE USER
The Council never logs in to another PC and never downloads a model on its
own (a download happens only when the user presses Download for one model).
It writes a .bat the user carries to the other PC (USB stick, shared folder)
and runs there as administrator. That file:
  1. checks it runs as administrator and that Ollama is installed;
  2. sets OLLAMA_HOST=0.0.0.0:11434 for the machine (setx /M), so Ollama
     listens on the network and not only on itself;
  3. adds ONE firewall rule, "Ollama for the Council": port 11434, TCP,
     inbound, from THIS PC's address only — every other machine on the
     network is still refused — and lists any other rule that lets every
     machine reach Ollama (Windows offers one the first time Ollama runs);
  4. restarts Ollama so it picks the setting up;
  5. offers to download each model (Y/N each; nothing without a yes), and
     says how to copy the models in instead when that PC is offline;
  6. prints the PC's own addresses to type into "Add a PC".

Everything written into the file is checked first (the machine name against
node_routing's name rule, the address as an IPv4 address, each model as an
Ollama name of US origin), so nothing the user typed can become a command.

THE TEST
`probe` asks the address for /api/version and /api/tags, directly (no proxy,
no redirect: local_models.open_direct) and only when the user presses Test:
an explicit, one-off request to a machine the user named. No chat or
generate call, so no prompt text leaves this PC.

LOOKING AFTER ITS MODELS FROM HERE (Andy, 2026-11-13: "Can I control the
other computers from the main machine?")
Once set up, the machine list can ask the other PC for its models
(`probe`), have it download one (`pull_model`: the user presses Download for
that one US-origin model; the OTHER PC downloads it and needs internet) and
remove one (`remove_model`, after the user confirms). These are Ollama's own
/api/pull and /api/delete, which only this PC can reach through the
firewall rule. No Qt here.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence

from . import node_routing as nr

PORT = 11434
SETUP_DIR = ".council_nodes"
RULE_NAME = "Ollama for the Council"


class SetupError(ValueError):
    pass


# ============================================================
# Addresses
# ============================================================

def _ipv4(text: str) -> Optional[ipaddress.IPv4Address]:
    try:
        ip = ipaddress.ip_address(str(text or "").strip())
    except ValueError:
        return None
    return ip if isinstance(ip, ipaddress.IPv4Address) else None


def _usable(ip: Optional[ipaddress.IPv4Address]) -> bool:
    return bool(ip) and not (ip.is_loopback or ip.is_unspecified
                             or ip.is_link_local or ip.is_multicast)


def route_address(target: str) -> str:
    """This PC's address on the way to ``target`` (an address or host
    name): a UDP socket is 'connected' there, which only asks the routing
    table — no packet is sent. "" when there is no way there."""
    host = host_of(target)
    if not host:
        return ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, PORT))
            ip = _ipv4(s.getsockname()[0])
    except OSError:
        return ""
    return str(ip) if _usable(ip) else ""


def this_pc_addresses(resolve: Optional[Callable[[str], Sequence[str]]] = None
                      ) -> List[str]:
    """This PC's IPv4 addresses on its networks (not loopback, not the
    169.254 'no network' kind), private ones first."""
    if resolve is None:
        def resolve(name: str) -> Sequence[str]:
            try:
                return socket.gethostbyname_ex(name)[2]
            except OSError:
                return []
    seen: List[str] = []
    for raw in list(resolve(socket.gethostname())):
        ip = _ipv4(raw)
        if _usable(ip) and str(ip) not in seen:
            seen.append(str(ip))
    return sorted(seen, key=lambda a: (not _ipv4(a).is_private, a))


def host_of(address: str) -> str:
    """'192.168.1.31', 'http://192.168.1.31:11434/' or 'gaming-pc' ->
    the host part alone."""
    text = str(address or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    try:
        return urllib.parse.urlsplit(text).hostname or ""
    except ValueError:
        return ""


def url_for(address: str) -> str:
    """The Ollama address Machines & roles stores for what the user typed:
    'http://<host>:<port>', port 11434 unless they gave one."""
    text = str(address or "").strip()
    if not text:
        raise SetupError("type the other PC's address")
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urllib.parse.urlsplit(text)
        port = parts.port or PORT
    except ValueError as exc:
        raise SetupError(f"{address!r} is not an address ({exc})")
    host = parts.hostname or ""
    if not host or parts.scheme not in ("http", "https"):
        raise SetupError(f"{address!r} is not an address")
    if _ipv4(host) is None and not re.match(r"^[A-Za-z0-9][A-Za-z0-9.-]*$", host):
        raise SetupError(f"{address!r} is not an address")
    url = f"{parts.scheme}://{host}:{port}"
    if not nr._URL.match(url):
        raise SetupError(f"{address!r} is not an address")
    from .local_models import is_loopback_url
    if is_loopback_url(url):
        raise SetupError("that is this PC; type the OTHER PC's address")
    return url


# ============================================================
# Which models the other PC needs
# ============================================================

def wanted_models(machine: str, routing: nr.Routing,
                  role_model: Callable[[str], str]) -> List[str]:
    """The Ollama models ``machine`` needs: those of the roles bound to it
    (each role runs its own model there) and its shared-work model. Only
    US-origin names; a .gguf role is not one (it never leaves this PC).
    ``role_model(role)`` gives a role's model label ('ollama:gemma3:12b',
    a .gguf path, or '')."""
    from . import local_models as lm
    out: List[str] = []
    node = routing.nodes.get(machine)
    names = [role_model(role) for role, b in routing.roles.items()
             if b.node == machine]
    if node is not None and node.model:
        names.append(lm.ollama_id(node.model))
    for label in names:
        if not lm.is_ollama_id(label):
            continue
        name = lm.ollama_name(label)
        if name and not nr.check_model(name) and name not in out:
            out.append(name)
    return out


def role_model_label(vault_dir: Path) -> Callable[[str], str]:
    """role -> its slot's model label in model_slots.json ('' for the main
    slot's GGUF, which cannot run elsewhere)."""
    from . import model_slots
    try:
        config = model_slots.load(Path(vault_dir))
    except Exception:                                     # noqa: BLE001
        config = model_slots.SlotConfig()

    def label(role: str) -> str:
        slot = config.slots.get(config.slot_for(role))
        return slot.path if slot is not None else ""
    return label


# ============================================================
# The setup file
# ============================================================

def _checked_models(models: Iterable[str]) -> List[str]:
    out: List[str] = []
    for m in models:
        m = str(m or "").strip()
        if not m:
            continue
        why = nr.check_model(m)
        if why:
            raise SetupError(why)
        if m not in out:
            out.append(m)
    return out


def setup_script(machine: str, this_pc_ip: str, models: Iterable[str] = (),
                 *, today: Optional[date] = None) -> str:
    """The Windows .bat (CRLF line ends) that makes the other PC a model
    machine answering ONLY ``this_pc_ip``. Raises SetupError on a bad name,
    address or model."""
    machine = str(machine or "").strip()
    if not nr._NAME.match(machine):
        raise SetupError("give the machine a name: letters, digits, space, "
                         "'.', '_' or '-', up to 64")
    ip = _ipv4(this_pc_ip)
    if not _usable(ip):
        raise SetupError(f"{this_pc_ip!r} is not this PC's network address")
    models = _checked_models(models)
    when = (today or date.today()).isoformat()
    L: List[str] = [
        "@echo off",
        "setlocal",
        f"title Council: set up {machine}",
        f"REM Made by the Council on {when}. Run it on the OTHER PC ({machine}),",
        f"REM as administrator. Ollama there will answer the Council PC at {ip}",
        "REM and nobody else. Nothing here downloads a model without asking.",
        "echo.",
        f"echo Setting up this PC as \"{machine}\" for the Council PC at {ip}.",
        "echo.",
        "net session >nul 2>&1",
        "if errorlevel 1 (",
        "  echo This file needs administrator rights: right-click it and",
        "  echo choose \"Run as administrator\".",
        "  pause",
        "  exit /b 1",
        ")",
        "set \"OLLAMA_EXE=\"",
        "for /f \"delims=\" %%P in ('where ollama 2^>nul') do if not defined OLLAMA_EXE set \"OLLAMA_EXE=%%P\"",
        "if not defined OLLAMA_EXE if exist \"%LOCALAPPDATA%\\Programs\\Ollama\\ollama.exe\" set \"OLLAMA_EXE=%LOCALAPPDATA%\\Programs\\Ollama\\ollama.exe\"",
        "if not defined OLLAMA_EXE (",
        "  echo Ollama is not installed on this PC. Install it from",
        "  echo https://ollama.com/download ^(or copy the installer over^),",
        "  echo then run this file again.",
        "  pause",
        "  exit /b 1",
        ")",
        "",
        "echo [1/5] Ollama will listen on the network (OLLAMA_HOST=0.0.0.0:11434)",
        f"setx OLLAMA_HOST \"0.0.0.0:{PORT}\" /M >nul",
        "if errorlevel 1 (",
        "  echo Could not set OLLAMA_HOST. Nothing else was changed.",
        "  pause",
        "  exit /b 1",
        ")",
        "",
        f"echo [2/5] Firewall: port {PORT} open to {ip} only",
        f"netsh advfirewall firewall delete rule name=\"{RULE_NAME}\" >nul 2>&1",
        f"netsh advfirewall firewall add rule name=\"{RULE_NAME}\" dir=in "
        f"action=allow protocol=TCP localport={PORT} remoteip={ip} profile=any >nul",
        "if errorlevel 1 (",
        "  echo Could not add the firewall rule.",
        "  pause",
        "  exit /b 1",
        ")",
        "echo Other firewall rules that let EVERY machine reach Ollama (none is best):",
        "powershell -NoProfile -Command \"Get-NetFirewallApplicationFilter -Program '*ollama*' "
        "-ErrorAction SilentlyContinue | Get-NetFirewallRule | Where-Object { $_.Enabled -eq 'True' "
        "-and $_.Direction -eq 'Inbound' -and $_.Action -eq 'Allow' } | ForEach-Object { '   ' + "
        "$_.DisplayName }\"",
        "echo If any are listed, turn them off in Windows Defender Firewall,",
        "echo Advanced settings, Inbound Rules, so only the Council PC gets in.",
        "",
        "echo [3/5] Restarting Ollama",
        "taskkill /F /IM \"ollama app.exe\" >nul 2>&1",
        "taskkill /F /IM ollama.exe >nul 2>&1",
        "timeout /t 2 /nobreak >nul",
        f"set \"OLLAMA_HOST=0.0.0.0:{PORT}\"",
        "if exist \"%LOCALAPPDATA%\\Programs\\Ollama\\ollama app.exe\" (",
        "  start \"\" \"%LOCALAPPDATA%\\Programs\\Ollama\\ollama app.exe\"",
        ") else (",
        "  start \"Ollama\" /min \"%OLLAMA_EXE%\" serve",
        ")",
        "timeout /t 5 /nobreak >nul",
        f"set \"OLLAMA_HOST=127.0.0.1:{PORT}\"",
        "",
        "echo [4/5] Models",
    ]
    if models:
        for m in models:
            L += [
                f"choice /M \"Download {m} now (needs internet on this PC)\"",
                "if not errorlevel 2 \"%OLLAMA_EXE%\" pull " + m,
            ]
        L += [
            "echo If this PC is offline: on a PC that has the models, copy the folder",
            "echo %USERPROFILE%\\.ollama\\models over this PC's own, then restart Ollama.",
            "echo Installed here now:",
            "\"%OLLAMA_EXE%\" list",
        ]
    else:
        L += ["echo No model was named. Install the model each role uses here",
              "echo (ollama pull ^<name^>) before binding a role to this PC."]
    L += [
        "",
        "echo [5/5] This PC's addresses. Type one into the Council's Add a PC window:",
        "ipconfig | findstr /R /C:\"IPv4\"",
        "echo.",
        "echo Tip: give this PC and the Council PC fixed addresses (a DHCP",
        f"echo reservation in the router). If the Council PC's address stops being {ip},",
        "echo make a new setup file from the Council and run it here again.",
        "echo.",
        "echo Done.",
        "pause",
        "endlocal",
    ]
    return "\r\n".join(L) + "\r\n"


def _safe_file_stem(machine: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", machine.strip()).strip("._") or "machine"
    return stem[:60]


def setup_path(vault_dir: Path, machine: str) -> Path:
    return Path(vault_dir) / SETUP_DIR / f"council_node_setup_{_safe_file_stem(machine)}.bat"


def write_setup_script(target: Path, text: str) -> Path:
    """Write the setup file atomically (temp + replace). ``target`` is a
    path the user chose or `setup_path`; it is a new file the Council makes,
    never a user file (the dialog asks before replacing one)."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="ascii", errors="replace", newline="") as fh:
        fh.write(text)
    os.replace(tmp, target)
    return target


# ============================================================
# The test
# ============================================================

@dataclass
class Probe:
    url: str
    reachable: bool = False
    version: str = ""
    latency_ms: Optional[float] = None
    models: List[str] = field(default_factory=list)
    error: str = ""


def _plain_error(exc: BaseException) -> str:
    text = str(getattr(exc, "reason", "") or exc)
    low = text.lower()
    if "refused" in low or "10061" in low:
        return ("the PC answered but nothing listens on that port: Ollama is "
                "not running there, or still listens only on itself (run the "
                "setup file there, then restart Ollama)")
    if "timed out" in low or "10060" in low:
        return ("no answer: the PC is off, the address is wrong, or its "
                "firewall drops the call (run the setup file there; check "
                "this PC's address is the one the file names)")
    if "getaddrinfo" in low or "name or service" in low or "11001" in low:
        return "that name is not known on this network; use its IP address"
    return text


def probe(url: str, *, timeout: float = 4.0,
          get_json: Optional[Callable[[str, float], dict]] = None) -> Probe:
    """Ask ``url`` for /api/version and /api/tags. Never raises."""
    from . import local_models as lm
    get = get_json or lm._get_json
    out = Probe(url=url.rstrip("/"))
    start = time.monotonic()
    try:
        version = get(out.url + "/api/version", timeout) or {}
        out.latency_ms = round((time.monotonic() - start) * 1000.0, 1)
        out.version = str(version.get("version", "")) if isinstance(version, dict) else ""
        tags = get(out.url + "/api/tags", timeout) or {}
        models = tags.get("models") if isinstance(tags, dict) else None
        out.models = sorted({str(m.get("name") or m.get("model") or "")
                             for m in (models or []) if isinstance(m, dict)} - {""})
        out.reachable = True
    except Exception as exc:                              # noqa: BLE001
        out.error = _plain_error(exc)
    return out


def _has(installed: Sequence[str], name: str) -> bool:
    full = lambda n: n if ":" in n.rsplit("/", 1)[-1] else n + ":latest"  # noqa: E731
    return full(name) in {full(i) for i in installed}


def check(found: Probe, wanted: Sequence[str] = ()) -> List[str]:
    """The test's result in lines for the user."""
    if not found.reachable:
        return [f"✗ {found.url}: {found.error}"]
    from . import local_models as lm
    lines = [f"✓ {found.url} answers (Ollama {found.version or '?'}, "
             f"{found.latency_ms} ms)."]
    us = [m for m in found.models if lm.maker_and_origin(m)[1] == "US"]
    lines.append("US-origin models there: " + (", ".join(us) if us else "none"))
    missing = [m for m in wanted if not _has(found.models, m)]
    if missing:
        lines.append("✗ Not installed there yet: " + ", ".join(missing) +
                     ". Download them on that PC (the setup file offers to).")
    elif wanted:
        lines.append("✓ It has every model its roles need.")
    return lines


# ============================================================
# Looking after the other PC's models from here
# ============================================================

def _request(url: str, path: str, method: str, body: dict, timeout: float):
    import json as _json
    import urllib.request
    from . import local_models as lm
    req = urllib.request.Request(
        url.rstrip("/") + path, data=_json.dumps(body).encode("utf-8"),
        method=method, headers={"Content-Type": "application/json"})
    return lm.open_direct(req, timeout)


def pull_model(url: str, name: str, *,
               on_progress: Optional[Callable[[str, Optional[float]], None]] = None,
               should_stop: Callable[[], bool] = lambda: False,
               timeout: float = 60.0, request=None) -> str:
    """Have the machine at ``url`` download ``name`` (Ollama's /api/pull),
    only because the user pressed Download for it. US-origin names only.
    The download is the OTHER PC's: it needs internet there, and this PC
    only watches the progress. ``on_progress(status, fraction or None)``.
    Returns the last status ('success'). Raises SetupError."""
    import json as _json
    why = nr.check_model(name)
    if why or not name:
        raise SetupError(why or "name a model")
    send = request or _request
    last = ""
    try:
        with send(url, "/api/pull", "POST", {"model": name, "stream": True},
                  timeout) as resp:
            for raw in resp:
                if should_stop():
                    return "stopped (the other PC may carry on downloading)"
                try:
                    msg = _json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if msg.get("error"):
                    raise SetupError(f"{name}: {msg['error']}")
                last = str(msg.get("status", last))
                total, done = msg.get("total"), msg.get("completed")
                frac = (float(done) / float(total)
                        if total and done is not None else None)
                if on_progress is not None:
                    on_progress(last, frac)
    except SetupError:
        raise
    except Exception as exc:                              # noqa: BLE001
        raise SetupError(f"{name}: {_plain_error(exc)}")
    if last != "success":
        raise SetupError(f"{name}: the download stopped at '{last or 'start'}'")
    return last


def remove_model(url: str, name: str, *, timeout: float = 30.0,
                 request=None) -> None:
    """Delete ``name`` from the machine at ``url`` (Ollama's /api/delete).
    The dialog asks the user first; nothing calls this on its own."""
    name = str(name or "").strip()
    if not name or not nr._MODEL.match(name):
        raise SetupError(f"{name!r} is not a model name")
    send = request or _request
    try:
        with send(url, "/api/delete", "DELETE", {"model": name}, timeout):
            pass
    except Exception as exc:                              # noqa: BLE001
        code = getattr(exc, "code", None)
        if code == 404:
            raise SetupError(f"{name} is not on that PC")
        raise SetupError(f"{name}: {_plain_error(exc)}")


__all__ = ["pull_model", "remove_model", "PORT", "RULE_NAME", "SETUP_DIR", "SetupError", "Probe",
           "route_address", "this_pc_addresses", "host_of", "url_for",
           "wanted_models", "role_model_label", "setup_script", "setup_path",
           "write_setup_script", "probe", "check"]
