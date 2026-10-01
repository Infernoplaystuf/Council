"""
council_core.child_proc — run a short-lived child Python so that a LONG run
of them cannot starve the machine, leak, or be misgraded.

WHY THIS EXISTS (the phi4 baseline, 2026-10-01)
The old benchmark's phi4:14b run started failing every child process late in
the run: the generated apps' runtime probes and every hidden code test exited
3221225794 = 0xC0000142, STATUS_DLL_INIT_FAILED, with nothing on stderr — and
the harness graded each one as the MODEL's failure (runtime_failed /
test_failure). What was measured on this PC:

  * the System event log at 12:09:36, mid-way through the run before it:
    "Windows - Virtual Memory Minimum Too Low: Your system is low on virtual
    memory" and Volsnap "shadow copy storage failed to grow" (C: was 97 %
    full, so the paging file could not grow);
  * the commit limit is 31.7 GB RAM + ~7.3 GB paging file = ~39 GB, and the
    desktop apps alone hold ~24 GB of it;
  * the bench process itself held the model: phi4 through llama_cpp
    (partial offload) has 12.8 GB of PRIVATE commit (probe_phi4.jsonl);
  * a child started with its committed memory capped (a Job Object limit —
    the safe way to starve ONE process) dies with EXACTLY 0xC0000142 and no
    stderr when the cap bites during DLL initialisation, and with
    0xC000012D (STATUS_COMMITMENT_LIMIT) or a MemoryError a little either
    side of it. No python.exe was left running from those runs, and the
    event log has no desktop-heap (Win32k 243) entry: not a process leak.

So: the machine ran out of COMMIT, and every new process failed to start.
The harness cannot add RAM, but it can stop making it worse and stop lying
about it. Each child here:

  1. waits for commit headroom before it starts (GlobalMemoryStatusEx's
     ullAvailPageFile — what can still be committed, machine-wide), up to a
     limit, and says how much there was;
  2. runs in a Job Object: KILL_ON_JOB_CLOSE, so a timeout or a crash takes
     the whole process TREE with it (subprocess's kill is the direct child
     only), any descendant still alive after the child exits is counted as
     LEAKED and killed, and an optional per-process commit cap, so a runaway
     model-written handler (np.zeros((10**5, 10**5))) gets a MemoryError
     instead of draining the commit every later child needs;
  3. gets no console (CREATE_NO_WINDOW) — no conhost.exe per child;
  4. is classified: an exit of 0xC0000142 / 0xC000012D / 0xC0000017, or a
     CreateProcess failure of ERROR_COMMITMENT_LIMIT and friends, is an
     INFRASTRUCTURE failure (``infra``), retried after waiting for headroom,
     and never handed to a grader as the code's fault.

Stdlib only; no Council imports (gui_smoke and docs_bench use it too).
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

#: Exit codes that mean "the process could not start or could not get
#: memory" — the machine's fault, not the code's.
INFRA_EXIT_CODES: Dict[int, str] = {
    0xC0000142: "STATUS_DLL_INIT_FAILED",
    0xC000012D: "STATUS_COMMITMENT_LIMIT",
    0xC0000017: "STATUS_NO_MEMORY",
    0xC000009A: "STATUS_INSUFFICIENT_RESOURCES",
}
#: WinError numbers CreateProcess fails with when the machine is out of
#: commit / quota.
INFRA_WINERRORS: Dict[int, str] = {
    8: "ERROR_NOT_ENOUGH_MEMORY", 14: "ERROR_OUTOFMEMORY",
    1450: "ERROR_NO_SYSTEM_RESOURCES", 1453: "ERROR_WORKING_SET_QUOTA",
    1455: "ERROR_COMMITMENT_LIMIT", 1816: "ERROR_NOT_ENOUGH_QUOTA",
}

#: Commit (MB) a child should find free before it starts. 1 GB: a Qt probe
#: commits ~150 MB, a hidden test ~25 MB; the rest is for the machine.
DEFAULT_MIN_COMMIT_MB = 1024
#: How long to wait for that headroom before starting anyway.
DEFAULT_COMMIT_WAIT_S = 60.0
#: Infra failures are retried this many times (after waiting for headroom).
DEFAULT_RETRIES = 2
#: Output kept per stream (the TAIL): a flooding child must not fill the
#: parent's memory.
DEFAULT_MAX_OUTPUT = 1 << 20


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def min_commit_mb() -> int:
    return _env_int("COUNCIL_BENCH_MIN_COMMIT_MB", DEFAULT_MIN_COMMIT_MB)


# ============================================================
# Memory status
# ============================================================

def memory_status() -> Dict[str, Optional[int]]:
    """{commit_free_mb, commit_limit_mb, ram_free_mb, ram_total_mb}; None
    for whatever this platform cannot say. Never raises."""
    out: Dict[str, Optional[int]] = {"commit_free_mb": None,
                                     "commit_limit_mb": None,
                                     "ram_free_mb": None,
                                     "ram_total_mb": None}
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(st)
            if _K32 is not None and _K32.GlobalMemoryStatusEx(
                    ctypes.byref(st)):
                mb = 1024 * 1024
                out.update(commit_free_mb=int(st.ullAvailPageFile // mb),
                           commit_limit_mb=int(st.ullTotalPageFile // mb),
                           ram_free_mb=int(st.ullAvailPhys // mb),
                           ram_total_mb=int(st.ullTotalPhys // mb))
        elif os.path.exists("/proc/meminfo"):
            info = {}
            with open("/proc/meminfo", encoding="ascii") as fh:
                for line in fh:
                    k, _, v = line.partition(":")
                    info[k.strip()] = int(v.split()[0]) // 1024
            limit, used = info.get("CommitLimit"), info.get("Committed_AS")
            out.update(ram_free_mb=info.get("MemAvailable"),
                       ram_total_mb=info.get("MemTotal"),
                       commit_limit_mb=limit,
                       commit_free_mb=(limit - used) if limit and used
                       is not None else None)
    except Exception:                                     # noqa: BLE001
        pass
    return out


def wait_for_commit(min_mb: Optional[int] = None,
                    max_wait: float = DEFAULT_COMMIT_WAIT_S,
                    should_stop: Optional[Callable[[], bool]] = None,
                    poll: float = 1.0,
                    status: Callable[[], Dict[str, Optional[int]]]
                    = memory_status) -> Dict[str, Any]:
    """Block until ``min_mb`` of commit is free (or ``max_wait`` passes, or
    should_stop()). {ok, free_mb, waited_s}. ``ok`` is True when there was
    room or the platform cannot say."""
    min_mb = min_commit_mb() if min_mb is None else int(min_mb)
    t0 = time.monotonic()
    while True:
        free = status().get("commit_free_mb")
        if free is None or free >= min_mb:
            return {"ok": True, "free_mb": free,
                    "waited_s": round(time.monotonic() - t0, 2)}
        if time.monotonic() - t0 >= max_wait or (
                should_stop is not None and should_stop()):
            return {"ok": False, "free_mb": free,
                    "waited_s": round(time.monotonic() - t0, 2)}
        time.sleep(poll)


def process_count() -> Optional[int]:
    """How many processes the OS is running now (None if unknown) — the
    soak compares it before and after."""
    try:
        if sys.platform == "win32":
            import ctypes
            import ctypes.wintypes as wt
            psapi = ctypes.WinDLL("psapi")
            arr = (wt.DWORD * 8192)()
            got = wt.DWORD()
            if psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr),
                                   ctypes.byref(got)):
                return int(got.value // ctypes.sizeof(wt.DWORD))
            return None
        if os.path.isdir("/proc"):
            return sum(1 for n in os.listdir("/proc") if n.isdigit())
    except Exception:                                     # noqa: BLE001
        pass
    return None


# ============================================================
# Job objects (Windows)
# ============================================================

_K32 = None
_NTDLL = None
if sys.platform == "win32":
    try:
        import ctypes
        import ctypes.wintypes as _wt
        # Our OWN library objects: argtypes set here must not leak into
        # ctypes.windll.kernel32, which other modules use with their own.
        _K32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _NTDLL = ctypes.WinDLL("ntdll")
        _K32.CreateJobObjectW.restype = _wt.HANDLE
        _K32.CreateJobObjectW.argtypes = [ctypes.c_void_p, _wt.LPCWSTR]
        _K32.SetInformationJobObject.argtypes = [
            _wt.HANDLE, ctypes.c_int, ctypes.c_void_p, _wt.DWORD]
        _K32.QueryInformationJobObject.argtypes = [
            _wt.HANDLE, ctypes.c_int, ctypes.c_void_p, _wt.DWORD,
            ctypes.POINTER(_wt.DWORD)]
        _K32.AssignProcessToJobObject.argtypes = [_wt.HANDLE, _wt.HANDLE]
        _K32.TerminateJobObject.argtypes = [_wt.HANDLE, _wt.UINT]
        _K32.CloseHandle.argtypes = [_wt.HANDLE]
        _NTDLL.NtResumeProcess.argtypes = [_wt.HANDLE]
    except Exception:                                     # noqa: BLE001
        _K32 = _NTDLL = None

_JOB_BASIC_ACCOUNTING = 1
_JOB_EXTENDED_LIMITS = 9
_LIMIT_PROCESS_MEMORY = 0x100
_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_CREATE_SUSPENDED = 0x4
_CREATE_NO_WINDOW = 0x08000000


def _job_structs():
    import ctypes
    import ctypes.wintypes as wt

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "r_ops", "w_ops", "o_ops", "r_bytes", "w_bytes", "o_bytes")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wt.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wt.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wt.DWORD),
                    ("SchedulingClass", wt.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("Basic", BASIC), ("Io", IO),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class ACCOUNTING(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_longlong),
                    ("TotalKernelTime", ctypes.c_longlong),
                    ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                    ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                    ("TotalPageFaultCount", wt.DWORD),
                    ("TotalProcesses", wt.DWORD),
                    ("ActiveProcesses", wt.DWORD),
                    ("TotalTerminatedProcesses", wt.DWORD)]
    return EXTENDED, ACCOUNTING


class _Job:
    """One Job Object holding one child and everything it starts."""

    def __init__(self, memory_limit_mb: Optional[int] = None):
        self.handle = None
        self.peak_mb: Optional[int] = None
        if _K32 is None:
            return
        import ctypes
        EXTENDED, _ACC = _job_structs()
        h = _K32.CreateJobObjectW(None, None)
        if not h:
            return
        info = EXTENDED()
        info.Basic.LimitFlags = _LIMIT_KILL_ON_JOB_CLOSE
        if memory_limit_mb:
            info.Basic.LimitFlags |= _LIMIT_PROCESS_MEMORY
            info.ProcessMemoryLimit = int(memory_limit_mb) * 1024 * 1024
        if not _K32.SetInformationJobObject(h, _JOB_EXTENDED_LIMITS,
                                            ctypes.byref(info),
                                            ctypes.sizeof(info)):
            _K32.CloseHandle(h)
            return
        self.handle = h

    def assign(self, proc: subprocess.Popen) -> bool:
        if not self.handle:
            return False
        import ctypes.wintypes as wt
        return bool(_K32.AssignProcessToJobObject(
            self.handle, wt.HANDLE(int(proc._handle))))   # noqa: SLF001

    def active(self) -> int:
        if not self.handle:
            return 0
        import ctypes
        _EXT, ACCOUNTING = _job_structs()
        acc = ACCOUNTING()
        if not _K32.QueryInformationJobObject(
                self.handle, _JOB_BASIC_ACCOUNTING, ctypes.byref(acc),
                ctypes.sizeof(acc), None):
            return 0
        return int(acc.ActiveProcesses)

    def read_peak(self) -> None:
        if not self.handle:
            return
        import ctypes
        EXTENDED, _ACC = _job_structs()
        info = EXTENDED()
        if _K32.QueryInformationJobObject(
                self.handle, _JOB_EXTENDED_LIMITS, ctypes.byref(info),
                ctypes.sizeof(info), None):
            self.peak_mb = int(info.PeakProcessMemoryUsed // (1024 * 1024))

    def kill(self) -> None:
        if self.handle:
            _K32.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle:
            _K32.CloseHandle(self.handle)        # KILL_ON_JOB_CLOSE
            self.handle = None


def _resume(proc: subprocess.Popen) -> None:
    import ctypes.wintypes as wt
    _NTDLL.NtResumeProcess(wt.HANDLE(int(proc._handle)))  # noqa: SLF001


# ============================================================
# Running one child
# ============================================================

@dataclass
class ChildResult:
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    seconds: float = 0.0
    timed_out: bool = False
    stopped: bool = False
    #: "" or why this was the MACHINE's failure, not the code's.
    infra: str = ""
    attempts: int = 0
    #: Commit free (MB) when the last attempt started; None if unknown.
    commit_free_mb: Optional[int] = None
    #: Seconds spent waiting for commit headroom, all attempts.
    waited_s: float = 0.0
    #: Descendants still running after the child exited (then killed).
    leaked: int = 0
    #: Ran inside a Job Object (Windows).
    job: bool = False
    #: Peak committed memory of any process in the job (MB), Windows.
    peak_mb: Optional[int] = None
    error: str = ""
    infra_history: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (self.returncode == 0 and not self.timed_out
                and not self.infra and not self.stopped)


def infra_reason(returncode: Optional[int], exc: Optional[BaseException]
                 = None) -> str:
    """Why this exit (or this failure to start) is the machine's fault, or
    "" when it is not."""
    if exc is not None:
        code = getattr(exc, "winerror", None)
        if code in INFRA_WINERRORS:
            return f"could not start: {INFRA_WINERRORS[code]} ({exc})"
        if isinstance(exc, MemoryError):
            return f"could not start: {exc!r}"
        return ""
    if returncode is None:
        return ""
    code = returncode & 0xFFFFFFFF
    name = INFRA_EXIT_CODES.get(code)
    return f"exit 0x{code:08X} {name}" if name else ""


def child_env(extra: Optional[Dict[str, str]] = None,
              drop: Sequence[str] = ("PYTHONPATH", "PYTHONHOME",
                                     "PYTHONSTARTUP", "PYTHONINSPECT")
              ) -> Dict[str, str]:
    """The parent's environment minus what would make a fresh interpreter
    load someone else's code, plus ``extra``."""
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.update(extra or {})
    return env


