"""
"Add a PC" (council_core.node_setup, council_qt.widgets.add_pc) and the
machine list's shared-work model (council_core.node_routing).

Pinned: the setup file opens Ollama to THIS PC's address only and nothing
the user typed can become a command in it; only US-origin models are
offered, downloaded or used for shared work; the test asks /api/version and
/api/tags only; a download happens only when called for one model and a
removal only after the user confirms; Connections shares its reading with
Machines & roles' machines that have a shared-work model.
"""
from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

from council_core import model_slots, node_routing as nr, node_setup as ns  # noqa: E402
from tests.fake_ollama import FakeOllama  # noqa: E402
from tests.fake_ollama import tag as _fake_tag  # noqa: E402


def tag(name):
    return _fake_tag(name, size=2_000_000_000, family="llama", params="3B",
                     quant="Q4_K_M")


# ── addresses ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("typed, url", [
    ("192.168.1.31", "http://192.168.1.31:11434"),
    (" http://192.168.1.31:11434/ ", "http://192.168.1.31:11434"),
    ("192.168.1.31:8080", "http://192.168.1.31:8080"),
    ("gaming-pc", "http://gaming-pc:11434"),
])
def test_url_for_accepts_an_address(typed, url):
    assert ns.url_for(typed) == url


@pytest.mark.parametrize("typed", [
    "", "localhost", "127.0.0.1", "http://127.0.0.1:11434", "a b",
    "1.2.3.4 & del x", "ftp://192.168.1.3", "gaming_pc;rm",
])
def test_url_for_refuses_this_pc_and_nonsense(typed):
    with pytest.raises(ns.SetupError):
        ns.url_for(typed)


def test_this_pc_addresses_skip_loopback_and_no_network():
    got = ns.this_pc_addresses(lambda _n: [
        "127.0.0.1", "169.254.3.4", "8.8.4.4", "192.168.1.20", "192.168.1.20",
        "10.0.0.5"])
    assert got == ["10.0.0.5", "192.168.1.20", "8.8.4.4"]


def test_route_address_of_nothing_is_empty():
    assert ns.route_address("") == ""


# ── the setup file ────────────────────────────────────────────────────────
def test_the_setup_file_opens_ollama_to_this_pc_only():
    text = ns.setup_script("gaming-pc", "192.168.1.20", ["gemma3:12b"],
                           today=date(2026, 11, 13))
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
    assert 'setx OLLAMA_HOST "0.0.0.0:11434" /M' in text
    rule = [l for l in text.splitlines() if "firewall add rule" in l]
    assert rule == ['netsh advfirewall firewall add rule name="Ollama for the '
                    'Council" dir=in action=allow protocol=TCP localport=11434 '
                    'remoteip=192.168.1.20 profile=any >nul']
    assert "net session" in text                  # administrator check
    # a download only after a yes, one model at a time
    assert 'choice /M "Download gemma3:12b now' in text
    assert 'if not errorlevel 2 "%OLLAMA_EXE%" pull gemma3:12b' in text
    assert "2026-11-13" in text and "ipconfig" in text


def test_without_models_it_downloads_nothing():
    text = ns.setup_script("pc2", "10.0.0.5", [])
    assert "%OLLAMA_EXE%\" pull" not in text and "choice" not in text


@pytest.mark.parametrize("name, ip, models", [
    ("a&b", "192.168.1.20", []),
    ("", "192.168.1.20", []),
    ("pc", "192.168.1.20 & del C:\\x", []),
    ("pc", "127.0.0.1", []),
    ("pc", "fe80::1", []),
    ("pc", "192.168.1.20", ["gemma3:12b & del x"]),
    ("pc", "192.168.1.20", ["qwen2.5:7b"]),        # not US-origin
    ("pc", "192.168.1.20", ["deepseek-r1:8b"]),
])
def test_nothing_typed_becomes_a_command(name, ip, models):
    with pytest.raises(ns.SetupError):
        ns.setup_script(name, ip, models)


def test_the_file_is_written_atomically_into_the_vault(tmp_path):
    path = ns.setup_path(tmp_path, "gaming pc/2")
    assert path.parent == tmp_path / ns.SETUP_DIR
    assert path.name == "council_node_setup_gaming_pc_2.bat"
    out = ns.write_setup_script(path, ns.setup_script("pc", "10.0.0.5", []))
    assert out.read_bytes().count(b"\r\n") > 10
    assert not list(path.parent.glob("*.tmp"))


# ── which models the other PC needs ───────────────────────────────────────
def test_wanted_models_are_the_bound_roles_and_the_shared_model():
    routing = nr.Routing(True, {"pc2": nr.Node("pc2", "http://10.0.0.5:11434",
                                               True, 1, "llama3.2:3b")},
                         {"skeptic": nr.Binding("pc2"), "sage": nr.Binding("pc2"),
                          "judge": nr.Binding("pc2"), "artist": nr.Binding("pc2")})
    labels = {"skeptic": "ollama:gemma3:12b", "sage": "ollama:gemma3:12b",
              "judge": "C:/models/phi-4.gguf",            # stays on this PC
              "artist": "ollama:qwen2.5:7b"}              # not US-origin
    assert ns.wanted_models("pc2", routing, labels.get) == [
        "gemma3:12b", "llama3.2:3b"]


