"""
council_core.local_models — which models this PC can run, without loading one.

WHY THIS IS NOT IN council_engine
The Models tab and the Roles panel need the list on a worker within a fraction
of a second; importing council_engine to get it costs seconds (it is the whole
deliberation engine). So the listing lives here, stdlib only, and
council_engine.list_local_models() delegates to it — one answer for both.

TWO KINDS OF LOCAL MODEL
  * GGUF files in the model folders, loaded in-process by llama-cpp-python.
    id = the absolute path.
  * Models a localhost Ollama server already has (``ollama list``).
    id = "ollama:<name>", e.g. "ollama:llama3.1:8b". On this PC the council
    conda env has no llama-cpp-python at all, so these are the only models
    the Qt app can reach without an install (measured 2026-10-01).

ORIGIN IS A PRODUCT RULE
Recommended models must be US-origin (model_catalog's header says why). A
non-US model is LISTED — the user installed it and may want to measure it —
but marked, and never recommended or picked by default. Origin is read from
the model FAMILY (Ollama's details.family / the GGUF architecture) and the
name, non-US fragments first: a US repacker or a llama-architecture distill of
a DeepSeek model is still DeepSeek's.

NOTHING HERE STARTS A GENERATION
Only Ollama's metadata endpoints are called (/api/version, /api/tags,
/api/show), and only on a loopback host unless the caller explicitly allows a
remote one. /api/tags on Ollama 0.35 already carries the context length and
the capabilities ("tools", "thinking"), measured at 47 ms for six models;
/api/show costs 70-330 ms per model, so it is asked only when tags lack them,
and cached by digest.
"""
from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import struct
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

OLLAMA_PREFIX = "ollama:"
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
GB = 1024 ** 3


# ============================================================
# Ids
# ============================================================

def is_ollama_id(model_id: Any) -> bool:
    return isinstance(model_id, str) and model_id.strip().lower().startswith(
        OLLAMA_PREFIX)


def ollama_name(model_id: str) -> str:
    """'ollama:llama3.1:8b' -> 'llama3.1:8b' (and a bare name unchanged)."""
    s = (model_id or "").strip()
    return s[len(OLLAMA_PREFIX):].strip() if is_ollama_id(s) else s


def ollama_id(name: str) -> str:
    return OLLAMA_PREFIX + ollama_name(name)


# ============================================================
# Hosts — loopback only unless a caller explicitly allows otherwise
# ============================================================

def ollama_host() -> str:
    """COUNCIL_OLLAMA_HOST, else the loopback default. Read live, so a test
    pointing it at a fake server and a user moving the port both work."""
    raw = os.environ.get("COUNCIL_OLLAMA_HOST", "").strip()
    return (raw or DEFAULT_OLLAMA_HOST).rstrip("/")


_LOOPBACK = re.compile(
    r"^https?://(localhost|127\.\d{1,3}\.\d{1,3}\.\d{1,3}|\[::1\])(:\d+)?(/|$)",
    re.IGNORECASE)


def is_loopback_url(url: str) -> bool:
    """Is ``url``'s host this PC? The host is matched WHOLE — up to the port
    or the first "/" — so "localhost.evil.example", "127.0.0.1.evil.example"
    and "localhost@evil.example" (userinfo; the host is evil.example) are
    other machines. No DNS: a name that merely resolves to 127.0.0.1 is not
    trusted, since it can resolve elsewhere by the time the call connects.

    A dotted quad must also BE an address: "127.999.0.1" fits the pattern
    but no IP parser accepts it, so the OS would look it up as a host NAME
    — which a hosts file or a LAN's DNS can point anywhere."""
    m = _LOOPBACK.match((url or "").strip())
    if m is None:
        return False
    host = m.group(1)
    if host[0].isdigit():
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False
    return True


