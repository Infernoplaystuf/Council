"""
Which plugins a Typhon project runs, and with what settings (plugin design
4.1, 4.2; stage P1 row 2): council_core.plugin_lists, and the project's list
written when a "Typhon (plugins)" project is made.

Your answers, as tested here:
  * question 1 — a plugin added to the app-wide base set reaches only
    projects created afterwards: a project keeps a COPY of the base set made
    when the project is;
  * question 14 — a setting changed in a plugin's window changes only this
    project; "make this the default for every project" puts it in the base
    set, which every project that has not overridden it follows.

And the plugin home: TYPHON_PLUGIN_HOME, else the folder the project
recorded (home-relative), else ~/.council/typhon — read, never created.
Every write is whole, under a lock, with a temporary name of its own, and a
file Typhon cannot trust is left exactly as it is.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from council_core import plugin_lists as pl  # noqa: E402

BUILTINS = ["typhon.basler", "typhon.prophesee", "typhon.simulated",
            "typhon.trigger", "typhon.classify", "typhon.timing",
            "typhon.crop"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    folder = tmp_path / "plugin_home"
    folder.mkdir()
    monkeypatch.setenv(pl.HOME_ENV, str(folder))
    return folder


@pytest.fixture
def fake_user(tmp_path, monkeypatch):
    """A home folder of the test's own, so ~ means it."""
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setenv("USERPROFILE", str(user))
    monkeypatch.setenv("HOME", str(user))
    assert Path.home() == user
    return user


def project(tmp_path, name, home, base=None):
    folder = tmp_path / name
    folder.mkdir()
    base = base or pl.read_base_set(home, BUILTINS)
    pl.make_project(folder, base, home)
    return folder


def files_of(folder):
    return {p.name: p.read_bytes() for p in Path(folder).iterdir()
            if p.is_file()}


# ======================================================================
# The plugin home (design 4.1)
# ======================================================================
def test_the_variable_names_the_plugin_home(home):
    chosen = pl.resolve_home()
    assert (chosen.path, chosen.source, chosen.note) == (home, "env", "")


def test_without_the_variable_the_home_is_councils_typhon_folder(
        fake_user, monkeypatch):
    monkeypatch.delenv(pl.HOME_ENV, raising=False)
    chosen = pl.resolve_home()
    assert chosen.path == fake_user / ".council" / "typhon"
    assert chosen.source == "default"
    assert not (fake_user / ".council").exists(), "read, never created"


def test_a_project_finds_the_home_it_recorded_however_it_was_started(
        fake_user, monkeypatch):
    side = fake_user / "Typhon-plugins-app" / "typhon"
    side.mkdir(parents=True)
    monkeypatch.delenv(pl.HOME_ENV, raising=False)   # a double-clicked app
    recorded = pl.home_text(side)
    assert recorded == "~/Typhon-plugins-app/typhon", "home-relative"
    chosen = pl.resolve_home(recorded)
    assert (chosen.path, chosen.source) == (side, "project")


def test_a_recorded_home_that_is_missing_is_named_and_not_created(
        fake_user, monkeypatch):
    monkeypatch.delenv(pl.HOME_ENV, raising=False)
    chosen = pl.resolve_home("~/Gone/typhon")
    assert chosen.source == "default"
    assert chosen.path == fake_user / ".council" / "typhon"
    assert "~/Gone/typhon" in chosen.note and "nothing was created" in \
        chosen.note
    assert not (fake_user / "Gone").exists()
    assert not (fake_user / ".council").exists()


def test_the_variable_wins_over_the_recorded_home_and_says_so(
        home, fake_user):
    chosen = pl.resolve_home("~/elsewhere/typhon")
    assert chosen.path == home and chosen.source == "env"
    assert "~/elsewhere/typhon" in chosen.note


def test_a_home_outside_the_home_folder_is_recorded_absolute(
        tmp_path, fake_user):
    outside = tmp_path / "shared" / "typhon"
    assert pl.home_text(outside) == outside.resolve().as_posix()
    assert pl.expand(pl.home_text(outside)).resolve() == outside.resolve()


# ======================================================================
# The base set (design 4.2)
# ======================================================================
def test_a_new_plugin_home_has_every_built_in_and_writes_nothing(home):
    base = pl.read_base_set(home, BUILTINS)
    assert base.plugins == BUILTINS and base.settings == {}
    assert not base.written
    assert list(home.iterdir()) == [], "reading wrote nothing"


