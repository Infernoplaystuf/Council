"""A Ledger run's unmatched values go to a temporary file past a limit
(source_extract.ObsSpool) - user 2026-10-22: "for files that have already
been processed lets no longer keep them in memory to free up RAM"."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import source_extract as se  # noqa: E402


def obs(i: int) -> se.Obs:
    return se.Obs("101", "", "", f"v{i}", 0.25, "none", f"Label {i}", f"b.xlsx › S!B{i}",
                  guesses=[("Power (W)", 0.25), ("Date", 0.2)], part_how="on Build")


@pytest.fixture
def small(monkeypatch):
    monkeypatch.setattr(se, "SPOOL_KEEP", 3)


def test_past_the_limit_values_go_to_disk_and_come_back_in_order(small):
    sp = se.ObsSpool()
    sp.extend(obs(i) for i in range(10))
    assert len(sp) == 10 and sp._file is not None and len(sp._mem) < 3
    back = list(sp)
    assert [o.value for o in back] == [f"v{i}" for i in range(10)]
    assert back[4] == obs(4) and back[4].guesses == [("Power (W)", 0.25), ("Date", 0.2)]
    assert list(sp) == back                                  # read again: the same
    assert sp[7] == obs(7) and sp[-1] == obs(9)
    assert [o.value for o in [obs(-1)] + sp][:2] == ["v-1", "v0"]
    sp.close()


@pytest.mark.parametrize("cut", [0, 2, 5, 9, 10])
def test_a_cancel_drops_the_end(small, cut):
    sp = se.ObsSpool(obs(i) for i in range(10))
    del sp[cut:]
    assert [o.value for o in sp] == [f"v{i}" for i in range(cut)]
    sp.extend([obs(50), obs(51)])                            # and goes on after
    assert [o.value for o in sp][-2:] == ["v50", "v51"] and len(sp) == cut + 2
    with pytest.raises(TypeError):
        del sp[1:3]
    sp.close()


def test_under_the_limit_nothing_touches_the_disk():
    sp = se.ObsSpool(obs(i) for i in range(10))
    assert sp._file is None and sp == [obs(i) for i in range(10)]
    assert not se.ObsSpool() and bool(sp)


def test_a_value_is_smaller_than_it_was():
    o = obs(1)
    assert not hasattr(o, "__dict__")                        # slots
    with pytest.raises(AttributeError):
        o.something_else = 1
