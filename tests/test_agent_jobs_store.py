"""
The Jobs tab's store (agent_jobs.JobStore) never loses a step or a job.

Found on 15 October 2026 by a whole-suite run: a job's tool step went missing
while the test polled the store. On Windows a file another thread has open
cannot be replaced, and JobStore._write swallowed that PermissionError, so the
update was dropped without a word. Its twin was worse: a read that failed the
same way came back as an EMPTY list, and the write that followed saved only
the one job being changed - every other job gone.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_jobs as aj  # noqa: E402


def _store(tmp_path):
    return aj.JobStore(vault_dir=tmp_path)


def test_every_step_survives_a_reader_polling_the_file(tmp_path):
    """The Jobs tab reads the store on the UI thread while the job's worker
    appends steps: every step must land."""
    store = _store(tmp_path)
    store.upsert(aj.AgentJob(job_id="j1", goal="probe"))
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            store.get("j1")
            store.all()
            store.get_summary("j1")

    readers = [threading.Thread(target=poll, daemon=True) for _ in range(3)]
    for r in readers:
        r.start()
    try:
        for i in range(150):
            store.append_step("j1", aj.JobStep(index=i, kind="tool",
                                               observation=f"step {i}"))
    finally:
        stop.set()
        for r in readers:
            r.join(5)
    job = store.get("j1")
    assert [s.index for s in job.steps] == list(range(150))


def test_a_read_that_fails_never_wipes_the_other_jobs(tmp_path, monkeypatch):
    """A momentary read failure (another program holding the file) must not
    be taken for an empty store."""
    store = _store(tmp_path)
    for n in range(3):
        store.upsert(aj.AgentJob(job_id=f"keep{n}", goal="keep me"))
    real = Path.read_text
    fails = {"left": 2}

    def flaky(self, *a, **k):
        if self.name == aj._STORE_NAME and fails["left"]:
            fails["left"] -= 1
            raise PermissionError(13, "The process cannot access the file "
                                      "because it is being used by another "
                                      "process")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", flaky)
    store.upsert(aj.AgentJob(job_id="new", goal="new job"))
    monkeypatch.setattr(Path, "read_text", real)
    ids = sorted(j.job_id for j in store.all())
    assert ids == ["keep0", "keep1", "keep2", "new"]


def test_a_write_that_cannot_replace_the_file_is_retried(tmp_path, monkeypatch):
    """Windows refuses the replace while a reader has the file open; the
    write waits and tries again instead of dropping the update."""
    store = _store(tmp_path)
    store.upsert(aj.AgentJob(job_id="j1", goal="probe"))
    real = Path.replace
    fails = {"left": 3}

    def busy(self, target):
        if Path(target).name == aj._STORE_NAME and fails["left"]:
            fails["left"] -= 1
            raise PermissionError(5, "Access is denied")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", busy)
    store.append_step("j1", aj.JobStep(index=0, observation="kept"))
    monkeypatch.setattr(Path, "replace", real)
    assert [s.observation for s in store.get("j1").steps] == ["kept"]


def test_a_store_that_cannot_be_written_says_so(tmp_path, monkeypatch):
    """When the file stays locked, the update is not dropped in silence."""
    store = _store(tmp_path)
    store.upsert(aj.AgentJob(job_id="j1", goal="probe"))
    real = Path.replace

    def locked(self, target):
        if Path(target).name == aj._STORE_NAME:
            raise PermissionError(5, "Access is denied")
        return real(self, target)

    monkeypatch.setattr(aj, "_RETRY_SECONDS", 0.2)
    monkeypatch.setattr(Path, "replace", locked)
    with pytest.raises(aj.JobStoreError, match="could not save"):
        store.append_step("j1", aj.JobStep(index=0))


def test_a_damaged_file_is_kept_aside_not_overwritten(tmp_path):
    """A file that no longer parses is the user's job history: it is moved
    aside, never silently replaced by the next write."""
    store = _store(tmp_path)
    store.path.write_text("{ this is not json", encoding="utf-8")
    store.upsert(aj.AgentJob(job_id="j1", goal="probe"))
    kept = list(tmp_path.glob(aj._STORE_NAME + ".damaged-*"))
    assert len(kept) == 1
    assert kept[0].read_text(encoding="utf-8") == "{ this is not json"
    assert [j.job_id for j in store.all()] == ["j1"]
    assert json.loads(store.path.read_text(encoding="utf-8"))[0]["job_id"] == "j1"


def test_the_worker_outlives_a_job_list_it_cannot_write(tmp_path, monkeypatch):
    """A job that fails while the job list cannot be saved must not kill the
    Jobs tab's worker: the failure is still reported, and the next job runs."""
    import queue
    import time as _time

    import agent_jobs_runner as ajr

    posted = queue.Queue()
    jr = ajr.JobRunner(vault_dir=tmp_path, file_root=tmp_path, ui_q=posted,
                       runner=object())
    ran = []

    def run_job(job_id):
        ran.append(job_id)
        if len(ran) == 1:
            raise RuntimeError("the first job broke")

    real_set_status = jr.store.set_status

    def set_status(job_id, status, stopped_reason=None):
        if status == aj.JobStatus.FAILED.value:
            raise aj.JobStoreError("could not save the job list: locked")
        return real_set_status(job_id, status, stopped_reason)

    monkeypatch.setattr(jr, "_run_job", run_job)
    monkeypatch.setattr(jr.store, "set_status", set_status)
    first = jr.submit("one")
    second = jr.submit("two")
    deadline = _time.monotonic() + 5
    while len(ran) < 2 and _time.monotonic() < deadline:
        _time.sleep(0.02)
    assert ran == [first, second], "the worker died with the first job"
    done = []
    while not posted.empty():
        msg = posted.get()
        if msg[0] == "job_done":
            done.append(msg)
    assert done and done[0][1] == first
    assert "the first job broke" in done[0][3]
    assert "could not save the job list" in done[0][3]
