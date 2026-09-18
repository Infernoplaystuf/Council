"""
council_core.nodes — which Ollama hosts are up, and what they are running.

A read-out of every host the dispatcher knows about: reachable, latency,
running and installed models. Auto-refreshed, plus one field that re-points the
dispatcher at a new host list.

FOUR THINGS THE TK TAB GETS WRONG, ALL OF THEM SILENT

*The probe has no error handling, and the auto-refresh re-arms only on success.*
The 15-second timer is re-armed inside the branch that handles a RESULT. Point
a configured host at any HTTP server whose `/api/ps` returns a JSON array
rather than an object and the probe raises `AttributeError` in a daemon thread;
the result never arrives, the timer is never re-armed, and the tab shows
"Probing…" until the app is restarted. `probe()` returns a problem instead of
raising, so the caller always gets something to re-arm on.

*Refresh Now can show ten-second-old rows under a fresh timestamp.*
`probe_all()` reads `LoadAwareDispatcher`'s cache, whose TTL is 10 seconds.
`invalidate()` exists and has ZERO callers in the whole repository. Kill a node,
press Refresh Now inside ten seconds, and every row still reads "up" with its
old latency while the label says it just updated. `probe(force=True)` calls
`invalidate()` first, which is what the button means.

*A probe in flight when the host list changes repaints the tree with the old
list.* The worker reads the dispatcher at call time, and posts its result
unconditionally. Apply a new host list while an auto-refresh is probing four
unreachable hosts at four seconds each, and the older probe can land last. Each
result carries the hosts it actually probed, so a view can drop a stale one.

*The applied host list is never saved.* It is seeded from `COUNCIL_PI_HOSTS`,
read once at import. Add two nodes, use them all session, restart: gone, with
nothing said. This module does not silently start writing to the user's config
either — `hosts_are_temporary()` states the fact so the view can.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

#: How many installed models a row shows before it says "+N more". Six is the
#: Tk number and it is about right: a Pi with thirty models pinned would
#: otherwise push every other column off the screen.
INSTALLED_SHOWN = 6

#: What a column shows when there is nothing to show because the host is down —
#: distinct from "none", which means reachable and running nothing.
NOT_APPLICABLE = "—"

UP, DOWN = "● up", "✕ down"


@dataclass(frozen=True)
class NodeRow:
    """One line of the table, already in the words it will be shown in."""
    host: str
    status: str
    latency: str
    active: str
    installed: str
    up: bool


def to_row(status: Any) -> NodeRow:
    """A NodeStatus as the five strings a table shows.

    The reachable-versus-unreachable distinction is the whole point of the
    em-dashes: a host that is DOWN has no latency and no model list, which is
    different from a host that is up and running nothing. Collapsing both to
    "none" would make an unreachable node look idle.
    """
    reachable = bool(getattr(status, "reachable", False))
    installed = list(getattr(status, "installed_models", []) or [])
    active_names = list(getattr(status, "active_model_names", []) or [])

    if reachable:
        latency = f"{float(getattr(status, 'latency_ms', 0.0)):.0f} ms"
        active = ", ".join(active_names) if active_names else "none"
    else:
        latency = NOT_APPLICABLE
        active = NOT_APPLICABLE

    if installed:
        shown = ", ".join(installed[:INSTALLED_SHOWN])
        if len(installed) > INSTALLED_SHOWN:
            shown += f" (+{len(installed) - INSTALLED_SHOWN} more)"
    else:
        shown = NOT_APPLICABLE

    return NodeRow(host=str(getattr(status, "host", "")),
                   status=UP if reachable else DOWN,
                   latency=latency, active=active, installed=shown,
                   up=reachable)


def parse_hosts(raw: str) -> List[str]:
    """A comma-separated host box as a list.

    An empty box means NO EXTRA HOSTS — the dispatcher keeps its default,
    which is localhost. It does not mean "no hosts at all"; a user who clears
    the field is removing the Pis, not switching the app off.
    """
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


@dataclass
class ProbeResult:
    rows: List[NodeRow] = field(default_factory=list)
    #: Why the probe produced nothing. Empty on success.
    problem: str = ""
    #: The hosts this probe actually ran against. A view compares it with the
    #: dispatcher's current hosts and drops a result that belongs to a list the
    #: user has already replaced.
    hosts: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problem


def probe(dispatcher: Any, *, force: bool = False) -> ProbeResult:
    """Every host's status. Blocking — call it from a worker. NEVER raises.

    `force` invalidates the dispatcher's cache first. Without it "Refresh Now"
    is a button that can do nothing at all for ten seconds and say it worked.
    """
    hosts = tuple(str(h) for h in getattr(dispatcher, "hosts", ()) or ())
    if dispatcher is None:
        return ProbeResult(problem="There is no dispatcher to probe.",
                           hosts=hosts)
    if force:
        try:
            dispatcher.invalidate()
        except Exception:                                 # noqa: BLE001
            # A dispatcher without invalidate() is older than this code; a
            # stale row is much better than a dead Refresh button.
            pass
    try:
        statuses = dispatcher.probe_all()
    except Exception as exc:                              # noqa: BLE001
        # The Tk worker has no guard at all, and its 15s re-arm lives only on
        # the success path — so one bad host stops the tab refreshing for the
        # life of the process.
        return ProbeResult(problem=f"Could not probe the nodes: {exc!r}",
                           hosts=hosts)
    return ProbeResult(rows=[to_row(s) for s in statuses], hosts=hosts)


def is_stale(result: ProbeResult, dispatcher: Any) -> bool:
    """Whether this result belongs to a host list that has been replaced.

    Two probes can be in flight at once — the 15s auto-refresh and the one
    Apply kicks off — and the slower one can land last. Without this the table
    shows the previous host list under the new one's label.
    """
    current = tuple(str(h) for h in getattr(dispatcher, "hosts", ()) or ())
    return tuple(result.hosts) != current


@dataclass
class RebuildResult:
    ok: bool
    message: str = ""
    dispatcher: Any = None
    personalities: Any = None


def rebuild(raw_hosts: str, vault_dir: Any, *, session_id: str = "qt",
            prior_session_id: str = "") -> RebuildResult:
    """Point the dispatcher at a new host list and rebuild the council.

    BLOCKING, and heavily so: it maps model files and can load GGUF weights.
    The Tk version runs it on the UI thread, where a 32B Q4_K_M stops the
    window repainting for tens of seconds.

    NOTHING IS SWAPPED UNTIL BOTH HALVES SUCCEED. The Tk code assigns
    `self.dispatcher` and only then builds the personalities, with no try —
    so a failure leaves a new dispatcher wired to the old council, and the two
    disagree about which hosts exist for the rest of the session.
    """
    import council_engine

    from council_core import council_turn

    hosts = parse_hosts(raw_hosts)
    try:
        dispatcher = council_engine.build_dispatcher(extra_hosts=hosts)
    except Exception as exc:                              # noqa: BLE001
        return RebuildResult(False, f"Could not build the dispatcher: {exc!r}")

    personalities, problem = council_turn.load_personalities(
        vault_dir, session_id=session_id, dispatcher=dispatcher)
    if personalities is None:
        # The caller's existing dispatcher is untouched, because this one was
        # never handed back.
        return RebuildResult(False, problem or "The council could not be "
                                               "rebuilt for those hosts.")
    return RebuildResult(True, f"Dispatcher rebuilt with hosts: {hosts}",
                         dispatcher=dispatcher, personalities=personalities)


def hosts_are_temporary() -> str:
    """Why an applied host list does not survive a restart.

    Said rather than silently fixed: persisting it means writing to the user's
    configuration, and a tab that starts doing that on its own is a surprise.
    """
    return ("This list applies to the current session. It is read from "
            "COUNCIL_PI_HOSTS at startup, so set that to keep it.")
