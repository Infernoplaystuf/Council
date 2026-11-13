"""
council_core.ledger_spread — the Ledger's model questions answered on several
machines at once (N2 of "several PCs as Council machines", Andy 2026-11-13).

WHICH MACHINES
This PC (the model the user picked for the Ledger, one question at a time)
and every machine in Machines & roles that takes shared work
(node_routing.sharing_nodes: enabled, routing on, a "Model for shared work"),
each asked with ITS OWN model, up to its "Calls at once". Setting a machine
up that way is the opt-in; with none, nothing here is used and the Ledger
asks this PC alone, exactly as before. COUNCIL_LEDGER_SPREAD=0 turns it off.

WHAT IS SPREAD
  * the part question of each part-less workbook (PartAsker, used by
    source_extract._extract_all's _Pipeline: the next workbooks are read
    while machines answer, and each workbook's values still go into the run
    in file order);
  * the header-layout questions of a table template (ledger_records'
    _ask_model, every layout at once).
The one question about left-over labels stays a single question.

A MACHINE THAT FAILS
is not asked again in this run, and the question goes to another machine
that has not tried it, so one machine down costs one try, not the run. Only
when every machine failed does the question fail - and then
ledger_llm.one_shot does what it always did.

ONE THREAD AT A TIME OUTSIDE THE MODEL
The questions' own code (find_part, one_shot, the AssistReport they count
into) was written for one thread. Each question runs holding the BATON and
lets go of it only while a machine is thinking (Spread.__call__), so
counting and problem lines never run side by side; the waiting does.
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

Chat = Callable[[str, Dict[str, Any]], Tuple[str, str]]
THIS_PC = "this PC"


@dataclass
class Machine:
    name: str
    chat: Chat
    slots: int = 1
    busy: int = 0
    failed: str = ""


class AllFailed(RuntimeError):
    pass


def enabled() -> bool:
    return os.environ.get("COUNCIL_LEDGER_SPREAD", "").strip().lower() not in (
        "0", "false", "no", "off")


def node_chat(name: str, url: str, model: str, timeout: float,
              should_stop: Optional[Callable[[], bool]] = None) -> Chat:
    """A Chat on another machine's Ollama (through council_engine.local_chat,
    whose localhost guard lets the call through because the machine is a
    registered, enabled node with routing on)."""
    from council_core import ledger_llm

    def chat(prompt: str, schema: Dict[str, Any]) -> Tuple[str, str]:
        import council_engine as ce
        window = ledger_llm.ledger_window(ledger_llm._ollama_context(model, url), prompt)
        with ce.model_window(window):
            text = ce.local_chat([{"role": "user", "content": prompt}],
                                 temperature=0.0, num_predict=ledger_llm.fill_reply(),
                                 json_schema=schema, timeout=int(max(5, timeout)),
                                 model=f"ollama:{model}", host=url,
                                 should_stop=should_stop)
        if not (text or "").strip():
            raise RuntimeError(f"{name} gave an empty answer")
        return text, f"{model} on {name}"
    return chat


def other_machines(routing=None) -> List[Tuple[str, str, str, int]]:
    """(name, url, model, calls at once) of the machines that take shared
    work, US-origin models only (node_routing.check_model holds that too)."""
    from council_core import node_routing as nr
    try:
        r = routing if routing is not None else nr.load(_vault())
    except Exception:                                      # noqa: BLE001
        return []
    return [(n.name, n.url, n.model, n.parallel) for n in nr.sharing_nodes(r)
            if not nr.check_model(n.model)]


def _vault():
    from council_core import paths
    return paths.vault_dir()


class Spread:
    """A Chat that asks whichever machine has a free call, the most free
    first, this PC first among equals."""

    def __init__(self, machines: Sequence[Machine],
                 should_stop: Optional[Callable[[], bool]] = None):
        self.machines = list(machines)
        self.should_stop = should_stop or (lambda: False)
        self.baton = threading.Lock()
        self._cv = threading.Condition()
        self._held = threading.local()
        self.used: Dict[str, int] = {}

    @property
    def capacity(self) -> int:
        return max(1, sum(m.slots for m in self.machines if not m.failed))

    def names(self) -> List[str]:
        return [m.name for m in self.machines]

    # -- choosing a machine -------------------------------------------------
    def _take(self, tried: set) -> Optional[Machine]:
        with self._cv:
            while True:
                live = [m for m in self.machines if not m.failed and m.name not in tried]
                if not live:
                    return None
                free = [m for m in live if m.busy < m.slots]
                if free:
                    best = max(free, key=lambda m: (m.slots - m.busy,
                                                    -self.machines.index(m)))
                    best.busy += 1
                    return best
                if self.should_stop():
                    return None
                self._cv.wait(0.25)

    def _give_back(self, m: Machine, failed: str = "") -> None:
        with self._cv:
            m.busy -= 1
            if failed:
                m.failed = failed
            self._cv.notify_all()

    # -- a question ---------------------------------------------------------
    def __call__(self, prompt: str, schema: Dict[str, Any]) -> Tuple[str, str]:
        held = getattr(self._held, "on", False)
        if held:                                   # let the others count meanwhile
            self._held.on = False
            self.baton.release()
        try:
            return self._ask(prompt, schema)
        finally:
            if held:
                self.baton.acquire()
                self._held.on = True

    def _ask(self, prompt: str, schema: Dict[str, Any]) -> Tuple[str, str]:
        tried: set = set()
        last: Optional[BaseException] = None
        while True:
            m = self._take(tried)
            if m is None:
                if last is not None:
                    raise last
                raise AllFailed("no machine could answer: " + "; ".join(
                    f"{x.name}: {x.failed}" for x in self.machines if x.failed))
            tried.add(m.name)
            try:
                out = m.chat(prompt, schema)
            except BaseException as exc:            # noqa: BLE001
                if self.should_stop():
                    self._give_back(m)
                    raise
                self._give_back(m, failed=str(exc).strip() or type(exc).__name__)
                last = exc
                continue
            self._give_back(m)
            with self._cv:
                self.used[m.name] = self.used.get(m.name, 0) + 1
            return out

    # -- running questions side by side -------------------------------------
    def run(self, fn: Callable[[], Any]) -> Any:
        """``fn`` holding the baton (see the module docstring)."""
        with self.baton:
            self._held.on = True
            try:
                return fn()
            finally:
                self._held.on = False

    def map(self, fns: Sequence[Callable[[], Any]]) -> List[Tuple[Any, Optional[BaseException]]]:
        """Every fn at once (as many as the machines take), in order:
        (value, None) or (None, error)."""
        def one(fn):
            try:
                return self.run(fn), None
            except BaseException as exc:            # noqa: BLE001
                return None, exc
        with ThreadPoolExecutor(max_workers=self.capacity,
                                thread_name_prefix="ledger-spread") as pool:
            return list(pool.map(one, fns))

    def summary(self) -> str:
        """'pc2 answered 14, this PC 9' - for the run's problems/summary."""
        bits = [f"{n} answered {c}" for n, c in sorted(self.used.items(),
                                                      key=lambda kv: -kv[1])]
        down = [f"{m.name} stopped answering ({m.failed})" for m in self.machines
                if m.failed]
        return "; ".join(bits + down)


