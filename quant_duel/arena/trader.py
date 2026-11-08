"""One model's morning: the packet, its data requests, its decision.

The model talks JSON through Ollama's own chat API (``format: json``), so
every family works the same way whether or not it has native tool calling.
Each turn it either asks for data or decides:

    {"action": "request", "requests": [{"tool": "history", ...}, ...]}
    {"action": "trade", "holdings": {"NVDA": 20, "KO": 10}, "reason": "..."}
    {"action": "hold", "reason": "..."}

Requests are capped (``max_requests`` per morning); after the cap, or on
the last turn, the model must decide. Anything unusable becomes an
``error`` order, which keeps the book as it is.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..llm.client import LLMError, extract_json
from .market import TOOLS

RULES = """You are {name}, a contestant in a paper-trading duel against
other AI models. Everyone starts with $10,000 of pretend money; whoever has
the most money at the close on {end} wins. Standings are posted every Sunday.

Rules:
- You may hold only these tickers: {tickers}.
- Name the holdings you want as PERCENTS of your total money (cash +
  holdings). Whatever is left over stays in cash. No borrowing, no
  shorting: percents are >= 0 and add up to at most 100.
- Your order fills at today's market open. Every dollar you trade costs
  {cost_bps} basis points ({cost_pct}%), so trading a lot costs money.
- The holdings you name REPLACE your current ones. "hold" keeps them as is.

Each turn reply with ONE JSON object and nothing else, either:
  {{"action": "request", "requests": [ ... ]}}  to ask for more data, or
  {{"action": "trade", "holdings": {{"TICKER": percent, ...}},
    "reason": "one or two sentences"}}  or
  {{"action": "hold", "reason": "one or two sentences"}}
You may make up to {max_requests} data requests this morning in total.
Data you can request:
{tools}"""

PACKET = """Morning of {today}. Prices below are from the close on {close_day}.
Your order fills at the open on {fill_day}. {days_left} trading days remain
in the duel (it ends at the close on {end}).

YOUR BOOK: ${equity:,.2f} total ({total_pct:+.2f}% since start), cash
${cash:,.2f}.
Current holdings (percent of your money at the last close):
{holdings}

LEADERBOARD (last close):
{board}

MARKET at the close on {close_day} (% values are percents; vsMA = distance
from the moving average; volat20 = annualised 20-day volatility):
{table}

HEADLINES since the previous close:
{news}

Decide now, or request data first."""


def ollama_chat(url: str, model: str, messages: List[Dict[str, str]], *,
                temperature: float, num_ctx: int, max_tokens: int,
                timeout_s: float, keep_alive: Any = "5m",
                json_format: bool = True, think: Any = None) -> str:
    """One chat turn. A reasoning model (gpt-oss) returns an empty reply
    under ``format: json``, so it runs with ``json_format=False`` and a
    ``think`` level instead; its JSON is then pulled out of the text."""
    body: Dict[str, Any] = {
        "model": model, "messages": messages, "stream": False,
        "keep_alive": keep_alive,
        "options": {"temperature": temperature, "num_ctx": num_ctx,
                    "num_predict": max_tokens}}
    if json_format:
        body["format"] = "json"
    if think is not None:
        body["think"] = think
    req = urllib.request.Request(
        url.rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    last: Exception = LLMError("no attempt")
    for attempt in range(3):
        if attempt:
            time.sleep(3 * attempt)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                reply = json.loads(r.read().decode())
            return reply["message"]["content"]
        except urllib.error.HTTPError as exc:
            last = exc
            if 400 <= exc.code < 500:
                break
        except (urllib.error.URLError, TimeoutError, OSError, KeyError,
                ValueError) as exc:
            last = exc
    raise LLMError(f"Ollama call failed for {model}: {last}")


def unload(url: str, model: str) -> None:
    """Free the GPU for the next contestant."""
    try:
        ollama_chat(url, model, [], temperature=0, num_ctx=512, max_tokens=1,
                    timeout_s=60, keep_alive=0)
    except LLMError:
        pass


Chat = Callable[[List[Dict[str, str]]], str]
Answer = Callable[[Dict[str, Any]], str]


def morning(chat: Chat, system: str, packet: str, answer: Answer, *,
            max_requests: int, max_rounds: int
            ) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """Run the conversation; returns (decision, transcript)."""
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": packet}]
    used, log = 0, []
    for turn in range(max_rounds):
        last_turn = turn == max_rounds - 1 or used >= max_requests
        raw = None
        try:
            raw = chat(messages)
            obj = extract_json(raw)
        except LLMError as exc:
            if raw is not None and turn < max_rounds - 1:
                messages += [{"role": "assistant", "content": raw[:2000]},
                             {"role": "user", "content":
                              f"That was not usable ({exc}). Reply with ONE "
                              "JSON object only."}]
                continue
            return {"action": "error", "reason": str(exc)[:300],
                    "requests": log}, messages
        messages.append({"role": "assistant", "content": raw[:4000]})
        action = str(obj.get("action", "")).lower()
        if action in ("trade", "hold"):
            obj["action"] = action
            obj["requests"] = log
            return obj, messages
        if action == "request" and not last_turn:
            reqs = obj.get("requests") or [obj]
            if isinstance(reqs, dict):
                reqs = [reqs]
            replies = []
            if not isinstance(reqs, list):
                reqs = [reqs]
            for r in reqs:
                if used >= max_requests:
                    replies.append("(request limit reached)")
                    break
                used += 1
                if not isinstance(r, dict):
                    text = (f"Not understood: {json.dumps(r)[:120]}. Each "
                            "request is an object such as "
                            '{"tool": "history", "ticker": "NVDA", '
                            '"days": 30}.')
                else:
                    text = answer(r)
                log.append({"request": r, "chars": len(text)})
                replies.append(text)
            left = max_requests - used
            tail = ("You have no requests left: decide now (trade or hold)."
                    if left <= 0 else f"{left} requests left.")
            messages.append({"role": "user",
                             "content": "\n\n".join(replies) + "\n\n" + tail})
            continue
        if last_turn:
            nudge = "Decide now: reply with a trade or hold JSON object only."
        else:
            nudge = ('Not understood: "action" must be "request", "trade" '
                     'or "hold". To ask for data reply e.g. {"action": '
                     '"request", "requests": [{"tool": "history", "ticker": '
                     '"NVDA", "days": 30}]}. Otherwise trade or hold.')
        messages.append({"role": "user", "content": nudge})
    return {"action": "error", "reason": "no decision after "
            f"{max_rounds} turns", "requests": log}, messages


def system_prompt(name: str, cfg: Dict[str, Any], tickers,
                  max_requests: Optional[int] = None) -> str:
    return RULES.format(
        name=name, end=cfg["end"], tickers=", ".join(tickers),
        cost_bps=cfg["cost_bps"], cost_pct=cfg["cost_bps"] / 100,
        max_requests=cfg["max_requests"] if max_requests is None
        else max_requests,
        tools="\n".join(f"- {k}: {v}" for k, v in TOOLS.items()))


__all__ = ["ollama_chat", "unload", "morning", "system_prompt", "PACKET",
           "RULES"]
