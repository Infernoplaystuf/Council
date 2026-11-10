"""
council_core.searxng — run a SearXNG search server on this PC, in Docker,
for the Research tab.

The user asked (2026-11-10) for a web search that needs no API key, after
DuckDuckGo answered Council with its bot check instead of results. SearXNG
is an open-source metasearch server: it asks several engines (Google, Bing,
Brave, Mojeek, ...) and merges what they find, so one engine refusing does
not stop a search, and it needs no account. council_core.web already
searches a SearXNG server; this module starts one.

    setup()    Settings › Online › "Set up SearXNG": writes the server's
               settings.yml under <app dir>/searxng (JSON answers on, the
               rate limiter off as only this PC can reach it), starts the
               container ``council-searxng`` from IMAGE on 127.0.0.1:8888
               only (never on the network), restarting with Docker, waits
               until it answers in JSON, and saves SearXNG as the search
               engine. A container already made is just started again.
    status()   whether Docker is here and running, and whether the server
               answers - a sentence for the Settings tab.

Docker Desktop must be installed and running; nothing here installs it. The
first setup downloads the image (a few hundred MB), so it needs the Online
switch on, like every other way Council reaches the internet.

Nothing here imports Qt. No shell: every docker call is an argument list.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from council_core import paths
from council_core import web

#: the image, pinned: a newer one is a deliberate change here
IMAGE = "searxng/searxng:2026.10.25-54ca1d9cf"
CONTAINER = "council-searxng"
PORT = 8888
URL = web.DEFAULT_SEARXNG
CONFIG_DIR = "searxng"
#: seconds to wait for the server to answer after it starts
READY_WAIT = 90
#: seconds a docker run may take (the first one downloads the image)
PULL_WAIT = 900

Progress = Callable[[str], None]

SETTINGS_YML = """# Written by Council (council_core/searxng.py). SearXNG's own defaults,
# with JSON answers for Council and no rate limiter: the port is bound to
# 127.0.0.1 only, so nothing but this PC can reach it.
use_default_settings: true
server:
  secret_key: "{key}"
  limiter: false
  image_proxy: false
  bind_address: "0.0.0.0"
search:
  formats:
    - html
    - json
"""


class SearxngError(RuntimeError):
    """Setup could not finish; the message is a sentence for the user."""


@dataclass
class Status:
    docker: bool = False        # docker.exe is installed
    running: bool = False       # Docker answers
    answers: bool = False       # the server answers on URL
    text: str = ""


def config_dir() -> Path:
    return paths.app_dir() / CONFIG_DIR


def write_config(folder: Optional[Path] = None) -> Path:
    """settings.yml, written once; a later setup keeps its secret key."""
    folder = folder or config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "settings.yml"
    if not path.exists():
        tmp = path.with_suffix(".yml.tmp")
        tmp.write_text(SETTINGS_YML.format(key=secrets.token_hex(32)), encoding="utf-8")
        os.replace(tmp, path)
    return path


def _docker() -> str:
    return shutil.which("docker") or ""


def _run(args: List[str], timeout: float = 60) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout, creationflags=flags)
    except subprocess.TimeoutExpired:
        raise SearxngError(f"Docker did not finish in {int(timeout)} s "
                           f"({' '.join(args[1:3])})") from None
    except OSError as exc:
        raise SearxngError(f"could not run Docker: {exc}") from None


def _docker_running(docker: str) -> bool:
    try:
        return _run([docker, "info", "--format", "{{.ServerVersion}}"], 30).returncode == 0
    except SearxngError:
        return False


def answers(url: str = URL, timeout: float = 5) -> bool:
    """The server gives a JSON answer (this PC only: no Online check)."""
    req = urllib.request.Request(f"{url.rstrip('/')}/config",
                                 headers={"User-Agent": web.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            json.loads(resp.read(200_000).decode("utf-8", errors="replace"))
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def status() -> Status:
    s = Status()
    s.answers = answers()
    docker = _docker()
    s.docker = bool(docker)
    s.running = bool(docker) and (s.answers or _docker_running(docker))
    if s.answers:
        s.text = f"SearXNG is running on this PC ({URL})."
    elif not s.docker:
        s.text = ("SearXNG is not set up. It runs in Docker: install Docker "
                  "Desktop (docker.com), start it, then press Set up SearXNG.")
    elif not s.running:
        s.text = "Docker Desktop is not running: start it, then press Set up SearXNG."
    else:
        s.text = "SearXNG is not running yet: press Set up SearXNG."
    return s


def _state(docker: str) -> str:
    """The container's state ("running", "exited", ...) or "" if none."""
    done = _run([docker, "ps", "-a", "--filter", f"name=^{CONTAINER}$",
                 "--format", "{{.State}}"], 30)
    return done.stdout.strip().splitlines()[0] if done.stdout.strip() else ""


def setup(on_progress: Optional[Progress] = None) -> web.Settings:
    """Start SearXNG in Docker on 127.0.0.1:PORT and search with it."""
    say = on_progress or (lambda _m: None)
    web.require_online()                  # the image comes from the internet
    docker = _docker()
    if not docker:
        raise SearxngError("Docker is not installed. Install Docker Desktop "
                           "(docker.com), start it, then try again.")
    say("Checking Docker…")
    if not _docker_running(docker):
        raise SearxngError("Docker Desktop is not running: start it, wait "
                           "until it says it is running, then try again.")
    folder = config_dir()
    write_config(folder)
    state = _state(docker)
    if state == "running":
        say("SearXNG is already running…")
    elif state:
        say("Starting SearXNG…")
        done = _run([docker, "start", CONTAINER], 120)
        if done.returncode != 0:
            raise SearxngError(f"Docker could not start {CONTAINER}: "
                               f"{(done.stderr or done.stdout).strip()[:300]}")
    else:
        say("Downloading and starting SearXNG (the first time takes a few minutes)…")
        done = _run([docker, "run", "-d", "--name", CONTAINER,
                     "--restart", "unless-stopped",
                     "-p", f"127.0.0.1:{PORT}:8080",
                     "-v", f"{folder}:/etc/searxng",
                     IMAGE], PULL_WAIT)
        if done.returncode != 0:
            raise SearxngError(f"Docker could not start SearXNG: "
                               f"{(done.stderr or done.stdout).strip()[:300]}")
    say("Waiting for SearXNG to answer…")
    deadline = time.monotonic() + READY_WAIT
    while not answers():
        if time.monotonic() > deadline:
            raise SearxngError(f"SearXNG started but did not answer on {URL} "
                               f"within {READY_WAIT} s; see `docker logs {CONTAINER}`.")
        time.sleep(2)
    s = web.load()
    s.search, s.searxng_url = "searxng", URL
    web.save(s)
    say(f"SearXNG is running on this PC ({URL}) and Research searches with it.")
    return s