def test_role_model_label_reads_model_slots(tmp_path):
    cfg = model_slots.SlotConfig(
        slots={model_slots.MAIN: model_slots.Slot(model_slots.MAIN),
               "s1": model_slots.Slot("s1", "ollama:gemma3:12b")},
        roles={"skeptic": "s1"})
    model_slots.save(tmp_path, cfg)
    label = ns.role_model_label(tmp_path)
    assert label("skeptic") == "ollama:gemma3:12b"
    assert label("writer") == ""


# ── the test ──────────────────────────────────────────────────────────────
def test_probe_asks_version_and_tags_only():
    with FakeOllama(tags=[tag("gemma3:12b"), tag("qwen2.5:7b")]) as srv:
        found = ns.probe(srv.url)
        paths = [p for _m, p, _b in srv.state.requests]
    assert found.reachable and found.version == "0.35.0-fake"
    assert found.models == ["gemma3:12b", "qwen2.5:7b"]
    assert paths == ["/api/version", "/api/tags"]
    lines = ns.check(found, ["gemma3:12b", "llama3.1:8b"])
    assert lines[0].startswith("✓")
    assert "US-origin models there: gemma3:12b" in lines[1]
    assert "Not installed there yet: llama3.1:8b" in lines[2]
    assert ns.check(found, ["gemma3:12b"])[-1] == "✓ It has every model its roles need."


def test_probe_of_nothing_says_what_to_do():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    found = ns.probe(f"http://127.0.0.1:{port}", timeout=2)
    assert not found.reachable
    assert "Ollama is not running there" in found.error
    assert ns.check(found)[0].startswith("✗")


# ── looking after its models from here ────────────────────────────────────
class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_pull_streams_progress_and_needs_success():
    sent = []

    def request(url, path, method, body, timeout):
        sent.append((url, path, method, body))
        return _Resp(b"\n".join(json.dumps(m).encode() for m in [
            {"status": "pulling manifest"},
            {"status": "downloading", "total": 100, "completed": 50},
            {"status": "success"}]))

    seen = []
    assert ns.pull_model("http://10.0.0.5:11434", "gemma3:12b",
                         on_progress=lambda s, f: seen.append((s, f)),
                         request=request) == "success"
    assert sent == [("http://10.0.0.5:11434", "/api/pull", "POST",
                     {"model": "gemma3:12b", "stream": True})]
    assert ("downloading", 0.5) in seen


def test_pull_refuses_a_non_us_model_before_asking():
    def request(*a, **k):
        raise AssertionError("no request for a refused model")
    with pytest.raises(ns.SetupError, match="not a US-origin"):
        ns.pull_model("http://10.0.0.5:11434", "qwen2.5:7b", request=request)


def test_pull_reports_the_other_pcs_error():
    def request(*a):
        return _Resp(json.dumps({"error": "no internet"}).encode())
    with pytest.raises(ns.SetupError, match="no internet"):
        ns.pull_model("http://10.0.0.5:11434", "gemma3:12b", request=request)


def test_remove_sends_one_delete():
    sent = []

    def request(url, path, method, body, timeout):
        sent.append((path, method, body))
        return _Resp(b"")
    ns.remove_model("http://10.0.0.5:11434", "qwen2.5:7b", request=request)
    assert sent == [("/api/delete", "DELETE", {"model": "qwen2.5:7b"})]
    with pytest.raises(ns.SetupError):
        ns.remove_model("http://10.0.0.5:11434", "x & y", request=request)


# ── the machine list's shared-work model ──────────────────────────────────
def test_shared_model_round_trips_and_must_be_us(tmp_path):
    r = nr.Routing(True, {"pc2": nr.Node("pc2", "http://10.0.0.5:11434", True, 2,
                                         "gemma3:12b"),
                          "pc3": nr.Node("pc3", "http://10.0.0.6:11434", True)})
    nr.save(tmp_path, r)
    data = json.loads(nr.path_for(tmp_path).read_text())
    assert data["nodes"]["pc2"]["model"] == "gemma3:12b"
    assert "model" not in data["nodes"]["pc3"]           # old files unchanged
    back = nr.load(tmp_path)
    assert back.nodes["pc2"].model == "gemma3:12b"
    assert [n.name for n in nr.sharing_nodes(back)] == ["pc2"]
    data["nodes"]["pc2"]["model"] = "qwen2.5:7b"
    with pytest.raises(nr.RoutingError, match="not a US-origin"):
        nr.parse(data)
    assert nr.sharing_nodes(nr.Routing(False, back.nodes)) == []


# ── the windows ───────────────────────────────────────────────────────────
pytest.importorskip("PySide6", reason="the Qt shell needs PySide6")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _settle(qapp, until, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.01)
    return False