def require_local(url: str, *, allow_remote: bool = False) -> None:
    """The same guard as council_engine._ensure_localhost, for this module."""
    if not allow_remote and not is_loopback_url(url):
        raise RuntimeError(
            f"Refusing non-local model endpoint: {url}\n"
            "Only a localhost Ollama is used unless remote nodes are enabled.")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is an error, not a hop. Ollama never redirects, and whatever
    else answers on a loopback port could point the call at another
    machine — urllib would re-send the request there."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None          # -> the default handler raises HTTPError


#: The opener for every call to a model server: NO proxy, NO redirects.
#: urllib.request.urlopen asks for a proxy on every call — HTTP_PROXY, or on
#: Windows the system proxy, whose "bypass for local addresses" exempts
#: "localhost" but not "127.0.0.1" — and then opens the connection to the
#: PROXY and hands it the request. So a URL the loopback guard approved still
#: left the PC: measured 2026-10-05 with a recording proxy, it got the whole
#: /api/chat body from llm_bench and /api/version, /api/tags and /api/show
#: from here (and, unable to reach its own 127.0.0.1, answered 502, so the
#: app said "No Ollama server answers" with Ollama up). The chat stream in
#: council_engine is http.client, which never used a proxy.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                      _NoRedirect())


def open_direct(req: Any, timeout: float):
    """urllib.request.urlopen(req, timeout=timeout) without proxies or
    redirects — for this PC's Ollama and for nodes the user listed, which
    the chat itself (http.client) also reaches directly."""
    return _DIRECT.open(req, timeout=timeout)


# ============================================================
# Ollama metadata (never /api/chat or /api/generate here)
# ============================================================

_CACHE_LOCK = threading.Lock()
_TAGS_CACHE: Dict[str, Tuple[float, Optional[List[Dict[str, Any]]]]] = {}
_SHOW_CACHE: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
_REACH_CACHE: Dict[str, Tuple[float, bool]] = {}


def invalidate_cache() -> None:
    with _CACHE_LOCK:
        _TAGS_CACHE.clear()
        _SHOW_CACHE.clear()
        _REACH_CACHE.clear()


def forget_host(host: Optional[str] = None) -> None:
    """Drop what is cached about ``host``'s reachability and model list.

    For a one-off probe that must not leave a verdict behind: the startup
    readiness check (council_core.model_ready) runs before Ollama may be up,
    and the "unreachable" it cached was what the engine read for the next
    10 s — so an Ollama started just after the app was treated as absent
    (found in review)."""
    host = (host or ollama_host()).rstrip("/")
    with _CACHE_LOCK:
        _REACH_CACHE.pop(host, None)
        _TAGS_CACHE.pop(host, None)