def test_the_base_set_is_written_whole_when_it_changes(home):
    pl.set_base_plugins(home, BUILTINS, BUILTINS + ["acme.thermal"])
    pl.set_default_settings(home, BUILTINS, "typhon.classify",
                            {"show_after_stop": True})
    doc = json.loads((home / pl.BASE_SET_FILE).read_text(encoding="utf-8"))
    assert doc == {"format": pl.BASE_SET_FORMAT, "format_version": 1,
                   "plugins": BUILTINS + ["acme.thermal"],
                   "settings": {"typhon.classify":
                                {"show_after_stop": True}}}
    assert sorted(p.name for p in home.iterdir()) == [
        ".base_set.json.lock", pl.BASE_SET_FILE], "no temporary file left"


# ======================================================================
# A project's list — question 1
# ======================================================================
def test_a_project_copies_the_base_set_when_it_is_made(tmp_path, home,
                                                       fake_user):
    first = project(tmp_path, "first", home)
    doc = json.loads((first / pl.PROJECT_FILE).read_text(encoding="utf-8"))
    assert doc["format"] == pl.PROJECT_FORMAT and doc["format_version"] == 1
    assert doc["base_set"] == BUILTINS
    assert (doc["added"], doc["off"], doc["settings"]) == ([], [], {})
    assert doc["plugin_home"] == pl.home_text(home)
    assert pl.read_project(first).runs() == BUILTINS


def test_a_plugin_added_to_the_base_set_reaches_only_later_projects(
        tmp_path, home):
    """Question 1, the user's words: "a plugin added to the app-wide base set
    reaches only projects created afterwards"."""
    before = project(tmp_path, "before", home)
    pl.set_base_plugins(home, BUILTINS, BUILTINS + ["acme.thermal"])
    after = project(tmp_path, "after", home)
    assert "acme.thermal" not in pl.read_project(before).runs()
    assert "acme.thermal" in pl.read_project(after).runs()


def test_a_plugin_taken_out_of_the_base_set_stays_in_older_projects(
        tmp_path, home):
    before = project(tmp_path, "before", home)
    pl.set_base_plugins(home, BUILTINS, [p for p in BUILTINS
                                         if p != "typhon.timing"])
    after = project(tmp_path, "after", home)
    assert "typhon.timing" in pl.read_project(before).runs()
    assert "typhon.timing" not in pl.read_project(after).runs()


def test_a_project_adds_and_switches_off_plugins_of_its_own(tmp_path, home):
    folder = project(tmp_path, "p", home)
    pl.switch_on(folder, "acme.thermal")
    pl.switch_off(folder, "typhon.timing")
    listed = pl.read_project(folder)
    assert listed.added == ["acme.thermal"] and listed.off == \
        ["typhon.timing"]
    assert listed.runs() == [p for p in BUILTINS if p != "typhon.timing"] \
        + ["acme.thermal"]
    pl.switch_on(folder, "typhon.timing")
    pl.switch_off(folder, "acme.thermal")
    listed = pl.read_project(folder)
    assert (listed.added, listed.off) == ([], [])
    assert listed.runs() == BUILTINS


def test_a_project_list_is_made_once_and_never_over_a_file(tmp_path, home):
    folder = project(tmp_path, "p", home)
    kept = (folder / pl.PROJECT_FILE).read_bytes()
    with pytest.raises(pl.PluginListError, match="never replaced"):
        pl.make_project(folder, pl.read_base_set(home, BUILTINS), home)
    assert (folder / pl.PROJECT_FILE).read_bytes() == kept
    with pytest.raises(pl.PluginListError, match="does not exist"):
        pl.switch_on(tmp_path, "acme.thermal")


# ======================================================================
# Settings — question 14
# ======================================================================
def test_a_plugins_window_changes_only_this_project(tmp_path, home):
    """Question 14: "a setting changed in a plugin window changes only this
    project"."""
    a = project(tmp_path, "a", home)
    b = project(tmp_path, "b", home)
    pl.save_project_settings(a, "typhon.classify",
                             {"show_after_stop": False, "model": "birds"})
    base = pl.read_base_set(home, BUILTINS)
    assert pl.effective_settings(base, pl.read_project(a),
                                 "typhon.classify") == \
        {"show_after_stop": False, "model": "birds"}
    assert pl.effective_settings(base, pl.read_project(b),
                                 "typhon.classify") == {}
    assert not (home / pl.BASE_SET_FILE).exists(), "the base set untouched"