def run(argv: Sequence[str], *, cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None, timeout: float = 60.0,
        input: Optional[str] = None, memory_limit_mb: Optional[int] = None,
        retries: int = DEFAULT_RETRIES, min_commit: Optional[int] = None,
        commit_wait: float = DEFAULT_COMMIT_WAIT_S,
        should_stop: Optional[Callable[[], bool]] = None,
        on_line: Optional[Callable[[str], None]] = None,
        max_output: int = DEFAULT_MAX_OUTPUT) -> ChildResult:
    """Run ``argv`` to completion (text mode, utf-8). NEVER RAISES.

    ``timeout`` and ``should_stop`` kill the whole process tree. An
    infrastructure failure (see the module docstring) is retried up to
    ``retries`` times after waiting for commit headroom; if it persists,
    ``infra`` says why and the caller must not grade the output.
    ``on_line`` receives each stdout line as it arrives."""
    res = ChildResult()
    t_all = time.perf_counter()
    for attempt in range(max(0, int(retries)) + 1):
        res.attempts = attempt + 1
        room = wait_for_commit(min_commit, commit_wait if attempt == 0
                               else max(commit_wait, 30.0), should_stop)
        res.commit_free_mb = room["free_mb"]
        res.waited_s = round(res.waited_s + room["waited_s"], 2)
        if should_stop is not None and should_stop():
            res.stopped = True
            break
        one = _run_once(argv, cwd=cwd, env=env, timeout=timeout, input=input,
                        memory_limit_mb=memory_limit_mb,
                        should_stop=should_stop, on_line=on_line,
                        max_output=max(1024, int(max_output)))
        for k in ("returncode", "stdout", "stderr", "timed_out", "stopped",
                  "leaked", "job", "peak_mb", "error"):
            setattr(res, k, getattr(one, k))
        res.infra = one.infra
        if not one.infra or one.stopped:
            break
        res.infra_history.append(one.infra)
        time.sleep(min(2.0 * (attempt + 1), 5.0))
    if res.infra:
        res.infra = (f"{res.infra} after {res.attempts} attempt(s); commit "
                     f"free {res.commit_free_mb} MB — the machine could not "
                     f"start the process (out of virtual memory?)")
    res.seconds = round(time.perf_counter() - t_all, 3)
    return res


