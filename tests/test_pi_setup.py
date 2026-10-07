"""council_core/pi_setup/setup.py — the two set-up paths end to end, with the
loopback SSH "Pi" from test_pi_remote and no real disk."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("paramiko")

from council_core.pi_setup import disks as dk  # noqa: E402
from council_core.pi_setup import firstboot as fb  # noqa: E402
from council_core.pi_setup import remote  # noqa: E402
from council_core.pi_setup import setup as su  # noqa: E402

from tests.test_pi_remote import FakePi  # noqa: E402
from tests.test_pi_disks import BLANK_CARD, SD_CARD  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_PI_STATE_DIR", str(tmp_path / "state"))
    vault = tmp_path / "vault"
    vault.mkdir()
    return {"vault": vault, "keys": tmp_path / "keys", "tmp": tmp_path}


@pytest.fixture
def pi():
    p = FakePi()
    yield p
    p.close()


def card(raw=None):
    """An eligible card with nothing on it (see test_pi_disks.BLANK_CARD);
    SD_CARD is an old Pi card, which holds files."""
    return dk.judge(dk.parse([raw or BLANK_CARD])[0], [])


def cfg():
    return fb.FirstBoot(hostname="council-pi-2", username="council",
                        password="correct-horse-42", wifi_ssid="Home",
                        wifi_password="wifi-pass-123", wifi_country="US")


def test_find_by_host_key_picks_only_the_matching_host():
    keys = {"10.0.0.5": "ssh-ed25519 OTHER", "10.0.0.9": "ssh-ed25519 MINE",
            "10.0.0.7": None}
    got = su.find_by_host_key("ssh-ed25519 MINE council", "council-pi-2",
                              candidates=lambda: list(keys),
                              key_of=lambda ip, port: keys[ip])
    assert got == "10.0.0.9"
    assert su.find_by_host_key("ssh-ed25519 NOPE", "x", candidates=lambda: list(keys),
                               key_of=lambda ip, port: keys[ip]) is None


def test_server_key_line_reads_the_real_key(pi):
    assert su.server_key_line("127.0.0.1", pi.port) == " ".join(pi.host_public.split()[:2])


def test_prepare_refuses_without_the_exact_confirm_code(env, tmp_path):
    with pytest.raises(ValueError, match="type exactly"):
        su.prepare_new_pi(disk=card(), typed_confirm="yes", image=tmp_path / "x.img",
                          init_format=fb.CLOUDINIT, extract_sha256="", extract_size=0,
                          cfg=cfg(), model="llama3.2:3b", key_dir=env["keys"])
    assert not (env["tmp"] / "state").exists()


def test_prepare_refuses_a_non_us_model(env, tmp_path):
    c = card()
    with pytest.raises(ValueError, match="US-origin"):
        su.prepare_new_pi(disk=c, typed_confirm=c.confirm_code, image=tmp_path / "x.img",
                          init_format=fb.CLOUDINIT, extract_sha256="", extract_size=0,
                          cfg=cfg(), model="qwen2.5:3b", key_dir=env["keys"])


def test_prepare_writes_a_job_without_secrets(env, tmp_path):
    c = card()
    conf = cfg()
    out = su.prepare_new_pi(disk=c, typed_confirm=c.confirm_code, image=tmp_path / "x.img",
                            init_format=fb.CLOUDINIT, extract_sha256="ab" * 32,
                            extract_size=10, cfg=conf, model="llama3.2:3b",
                            key_dir=env["keys"])
    job = out["job"].read_text(encoding="utf-8")
    assert "correct-horse-42" not in job and "wifi-pass-123" not in job
    assert conf.password == "" and conf.wifi_password == "" and conf.host_key_private == ""
    data = json.loads(job)
    assert data["disk"] == c.identity().to_json()
    ud = data["firstboot_files"]["user-data"]
    assert "ssh_keys:" in ud and out["pending"].host_public.split()[1] in ud
    assert su.pending()[0].hostname == "council-pi-2"


def test_finish_new_pi_finds_by_key_and_needs_no_password(env, pi):
    # The card put the Council's key and the host key on the Pi.
    from council_core.pi_setup import pi_secrets
    _priv, pub = pi_secrets.council_key(env["keys"])
    pi.authorized.add(" ".join(pub.split()[:2]))
    item = su.Pending(id="j1", hostname="council-pi-2", username="council",
                      host_public=pi.host_public, model="llama3.2:3b")
    su._save_pending([item])
    out = su.finish_new_pi(env["vault"], item, key_dir=env["keys"], port=pi.port,
                           finder=lambda pub_, name: "127.0.0.1", timeout_s=5)
    assert out.ok, out.message
    assert "password" not in pi.logins
    node = json.loads((env["vault"] / "node_registry.json").read_text())["nodes"][0]
    assert node["password"] == "" and node["auth_method"] == "key"
    assert su.pending() == []


def test_setup_existing_asks_about_the_key_then_completes(env, pi):
    with pytest.raises(remote.HostKeyUnknown) as exc:
        su.setup_existing(env["vault"], host="127.0.0.1", username="council",
                          password=pi.password, name="pi-a", port=pi.port, key_dir=env["keys"])
    out = su.setup_existing(env["vault"], host="127.0.0.1", username="council",
                            password=pi.password, name="pi-a", port=pi.port,
                            key_dir=env["keys"], approved_fingerprint=exc.value.fingerprint)
    assert out.ok and out.model == "llama3.1:8b"              # 7.7 GB Pi 5
    assert pi.logins.count("password") == 1


def test_setup_existing_reports_the_failing_step(env, pi):
    pi.fail_on = "install -y nftables"
    out = su.setup_existing(env["vault"], host="127.0.0.1", username="council",
                            password=pi.password, name="pi-a", port=pi.port,
                            key_dir=env["keys"],
                            approved_fingerprint=remote.fingerprint(pi.host_key))
    assert not out.ok and "firewall tool" in out.message
    assert not (env["vault"] / "node_registry.json").exists()


def test_secure_existing_node_clears_the_stored_password(env, pi, monkeypatch):
    from council_core import apothecary as apoth
    reg = apoth._ae.NodeRegistry(str(apoth.registry_path(env["vault"])))
    reg.upsert(apoth.NodeEntry(name="NodePrimus", host="127.0.0.1", port=pi.port,
                               username="council", password=pi.password, model="llama3.2:1b"))
    monkeypatch.setattr(remote, "this_pc_ip_toward", lambda h: "192.168.1.50")
    out = su.secure_existing_node(env["vault"], "NodePrimus", key_dir=env["keys"],
                                  approved_fingerprint=remote.fingerprint(pi.host_key))
    assert out.ok and "192.168.1.50" in out.message
    saved = json.loads((env["vault"] / "node_registry.json").read_text())["nodes"][0]
    assert saved["password"] == "" and saved["auth_method"] == "key"
    assert any("ip saddr 192.168.1.50 accept" in c for c in pi.commands)


def test_wait_for_pi_gives_a_useful_timeout():
    with pytest.raises(TimeoutError, match="Ethernet cable"):
        su.wait_for_pi("ssh-ed25519 X", "p", timeout_s=0.05, poll_s=0.01,
                       finder=lambda *_: None)


# ── the user's decisions of 2026-10-07 ────────────────────────────────────
def test_setup_leaves_the_model_for_the_users_click(env, pi):
    out = su.setup_existing(env["vault"], host="127.0.0.1", username="council",
                            password=pi.password, name="pi-a", port=pi.port,
                            key_dir=env["keys"],
                            approved_fingerprint=remote.fingerprint(pi.host_key))
    assert out.ok and out.model_pending and out.username == "council"
    assert not any("ollama pull" in c for c in pi.commands)
    assert "llama3.1:8b (Meta, US) — about 4.9 GB" in out.message
    assert "Download on the Pi" in out.message
    got = su.download_model(host="127.0.0.1", username="council", model=out.model,
                            port=pi.port, key_dir=env["keys"])
    assert got.ok and pi.commands[-1] == "ollama pull llama3.1:8b"
    refused = su.download_model(host="127.0.0.1", username="council", model="qwen2.5:3b",
                                port=pi.port, key_dir=env["keys"])
    assert not refused.ok and "US-origin" in refused.message
    assert pi.commands[-1] == "ollama pull llama3.1:8b"


def test_setup_turns_password_login_off_and_says_how_to_undo_it(env, pi):
    out = su.setup_existing(env["vault"], host="127.0.0.1", username="council",
                            password=pi.password, name="pi-a", port=pi.port,
                            key_dir=env["keys"],
                            approved_fingerprint=remote.fingerprint(pi.host_key))
    assert out.ok and not pi.password_login
    assert remote.KEYS_ONLY_FILE in out.message and "systemctl reload ssh" in out.message


def test_switch_to_key_login_also_turns_passwords_off(env, pi, monkeypatch):
    from council_core import apothecary as apoth
    reg = apoth._ae.NodeRegistry(str(apoth.registry_path(env["vault"])))
    reg.upsert(apoth.NodeEntry(name="NodePrimus", host="127.0.0.1", port=pi.port,
                               username="council", password=pi.password, model="llama3.2:1b"))
    monkeypatch.setattr(remote, "this_pc_ip_toward", lambda h: "192.168.1.50")
    out = su.secure_existing_node(env["vault"], "NodePrimus", key_dir=env["keys"],
                                  approved_fingerprint=remote.fingerprint(pi.host_key))
    assert out.ok and not pi.password_login
    assert remote.KEYS_ONLY_FILE in out.message


def test_a_card_that_holds_files_needs_its_own_confirmation(env, tmp_path):
    old = card(SD_CARD)
    assert old.eligible and old.holds_files
    kw = dict(disk=old, typed_confirm=old.confirm_code, image=tmp_path / "x.img",
              init_format=fb.CLOUDINIT, extract_sha256="", extract_size=0, cfg=cfg(),
              model="llama3.2:3b", key_dir=env["keys"])
    with pytest.raises(ValueError, match="holds files.*bootfs"):
        su.prepare_new_pi(**kw)
    assert not (env["tmp"] / "state").exists()
    out = su.prepare_new_pi(**{**kw, "cfg": cfg()}, files_confirmed=True)
    assert json.loads(out["job"].read_text(encoding="utf-8"))["files_confirmed"] is True


def test_an_image_of_unknown_format_carries_both_and_the_card_decides(env, tmp_path):
    c = card()
    out = su.prepare_new_pi(disk=c, typed_confirm=c.confirm_code, image=tmp_path / "x.img",
                            init_format="", extract_sha256="", extract_size=0, cfg=cfg(),
                            model="llama3.2:3b", key_dir=env["keys"])
    job = json.loads(out["job"].read_text(encoding="utf-8"))
    assert job["init_format"] == "" and job["firstboot_files"] == {}
    assert set(job["firstboot_by_format"]) == {fb.CLOUDINIT, fb.SYSTEMD}
    assert out["pending"].host_public          # in case the card is cloud-init
    # The helper found a legacy (systemd) card: no pre-made host key on it.
    su.note_written(out["pending"].id, fb.SYSTEMD)
    item = su.pending()[0]
    assert item.host_public == ""
    with pytest.raises(ValueError, match="legacy image"):
        su.finish_new_pi(env["vault"], item, key_dir=env["keys"], timeout_s=0.01)
