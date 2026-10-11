"""Fetch and parse RSS 2.0 and Atom feeds with the standard library.

Defensive: a size cap, a timeout, and any document declaring entities or a
DOCTYPE is refused (no entity-expansion tricks).
"""
from __future__ import annotations

import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List, Optional

MAX_BYTES = 2_000_000
USER_AGENT = "quant-duel/1.0 (paper trading research; RSS reader)"


class FeedError(RuntimeError):
    pass


@dataclass(frozen=True)
class Item:
    title: str
    link: str
    raw_time: Optional[str]


def fetch(url: str, timeout_s: float = 20) -> bytes:
    if not url.startswith(("http://", "https://")):
        raise FeedError(f"not an http(s) feed: {url!r}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            data = r.read(MAX_BYTES + 1)
    except Exception as exc:                              # noqa: BLE001
        raise FeedError(f"fetch failed for {url}: {exc}") from exc
    if len(data) > MAX_BYTES:
        raise FeedError(f"feed larger than {MAX_BYTES} bytes: {url}")
    return data


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(el: ET.Element, name: str) -> Optional[str]:
    for child in el:
        if _local(child.tag) == name and child.text:
            return child.text.strip()
    return None


def parse(data: bytes) -> List[Item]:
    head = data[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in data.upper():
        raise FeedError("feed declares a DOCTYPE/entities; refused")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise FeedError(f"not valid XML: {exc}") from exc
    items: List[Item] = []
    for el in root.iter():
        kind = _local(el.tag)
        if kind == "item":                                   # RSS 2.0
            title = _text(el, "title")
            link = _text(el, "link") or _text(el, "guid") or ""
            when = _text(el, "pubDate") or _text(el, "date")
        elif kind == "entry":                                # Atom
            title = _text(el, "title")
            link = ""
            for child in el:
                if _local(child.tag) == "link":
                    link = child.get("href", "") or link
            when = _text(el, "published") or _text(el, "updated")
        else:
            continue
        if title:
            items.append(Item(" ".join(title.split()), link, when))
    return items
