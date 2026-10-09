"""Raspberry Pi OS images: the official list, an on-request download, or a
file the user already has.

THE ONLY TIME THE COUNCIL GOES ONLINE FOR THIS, AND ONLY WHEN ASKED
The Council is offline by design. `fetch_catalog` and `download` run only from
an explicit click, only against https://downloads.raspberrypi.com (the host
the official Imager uses — any URL in the list that points elsewhere is
refused), and every download is checked twice: against the ``.sha256`` file
published beside it, and - before the card is erased (flash_helper) and again
while it is written - against the list's ``extract_sha256`` and
``extract_size`` of the uncompressed image. A file that is not in the list
gets no assumed checksum and no first-boot format guessed from its name.

Images are cached in %LOCALAPPDATA%/Council/pi_images — never the vault.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

OFFICIAL_HOST = "downloads.raspberrypi.com"
CATALOG_URL = f"https://{OFFICIAL_HOST}/os_list_imagingutility_v4.json"
CATALOG_NAME = "os_list_imagingutility_v4.json"


class Cancelled(Exception):
    pass


@dataclass
class OsImage:
    name: str
    url: str
    release_date: str
    init_format: str             # cloudinit-rpi | systemd
    download_size: int
    extract_size: int
    extract_sha256: str
    devices: List[str]

    @property
    def filename(self) -> str:
        return Path(urllib.parse.urlsplit(self.url).path).name

    @property
    def is_lite(self) -> bool:
        return "Lite" in self.name


def cache_dir() -> Path:
    override = os.environ.get("COUNCIL_PI_IMAGE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) / "Council" if base else Path.home() / ".council_app") / "pi_images"


def _official(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() != OFFICIAL_HOST:
        raise ValueError(f"refusing {url!r}: images come only from https://{OFFICIAL_HOST}")
    return url


def parse_catalog(data: dict) -> List[OsImage]:
    """Every Raspberry Pi OS image in the list (any depth), newest list order."""
    out: List[OsImage] = []

    def walk(items):
        for it in items or []:
            if it.get("url") and it.get("extract_sha256") and \
                    "Raspberry Pi OS" in it.get("name", ""):
                try:
                    _official(it["url"])
                except ValueError:
                    continue
                out.append(OsImage(
                    name=it["name"], url=it["url"],
                    release_date=str(it.get("release_date", "")),
                    init_format=str(it.get("init_format", "")),
                    download_size=int(it.get("image_download_size") or 0),
                    extract_size=int(it.get("extract_size") or 0),
                    extract_sha256=str(it["extract_sha256"]).lower(),
                    devices=list(it.get("devices") or [])))
            walk(it.get("subitems"))
    walk(data.get("os_list"))
    return out


def cached_catalog(directory: Optional[Path] = None) -> List[OsImage]:
    p = Path(directory or cache_dir()) / CATALOG_NAME
    if not p.exists():
        return []
    return parse_catalog(json.loads(p.read_text(encoding="utf-8")))


def _fetch(url: str, timeout: float = 60.0):
    req = urllib.request.Request(_official(url), headers={"User-Agent": "TheCouncil-PiSetup"})
    return urllib.request.urlopen(req, timeout=timeout)       # noqa: S310 — host checked


def fetch_catalog(directory: Optional[Path] = None, *, opener=_fetch) -> List[OsImage]:
    """Download the official list (on request) and cache it."""
    d = Path(directory or cache_dir())
    d.mkdir(parents=True, exist_ok=True)
    with opener(CATALOG_URL) as r:
        raw = r.read()
    data = json.loads(raw.decode("utf-8"))
    tmp = d / (CATALOG_NAME + ".part")
    tmp.write_bytes(raw)
    tmp.replace(d / CATALOG_NAME)
    return parse_catalog(data)


def default_image(images: List[OsImage]) -> Optional[OsImage]:
    """Raspberry Pi OS Lite 64-bit: no desktop to spend a Pi's RAM on, and
    64-bit for Ollama."""
    for img in images:
        if img.name == "Raspberry Pi OS Lite (64-bit)":
            return img
    return next((i for i in images if i.is_lite), images[0] if images else None)


def sha256_file(path: Path, on_progress: Optional[Callable[[int], None]] = None) -> str:
    h = hashlib.sha256()
    done = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
            done += len(chunk)
            if on_progress:
                on_progress(done)
    return h.hexdigest()


def download(img: OsImage, directory: Optional[Path] = None, *,
             on_progress: Optional[Callable[[int, int], None]] = None,
             cancelled: Callable[[], bool] = lambda: False, opener=_fetch) -> Path:
    """The image file, downloaded on request and checked against the
    published .sha256. A cached file that already matches is reused; one that
    does not is moved aside as *.bad (never silently deleted)."""
    d = Path(directory or cache_dir())
    d.mkdir(parents=True, exist_ok=True)
    final = d / img.filename
    with opener(img.url + ".sha256") as r:
        expected = r.read().decode("ascii", "replace").split()[0].strip().lower()
    if len(expected) != 64:
        raise ValueError("the published checksum could not be read")
    if final.exists():
        if sha256_file(final) == expected:
            return final
        final.replace(final.with_suffix(final.suffix + ".bad"))
    part = d / (img.filename + ".part")
    h = hashlib.sha256()
    done = 0
    with opener(img.url, timeout=120.0) as r, open(part, "wb") as out:
        total = int(r.headers.get("Content-Length") or img.download_size or 0)
        while True:
            if cancelled():
                raise Cancelled("download cancelled")
            chunk = r.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if on_progress:
                on_progress(done, total)
    if h.hexdigest() != expected:
        part.replace(final.with_suffix(final.suffix + ".bad"))
        raise ValueError("the download does not match its published checksum; "
                         "it was kept as .bad and not used")
    part.replace(final)
    return final


def local_image(path: Path) -> Path:
    """A file the user already has: a .img or .img.xz (checked when written)."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)
    if not (p.name.endswith(".img") or p.name.endswith(".img.xz")):
        raise ValueError("choose a Raspberry Pi OS .img or .img.xz file")
    return p


def match_local(path: Path, images: List[OsImage]) -> Optional[OsImage]:
    """The catalog entry for a local file with the official name, if any —
    so its uncompressed checksum and first-boot format are known."""
    name = Path(path).name
    return next((i for i in images if i.filename == name), None)