def _get_json(url: str, timeout: float, body: Optional[dict] = None) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="GET" if body is None else "POST",
        headers={"Content-Type": "application/json"})
    with open_direct(req, timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def ollama_reachable(host: Optional[str] = None, *, timeout: float = 0.75,
                     max_age: float = 10.0, allow_remote: bool = False) -> bool:
    """Does an Ollama server answer /api/version? Cached ``max_age`` seconds:
    the engine asks before deciding a backend, and a refused connection on
    loopback costs a millisecond while a dead remote one costs the timeout."""
    host = (host or ollama_host()).rstrip("/")
    try:
        require_local(host, allow_remote=allow_remote)
    except RuntimeError:
        return False
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _REACH_CACHE.get(host)
        if hit and now - hit[0] < max_age:
            return hit[1]
    try:
        ok = bool(_get_json(host + "/api/version", timeout).get("version"))
    except Exception:                                     # noqa: BLE001
        ok = False
    with _CACHE_LOCK:
        _REACH_CACHE[host] = (now, ok)
    return ok


def ollama_tags(host: Optional[str] = None, *, timeout: float = 3.0,
                max_age: float = 5.0,
                allow_remote: bool = False) -> Optional[List[Dict[str, Any]]]:
    """The server's installed models (raw /api/tags entries), or None when no
    server answers. Cached briefly so the Roles panel and the engine asking in
    the same second cost one request."""
    host = (host or ollama_host()).rstrip("/")
    require_local(host, allow_remote=allow_remote)
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _TAGS_CACHE.get(host)
        if hit and now - hit[0] < max_age:
            return hit[1]
    try:
        models = list(_get_json(host + "/api/tags", timeout).get("models")
                      or [])
    except Exception:                                     # noqa: BLE001
        models = None
    with _CACHE_LOCK:
        _TAGS_CACHE[host] = (now, models)
        _REACH_CACHE[host] = (now, models is not None)
    return models


def ollama_show(name: str, host: Optional[str] = None, *, digest: str = "",
                timeout: float = 5.0, allow_remote: bool = False
                ) -> Dict[str, Any]:
    """/api/show for one model ({} when it fails). Cached per digest: a
    model's metadata changes only when it is re-pulled, which changes the
    digest."""
    host = (host or ollama_host()).rstrip("/")
    require_local(host, allow_remote=allow_remote)
    key = (host, name, digest)
    with _CACHE_LOCK:
        if key in _SHOW_CACHE:
            return _SHOW_CACHE[key]
    try:
        data = _get_json(host + "/api/show", timeout, {"model": name}) or {}
    except Exception:                                     # noqa: BLE001
        return {}
    with _CACHE_LOCK:
        _SHOW_CACHE[key] = data
    return data


def _context_from_model_info(info: Dict[str, Any]) -> Optional[int]:
    for k, v in (info or {}).items():
        if (isinstance(k, str) and k.endswith(".context_length")
                and "original" not in k and isinstance(v, int) and v > 0):
            return v
    return None


def ollama_model(name: str, host: Optional[str] = None, *,
                 allow_remote: bool = False, fill_from_show: bool = True,
                 max_age: float = 5.0) -> Optional[Dict[str, Any]]:
    """One installed model as a list_local_models() entry, or None when the
    server does not have it (or does not answer). The engine asks with a
    longer ``max_age``: it needs the context length and capabilities, which
    change only when a model is re-pulled."""
    want = ollama_name(name)
    tags = ollama_tags(host, allow_remote=allow_remote, max_age=max_age)
    if not tags:
        return None
    for raw in tags:
        if _same_ollama_name(raw.get("name") or raw.get("model") or "", want):
            return ollama_entry(raw, host=host, allow_remote=allow_remote,
                                fill_from_show=fill_from_show)
    return None


def _same_ollama_name(a: str, b: str) -> bool:
    """'phi3.5' and 'phi3.5:latest' are the same model."""
    def norm(s: str) -> str:
        s = s.strip().lower()
        return s if ":" in s else s + ":latest"
    return norm(a) == norm(b)


# ============================================================
# Origin
# ============================================================

#: (fragment, maker) checked FIRST — a non-US fragment anywhere in the name or
#: family wins over a US-looking architecture name ("deepseek-r1:8b" is a
#: llama-architecture distill; it is still DeepSeek's).
NON_US_MAKERS: Tuple[Tuple[str, str], ...] = (
    ("qwen", "Alibaba"), ("qwq", "Alibaba"), ("deepseek", "DeepSeek"),
    ("mixtral", "Mistral AI (France)"), ("mistral", "Mistral AI (France)"),
    ("codestral", "Mistral AI (France)"), ("ministral", "Mistral AI (France)"),
    ("yi", "01.AI"), ("internlm", "Shanghai AI Lab"), ("glm", "Zhipu"),
    ("baichuan", "Baichuan"), ("falcon", "TII (UAE)"),
    ("command-r", "Cohere (Canada)"), ("aya", "Cohere (Canada)"),
    ("minimax", "MiniMax"), ("kimi", "Moonshot"), ("exaone", "LG (Korea)"),
    # Llama-ARCHITECTURE models from outside the US: Ollama reports their
    # family as "llama", which alone reads as Meta / US. Measured in review:
    # "yi:6b", "solar", "openchat", "tinyllama", "llama-pro" all came out
    # ('Meta', 'US') — recommendable — before these entries.
    ("solar", "Upstage (Korea)"), ("openchat", "OpenChat (Tsinghua, China)"),
    ("tinyllama", "TinyLlama (SUTD, Singapore)"),
    ("llama-pro", "Tencent ARC (China)"), ("hunyuan", "Tencent (China)"),
    ("ernie", "Baidu (China)"), ("minicpm", "OpenBMB (China)"),
    ("jais", "G42 (UAE)"),
)
#: Fragments matched as a whole word, not a substring: "yi" is 01.AI's
#: ("yi:6b", "yi-coder:9b") but also the inside of unrelated names.
_WORD_FRAGMENTS = frozenset({"yi"})
US_MAKERS: Tuple[Tuple[str, str], ...] = (
    ("gpt-oss", "OpenAI"), ("gptoss", "OpenAI"),
    ("phi", "Microsoft"), ("llama", "Meta"), ("gemma", "Google"),
    ("granite", "IBM"), ("olmo", "AllenAI"), ("nemotron", "NVIDIA"),
    ("dolly", "Databricks"), ("dbrx", "Databricks"),
)


def maker_and_origin(name: str, family: str = "",
                     families: Iterable[str] = ()) -> Tuple[str, str]:
    """(maker, origin) with origin 'US' | 'non-US' | 'unknown'."""
    hay = " ".join([name or "", family or ""] + list(families or ())).lower()
    for frag, maker in NON_US_MAKERS:
        if frag in _WORD_FRAGMENTS:
            if re.search(rf"(?<![a-z0-9]){re.escape(frag)}(?![a-z])", hay):
                return maker, "non-US"
        elif frag in hay:
            return maker, "non-US"
    for frag, maker in US_MAKERS:
        if frag in hay:
            return maker, "US"
    return "unknown", "unknown"


def origin_label(origin: str) -> str:
    """What the Roles panel prints next to a model."""
    return {"US": "US", "non-US": "not US — measure only"}.get(
        origin, "origin unknown")


# ============================================================
# Sizes and parameter counts
# ============================================================

def parse_params_b(text: Any) -> Optional[float]:
    """'8.0B' -> 8.0, '137M' -> 0.137, 'llama3.1:8b' -> 8.0, '8x7B' -> 56."""
    s = str(text or "").lower()
    m = re.search(r"(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*b\b", s)
    if m:
        return float(m.group(1)) * float(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s*b\b", s)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*m\b", s)
    if m:
        return round(float(m.group(1)) / 1000.0, 3)
    return None


_QUANT_RE = re.compile(
    r"(IQ\d_[A-Z]+|Q\d_K_[SML]|Q\d_K|Q\d_\d|Q\d|BF16|F16|F32|MXFP4)",
    re.IGNORECASE)

#: llama.cpp's general.file_type enum (the common ones).
GGUF_FILE_TYPES = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0",
    9: "Q5_1", 10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L",
    14: "Q4_K_S", 15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K",
    32: "BF16", 38: "MXFP4",
}