def build(local_chat: Chat, *, timeout: float = 300.0,
          should_stop: Optional[Callable[[], bool]] = None,
          routing=None) -> Optional[Spread]:
    """A Spread over this PC and the machines that take shared work, or None
    when there are none (or COUNCIL_LEDGER_SPREAD=0): the Ledger then asks
    this PC alone, as before."""
    if not enabled():
        return None
    others = other_machines(routing)
    if not others:
        return None
    machines = [Machine(THIS_PC, local_chat, 1)]
    machines += [Machine(name, node_chat(name, url, model, timeout, should_stop),
                         max(1, int(slots)))
                 for name, url, model, slots in others]
    return Spread(machines, should_stop)


class PartAsker:
    """source_extract's part_finder, asked on several machines at once:
    ``submit(name, grids)`` gives a Future of (part, where); calling it asks
    and waits, as a plain part_finder does."""

    def __init__(self, spread: Spread, ask: Callable[[str, Any], Tuple[str, str]]):
        self.spread = spread
        self._ask = ask
        self._pool = ThreadPoolExecutor(max_workers=spread.capacity,
                                        thread_name_prefix="ledger-part")

    @property
    def capacity(self) -> int:
        return self.spread.capacity

    def submit(self, name: str, grids: Any) -> "Future":
        return self._pool.submit(self.spread.run, lambda: self._ask(name, grids))

    def __call__(self, name: str, grids: Any) -> Tuple[str, str]:
        return self.spread.run(lambda: self._ask(name, grids))

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


__all__ = ["Machine", "Spread", "PartAsker", "AllFailed", "build", "node_chat",
           "other_machines", "enabled", "THIS_PC"]
