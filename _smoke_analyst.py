"""Smoke test for sim_analyst — pure compute helpers + query
router against a synthetic SimRecorder."""
import shutil
import tempfile
from pathlib import Path


def main():
    tmp = Path(tempfile.mkdtemp(prefix="anvil_sa_smoke_"))
    try:
        import sim_analyst as sa
        import sim_recorder as sm

        # ── Pure helpers — no I/O ──
        assert sa.median_iqr([]) is None
        stats = sa.median_iqr([1, 2, 3, 4, 5, 6, 7, 8])
        assert stats["n"] == 8
        assert stats["min"] == 1 and stats["max"] == 8
        # Median of 1..8 is 4.5, p25 ≈ 2, p75 ≈ 6
        assert stats["median"] == 4.5
        print(f"PASS median_iqr -> {stats}")

        # group_by uses params then top-level
        runs = [
            {"id": "a", "params": {"persona_name": "Greedy"}, "metrics": {"s": 10}, "ok": True},
            {"id": "b", "params": {"persona_name": "Greedy"}, "metrics": {"s": 20}, "ok": True},
            {"id": "c", "params": {"persona_name": "Cautious"}, "metrics": {"s": 30}, "ok": True},
        ]
        groups = sa.group_by(runs, "persona_name")
        assert set(groups.keys()) == {"Greedy", "Cautious"}
        assert len(groups["Greedy"]) == 2 and len(groups["Cautious"]) == 1
        print("PASS group_by persona buckets")

        # best_per_persona — highest score per bucket
        bp = sa.best_per_persona(runs, "s")
        as_dict = dict(bp)
        assert as_dict["Greedy"]["id"] == "b"  # 20 > 10
        assert as_dict["Cautious"]["id"] == "c"  # only run
        # Sorted overall by metric desc → Cautious(30) first, Greedy(20)
        assert bp[0][0] == "Cautious"
        print(f"PASS best_per_persona -> {[(p, r['id']) for p, r in bp]}")

        # correlate
        c_runs = [
            {"params": {"difficulty": d}, "metrics": {"s": d * 10}}
            for d in range(1, 11)
        ]
        c = sa.correlate(c_runs, "difficulty", "s")
        assert c is not None and c["n"] == 10
        # Perfect positive linear → r should be ~1.0
        assert c["r"] >= 0.99, c
        print(f"PASS correlate (linear y=10x) -> r={c['r']}")

        # Too few pairs
        assert sa.correlate(c_runs[:2], "difficulty", "s") is None
        print("PASS correlate refuses with n<3")

        # Constant param → no variance
        flat = [{"params": {"x": 5}, "metrics": {"y": i}}
                for i in range(5)]
        assert sa.correlate(flat, "x", "y") is None
        print("PASS correlate refuses zero-variance")

        # failures
        mixed = runs + [{"id": "fail1", "params": {"persona_name": "Greedy"},
                          "metrics": {}, "ok": False, "exit_code": 1}]
        fails = sa.failures(mixed)
        assert len(fails) == 1 and fails[0]["id"] == "fail1"
        print(f"PASS failures isolates -> {[f['id'] for f in fails]}")

        # summarise_runs
        s = sa.summarise_runs(mixed)
        assert s["n"] == 4 and s["ok"] == 3 and s["failed"] == 1
        assert "Cautious" in s["personas_seen"] and "Greedy" in s["personas_seen"]
        assert s["metrics_seen"] == ["s"]
        print(f"PASS summarise_runs -> n={s['n']}, ok={s['ok']}, "
              f"failed={s['failed']}, personas={s['personas_seen']}")

        # ── End-to-end: write runs via SimRecorder + query ──
        rec = sm.SimRecorder(tmp)
        # Build a small synthetic sweep
        for i, persona in enumerate(("Greedy", "Cautious", "Greedy",
                                       "Cautious", "Greedy", "Cautious")):
            run = sm.SimRun(
                sim_name="balance_test",
                backend="python",
                params={
                    "difficulty": (i % 3) + 1,
                    "persona_name": persona,
                    "persona.greed": 0.95 if persona == "Greedy" else 0.5,
                },
                metrics={"score": 10 + i * 5, "deaths": float(i % 3)},
            )
            rec.record(run)
        # And one failure
        fail = sm.SimRun(
            sim_name="balance_test", backend="python",
            params={"difficulty": 9, "persona_name": "Greedy"},
            metrics={},
            error="ran out of stack",
        )
        # SimRun.ok is computed from error + exit_code so we don't need
        # to set ok explicitly.
        rec.record(fail)
        assert rec.count() == 7

        # ── Router: summary ──
        result = sa.answer_question("give me a summary", tmp)
        assert "Overview of 7" in result.answer, result.answer
        assert result.confidence == "high"
        print(f"PASS router → summary route")

        # ── Router: persona ──
        result = sa.answer_question(
            "compare personas across runs", tmp,
        )
        assert "per persona" in result.answer.lower(), result.answer
        assert "Greedy" in result.answer and "Cautious" in result.answer
        # Cautious medians should be present in computed_values
        assert "Cautious.median_score" in result.computed_values
        print(f"PASS router → per-persona route")

        # ── Router: best ──
        result = sa.answer_question("which run has the highest score", tmp)
        assert "Best run" in result.answer or "Top per persona" in result.answer
        print(f"PASS router → best-run route")

        # ── Router: correlation ──
        result = sa.answer_question(
            "how does difficulty affect score", tmp,
        )
        assert "Correlation" in result.answer or "fewer" in result.answer.lower()
        # We should have caught the link between difficulty and score
        if "Correlation" in result.answer:
            r = result.computed_values.get("r")
            assert r is not None and -1.0 <= r <= 1.0
        print(f"PASS router → correlation route")

        # ── Router: failures ──
        result = sa.answer_question("which runs failed", tmp)
        assert "1 of 7" in result.answer or "failed" in result.answer.lower()
        print(f"PASS router → failures route")

        # ── Empty vault ──
        empty_tmp = Path(tempfile.mkdtemp(prefix="anvil_sa_empty_"))
        try:
            result = sa.answer_question("summary", empty_tmp)
            assert "No sim runs" in result.answer
            assert result.confidence == "low"
            print(f"PASS empty-vault graceful fallback")
        finally:
            shutil.rmtree(empty_tmp, ignore_errors=True)

        # ── to_injection_block format ──
        block = result.to_injection_block()
        assert "[SIM ANALYST RESULT" in block
        assert "confidence=low" in block
        print(f"PASS to_injection_block format")

        print("\nAll sim_analyst smoke tests passed.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