def quant_from_name(name: str) -> str:
    m = _QUANT_RE.search(name or "")
    return m.group(1).upper() if m else ""


# ============================================================
# GGUF header reading (metadata + tensor sizes, never the weights)
# ============================================================

_GGUF_SCALARS = {
    0: (1, "<B"), 1: (1, "<b"), 2: (2, "<H"), 3: (2, "<h"), 4: (4, "<I"),
    5: (4, "<i"), 6: (4, "<f"), 7: (1, "<?"), 10: (8, "<Q"), 11: (8, "<q"),
    12: (8, "<d"),
}


def _read_value(fh, val_type: int) -> Any:
    """One GGUF metadata value. Arrays are SKIPPED (returned as []): the
    vocabulary arrays are hundreds of thousands of strings and nothing here
    needs them."""
    if val_type == 8:
        (n,) = struct.unpack("<Q", fh.read(8))
        return fh.read(n).decode("utf-8", errors="replace")
    if val_type == 9:
        (elem_t,) = struct.unpack("<I", fh.read(4))
        (count,) = struct.unpack("<Q", fh.read(8))
        if elem_t in _GGUF_SCALARS:
            fh.seek(_GGUF_SCALARS[elem_t][0] * count, 1)
            return []
        if elem_t == 8:
            # Variable-length strings must be walked one length at a time.
            # Measured on the installed Ollama blobs: 14 ms (Phi-3.5, 32K
            # vocab) to 158 ms (Llama 3.1, 128K) for the whole header.
            for _ in range(count):
                (n,) = struct.unpack("<Q", fh.read(8))
                fh.seek(n, 1)
            return []
        raise ValueError(f"unsupported GGUF array type {elem_t}")
    if val_type in _GGUF_SCALARS:
        size, fmt = _GGUF_SCALARS[val_type]
        (v,) = struct.unpack(fmt, fh.read(size))
        return v
    raise ValueError(f"unsupported GGUF value type {val_type}")


