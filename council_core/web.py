"""
council_core.web — the one door from Council to the internet, and its switch.

Council is offline by design (CLAUDE.md: "nothing leaves the PC unless the
user opts in"). The user asked (2026-11-09) for Council to research on its
own - "look for guides on how to paint flamethrowers on warhammer models" -
and chose an opt-in Research feature whose first piece is ONE Online switch:

    OFF (the default)  nothing in Council reaches the internet through here;
                       every call raises Offline with a sentence saying where
                       the switch is.
    ON                 searches and page reads go out, one request at a time,
                       each with a deadline and a size cap.

The switch is ``<app dir>/online.json`` ({"online": bool, "search": ...}),
written atomically by the Settings tab. ``COUNCIL_OFFLINE=1`` in the
environment keeps Council offline whatever the file says (an air-gapped PC's
administrator sets it once).

The check is HERE, at the door (like council_core.mcp_client's "allow
remote"), so no caller can forget it: ``fetch`` and ``search`` call
``require_online`` first. intern_agent.py and vault_scraper.py, the older
web code, go through it too.

WHAT GOES OUT
  * Only http and https URLs; never a file:, ftp: or data: URL.
  * No cookies, no logins, no Referer; one plain User-Agent naming Council.
  * robots.txt is read (and kept for the run) and a page it disallows is not
    fetched.
  * A page is cut at MAX_BYTES; only text-like pages (HTML, plain text,
    Markdown, JSON) are read.

Search engines (``search``):
  * "duckduckgo" (default): DuckDuckGo's HTML page, no key or account;
  * "brave": the Brave Search API with the user's own key;
  * "searxng": the user's own SearXNG server (its JSON output).

What a page SAYS is data. Nothing here interprets it; council_core.
web_research tells the model to ignore instructions written in pages.

Nothing here imports Qt. No pickle.
"""
from __future__ import annotations

import html as html_lib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from council_core import paths

SETTINGS_FILE = "online.json"
ENGINES = ("duckduckgo", "brave", "searxng")
ENGINE_NAMES = {"duckduckgo": "DuckDuckGo (no account)",
                "brave": "Brave Search (your API key)",
                "searxng": "Your own SearXNG server"}
USER_AGENT = "CouncilResearch/1.0 (+offline-first desktop app; one page at a time)"
#: a page is read up to this many bytes, then cut
MAX_BYTES = 2_000_000
#: seconds a request may take
TIMEOUT = 20
#: content types read as text
TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain",
              "text/markdown", "application/json")

_lock = threading.Lock()


class Offline(RuntimeError):
    """The Online switch is off (or COUNCIL_OFFLINE=1): nothing was sent."""


class WebError(RuntimeError):
    """A request went out and failed; the message is a sentence."""


# ======================================================================
# The switch
# ======================================================================
@dataclass
class Settings:
    online: bool = False
    search: str = "duckduckgo"
    searxng_url: str = ""
    brave_key: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"online": self.online, "search": self.search,
                "searxng_url": self.searxng_url, "brave_key": self.brave_key}


def settings_path() -> Path:
    return paths.app_dir() / SETTINGS_FILE


def forced_offline() -> bool:
    """COUNCIL_OFFLINE=1 (or true/yes/on): offline whatever the file says."""
    return os.environ.get("COUNCIL_OFFLINE", "").strip().lower() in (
        "1", "true", "yes", "on")


def load() -> Settings:
    """The saved settings. A missing or broken file means offline."""
    try:
        doc = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    if not isinstance(doc, dict):
        return Settings()
    engine = doc.get("search") if doc.get("search") in ENGINES else "duckduckgo"
    return Settings(online=doc.get("online") is True, search=engine,
                    searxng_url=str(doc.get("searxng_url") or "").strip(),
                    brave_key=str(doc.get("brave_key") or "").strip())


def save(settings: Settings) -> Settings:
    if settings.search not in ENGINES:
        raise ValueError(f"search must be one of {', '.join(ENGINES)}")
    if settings.search == "searxng" and settings.searxng_url:
        _check_url(settings.searxng_url)
    from council_core.app_plugins import write_json_atomic
    with _lock:
        write_json_atomic(settings_path(), settings.to_dict())
    return settings


def set_online(on: bool) -> Settings:
    s = load()
    s.online = bool(on)
    return save(s)


def is_online() -> bool:
    return not forced_offline() and load().online


def why_offline() -> str:
    if forced_offline():
        return ("Council is kept offline on this PC (COUNCIL_OFFLINE is set), "
                "so it cannot reach the internet.")
    return ("Council is offline. Switch on Online in the Settings tab "
            "to let it search the web.")


def require_online() -> Settings:
    """The settings, or Offline. Every request goes through this first."""
    if forced_offline():
        raise Offline(why_offline())
    s = load()
    if not s.online:
        raise Offline(why_offline())
    return s


