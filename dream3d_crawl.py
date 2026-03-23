# ============================================================
# dream3d_crawl.py  —  Dream3D docs crawler
# ============================================================
# Scrapes all pages from https://www.dream3d.io/python_docs/
# and saves them as clean markdown files into your vault.
# Run this once (and again when docs update).
#
# Usage:
#   conda activate council
#   pip install requests beautifulsoup4 html2text
#   python dream3d_crawl.py
#
# Output: vault/dream3d_docs/*.md
# ============================================================

import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup
    import html2text
except ImportError:
    print("Missing dependencies. Run:")
    print("  pip install requests beautifulsoup4 html2text")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────
BASE_URL   = "https://www.dream3d.io/python_docs/"
VAULT_DIR  = Path(__file__).parent / "vault" / "dream3d_docs"
DELAY_S    = 0.5   # polite crawl delay between requests
TIMEOUT_S  = 15

# Pages to crawl — all substantive docs pages (skip release notes)
PAGES = [
    ("index",               ""),
    ("installation",        "Installation.html"),
    ("overview",            "Overview.html"),
    ("data_objects",        "DataObjects.html"),
    ("geometry",            "Geometry.html"),
    ("reference_frame",     "Reference_Frame_Notes.html"),
    ("python_introduction", "Python_Introduction.html"),
    ("user_api",            "User_API.html"),
    ("tutorial_1",          "Tutorial_1.html"),
    ("tutorial_2",          "Tutorial_2.html"),
    ("tutorial_3",          "Tutorial_3.html"),
    ("filter_simplnx",      "simplnx.html"),
    ("filter_orientation",  "OrientationAnalysis.html"),
    ("filter_itk",          "ITKImageProcessing.html"),
    ("writing_filter",      "Writing_A_New_Python_Filter.html"),
    ("developer_api",       "Developer_API.html"),
]

# ── HTML → Markdown converter ─────────────────────────────────
def _make_converter() -> html2text.HTML2Text:
    h = html2text.HTML2Text()
    h.ignore_links      = False
    h.ignore_images     = True
    h.ignore_tables     = False
    h.body_width        = 0       # no line wrapping
    h.protect_links     = False
    h.wrap_links        = False
    h.unicode_snob      = True
    h.skip_internal_links = True
    return h


def _clean_markdown(md: str) -> str:
    """Remove nav boilerplate, excessive blank lines, etc."""
    lines = md.splitlines()
    cleaned = []
    blank_run = 0
    for line in lines:
        stripped = line.strip()
        # Drop pure nav lines
        if stripped in ("* [SIMPLNX Python Docs](#)", "* [View page source]"):
            continue
        # Collapse excessive blank lines
        if not stripped:
            blank_run += 1
            if blank_run <= 2:
                cleaned.append("")
        else:
            blank_run = 0
            cleaned.append(line)
    return "\n".join(cleaned).strip()


def _fetch_page(session: requests.Session, url: str) -> str | None:
    try:
        resp = session.get(url, timeout=TIMEOUT_S)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"  ✗ fetch error: {e}")
        return None


def _extract_main_content(html: str) -> str:
    """Extract just the main content div, strip nav/sidebar."""
    soup = BeautifulSoup(html, "html.parser")

    # Remove nav, sidebar, footer, header
    for tag in soup.select("nav, .sidebar, .sphinxsidebar, footer, "
                           ".related, .navigation, #searchbox, "
                           ".headerlink, .toctree-wrapper"):
        tag.decompose()

    # Try to get main content area
    main = (
        soup.find("div", role="main") or
        soup.find("div", class_="document") or
        soup.find("article") or
        soup.find("div", class_="body") or
        soup.find("body")
    )
    return str(main) if main else html


# ── Main crawler ──────────────────────────────────────────────

def crawl():
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    converter = _make_converter()

    session = requests.Session()
    session.headers.update({
        "User-Agent": "CouncilBot/1.0 (Dream3D docs crawler for local RAG)"
    })

    print(f"Crawling {BASE_URL}")
    print(f"Output → {VAULT_DIR}\n")

    success = 0
    failed  = 0

    for name, path in PAGES:
        url      = urljoin(BASE_URL, path) if path else BASE_URL
        out_path = VAULT_DIR / f"{name}.md"

        print(f"  Fetching: {url}")
        html = _fetch_page(session, url)
        if html is None:
            failed += 1
            continue

        # Extract main content, convert to markdown
        main_html = _extract_main_content(html)
        md        = converter.handle(main_html)
        md        = _clean_markdown(md)

        # Prepend metadata header
        header = (
            f"# Dream3D-NX Docs: {name.replace('_', ' ').title()}\n"
            f"Source: {url}\n"
            f"---\n\n"
        )
        out_path.write_text(header + md, encoding="utf-8")
        size = len(md)
        print(f"  ✓ {out_path.name}  ({size:,} chars)")
        success += 1

        time.sleep(DELAY_S)

    # Write a combined "primer" file with the most essential pages
    # (index + intro + tutorial_1) for quick RAG retrieval
    _write_primer()

    print(f"\nDone. {success} pages saved, {failed} failed.")
    print(f"Vault: {VAULT_DIR}")
    print("\nNext: restart the council and run 'Re-index Vault Now' in the Agents tab.")


def _write_primer():
    """
    Write a single condensed primer file combining the most important
    structural knowledge about simplnx pipelines. This is what the
    Tech-Priest and Writer will almost always retrieve first.
    """
    primer_path = VAULT_DIR / "_primer_simplnx_api.md"

    # Read any pages that were successfully saved
    essential = ["python_introduction", "tutorial_1", "tutorial_2", "overview", "user_api"]
    sections  = []
    for name in essential:
        p = VAULT_DIR / f"{name}.md"
        if p.exists():
            sections.append(p.read_text(encoding="utf-8"))

    if not sections:
        return

    combined = "\n\n---\n\n".join(sections)
    primer_path.write_text(combined, encoding="utf-8")
    print(f"  ✓ _primer_simplnx_api.md  (combined, {len(combined):,} chars)")


if __name__ == "__main__":
    crawl()
