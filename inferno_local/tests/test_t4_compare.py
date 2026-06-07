"""T4 tests — blind A/B compare orchestration."""
from __future__ import annotations

import json
import random
import tempfile
import threading
import time
import unittest
from pathlib import Path

# We need to register a fake backend so build_runner returns our test
# runner without trying to load a GGUF. Easiest: monkeypatch
# build_runner for the duration of each test.
from inferno_local import model_runner
import compare_runners


class FakeRunner:
    """Echoes a per-identity canned response after a short sleep so we
    actually exercise the concurrent path."""
    name = "fake"
    def __init__(self, label: str, *, delay: float = 0.05,
                 reply: str = "ok", raise_exc: bool = False):
        self.label = label
        self.delay = delay
        self.reply = reply
        self.raise_exc = raise_exc

    def chat(self, messages, *, temperature=0.2, max_tokens=600):
        time.sleep(self.delay)
        if self.raise_exc:
            raise RuntimeError(f"{self.label} failed")
        return self.reply

    def stream_chat(self, *_a, **_kw):
        yield self.reply

    def describe(self):
        return {"backend": "fake", "label": self.label}


class TestConcurrentRunBlind(unittest.TestCase):
    """All candidates execute concurrently — total wall time should be
    close to the slowest single candidate, not the sum."""

    def setUp(self):
        # Monkey-patch build_runner to return FakeRunners keyed by label.
        self._orig = model_runner.build_runner
        def _fake_build(cfg):
            label = cfg.get("_label", "?")
            delay = cfg.get("_delay", 0.05)
            return FakeRunner(label, delay=delay,
                              reply=cfg.get("_reply", label),
                              raise_exc=cfg.get("_raise", False))
        model_runner.build_runner = _fake_build

    def tearDown(self):
        model_runner.build_runner = self._orig

    def test_returns_one_column_per_candidate(self):
        run = compare_runners.run_blind(
            "hello",
            [
                compare_runners.RunnerCandidate("granite",
                    {"backend": "fake", "_label": "granite", "_reply": "g-response"}),
                compare_runners.RunnerCandidate("phi-4",
                    {"backend": "fake", "_label": "phi-4", "_reply": "p-response"}),
                compare_runners.RunnerCandidate("llama-3.1",
                    {"backend": "fake", "_label": "llama-3.1", "_reply": "l-response"}),
            ],
        )
        self.assertEqual(len(run.columns), 3)
        # Every column has an id A/B/C, no duplicates
        ids = sorted(c.column_id for c in run.columns)
        self.assertEqual(ids, ["A", "B", "C"])
        # And texts came through
        all_texts = {c.text for c in run.columns}
        self.assertEqual(all_texts, {"g-response", "p-response", "l-response"})

    def test_concurrency_wall_time(self):
        """3 candidates each sleeping 0.3s should finish in ~0.3s, not 0.9s."""
        cands = [
            compare_runners.RunnerCandidate(
                f"r{i}",
                {"backend": "fake", "_label": f"r{i}", "_delay": 0.3,
                 "_reply": f"resp-{i}"})
            for i in range(3)
        ]
        t0 = time.time()
        run = compare_runners.run_blind("hi", cands)
        elapsed = time.time() - t0
        # Generous bound: < 0.7s. Sequential would be ≥ 0.9s.
        self.assertLess(elapsed, 0.7, f"compare wall time = {elapsed:.2f}s")
        self.assertEqual(len(run.columns), 3)

    def test_identity_hidden_until_reveal(self):
        cands = [
            compare_runners.RunnerCandidate(
                "alpha", {"backend": "fake", "_label": "alpha", "_reply": "x"}),
            compare_runners.RunnerCandidate(
                "beta",  {"backend": "fake", "_label": "beta",  "_reply": "y"}),
        ]
        run = compare_runners.run_blind("?", cands)
        # The CompareColumn stores hidden_label server-side, but it's not
        # in any "public" view we'd render. We model that by asserting the
        # column_id-only dict (what the UI would show) contains no labels.
        public_view = [{"column_id": c.column_id, "text": c.text} for c in run.columns]
        for v in public_view:
            self.assertNotIn("alpha", v["text"] + v["column_id"])
            self.assertNotIn("beta",  v["text"] + v["column_id"])
        # After reveal() the mapping appears in the record.
        with tempfile.TemporaryDirectory() as td:
            record = compare_runners.reveal(
                run, ranking=[run.columns[0].column_id, run.columns[1].column_id],
                log_path=Path(td) / "log.jsonl",
            )
            identities = {c["identity"] for c in record["columns"]}
            self.assertEqual(identities, {"alpha", "beta"})

    def test_column_order_randomised(self):
        """Across many runs with a fresh RNG seed, the same candidate
        should land in different column slots at least sometimes."""
        cands = [
            compare_runners.RunnerCandidate("A1", {"backend": "fake", "_label": "A1", "_reply": "1"}),
            compare_runners.RunnerCandidate("A2", {"backend": "fake", "_label": "A2", "_reply": "2"}),
            compare_runners.RunnerCandidate("A3", {"backend": "fake", "_label": "A3", "_reply": "3"}),
        ]
        first_col_identities = set()
        for seed in range(8):
            rng = random.Random(seed)
            run = compare_runners.run_blind("q", cands, rng=rng)
            first_col_identities.add(run.columns[0].hidden_label)
        # 3 candidates, 8 seeds → all three should appear at column A
        # at least once.
        self.assertGreaterEqual(len(first_col_identities), 2,
                                f"first column identities seen: {first_col_identities}")

    def test_failing_runner_does_not_kill_the_run(self):
        cands = [
            compare_runners.RunnerCandidate(
                "good", {"backend": "fake", "_label": "good", "_reply": "ok"}),
            compare_runners.RunnerCandidate(
                "bad",  {"backend": "fake", "_label": "bad",  "_raise": True}),
        ]
        run = compare_runners.run_blind("?", cands)
        self.assertEqual(len(run.columns), 2)
        errors = [c.error for c in run.columns if c.error]
        self.assertEqual(len(errors), 1)

    def test_empty_candidates_raises(self):
        with self.assertRaises(ValueError):
            compare_runners.run_blind("?", [])


