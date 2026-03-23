"""
vault_scraper.py — Scrape documentation sites into the Council vault.

Usage:
    python vault_scraper.py                        # scrape all DEFAULT_SOURCES
    python vault_scraper.py --url https://...      # scrape one URL
    python vault_scraper.py --list sources.txt     # scrape URLs from a file
    python vault_scraper.py --depth 2              # crawl up to depth 2
    python vault_scraper.py --dry-run              # show what would be scraped

Output:
    ~/.council/vault/scraped/<domain>/<slug>.txt   (plain text, RAG-ready)
    ~/.council/vault/scraped/_index.json           (manifest of all scraped files)

The vault RAG indexer picks up .txt files in the vault automatically on next
Council startup.

Requirements (already on your machine):
    pip install beautifulsoup4 lxml   (both confirmed present)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.request
import urllib.parse
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    from bs4 import BeautifulSoup
    _BS4_OK = True
except ImportError:
    _BS4_OK = False
    print("WARNING: beautifulsoup4 not found. Run: pip install beautifulsoup4 lxml")


# ── Default sources ──────────────────────────────────────────────────────────
# Each entry: (label, seed_url, max_pages, crawl_prefix)
# crawl_prefix: only follow links that start with this string (keeps crawl focused)

DEFAULT_SOURCES: List[Tuple[str, str, int, str]] = [
    # ── Coding ────────────────────────────────────────────────────────────
    ("python-howto",
     "https://docs.python.org/3/howto/index.html",
     40, "https://docs.python.org/3/howto/"),

    ("python-guide",
     "https://docs.python-guide.org/",
     30, "https://docs.python-guide.org/"),

    ("refactoring-guru-python",
     "https://refactoring.guru/design-patterns/python",
     25, "https://refactoring.guru/design-patterns/"),

    # ── LLM Prompting ─────────────────────────────────────────────────────
    ("anthropic-prompt-engineering",
     "https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/overview",
     20, "https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/"),

    ("learnprompting",
     "https://learnprompting.org/docs/intro",
     40, "https://learnprompting.org/docs/"),

    # ── Model Training ────────────────────────────────────────────────────
    ("hf-training",
     "https://huggingface.co/docs/transformers/training",
     20, "https://huggingface.co/docs/transformers/"),

    ("hf-peft",
     "https://huggingface.co/docs/peft/index",
     20, "https://huggingface.co/docs/peft/"),

    # ── LaTeX ─────────────────────────────────────────────────────────────
    ("overleaf-math",
     "https://www.overleaf.com/learn/latex/Mathematical_expressions",
     30, "https://www.overleaf.com/learn/latex/"),

    ("overleaf-tables",
     "https://www.overleaf.com/learn/latex/Tables",
     10, "https://www.overleaf.com/learn/latex/"),

    ("overleaf-bibliography",
     "https://www.overleaf.com/learn/latex/Bibliography_management_with_bibtex",
     10, "https://www.overleaf.com/learn/latex/"),

    ("latexref",
     "https://latexref.xyz/",
     60, "https://latexref.xyz/"),
]

# ── Large sources — full site crawls, high page counts ──────────────────────
# These are excluded from the default "All default sources" run to avoid
# multi-hour scrapes by accident. Use "All large sources" or pick individually.
#
# Real Python alone has ~2000+ tutorials. Overleaf has ~400 learn pages.
# Estimated scrape times at 0.8s delay:
#   realpython-tutorials : ~2000 pages = ~27 min
#   overleaf-full        :  ~400 pages =  ~5 min
#   python-docs-full     :  ~600 pages =  ~8 min
#   mdn-javascript       :  ~800 pages = ~11 min
#   hf-docs-full         :  ~500 pages =  ~7 min

LARGE_SOURCES: List[Tuple[str, str, int, str]] = [
    ("realpython-tutorials",
     "https://realpython.com/tutorials/all/",
     2000, "https://realpython.com/"),

    ("python-docs-full",
     "https://docs.python.org/3/",
     600, "https://docs.python.org/3/"),

    ("overleaf-full",
     "https://www.overleaf.com/learn",
     400, "https://www.overleaf.com/learn/"),

    ("mdn-javascript",
     "https://developer.mozilla.org/en-US/docs/Web/JavaScript/Guide",
     800, "https://developer.mozilla.org/en-US/docs/Web/JavaScript/"),

    ("hf-docs-full",
     "https://huggingface.co/docs/transformers/index",
     500, "https://huggingface.co/docs/transformers/"),

    ("hf-peft-full",
     "https://huggingface.co/docs/peft/index",
     200, "https://huggingface.co/docs/peft/"),

    ("pytorch-tutorials",
     "https://pytorch.org/tutorials/",
     300, "https://pytorch.org/tutorials/"),

    ("numpy-docs",
     "https://numpy.org/doc/stable/user/",
     200, "https://numpy.org/doc/stable/"),

    ("pandas-docs",
     "https://pandas.pydata.org/docs/user_guide/",
     200, "https://pandas.pydata.org/docs/"),

    ("latex-wikibook",
     "https://en.wikibooks.org/wiki/LaTeX",
     300, "https://en.wikibooks.org/wiki/LaTeX"),
]

# ── Sitemap-aware sources — use XML sitemap for complete URL list ─────────────
# These sites publish sitemaps which give us every page URL upfront,
# avoiding the need to crawl link-by-link. Much faster and more complete.

SITEMAP_SOURCES: List[Tuple[str, str, int]] = [
    # (label, sitemap_url, max_pages)
    ("realpython-sitemap",  "https://realpython.com/sitemap.xml",           2000),
    ("overleaf-sitemap",    "https://www.overleaf.com/sitemap.xml",          400),
    ("python-docs-sitemap", "https://docs.python.org/3/objects.inv",         600),
]

# GitHub raw markdown files — fetched as single files, no crawling needed
GITHUB_RAW_FILES: List[Tuple[str, str]] = [
    ("dair-prompt-guide-intro",
     "https://raw.githubusercontent.com/dair-ai/Prompt-Engineering-Guide/main/guides/prompts-intro.md"),
    ("dair-prompt-advanced",
     "https://raw.githubusercontent.com/dair-ai/Prompt-Engineering-Guide/main/guides/prompts-advanced-usage.md"),
    ("dair-prompt-techniques",
     "https://raw.githubusercontent.com/dair-ai/Prompt-Engineering-Guide/main/guides/prompts-techniques.md"),
    ("dair-prompt-applications",
     "https://raw.githubusercontent.com/dair-ai/Prompt-Engineering-Guide/main/guides/prompts-applications.md"),
    ("brex-prompt-engineering",
     "https://raw.githubusercontent.com/brexhq/prompt-engineering/main/README.md"),
    ("nanoGPT-readme",
     "https://raw.githubusercontent.com/karpathy/nanoGPT/master/README.md"),
    ("axolotl-readme",
     "https://raw.githubusercontent.com/axolotl-ai-cloud/axolotl/main/README.md"),
    ("python-pep8",
     "https://raw.githubusercontent.com/python/peps/main/peps/pep-0008.rst"),
    ("python-pep20",
     "https://raw.githubusercontent.com/python/peps/main/peps/pep-0020.rst"),
    ("system-design-primer",
     "https://raw.githubusercontent.com/donnemartin/system-design-primer/master/README.md"),
]


# ── Text extraction ──────────────────────────────────────────────────────────

# Tags that are pure noise — always removed
_NOISE_TAGS = [
    "script", "style", "nav", "header", "footer", "aside",
    "form", "button", "iframe", "noscript", "svg", "img",
    "figure", "figcaption",
]

# CSS class/id patterns that indicate navigation/sidebar noise
_NOISE_CLASSES = re.compile(
    r"nav|sidebar|toc|breadcrumb|menu|footer|header|cookie|banner|"
    r"advertisement|social|share|comment|related|pagination|search",
    re.IGNORECASE,
)

# Content-bearing container patterns
_CONTENT_CLASSES = re.compile(
    r"content|main|article|body|post|entry|doc|prose|markdown|"
    r"rst-content|document|page-content|section",
    re.IGNORECASE,
)


def _extract_text(html: str, url: str = "") -> str:
    """
    Extract clean plain text from HTML.
    Removes noise tags, finds main content container, collapses whitespace.
    """
    if not _BS4_OK:
        # Fallback: crude regex stripping
        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s{2,}", " ", text).strip()

    soup = BeautifulSoup(html, "lxml")

    # Remove all noise tags
    for tag in soup(_NOISE_TAGS):
        tag.decompose()

    # Remove elements whose class or id looks like navigation/sidebar
    for tag in soup.find_all(True):
        if not hasattr(tag, "attrs") or tag.attrs is None:
            continue
        classes = " ".join(tag.get("class", []))
        tag_id  = tag.get("id", "") or ""
        if _NOISE_CLASSES.search(classes) or _NOISE_CLASSES.search(tag_id):
            tag.decompose()

    # Find best content container
    main = (
        soup.find("main")
        or soup.find("article")
        or soup.find(True, class_=_CONTENT_CLASSES)
        or soup.find("div", id=_CONTENT_CLASSES)
        or soup.body
        or soup
    )

    # Convert to text preserving some structure
    lines: List[str] = []
    for elem in (main or soup).descendants:
        if not hasattr(elem, "name"):
            # NavigableString
            text = str(elem).strip()
            if text:
                lines.append(text)
        elif elem.name in ("h1", "h2", "h3", "h4"):
            text = elem.get_text(strip=True)
            if text:
                marker = "#" * int(elem.name[1])
                lines.append(f"\n{marker} {text}\n")
        elif elem.name in ("li",):
            text = elem.get_text(strip=True)
            if text:
                lines.append(f"- {text}")
        elif elem.name in ("code", "pre"):
            text = elem.get_text()
            if text.strip():
                lines.append(f"\n```\n{text.strip()}\n```\n")
        elif elem.name == "p":
            text = elem.get_text(strip=True)
            if text:
                lines.append(text + "\n")

    raw = "\n".join(lines)

    # Collapse excessive blank lines
    raw = re.sub(r"\n{4,}", "\n\n\n", raw)
    # Remove lines that are just punctuation or single chars
    raw = "\n".join(
        line for line in raw.splitlines()
        if len(line.strip()) > 2 or line.strip() == ""
    )
    return raw.strip()


def _extract_links(html: str, base_url: str, prefix: str) -> List[str]:
    """Extract internal links from HTML that start with prefix."""
    if not _BS4_OK:
        return []
    links: List[str] = []
    try:
        soup = BeautifulSoup(html, "lxml")
        for a in soup.find_all("a", href=True):
            try:
                href = a["href"].strip()
                if not href or href.startswith(("mailto:", "javascript:", "#")):
                    continue
                # Resolve relative URLs against base
                full = urllib.parse.urljoin(base_url, href)
                # Strip fragment and trailing slash for normalisation
                full = full.split("#")[0].rstrip("/")
                # Strip query strings from doc pages (avoid ?highlight= variants)
                full = full.split("?")[0]
                if not full:
                    continue
                # Keep only links within the crawl prefix
                norm_prefix = prefix.rstrip("/")
                if full.startswith(norm_prefix):
                    links.append(full)
            except Exception:
                continue
    except Exception:
        pass
    return list(dict.fromkeys(links))  # deduplicate preserving order


# ── Fetching ─────────────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
}

_ROBOTS_CACHE: Dict[str, urllib.robotparser.RobotFileParser] = {}


def _can_fetch(url: str) -> bool:
    """Check robots.txt. Returns True if allowed or robots.txt unreachable."""
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return False  # malformed URL
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    if robots_url not in _ROBOTS_CACHE:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(robots_url)
        try:
            import ssl as _ssl
            ctx = _ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode    = _ssl.CERT_NONE
            # Manually fetch robots.txt so we can pass SSL context
            req = urllib.request.Request(robots_url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=8, context=ctx) as resp:
                rp.parse(resp.read().decode("utf-8", errors="replace").splitlines())
        except Exception:
            pass  # unreachable = assume allowed
        _ROBOTS_CACHE[robots_url] = rp
    try:
        return _ROBOTS_CACHE[robots_url].can_fetch(_HEADERS["User-Agent"], url)
    except Exception:
        return True


def _fetch(url: str, timeout: int = 15) -> Optional[str]:
    """Fetch URL, return HTML string or None on failure."""
    import ssl as _ssl
    # Create a context that works on Windows without system cert issues
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = _ssl.CERT_NONE
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read()
            encoding = "utf-8"
            if "charset=" in content_type:
                enc_raw = content_type.split("charset=")[-1].split(";")[0].strip()
                if enc_raw:
                    encoding = enc_raw
            return raw.decode(encoding, errors="replace")
    except urllib.error.HTTPError as e:
        print(f"    HTTP {e.code}: {url}")
        return None
    except urllib.error.URLError as e:
        print(f"    URL error ({e.reason}): {url}")
        return None
    except Exception as e:
        print(f"    Fetch error ({type(e).__name__}: {e}): {url}")
        return None


def _fetch_raw(url: str, timeout: int = 15) -> Optional[str]:
    """Fetch raw text (for markdown/rst files)."""
    import ssl as _ssl
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = _ssl.CERT_NONE
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        print(f"    HTTP {e.code}: {url}")
        return None
    except urllib.error.URLError as e:
        print(f"    URL error ({e.reason}): {url}")
        return None
    except Exception as e:
        print(f"    Fetch error ({type(e).__name__}: {e}): {url}")
        return None


# ── Sitemap parsing ──────────────────────────────────────────────────────────

def _fetch_sitemap_urls(sitemap_url: str, prefix: str = "", max_urls: int = 5000) -> List[str]:
    """
    Fetch an XML sitemap and return all page URLs.
    Handles sitemap index files (sitemaps that point to other sitemaps).
    """
    import xml.etree.ElementTree as _ET

    def _get_urls_from_xml(xml_text: str) -> List[str]:
        urls = []
        try:
            root = _ET.fromstring(xml_text)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            # Sitemap index — recurse
            for sitemap in root.findall("sm:sitemap/sm:loc", ns):
                sub_xml = _fetch_raw(sitemap.text.strip())
                if sub_xml:
                    urls.extend(_get_urls_from_xml(sub_xml))
            # Regular sitemap
            for url in root.findall("sm:url/sm:loc", ns):
                urls.append(url.text.strip())
        except Exception:
            # Try without namespace
            try:
                root = _ET.fromstring(xml_text)
                for elem in root.iter():
                    if elem.tag.endswith("loc") and elem.text:
                        urls.append(elem.text.strip())
            except Exception:
                pass
        return urls

    raw = _fetch_raw(sitemap_url)
    if not raw:
        return []

    all_urls = _get_urls_from_xml(raw)

    # Filter by prefix if given
    if prefix:
        all_urls = [u for u in all_urls if u.startswith(prefix)]

    # Deduplicate and cap
    seen = set()
    result = []
    for u in all_urls:
        u_norm = u.rstrip("/").split("?")[0]
        if u_norm not in seen:
            seen.add(u_norm)
            result.append(u)
        if len(result) >= max_urls:
            break

    return result


def crawl_sitemap(
    label: str,
    sitemap_url: str,
    vault_dir: Path,
    index: Dict,
    *,
    prefix: str = "",
    max_pages: int = 2000,
    delay: float = 0.6,
    dry_run: bool = False,
    verbose: bool = True,
    progress_cb=None,
    abort_cb=None,
) -> int:
    """
    Crawl a site using its XML sitemap for a complete, ordered URL list.
    Faster and more complete than link-following crawl for large sites.
    """
    def _log(msg: str, error: bool = False):
        if verbose:
            print(msg)
        if progress_cb:
            progress_cb(msg, error)

    _log(f"  Fetching sitemap: {sitemap_url}")
    urls = _fetch_sitemap_urls(sitemap_url, prefix=prefix, max_urls=max_pages)
    if not urls:
        _log(f"  ✗ No URLs found in sitemap", True)
        return 0

    _log(f"  Sitemap: {len(urls)} URLs found (capped at {max_pages})")
    saved = 0

    for i, url in enumerate(urls[:max_pages], 1):
        if abort_cb and abort_cb():
            _log(f"  [abort] stopping {label}")
            break

        if not _can_fetch(url):
            _log(f"  [robots] skipping {url}")
            continue

        _log(f"  [{i}/{min(len(urls), max_pages)}] {url}")

        if dry_run:
            saved += 1
            continue

        html = _fetch(url)
        if not html:
            _log(f"    ✗ fetch failed: {url}", True)
            continue

        text = _extract_text(html, url)
        if len(text) < 200:
            _log(f"    ✗ too short ({len(text)} chars)")
            continue

        _write_vault(vault_dir, label, url, text, index)
        saved += 1
        _log(f"    ✓ {len(text):,} chars")
        time.sleep(delay)

    return saved


# ── Output helpers ─────────────────────────────────────────────────────────────

def _url_to_slug(url: str) -> str:
    """Convert URL to a safe filename slug."""
    parsed = urllib.parse.urlparse(url)
    path   = parsed.path.strip("/").replace("/", "_") or "index"
    path   = re.sub(r"[^a-zA-Z0-9_\-]", "_", path)
    path   = re.sub(r"_+", "_", path).strip("_")
    if len(path) > 80:
        path = path[:80] + "_" + hashlib.md5(url.encode()).hexdigest()[:6]
    return path or "page"


def _url_to_domain(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return re.sub(r"[^a-zA-Z0-9\-]", "_", parsed.netloc)


def _write_vault(vault_dir: Path, label: str, url: str, text: str,
                 index: Dict) -> Path:
    """Write extracted text to vault and update index."""
    domain   = _url_to_domain(url)
    slug     = _url_to_slug(url)
    out_dir  = vault_dir / "scraped" / domain
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{slug}.txt"

    header = (
        f"SOURCE: {url}\n"
        f"LABEL:  {label}\n"
        f"DOMAIN: {domain}\n"
        f"{'─' * 60}\n\n"
    )
    out_path.write_text(header + text, encoding="utf-8")

    index[url] = {
        "label":  label,
        "file":   str(out_path),
        "chars":  len(text),
        "domain": domain,
    }
    return out_path


# ── Crawler ───────────────────────────────────────────────────────────────────

def crawl(
    label: str,
    seed_url: str,
    prefix: str,
    vault_dir: Path,
    index: Dict,
    *,
    max_pages: int = 30,
    delay: float = 0.8,
    dry_run: bool = False,
    verbose: bool = True,
    progress_cb=None,   # callable(msg, error) for live GUI updates
    abort_cb=None,      # callable() -> bool, return True to stop
) -> int:
    """
    Crawl a site starting from seed_url, following links that start with prefix.
    Returns number of pages saved.
    """
    def _log(msg: str, error: bool = False):
        if verbose:
            print(msg)
        if progress_cb:
            progress_cb(msg, error)

    # Normalise seed URL
    seed_url = seed_url.rstrip("/").split("?")[0]
    norm_prefix = prefix.rstrip("/")

    visited: Set[str] = set()
    queue:   List[str] = [seed_url]
    saved   = 0

    while queue and len(visited) < max_pages:
        if abort_cb and abort_cb():
            _log(f"  [abort] stopping {label}")
            break

        url = queue.pop(0)
        # Normalise for dedup
        url_norm = url.rstrip("/").split("?")[0]
        if url_norm in visited:
            continue
        visited.add(url_norm)

        if not _can_fetch(url):
            _log(f"  [robots] skipping {url}")
            continue

        _log(f"  [{len(visited)}/{max_pages}] {url}")

        if dry_run:
            saved += 1
            continue

        html = _fetch(url)
        if not html:
            _log(f"    ✗ fetch failed: {url}", True)
            continue

        text = _extract_text(html, url)
        if len(text) < 200:
            _log(f"    ✗ too short ({len(text)} chars), skipping")
            continue

        path = _write_vault(vault_dir, label, url, text, index)
        saved += 1
        _log(f"    ✓ {len(text):,} chars → {path.name}")

        # Find more links and queue them
        new_links = _extract_links(html, url, norm_prefix)
        added = 0
        for link in new_links:
            link_norm = link.rstrip("/").split("?")[0]
            if link_norm not in visited and link not in queue:
                queue.append(link)
                added += 1
        if added:
            _log(f"    + queued {added} new links (queue depth: {len(queue)})")

        time.sleep(delay)

    return saved


def fetch_single(
    label: str,
    url: str,
    vault_dir: Path,
    index: Dict,
    *,
    raw: bool = False,
    dry_run: bool = False,
    verbose: bool = True,
) -> bool:
    """Fetch a single URL (HTML or raw text/markdown)."""
    if verbose:
        print(f"  fetching {url}")

    if dry_run:
        return True

    if not _can_fetch(url):
        if verbose:
            print(f"  [robots] skipping {url}")
        return False

    content = _fetch_raw(url) if raw else _fetch(url)
    if not content:
        if verbose:
            print(f"  ✗ failed")
        return False

    if raw:
        text = content
    else:
        text = _extract_text(content, url)

    if len(text) < 100:
        if verbose:
            print(f"  ✗ too short, skipping")
        return False

    path = _write_vault(vault_dir, label, url, text, index)
    if verbose:
        print(f"  ✓ {len(text):,} chars → {path.name}")
    return True


# ── Index ─────────────────────────────────────────────────────────────────────

def _load_index(vault_dir: Path) -> Dict:
    path = vault_dir / "scraped" / "_index.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_index(vault_dir: Path, index: Dict) -> None:
    path = vault_dir / "scraped" / "_index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Scrape documentation into the Council vault.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--url",     help="Scrape a single URL")
    parser.add_argument("--list",    help="File with one URL per line")
    parser.add_argument("--label",   help="Label for --url scrape", default="custom")
    parser.add_argument("--prefix",  help="Crawl prefix for --url (default: same as URL)")
    parser.add_argument("--depth",   type=int, default=1,
                        help="Crawl depth: 1=seed only, 2=follow links one level, etc.")
    parser.add_argument("--max",     type=int, default=None,
                        help="Max pages per source (overrides defaults)")
    parser.add_argument("--delay",   type=float, default=0.8,
                        help="Delay between requests in seconds (default: 0.8)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be scraped without writing files")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Skip URLs already in the index")
    parser.add_argument("--vault",   help="Vault directory (default: ~/.council/vault)")
    parser.add_argument("--no-github", action="store_true",
                        help="Skip GitHub raw file downloads")
    parser.add_argument("--only",    help="Comma-separated list of source labels to run")
    args = parser.parse_args()

    vault_dir = Path(args.vault) if args.vault else Path.home() / ".council" / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)

    if not _BS4_OK:
        print("ERROR: beautifulsoup4 required. Run: pip install beautifulsoup4 lxml")
        return

    index    = _load_index(vault_dir)
    total_ok = 0
    only_set = set(args.only.split(",")) if args.only else None

    # ── Single URL mode ───────────────────────────────────────────────────
    if args.url:
        prefix = args.prefix or args.url
        max_p  = args.max or 30
        if args.depth <= 1:
            ok = fetch_single(args.label, args.url, vault_dir, index,
                              dry_run=args.dry_run)
            total_ok += int(ok)
        else:
            total_ok += crawl(args.label, args.url, prefix, vault_dir, index,
                              max_pages=max_p, delay=args.delay,
                              dry_run=args.dry_run)
        _save_index(vault_dir, index)
        print(f"\nDone. {total_ok} page(s) saved.")
        return

    # ── List file mode ────────────────────────────────────────────────────
    if args.list:
        with open(args.list, encoding="utf-8") as f:
            urls = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        for url in urls:
            if args.skip_existing and url in index:
                print(f"  [skip] {url}")
                continue
            ok = fetch_single("custom", url, vault_dir, index,
                              dry_run=args.dry_run)
            total_ok += int(ok)
            time.sleep(args.delay)
        _save_index(vault_dir, index)
        print(f"\nDone. {total_ok} page(s) saved.")
        return

    # ── Default: scrape all DEFAULT_SOURCES ───────────────────────────────
    print(f"Vault: {vault_dir}")
    print(f"Dry run: {args.dry_run}\n")

    # GitHub raw files first (fast, no crawling)
    if not args.no_github:
        print("── GitHub raw files ─────────────────────────────────────")
        for label, url in GITHUB_RAW_FILES:
            if only_set and label not in only_set:
                continue
            if args.skip_existing and url in index:
                print(f"  [skip] {label}")
                continue
            ok = fetch_single(label, url, vault_dir, index,
                              raw=True, dry_run=args.dry_run)
            total_ok += int(ok)
            time.sleep(args.delay * 0.5)
        print()

    # Crawl doc sites
    print("── Documentation sites ──────────────────────────────────────")
    for label, seed, max_p, prefix in DEFAULT_SOURCES:
        if only_set and label not in only_set:
            continue
        if args.skip_existing and seed in index:
            print(f"  [skip] {label}")
            continue
        print(f"\n[{label}] {seed}")
        n = crawl(
            label, seed, prefix, vault_dir, index,
            max_pages=args.max or max_p,
            delay=args.delay,
            dry_run=args.dry_run,
        )
        total_ok += n
        print(f"  → {n} pages saved")
        _save_index(vault_dir, index)  # save after each source

    print(f"\n{'─'*50}")
    print(f"Total pages saved: {total_ok}")
    print(f"Index: {vault_dir / 'scraped' / '_index.json'}")
    print(f"\nRestart Council to re-index the vault RAG.")


if __name__ == "__main__":
    main()