# ======================================================================
# Fetching
# ======================================================================
@dataclass
class Page:
    url: str                     # the URL asked for
    final_url: str               # after redirects
    status: int
    content_type: str
    text: str                    # the body, decoded
    fetched: str = ""            # ISO time, UTC
    cut: bool = False            # longer than MAX_BYTES


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _check_url(url: str) -> urllib.parse.SplitResult:
    parts = urllib.parse.urlsplit(str(url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise WebError(f"only web addresses (http or https) can be fetched, "
                       f"not {str(url)[:120]!r}")
    if parts.username or parts.password:
        raise WebError("a web address with a user name or password in it "
                       "is not fetched")
    return parts


def _opener():
    """urllib's plain opener: proxies from the environment, redirects
    followed, and no cookie handling at all (none sent, none kept)."""
    return urllib.request.build_opener()


def _open(url: str, headers: Optional[Dict[str, str]] = None,
          timeout: float = TIMEOUT) -> Tuple[Any, bytes, bool]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept-Language": "en",
                                               **(headers or {})})
    try:
        resp = _opener().open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise WebError(f"{_host(url)} answered {exc.code} {exc.reason}") from None
    except urllib.error.URLError as exc:
        raise WebError(f"could not reach {_host(url)}: {exc.reason}") from None
    except (TimeoutError, OSError) as exc:
        raise WebError(f"could not reach {_host(url)}: {exc}") from None
    with resp:
        body = resp.read(MAX_BYTES + 1)
    return resp, body[:MAX_BYTES], len(body) > MAX_BYTES


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc or url[:80]


def _decode(body: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    enc = m.group(1) if m else ""
    if not enc:
        m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4000], re.I)
        enc = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    try:
        return body.decode(enc, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


class Robots:
    """robots.txt per site, read once per run. A site whose robots.txt
    cannot be read is allowed (the usual crawler rule)."""

    def __init__(self):
        self._sites: Dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}

    def allowed(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        site = f"{parts.scheme}://{parts.netloc}"
        if site not in self._sites:
            rp: Optional[urllib.robotparser.RobotFileParser] = None
            try:
                _, body, _ = _open(site + "/robots.txt", timeout=10)
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(body.decode("utf-8", errors="replace").splitlines())
            except WebError:
                rp = None
            self._sites[site] = rp
        rp = self._sites[site]
        return True if rp is None else rp.can_fetch(USER_AGENT, url)


def fetch(url: str, *, robots: Optional[Robots] = None) -> Page:
    """One page, after the switch and robots.txt are checked."""
    require_online()
    _check_url(url)
    if robots is not None and not robots.allowed(url):
        raise WebError(f"{_host(url)} asks crawlers not to read that page "
                       f"(robots.txt), so it was skipped")
    resp, body, cut = _open(url)
    ctype = resp.headers.get("Content-Type", "") or ""
    if ctype and not any(t in ctype.lower() for t in TEXT_TYPES):
        raise WebError(f"{_host(url)} sent {ctype.split(';')[0]}, not a "
                       f"web page Council can read")
    return Page(url=url, final_url=resp.geturl() or url,
                status=getattr(resp, "status", 200) or 200,
                content_type=ctype, text=_decode(body, ctype),
                fetched=now_iso(), cut=cut)


# ======================================================================
# Reading a page: HTML -> title, date and plain text with headings
# ======================================================================
_SKIP = {"script", "style", "noscript", "nav", "footer", "header", "aside",
         "form", "svg", "iframe", "button", "select", "template", "canvas"}
_BLOCK = {"p", "div", "section", "article", "main", "br", "tr", "table",
          "blockquote", "pre", "figure", "figcaption", "dd", "dt", "ul", "ol"}
_HEAD = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### ",
         "h5": "#### ", "h6": "#### "}
_VOID = {"br", "img", "hr", "meta", "link", "input", "source", "wbr", "area",
         "base", "col", "embed", "param", "track"}