def read_gguf(path: Any, *, max_kv: int = 4096,
              tensors: bool = False) -> Dict[str, Any]:
    """{"metadata": {...}, "tensors": [(name, bytes)], "file_size": n}.

    Tensor byte sizes are the gaps between consecutive data offsets (the last
    runs to the end of the file), so no table of quantisation block sizes is
    needed and every quant type — including ones newer than this code — is
    sized correctly. Returns {} for anything that is not a v2/v3 GGUF.
    """
    out: Dict[str, Any] = {}
    try:
        p = Path(str(path))
        size = p.stat().st_size
        with open(p, "rb") as fh:
            if fh.read(4) != b"GGUF":
                return {}
            (version,) = struct.unpack("<I", fh.read(4))
            if version not in (2, 3):
                return {}
            (n_tensors,) = struct.unpack("<Q", fh.read(8))
            (n_kv,) = struct.unpack("<Q", fh.read(8))
            if n_kv > max_kv:
                return {}
            meta: Dict[str, Any] = {}
            for _ in range(n_kv):
                (klen,) = struct.unpack("<Q", fh.read(8))
                key = fh.read(klen).decode("utf-8", errors="replace")
                (vtype,) = struct.unpack("<I", fh.read(4))
                meta[key] = _read_value(fh, vtype)
            out = {"metadata": meta, "file_size": size, "tensors": []}
            if not tensors or n_tensors > 100_000:
                return out
            infos = []
            for _ in range(n_tensors):
                (nlen,) = struct.unpack("<Q", fh.read(8))
                name = fh.read(nlen).decode("utf-8", errors="replace")
                (ndim,) = struct.unpack("<I", fh.read(4))
                fh.seek(8 * ndim, 1)
                fh.read(4)                                # ggml type
                (offset,) = struct.unpack("<Q", fh.read(8))
                infos.append((offset, name))
            align = meta.get("general.alignment")
            align = align if isinstance(align, int) and align > 0 else 32
            data_start = fh.tell()
            data_start += (-data_start) % align
            infos.sort()
            sized = []
            for i, (offset, name) in enumerate(infos):
                end = (infos[i + 1][0] if i + 1 < len(infos)
                       else size - data_start)
                sized.append((name, max(0, end - offset)))
            out["tensors"] = sized
            out["data_offset"] = data_start
    except Exception:                                     # noqa: BLE001
        return out if out.get("metadata") else {}
    return out


def gguf_arch(meta: Dict[str, Any]) -> str:
    a = meta.get("general.architecture")
    return str(a).strip() if isinstance(a, str) else ""


