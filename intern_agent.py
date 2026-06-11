# ============================================================
# intern_agent.py  —  Research-augmented Intern  [DESKTOP: RTX 5080 16GB]
# ============================================================
# Upgrades the Intern with optional web research capability.
# The model self-decides whether web info is needed before drafting.
#
# Install:
#   pip install crawl4ai
#   crawl4ai-setup   (installs Playwright browsers)
#
# Falls back gracefully to single-shot if crawl4ai not installed.
# ============================================================

from __future__ import annotations

import re
import json
import urllib.request
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# ── Crawl4AI (optional) ──────────────────────────────────────
# Probe-only at import time: find_spec locates the package without
# executing it. Actually importing crawl4ai costs ~2 s (async
# machinery + Playwright glue) and this module is imported at app
# startup — the real import is deferred to _scrape_url_crawl4ai.
import asyncio
import importlib.util as _ilu
_CRAWL4AI_OK = _ilu.find_spec("crawl4ai") is not None

import council_engine as ce


# ============================================================
# Data classes
# ============================================================

@dataclass
class ResearchResult:
    query: str
    urls_tried: List[str] = field(default_factory=list)
    content: str = ""          # markdown-cleaned scraped text
    success: bool = False
    error: str = ""


@dataclass
class InternDraft:
    task: str
    research: Optional[ResearchResult] = None
    draft: str = ""
    needed_research: bool = False
    event_log: List[str] = field(default_factory=list)

    def log(self, msg: str):
        self.event_log.append(msg)


# ============================================================
# Prompts
# ============================================================

NEEDS_RESEARCH_PROMPT = """\
You are deciding whether web research is needed to answer this task well.

TASK:
{task}

Answer with ONLY valid JSON — no other text, no markdown:
{{"needs_research": true/false, "reason": "one sentence", "query": "search query if needed or empty string"}}

Research is needed when:
- The task asks about current events, recent releases, or live data
- The task requires specific documentation or API details that may have changed
- The task asks "how to" for a specific library/tool/service

Research is NOT needed when:
- The task is a pure logic/algorithm problem
- The task uses only standard library Python
- The task is clearly self-contained with no external dependencies
"""

DRAFT_WITH_RESEARCH_PROMPT = """\
You are the INTERN — produce a quick, working first draft.

TASK:
{task}

WEB RESEARCH FINDINGS:
{research}

Using the research above as context, write a concise working draft.
- Keep it small and testable
- Include a usage example at the bottom
- Note your assumptions in comments
- Output code in a fenced Python block if code is needed
"""

DRAFT_NO_RESEARCH_PROMPT = """\
You are the INTERN — produce a quick, working first draft.

TASK:
{task}

Write a concise working draft.
- Keep it small and testable
- Include a usage example at the bottom
- Note your assumptions in comments
- Output code in a fenced Python block if code is needed
"""


# ============================================================
# Web search (DuckDuckGo — no API key needed)
# ============================================================