class _Reader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.stack: List[str] = []
        self.out: List[str] = []
        self.title = ""
        self.in_title = False
        self.meta: Dict[str, str] = {}
        self.times: List[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"])
            return
        if tag == "time" and a.get("datetime"):
            self.times.append(a["datetime"])
        if tag in _VOID:
            if tag == "br":
                self.out.append("\n")
            return
        if self.skip or tag in _SKIP:
            self.skip += 1
            self.stack.append(tag)
            return
        self.stack.append(tag)
        if tag == "title":
            self.in_title = True
        elif tag in _HEAD:
            self.out.append("\n\n" + _HEAD[tag])
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in _BLOCK:
            self.out.append("\n\n")

    def handle_endtag(self, tag):
        if tag in _VOID or tag not in self.stack:
            return
        while self.stack:
            top = self.stack.pop()
            if self.skip:
                self.skip -= 1
            if top == "title":
                self.in_title = False
            if top == tag:
                break
        if tag in _HEAD or tag in _BLOCK:
            self.out.append("\n\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
            return
        if self.skip:
            return
        self.out.append(data)


@dataclass
class Article:
    title: str
    text: str
    published: str = ""
    description: str = ""
    site: str = ""


def _tidy(text: str) -> str:
    lines = []
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if line in ("-", "#", "##", "###", "####"):
            continue
        lines.append(line)
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def read_page(page: Page) -> Article:
    """The readable part of a page: its title, date and text."""
    ctype = (page.content_type or "").lower()
    site = _host(page.final_url or page.url)
    if "html" not in ctype and not page.text.lstrip()[:200].lower().startswith(
            ("<!doctype html", "<html")):
        return Article(title=site, text=_tidy(page.text), site=site)
    r = _Reader()
    try:
        r.feed(page.text)
        r.close()
    except Exception:                                    # noqa: BLE001
        pass
    meta = r.meta
    title = (meta.get("og:title") or " ".join(r.title.split()) or site).strip()
    published = (meta.get("article:published_time") or meta.get("datepublished")
                 or meta.get("date") or meta.get("pubdate")
                 or (r.times[0] if r.times else "")).strip()
    desc = (meta.get("description") or meta.get("og:description") or "").strip()
    text = _tidy(html_lib.unescape("".join(r.out)))
    return Article(title=html_lib.unescape(title)[:300], text=text,
                   published=published[:40], description=desc[:500], site=site)


# ======================================================================
# Searching
# ======================================================================
@dataclass
class Hit:
    url: str
    title: str = ""
    snippet: str = ""


def _ddg_url(href: str) -> str:
    href = html_lib.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    parts = urllib.parse.urlsplit(href)
    if "duckduckgo.com" in parts.netloc and parts.path.startswith("/l/"):
        q = urllib.parse.parse_qs(parts.query)
        return (q.get("uddg") or [""])[0]
    return href


def parse_duckduckgo(page_html: str) -> List[Hit]:
    """Result links of DuckDuckGo's HTML page, in order."""
    hits: List[Hit] = []
    seen = set()
    starts = [m for m in re.finditer(r'<div[^>]+class="([^"]*)"', page_html)
              if "result" in m.group(1).split()]
    for i, start in enumerate(starts):
        if "result--ad" in start.group(1).split():
            continue
        end = starts[i + 1].start() if i + 1 < len(starts) else len(page_html)
        block = page_html[start.end():end]
        m = re.search(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                      block, re.S) or re.search(
            r'<a[^>]+href="([^"]+)"[^>]+class="[^"]*result__a[^"]*"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        url = _ddg_url(m.group(1))
        if not url.startswith(("http://", "https://")) or "duckduckgo.com" in url:
            continue
        if url in seen:
            continue
        seen.add(url)
        sm = re.search(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div|td)>', block, re.S)
        clean = lambda s: " ".join(html_lib.unescape(re.sub(r"<[^>]+>", "", s)).split())
        hits.append(Hit(url=url, title=clean(m.group(2)),
                        snippet=clean(sm.group(1)) if sm else ""))
    return hits


def search(query: str, limit: int = 6) -> List[Hit]:
    """Web results for ``query`` from the engine chosen in Settings."""
    s = require_online()
    query = " ".join(str(query or "").split())[:300]
    if not query:
        return []
    q = urllib.parse.quote_plus(query)
    if s.search == "brave":
        if not s.brave_key:
            raise WebError("Brave Search needs your API key (Settings › Online)")
        _, body, _ = _open(f"https://api.search.brave.com/res/v1/web/search?q={q}"
                           f"&count={max(1, min(limit, 20))}",
                           headers={"Accept": "application/json",
                                    "X-Subscription-Token": s.brave_key})
        doc = json.loads(body.decode("utf-8", errors="replace") or "{}")
        rows = ((doc.get("web") or {}).get("results") or [])
        hits = [Hit(url=r.get("url", ""), title=r.get("title", ""),
                    snippet=re.sub(r"<[^>]+>", "", r.get("description", "")))
                for r in rows if str(r.get("url", "")).startswith("http")]
    elif s.search == "searxng":
        if not s.searxng_url:
            raise WebError("set your SearXNG server's address (Settings › Online)")
        base = s.searxng_url.rstrip("/")
        _check_url(base)
        _, body, _ = _open(f"{base}/search?q={q}&format=json",
                           headers={"Accept": "application/json"})
        try:
            doc = json.loads(body.decode("utf-8", errors="replace") or "{}")
        except ValueError:
            raise WebError("the SearXNG server did not answer in JSON - switch on "
                           "the json format in its settings.yml") from None
        hits = [Hit(url=r.get("url", ""), title=r.get("title", ""),
                    snippet=r.get("content", ""))
                for r in doc.get("results") or [] if str(r.get("url", "")).startswith("http")]
    else:
        _, body, _ = _open(f"https://html.duckduckgo.com/html/?q={q}")
        hits = parse_duckduckgo(body.decode("utf-8", errors="replace"))
    out, seen = [], set()
    for h in hits:
        if h.url not in seen:
            seen.add(h.url)
            out.append(h)
    return out[:limit]


def pause(seconds: float = 0.5) -> None:
    """A short gap between requests to one engine, to be polite."""
    time.sleep(seconds)