def gguf_int(meta: Dict[str, Any], *suffixes: str) -> Optional[int]:
    arch = gguf_arch(meta)
    for suf in suffixes:
        v = meta.get(f"{arch}.{suf}") if arch else None
        if isinstance(v, int) and v > 0:
            return int(v)
    for key, v in meta.items():
        for suf in suffixes:
            if (isinstance(key, str) and key.endswith("." + suf)
                    and isinstance(v, int) and v > 0):
                return int(v)
    return None


def layer_bytes(layout: Dict[str, Any]) -> Tuple[int, int, int]:
    """(n_layers, bytes of the largest repeating block, bytes outside the
    blocks) from read_gguf(..., tensors=True). The largest block, not the
    mean: partial offload must not overrun on the heaviest layer. Zeros when
    the layout has no tensors."""
    meta = layout.get("metadata") or {}
    n_layers = gguf_int(meta, "block_count") or 0
    blocks: Dict[int, int] = {}
    other = 0
    for name, nbytes in layout.get("tensors") or ():
        m = re.match(r"blk\.(\d+)\.", name)
        if m:
            blocks[int(m.group(1))] = blocks.get(int(m.group(1)), 0) + nbytes
        else:
            other += nbytes
    if not blocks:
        return n_layers, 0, 0
    return max(n_layers, len(blocks)), max(blocks.values()), other


# ============================================================
# Entries
# ============================================================

_GGUF_ENTRY_CACHE: Dict[Tuple[str, int, int], Dict[str, Any]] = {}


def gguf_entry(path: Any) -> Optional[Dict[str, Any]]:
    """A GGUF file as a list_local_models() entry (None when unreadable).
    Cached by (path, size, mtime): the header walk is ~0.1-0.3 s per file."""
    p = Path(str(path))
    try:
        st = p.stat()
    except OSError:
        return None
    key = (str(p.resolve()).lower(), int(st.st_size), int(st.st_mtime))
    with _CACHE_LOCK:
        if key in _GGUF_ENTRY_CACHE:
            return dict(_GGUF_ENTRY_CACHE[key])
    meta = (read_gguf(p) or {}).get("metadata") or {}
    arch = gguf_arch(meta)
    gname = str(meta.get("general.name") or "").strip()
    label = gname or p.stem
    params = None
    count = meta.get("general.parameter_count")
    if isinstance(count, int) and count > 0:
        params = round(count / 1e9, 1)
    if params is None:
        params = (parse_params_b(meta.get("general.size_label") or "")
                  or parse_params_b(p.stem))
    ft = meta.get("general.file_type")
    quant = (GGUF_FILE_TYPES.get(ft, "") if isinstance(ft, int) else "") \
        or quant_from_name(p.name)
    maker, origin = maker_and_origin(f"{p.stem} {gname}", arch)
    ctx = gguf_int(meta, "context_length")
    entry = {
        "id": str(p.resolve()), "name": label, "backend": "gguf",
        "size_bytes": int(st.st_size), "params_b": params, "quant": quant,
        "family": arch or "unknown", "maker": maker, "origin": origin,
        "context_length": ctx, "capabilities": ["completion"],
        "path": str(p.resolve()), "file": p.name,
    }
    with _CACHE_LOCK:
        _GGUF_ENTRY_CACHE[key] = dict(entry)
    return entry