def test_the_defaults_are_followed_by_projects_that_did_not_override(
        tmp_path, home):
    a = project(tmp_path, "a", home)
    b = project(tmp_path, "b", home)
    pl.save_project_settings(a, "typhon.classify", {"show_after_stop": False})
    pl.set_default_settings(home, BUILTINS, "typhon.classify",
                            {"show_after_stop": True, "rate": 2})
    base = pl.read_base_set(home, BUILTINS)
    assert pl.effective_settings(base, pl.read_project(a),
                                 "typhon.classify") == \
        {"show_after_stop": False, "rate": 2}, "its own value wins, key by key"
    assert pl.effective_settings(base, pl.read_project(b),
                                 "typhon.classify") == \
        {"show_after_stop": True, "rate": 2}


def test_make_this_the_default_for_every_project(tmp_path, home):
    """The later button of question 14: this project's values become the
    defaults, every project that has not overridden them follows, and this
    project now follows the defaults too."""
    a = project(tmp_path, "a", home)
    b = project(tmp_path, "b", home)
    c = project(tmp_path, "c", home)
    pl.save_project_settings(c, "typhon.classify", {"model": "cats"})
    pl.save_project_settings(a, "typhon.classify",
                             {"model": "birds", "rate": 3})
    base, after = pl.make_default(a, home, BUILTINS, "typhon.classify",
                                  ["model"])
    assert base.settings["typhon.classify"] == {"model": "birds"}
    assert after.settings["typhon.classify"] == {"rate": 3}
    base = pl.read_base_set(home, BUILTINS)
    eff = {name: pl.effective_settings(base, pl.read_project(folder),
                                       "typhon.classify")
           for name, folder in (("a", a), ("b", b), ("c", c))}
    assert eff == {"a": {"model": "birds", "rate": 3},
                   "b": {"model": "birds"},
                   "c": {"model": "cats"}}
    with pytest.raises(pl.PluginListError, match="no settings"):
        pl.make_default(b, home, BUILTINS, "typhon.classify")


@pytest.mark.parametrize("values", [
    {"x": float("nan")}, {"x": float("inf")}, {"x": [1, 2]}, {"x": {"y": 1}},
    {"x": None}, {"x": 2 ** 63}, {"": 1}, {"k" * 65: 1},
    {"x": "t" * (pl.MAX_TEXT_VALUE + 1)}, ["x"],
])
def test_only_plain_values_are_kept(tmp_path, home, values):
    folder = project(tmp_path, "p", home)
    kept = (folder / pl.PROJECT_FILE).read_bytes()
    with pytest.raises(pl.PluginListError):
        pl.save_project_settings(folder, "typhon.classify", values)
    assert (folder / pl.PROJECT_FILE).read_bytes() == kept


def test_plain_values_round_trip(tmp_path, home):
    folder = project(tmp_path, "p", home)
    values = {"on": True, "name": "Bird bath", "n": -(2 ** 63),
              "big": 2 ** 63 - 1, "x": 0.25}
    pl.save_project_settings(folder, "acme.thermal", values)
    assert pl.read_project(folder).settings["acme.thermal"] == values


# ======================================================================
# Files Typhon cannot trust are left as they are
# ======================================================================
@pytest.mark.parametrize("text", [
    "{not json", '{"format": "something-else", "format_version": 1}',
    '{"format": "typhon-plugins"}', "[]",
    '{"format": "typhon-plugins", "format_version": 1, "added": "x"}',
    '{"format": "typhon-plugins", "format_version": 1, '
    '"settings": {"a.b": {"x": [1]}}}',
])
def test_a_damaged_project_list_is_refused_and_left_alone(tmp_path, text):
    path = tmp_path / pl.PROJECT_FILE
    path.write_text(text, encoding="utf-8")
    with pytest.raises(pl.PluginListError) as caught:
        pl.read_project(tmp_path)
    assert caught.value.path == path
    with pytest.raises(pl.PluginListError):
        pl.switch_on(tmp_path, "acme.thermal")
    assert path.read_text(encoding="utf-8") == text


