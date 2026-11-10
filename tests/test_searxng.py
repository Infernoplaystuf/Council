"""council_core.searxng: Settings › Online › "Set up SearXNG" - docker is
never really run here; its calls are recorded."""
from __future__ import annotations

import subprocess

import pytest

from council_core import searxng
from council_core import web


@pytest.fixture(autouse=True)
def switch():
    try:
        web.settings_path().unlink()
    except FileNotFoundError:
        pass
    yield
    try:
        web.settings_path().unlink()
    except FileNotFoundError:
        pass


class FakeDocker:
    def __init__(self, state="", running=True):
        self.calls = []
        self.state, self.running = state, running

    def __call__(self, args, timeout=60):
        self.calls.append(args[1:])
        out, code = "", 0
        if args[1] == "info":
            code = 0 if self.running else 1
        elif args[1] == "ps":
            out = self.state + "\n" if self.state else ""
        return subprocess.CompletedProcess(args, code, out, "")


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(searxng.shutil, "which", lambda _n: "docker")
    monkeypatch.setattr(searxng, "_run", fake)
    monkeypatch.setattr(searxng, "answers", lambda *a, **k: True)
    return fake


def test_setup_needs_the_switch(docker):
    with pytest.raises(web.Offline):
        searxng.setup()
    assert docker.calls == []


def test_setup_without_docker_says_so(monkeypatch):
    web.set_online(True)
    monkeypatch.setattr(searxng.shutil, "which", lambda _n: None)
    with pytest.raises(searxng.SearxngError, match="Docker Desktop"):
        searxng.setup()


def test_setup_with_docker_stopped_says_so(docker):
    web.set_online(True)
    docker.running = False
    with pytest.raises(searxng.SearxngError, match="not running"):
        searxng.setup()


def test_setup_runs_the_pinned_image_on_loopback_and_searches_with_it(docker):
    web.set_online(True)
    said = []
    s = searxng.setup(said.append)
    run = [c for c in docker.calls if c[0] == "run"][0]
    assert searxng.IMAGE in run and ":latest" not in searxng.IMAGE
    assert f"127.0.0.1:{searxng.PORT}:8080" in run      # never on the network
    assert (s.search, s.searxng_url) == ("searxng", web.DEFAULT_SEARXNG)
    assert web.load().search == "searxng"
    yml = (searxng.config_dir() / "settings.yml").read_text(encoding="utf-8")
    assert "- json" in yml and "limiter: false" in yml
    assert said[-1].startswith("SearXNG is running")


def test_a_container_already_made_is_started_not_made_again(docker):
    web.set_online(True)
    docker.state = "exited"
    searxng.setup()
    assert ["start", searxng.CONTAINER] in docker.calls
    assert not any(c[0] == "run" for c in docker.calls)


def test_the_config_keeps_its_secret_key(tmp_path):
    first = searxng.write_config(tmp_path).read_text(encoding="utf-8")
    again = searxng.write_config(tmp_path).read_text(encoding="utf-8")
    assert first == again and "secret_key" in first


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_the_settings_tab_shows_setup_for_searxng_only(qapp):
    from council_qt.tabs.settings import SettingsActions, SettingsTab

    class Stub(SettingsActions):
        """No vault: the Settings tab keeps only the app folder, which the
        test session already points somewhere throwaway."""
    tab = SettingsTab(actions=Stub())
    tab.show()
    tab.engine_box.setCurrentIndex(tab.engine_box.findData("searxng"))
    assert tab.searxng_row.isVisible()
    tab.engine_box.setCurrentIndex(tab.engine_box.findData("duckduckgo"))
    assert not tab.searxng_row.isVisible()
    tab.close()