def ollama_entry(raw: Dict[str, Any], *, host: Optional[str] = None,
                 allow_remote: bool = False,
                 fill_from_show: bool = True) -> Dict[str, Any]:
    """An /api/tags entry as a list_local_models() entry."""
    name = str(raw.get("name") or raw.get("model") or "")
    det = raw.get("details") or {}
    family = str(det.get("family") or "")
    families = [str(f) for f in (det.get("families") or [])]
    ctx = det.get("context_length")
    caps = raw.get("capabilities")
    params = parse_params_b(det.get("parameter_size") or "")
    if fill_from_show and (not isinstance(ctx, int) or caps is None):
        # Older servers do not put these in /api/tags.
        show = ollama_show(name, host, digest=str(raw.get("digest") or ""),
                           allow_remote=allow_remote)
        info = show.get("model_info") or {}
        if not isinstance(ctx, int):
            ctx = _context_from_model_info(info)
        if caps is None:
            caps = show.get("capabilities")
        count = info.get("general.parameter_count")
        if params is None and isinstance(count, int):
            params = round(count / 1e9, 1)
    if params is None:
        params = parse_params_b(name)
    maker, origin = maker_and_origin(name, family, families)
    caps = list(caps) if isinstance(caps, list) else ["completion"]
    return {
        "id": ollama_id(name), "name": name, "backend": "ollama",
        "size_bytes": int(raw.get("size") or 0), "params_b": params,
        "quant": str(det.get("quantization_level") or ""),
        "family": family or "unknown", "maker": maker, "origin": origin,
        "context_length": ctx if isinstance(ctx, int) else None,
        "capabilities": caps, "digest": str(raw.get("digest") or ""),
    }


def list_local_models(*, dirs: Optional[Sequence[Path]] = None,
                      also: Iterable[str] = (), host: Optional[str] = None,
                      include_ollama: bool = True, include_gguf: bool = True,
                      allow_remote: bool = False) -> List[Dict[str, Any]]:
    """Every model this PC can run: GGUF files, then Ollama models.

    Never raises and never downloads. An Ollama server that does not answer
    contributes nothing; a GGUF that cannot be read is skipped.
    """
    out: List[Dict[str, Any]] = []
    if include_gguf:
        try:
            from . import model_slots
            files = model_slots.known_files(dirs, also=[a for a in also
                                                        if a and not
                                                        is_ollama_id(a)])
        except Exception:                                 # noqa: BLE001
            files = []
        for f in files:
            e = gguf_entry(f)
            if e:
                out.append(e)
    if include_ollama:
        try:
            tags = ollama_tags(host, allow_remote=allow_remote) or []
        except Exception:                                 # noqa: BLE001
            tags = []
        for raw in tags:
            try:
                e = ollama_entry(raw, host=host, allow_remote=allow_remote)
            except Exception:                             # noqa: BLE001
                continue
            if "embedding" in e["capabilities"] and \
                    "completion" not in e["capabilities"]:
                continue                       # an embedder cannot chat
            out.append(e)
    return out


def describe(entry: Dict[str, Any]) -> str:
    """'llama3.1:8b — Meta · US · Ollama · 4.6 GB' for a combo box."""
    size = entry.get("size_bytes") or 0
    bits = [entry.get("maker") or "unknown",
            origin_label(entry.get("origin") or "unknown"),
            "Ollama" if entry.get("backend") == "ollama" else "GGUF"]
    if size:
        bits.append(f"{size / GB:.1f} GB")
    return f"{entry.get('name') or entry.get('id')} — " + " · ".join(bits)


# ============================================================
# Which installed model for which role
# ============================================================

#: Roles that want a model that can call tools natively (the docs role pings
#: a documentation server through tools).
TOOL_ROLES = ("docs",)
#: Roles whose output is code or structured JSON.
CODE_ROLES = ("coder", "docs")

#: VRAM kept free beyond the weights for the context (≈1 GB at 8K on an 8B
#: model) and the CUDA/desktop overhead — the catalog's own margin.
FIT_MARGIN_GB = 1.5


def is_moe(entry: Dict[str, Any]) -> bool:
    fam = f"{entry.get('family', '')} {entry.get('name', '')}".lower()
    return any(f in fam for f in ("gptoss", "gpt-oss", "mixtral", "moe",
                                  "qwen3moe", "deepseek2", "olmoe"))