def _run_once(argv, *, cwd, env, timeout, input, memory_limit_mb,
              should_stop, on_line, max_output) -> ChildResult:
    res = ChildResult()
    windows = sys.platform == "win32"
    job = _Job(memory_limit_mb) if windows else None
    flags = 0
    if windows:
        flags = _CREATE_NO_WINDOW
        if job is not None and job.handle and _NTDLL is not None:
            flags |= _CREATE_SUSPENDED
    try:
        proc = subprocess.Popen(
            list(argv), cwd=cwd, env=env,
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=flags, start_new_session=not windows)
    except (OSError, MemoryError) as exc:
        if job is not None:
            job.close()
        res.infra = infra_reason(None, exc)
        res.error = f"could not start {argv[0]!r}: {exc!r}"
        return res
    if windows and flags & _CREATE_SUSPENDED:
        res.job = job.assign(proc)
        _resume(proc)
    out_buf, err_buf = bytearray(), bytearray()

    def pump(stream, buf: bytearray, cb) -> None:
        """Keep only the last max_output bytes: a child that floods its
        output (`while True: print(...)`) must not fill the PARENT's
        memory — measured +706 MB in 8 s with capture_output (docs_bench).
        Chunks, not readline: a flood with no newline is one endless line."""
        pending = b""
        try:
            for chunk in iter(lambda: stream.read1(65536), b""):
                buf.extend(chunk)
                if len(buf) > max_output:
                    del buf[:-max_output]
                if cb is None:
                    continue
                pending += chunk
                *lines, pending = pending.split(b"\n")
                pending = pending[-65536:]
                for raw in lines:
                    try:
                        cb(raw.decode("utf-8", errors="replace")
                           .rstrip("\r"))
                    except Exception:                     # noqa: BLE001
                        pass
            if cb is not None and pending:
                try:
                    cb(pending.decode("utf-8", errors="replace"))
                except Exception:                         # noqa: BLE001
                    pass
        except (OSError, ValueError):
            pass

    readers = [threading.Thread(target=pump, args=(proc.stdout, out_buf,
                                                   on_line), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, err_buf,
                                                   None), daemon=True)]
    for t in readers:
        t.start()
    if input is not None:
        try:
            proc.stdin.write(input.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass
    t0 = time.monotonic()
    while True:
        try:
            proc.wait(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            pass
        if should_stop is not None and should_stop():
            res.stopped = True
            break
        if timeout and time.monotonic() - t0 > timeout:
            res.timed_out = True
            break
    if res.stopped or res.timed_out:
        _kill_tree(proc, job)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    if job is not None and job.handle:
        # The console host Windows starts beside a console child sits in
        # the same job and exits a moment after it: give it that moment
        # before anything still running is counted as leaked.
        deadline = time.monotonic() + 1.5
        res.leaked = job.active()
        while res.leaked and time.monotonic() < deadline:
            time.sleep(0.05)
            res.leaked = job.active()
        job.read_peak()
        res.peak_mb = job.peak_mb
        if res.leaked:
            job.kill()
        job.close()
    for t in readers:
        t.join(5)
    for s in (proc.stdout, proc.stderr):
        try:
            s.close()
        except Exception:                                 # noqa: BLE001
            pass
    res.returncode = proc.returncode
    res.stdout = bytes(out_buf).decode("utf-8", "replace").replace(
        "\r\n", "\n")
    res.stderr = bytes(err_buf).decode("utf-8", "replace").replace(
        "\r\n", "\n")
    if not (res.timed_out or res.stopped):
        res.infra = infra_reason(proc.returncode)
    return res


def _kill_tree(proc: subprocess.Popen, job: Optional[_Job]) -> None:
    try:
        if job is not None and job.handle:
            job.kill()
        elif sys.platform != "win32":
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except Exception:                                     # noqa: BLE001
        try:
            proc.kill()
        except Exception:                                 # noqa: BLE001
            pass