def test_add_a_pc_saves_the_file_tests_and_adds(qapp, tmp_path):
    from council_qt.widgets.add_pc import AddPcDialog
    from council_qt.widgets.machine_routing import MachineRoutingDialog
    with FakeOllama(tags=[tag("gemma3:12b")]) as srv:
        asked = []

        def probe(url):
            asked.append(url)
            return ns.probe(srv.url)          # the "other PC" is the fake

        d = MachineRoutingDialog(vault_dir=tmp_path)
        dlg = AddPcDialog(d, vault_dir=tmp_path, models=["gemma3:12b"],
                          addresses=["192.168.1.20"], probe=probe)
        try:
            dlg.name_edit.setText("gaming-pc")
            path = dlg.save_setup()
            assert path == ns.setup_path(tmp_path, "gaming-pc") and path.exists()
            assert "remoteip=192.168.1.20" in path.read_text()
            dlg.address_edit.setText("192.168.1.31")
            dlg.test()
            assert _settle(qapp, lambda: dlg._probed is not None)
            assert asked == ["http://192.168.1.31:11434"]
            assert "✓ It has every model" in dlg.status.text()
            dlg.add()
            node = d.add_pc(dialog=dlg)
            assert (node.name, node.url, node.enabled, node.model) == (
                "gaming-pc", "http://192.168.1.31:11434", True, "gemma3:12b")
            assert d.machine_names() == ["gaming-pc"]
            assert d.save()
            saved = nr.load(tmp_path)
            assert saved.nodes["gaming-pc"].model == "gemma3:12b"
            assert not saved.routing_enabled        # the master switch is the user's
        finally:
            dlg.deleteLater()
            d.deleteLater()


def test_an_untested_pc_is_added_disabled(qapp, tmp_path):
    from council_qt.widgets.add_pc import AddPcDialog
    dlg = AddPcDialog(vault_dir=tmp_path, addresses=["192.168.1.20"])
    try:
        dlg.name_edit.setText("pc2")
        dlg.address_edit.setText("192.168.1.32")
        node = dlg.add()
        assert node is not None and not node.enabled and node.model == ""
    finally:
        dlg.deleteLater()


def test_a_non_us_shared_model_is_not_saved(qapp, tmp_path):
    from PySide6.QtWidgets import QTableWidgetItem
    from council_qt.widgets.machine_routing import MachineRoutingDialog
    d = MachineRoutingDialog(vault_dir=tmp_path)
    try:
        d._add_machine(nr.Node("pc2", "http://10.0.0.5:11434", True, 1))
        d.machines.setItem(0, 4, QTableWidgetItem("qwen2.5:7b"))
        assert not d.save()
        assert "not a US-origin" in d.status.text()
        assert not nr.path_for(tmp_path).exists()
    finally:
        d.deleteLater()


def test_models_window_lists_and_removes_only_after_yes(qapp, tmp_path):
    from council_qt.widgets.add_pc import MachineModelsDialog
    with FakeOllama(tags=[tag("gemma3:12b"), tag("qwen2.5:7b")]) as srv:
        removed = []
        dlg = MachineModelsDialog(name="pc2", url="http://10.0.0.5:11434",
                                  probe=lambda _u: ns.probe(srv.url),
                                  remove=lambda u, n: removed.append((u, n)))
        try:
            assert _settle(qapp, lambda: dlg.list.count() == 2)
            texts = [dlg.list.item(i).text() for i in range(2)]
            assert texts == ["gemma3:12b", "qwen2.5:7b   (not US-origin)"]
            dlg.list.setCurrentRow(1)
            assert not dlg.remove_selected(confirm=lambda _t: False)
            assert removed == []
            assert dlg.remove_selected(confirm=lambda _t: True)
            assert removed == [("http://10.0.0.5:11434", "qwen2.5:7b")]
            dlg.pull_edit.setText("deepseek-r1:8b")
            dlg.download()
            assert "not a US-origin" in dlg.status.text()
        finally:
            dlg._stop.set()
            _settle(qapp, lambda: False, 0.2)
            dlg.deleteLater()


def test_connections_shares_with_machines_that_have_a_shared_model(tmp_path, monkeypatch):
    monkeypatch.delenv("COUNCIL_REMOTE_NODES", raising=False)
    from council_qt.tabs.connections import ConnectionsActions
    nr.save(tmp_path, nr.Routing(True, {
        "pc2": nr.Node("pc2", "http://10.0.0.5:11434", True, 1, "gemma3:12b"),
        "pc3": nr.Node("pc3", "http://10.0.0.6:11434", True, 1),           # no model
        "pc4": nr.Node("pc4", "http://10.0.0.7:11434", False, 1, "llama3.2:3b")}))
    acts = ConnectionsActions(vault_dir=tmp_path)
    assert acts.pi_workers() == [("pc2", "http://10.0.0.5:11434", "gemma3:12b")]
    # routing off: nothing is shared
    r = nr.load(tmp_path)
    r.routing_enabled = False
    nr.save(tmp_path, r)
    assert acts.pi_workers() == []