class TestRevealAndLog(unittest.TestCase):

    def setUp(self):
        self._orig = model_runner.build_runner
        def _fake_build(cfg):
            return FakeRunner(cfg.get("_label", "?"),
                              reply=cfg.get("_reply", "ok"))
        model_runner.build_runner = _fake_build

    def tearDown(self):
        model_runner.build_runner = self._orig

    def test_log_appended(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "log.jsonl"
            cands = [
                compare_runners.RunnerCandidate(
                    "alpha", {"backend": "fake", "_label": "alpha", "_reply": "x"}),
                compare_runners.RunnerCandidate(
                    "beta",  {"backend": "fake", "_label": "beta",  "_reply": "y"}),
            ]
            r1 = compare_runners.run_blind("q1", cands)
            compare_runners.reveal(r1, ranking=[r1.columns[0].column_id,
                                                r1.columns[1].column_id],
                                   log_path=log)
            r2 = compare_runners.run_blind("q2", cands)
            compare_runners.reveal(r2, ranking=[r2.columns[1].column_id,
                                                r2.columns[0].column_id],
                                   log_path=log)
            content = log.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(content), 2)
            for line in content:
                rec = json.loads(line)
                self.assertIn("run_id", rec)
                self.assertIn("ranking", rec)
                self.assertIn("columns", rec)

    def test_winners_summary(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "log.jsonl"
            cands = [
                compare_runners.RunnerCandidate(
                    "alpha", {"backend": "fake", "_label": "alpha", "_reply": "x"}),
                compare_runners.RunnerCandidate(
                    "beta",  {"backend": "fake", "_label": "beta",  "_reply": "y"}),
            ]
            # 3 runs picking alpha as winner, 1 picking beta
            for _ in range(3):
                r = compare_runners.run_blind("q", cands)
                # Pick the column whose hidden_label is 'alpha' as winner
                alpha_col = next(c.column_id for c in r.columns
                                 if c.hidden_label == "alpha")
                beta_col  = next(c.column_id for c in r.columns
                                 if c.hidden_label == "beta")
                compare_runners.reveal(r, ranking=[alpha_col, beta_col],
                                       log_path=log)
            r = compare_runners.run_blind("q", cands)
            alpha_col = next(c.column_id for c in r.columns
                             if c.hidden_label == "alpha")
            beta_col  = next(c.column_id for c in r.columns
                             if c.hidden_label == "beta")
            compare_runners.reveal(r, ranking=[beta_col, alpha_col], log_path=log)
            summary = compare_runners.winners_summary(log_path=log)
            self.assertEqual(summary[0]["identity"], "alpha")
            self.assertEqual(summary[0]["wins"], 3)


class TestCloudBackendBlocked(unittest.TestCase):
    """The factory rejection still fires when the comparison includes a
    cloud config — caller doesn't need extra guards."""

    def test_cloud_in_candidates_raises(self):
        from inferno_local import security
        # Restore real build_runner for this test
        cands = [
            compare_runners.RunnerCandidate(
                "ok", {"backend": "ollama",
                       "url": "http://127.0.0.1:11434",
                       "model": "x"}),
            compare_runners.RunnerCandidate(
                "bad", {"backend": "openai"}),
        ]
        with self.assertRaises(security.EgressBlocked):
            compare_runners.run_blind("?", cands)


if __name__ == "__main__":
    unittest.main()