def test_a_list_from_a_newer_typhon_is_never_rewritten(tmp_path, home):
    newer = {"format": pl.BASE_SET_FORMAT, "format_version": 2,
             "plugins": [], "settings": {}, "what": "a later field"}
    path = home / pl.BASE_SET_FILE
    path.write_text(json.dumps(newer), encoding="utf-8")
    kept = path.read_bytes()
    with pytest.raises(pl.PluginListError) as caught:
        pl.read_base_set(home, BUILTINS)
    assert caught.value.newer
    with pytest.raises(pl.PluginListError):
        pl.set_base_plugins(home, BUILTINS, BUILTINS)
    assert path.read_bytes() == kept


# ======================================================================
# Many writers
# ======================================================================
def test_writers_on_many_threads_lose_nothing(tmp_path, home):
    """The design's research lost a block in 47 of 80 saves when two threads
    shared one temporary name: here every writer has its own, under one
    lock, and every key arrives."""
    folder = project(tmp_path, "p", home)
    errors = []

    def writer(n):
        try:
            for i in range(10):
                pl.save_project_settings(folder, f"acme.p{n}",
                                         {f"k{i}": i})
        except Exception as exc:                          # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
    settings = pl.read_project(folder).settings
    assert {pid: len(v) for pid, v in settings.items()} == \
        {f"acme.p{n}": 10 for n in range(8)}
    assert not [p for p in folder.iterdir() if p.name.endswith(".tmp")]


def test_a_second_process_waits_for_the_lock(tmp_path, home):
    """The lock is an OS lock on a sidecar file, so another window (another
    process) waits for it — here it gives up after its timeout and says so,
    changing nothing."""
    import subprocess

    folder = project(tmp_path, "p", home)
    kept = (folder / pl.PROJECT_FILE).read_bytes()
    with pl._locked(pl.project_path(folder)):
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "from council_core import plugin_lists as pl;"
            "pl.LOCK_SECONDS = 0.3\n"
            "try:\n"
            "    pl.save_project_settings(sys.argv[2], 'acme.x', {'a': 1})\n"
            "except pl.PluginListError as exc:\n"
            "    print('REFUSED', exc)\n")
        out = subprocess.run([sys.executable, "-c", code, str(ROOT),
                              str(folder)], capture_output=True, text=True,
                             timeout=60, env=dict(os.environ))
    assert "REFUSED" in out.stdout and "another window" in out.stdout, \
        (out.stdout, out.stderr)
    assert (folder / pl.PROJECT_FILE).read_bytes() == kept


# ======================================================================
# A "Typhon (plugins)" project gets its list when it is made
# ======================================================================
def test_building_typhon_plugins_writes_the_projects_list(tmp_path, home):
    import gui_examples as gx
    import run_example_gui as rex

    pl.set_base_plugins(home, BUILTINS, BUILTINS + ["acme.thermal"])
    pdir = rex.build("typhon_plugins", project="tp", vault_dir=tmp_path / "v",
                     target="qt")
    listed = pl.read_project(pdir)
    assert listed is not None
    assert listed.base_set == BUILTINS + ["acme.thermal"]
    assert pl.expand(listed.plugin_home).resolve() == home.resolve()
    assert gx.keeps_plugin_list("typhon_plugins")
    assert not gx.keeps_plugin_list("typhon")
    today = rex.build("typhon", project="today", vault_dir=tmp_path / "v",
                      target="qt")
    assert not (today / pl.PROJECT_FILE).exists(), "today's Typhon: as before"


def test_typhon_plugins_is_never_made_with_a_plugin_home_under_council(
        tmp_path, fake_user, monkeypatch):
    """Built with TYPHON_PLUGIN_HOME unset, its project would record
    ~/.council/typhon, where the plugin Typhon never writes while it runs
    beside today's Typhon. Refused before anything is written."""
    from council_core import designer_examples as dx

    monkeypatch.delenv(pl.HOME_ENV, raising=False)
    vault = tmp_path / "side_vault"
    with pytest.raises(dx.ExampleError, match="plugin home"):
        dx.build_project("typhon_plugins", "tp", vault, toolkit="qt")
    assert not any(vault.rglob("*")) if vault.exists() else True
    answers = dx.ExampleAnswers(example="typhon_plugins", project="tp",
                                toolkit="qt", python="")
    assert "plugin home" in dx.problem(answers, vault)
    assert not (fake_user / ".council").exists()
    monkeypatch.setenv(pl.HOME_ENV, str(tmp_path / "side_home"))
    assert dx.problem(answers, vault) == ""
    assert dx.problem(dx.ExampleAnswers(example="typhon", project="t",
                                        toolkit="qt", python=""),
                      vault) == "", "today's Typhon: as before"