def effective_params_b(entry: Dict[str, Any]) -> float:
    """Dense-equivalent size for ranking. An MoE's quality sits between its
    active and total parameter counts — the geometric mean is the usual rule
    of thumb — so gpt-oss-20b (3.6B active of 20.9B, from model_catalog) is
    ranked like an ~8.7B dense model, not a 20.9B one. An MoE the catalog
    does not know is assumed to run a fifth of its weights per token."""
    total = float(entry.get("params_b") or 0.0)
    if not total or not is_moe(entry):
        return total
    active = None
    try:
        import model_catalog
        name = ollama_name(str(entry.get("id") or "")).lower()
        for spec in model_catalog.MODELS:
            if spec.active_params_b and spec.ollama and (
                    _same_ollama_name(spec.ollama, name)
                    or spec.hf_file.lower() == str(entry.get("file") or
                                                   "").lower()):
                active = spec.active_params_b
                break
    except Exception:                                     # noqa: BLE001
        active = None
    active = active or total * 0.2
    return round(math.sqrt(total * active), 1)


def fit(entry: Dict[str, Any], vram_gb: Optional[float],
        ram_gb: Optional[float]) -> str:
    """'gpu' (weights + margin fit the card), 'partial' (some layers on the
    card, the rest in RAM), 'cpu' (no usable GPU) or 'too big'."""
    size_gb = (entry.get("size_bytes") or 0) / GB
    if not size_gb:
        return "unknown"
    if vram_gb and size_gb + FIT_MARGIN_GB <= vram_gb:
        return "gpu"
    ram_ok = (not ram_gb) or size_gb <= ram_gb * 0.6
    if vram_gb and ram_ok:
        return "partial"
    return "cpu" if ram_ok else "too big"


def rank_for_role(models: Sequence[Dict[str, Any]], role: str, *,
                  vram_gb: Optional[float] = None,
                  ram_gb: Optional[float] = None) -> List[Dict[str, Any]]:
    """US-origin models ordered best-first for ``role``, each with "fit",
    "score" and a one-line "why". Non-US and unknown-origin models are left
    out: they may be measured, never recommended.

    STATIC: a full-GPU fit beats a bigger model that must spill to RAM,
    because a dense model with layers on the CPU decodes several times slower
    (an MoE spills far more cheaply — only its active experts run). A measured
    benchmark, when the bench branch lands, should replace this ordering.
    """
    ranked = []
    for e in models:
        if e.get("origin") != "US":
            continue
        caps = e.get("capabilities") or []
        if "completion" not in caps:
            continue
        where = fit(e, vram_gb, ram_gb)
        if where == "too big":
            continue
        base = {"gpu": 3.0, "partial": 2.5 if is_moe(e) else 1.0,
                "cpu": 0.5, "unknown": 1.0}[where]
        params = effective_params_b(e)
        score = base + min(params, 16.0) / 8.0
        why = [{"gpu": "fits the GPU", "partial": "part GPU, part RAM",
                "cpu": "runs on the CPU", "unknown": "size unknown"}[where]]
        if role in TOOL_ROLES and "tools" in caps:
            score += 0.75
            why.append("native tool calling")
        ctx = e.get("context_length") or 0
        if role in CODE_ROLES and ctx and ctx < 8192:
            score -= 1.0
            why.append(f"short context ({ctx})")
        ranked.append(dict(e, fit=where, score=round(score, 2),
                           why=", ".join(why)))
    ranked.sort(key=lambda e: (-e["score"], e.get("size_bytes") or 0))
    return ranked


def recommend_for_role(models: Sequence[Dict[str, Any]], role: str, *,
                       vram_gb: Optional[float] = None,
                       ram_gb: Optional[float] = None
                       ) -> Optional[Dict[str, Any]]:
    ranked = rank_for_role(models, role, vram_gb=vram_gb, ram_gb=ram_gb)
    return ranked[0] if ranked else None