def _ddg_search(query: str, max_results: int = 5) -> List[str]:
    """
    Lightweight DuckDuckGo search using their HTML endpoint.
    Returns a list of URLs. No API key required.
    """
    encoded = urllib.parse.quote_plus(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        # Extract result URLs from DDG HTML
        urls = re.findall(r'href="(https?://[^"&]+)"', html)
        # Filter out DDG internal links
        filtered = [u for u in urls if "duckduckgo" not in u and "duck.com" not in u]
        # Deduplicate while preserving order
        seen: set = set()
        unique: List[str] = []
        for u in filtered:
            if u not in seen:
                seen.add(u)
                unique.append(u)
        return unique[:max_results]
    except Exception as e:
        return []


# ============================================================
# Web scraping with Crawl4AI
# ============================================================

def _scrape_url_crawl4ai(url: str, timeout: int = 15) -> Tuple[bool, str]:
    """Scrape a URL and return clean markdown using Crawl4AI."""
    if not _CRAWL4AI_OK:
        return False, "crawl4ai not installed"
    # Deferred heavy import — see the find_spec probe at module top.
    try:
        from crawl4ai import AsyncWebCrawler, CacheMode
    except Exception as exc:
        return False, f"crawl4ai import failed: {exc!r}"

    async def _crawl():
        async with AsyncWebCrawler(verbose=False) as crawler:
            result = await crawler.arun(
                url=url,
                cache_mode=CacheMode.BYPASS,
                word_count_threshold=50,
                exclude_external_links=True,
                remove_overlay_elements=True,
            )
            return result

    try:
        loop = None
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        result = loop.run_until_complete(_crawl())
        if result.success and result.markdown:
            # Trim to a reasonable size
            content = result.markdown[:8000]
            return True, content
        return False, result.error_message or "No content returned"
    except Exception as e:
        return False, str(e)


def _scrape_url_stdlib(url: str) -> Tuple[bool, str]:
    """
    Minimal HTML scraper using only stdlib.
    Strips tags, returns plain text. Used when crawl4ai is unavailable.
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; CouncilBot/1.0)"}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        # Strip scripts and styles
        html = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        # Strip tags
        text = re.sub(r"<[^>]+>", " ", html)
        # Clean whitespace
        text = re.sub(r"\s{3,}", "\n\n", text)
        text = text.strip()
        return True, text[:6000]
    except Exception as e:
        return False, str(e)


def _do_research(query: str, max_pages: int = 3) -> ResearchResult:
    """
    Search DuckDuckGo for query, scrape top results, return combined markdown.
    """
    result = ResearchResult(query=query)

    urls = _ddg_search(query, max_results=max_pages + 2)
    if not urls:
        result.error = "DuckDuckGo search returned no URLs"
        return result

    combined: List[str] = []
    for url in urls[:max_pages]:
        result.urls_tried.append(url)

        if _CRAWL4AI_OK:
            ok, content = _scrape_url_crawl4ai(url)
        else:
            ok, content = _scrape_url_stdlib(url)

        if ok and content.strip():
            combined.append(f"### Source: {url}\n\n{content.strip()}\n")
            if len("\n\n".join(combined)) > 12000:
                break

    if combined:
        result.content = "\n\n".join(combined)[:12000]
        result.success = True
    else:
        result.error = "Scraped pages returned no usable content"

    return result


# ============================================================
# Decision: does this task need research?
# ============================================================

def _needs_research(task: str, personality_model: Any) -> Tuple[bool, str]:
    """
    Ask the model whether web research is needed.
    Returns (needs_research: bool, query: str)

    Uses a lightweight direct Ollama call (no history/memory injection)
    to keep the prompt tiny and routing decisions fast.
    """
    prompt = NEEDS_RESEARCH_PROMPT.format(task=task)

    # Try lightweight direct call first — skips conversation history,
    # role memory, and council context to keep the prompt tiny.
    # Falls back to full PersonalityModel.respond() if direct call fails.
    raw = ""
    try:
        spec = (
            personality_model.registry.get(personality_model.backend_key)
            if personality_model.backend_key
            else personality_model.registry.best_for(
                weights=personality_model.weights,
                fallback_key="local_fast"
            )
        )
        raw = spec.generate(
            developer_instructions="You are a research routing assistant. Output only valid JSON.",
            user_text=prompt,
            temperature=0.0,
            max_tokens=80,   # only needs {"needs_research":true/false,"reason":"...","query":"..."}
            trace=False,
        )
    except Exception:
        # Fallback to full respond() if spec lookup fails
        try:
            raw = personality_model.respond(prompt)
        except Exception:
            return False, ""

    # Try to extract JSON
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return False, ""
    try:
        obj = json.loads(m.group(0))
        needs = bool(obj.get("needs_research", False))
        query = str(obj.get("query", "")).strip()
        return needs, query
    except Exception:
        return False, ""


# ============================================================
# Public API
# ============================================================

class InternAgent:
    """
    Research-augmented Intern.
    Decides autonomously whether to search the web before drafting.
    Falls back to single-shot if crawl4ai isn't installed.
    """

    def __init__(
        self,
        personality_model: Any,          # ce.PersonalityModel
        event_callback: Optional[Callable[[str, str], None]] = None,
        max_research_pages: int = 3,
    ):
        self.model = personality_model
        self.event_callback = event_callback
        self.max_research_pages = max_research_pages

        if _CRAWL4AI_OK:
            print("[InternAgent] crawl4ai available — web research enabled")
        else:
            print("[InternAgent] crawl4ai not installed — web research disabled (stdlib fallback)")

    def _emit(self, phase: str, msg: str):
        if self.event_callback:
            self.event_callback(phase, msg)

    def run(self, task: str, extra_context: str = "") -> InternDraft:
        """
        Run the Intern agent on a task.
        Returns InternDraft with .draft, .research, .needed_research
        """
        draft = InternDraft(task=task)

        # Step 1: decide if research is needed
        self._emit("research_decide", "Deciding if web research is needed…")
        needs, query = _needs_research(task, self.model)
        draft.needed_research = needs
        draft.log(f"Research needed: {needs}" + (f", query: {query}" if needs else ""))

        # Step 2: do research if needed
        research_context = ""
        if needs and query:
            self._emit("research_search", f"Searching: {query}")
            research = _do_research(query, max_pages=self.max_research_pages)
            draft.research = research

            if research.success:
                self._emit("research_done",
                           f"Scraped {len(research.urls_tried)} pages for: {query}")
                research_context = research.content
            else:
                self._emit("research_fail",
                           f"Research failed: {research.error}. Proceeding without.")

        # Step 3: draft
        self._emit("draft", "Writing draft…")
        if research_context:
            prompt = DRAFT_WITH_RESEARCH_PROMPT.format(
                task=task,
                research=research_context[:6000],
            )
        else:
            prompt = DRAFT_NO_RESEARCH_PROMPT.format(task=task)

        if extra_context:
            prompt = f"COUNCIL CONTEXT:\n{extra_context}\n\n{prompt}"

        draft.draft = self.model.respond(prompt)
        draft.log("Draft complete")
        self._emit("draft_done", "Draft complete")

        return draft
