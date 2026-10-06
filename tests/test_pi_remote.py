"""council_core/pi_setup/remote.py against a real SSH server — paramiko's own
server, on 127.0.0.1, in this process — standing in for a Pi. It accepts the
password once, then only the key the Council installs; it answers the facts
command like a Pi 5 and records every command it is sent. No real Pi, no
network beyond loopback."""
from __future__ import annotations

import io
import json
import re
import socket
import threading

import pytest

paramiko = pytest.importorskip("paramiko")

from council_core.pi_setup import firstboot as fb  # noqa: E402
from council_core.pi_setup import pi_models  # noqa: E402
from council_core.pi_setup import pi_secrets  # noqa: E402
from council_core.pi_setup import remote  # noqa: E402

PI_FACTS = "council-pi-1\naarch64\n8046508\nDebian GNU/Linux 13 (trixie)\nRaspberry Pi 5 Model B Rev 1.1\n"


class FakePi:
    def __init__(self, user="council", password="pw-123456"):
        pem, self.host_public = pi_secrets.host_keypair()
        self.host_key = paramiko.Ed25519Key.from_private_key(io.StringIO(pem))
        self.user, self.password = user, password
        self.authorized = set()
        self.commands = []
        self.fail_on = None
        self.logins = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.alive = True
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self):
        self.alive = False
        self.sock.close()

    def _serve(self):
        while self.alive:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def _session(self, conn):
        pi = self
        t = paramiko.Transport(conn)
        t.add_server_key(self.host_key)

        class Srv(paramiko.ServerInterface):
            def get_allowed_auths(self, username):
                return "password,publickey"

            def check_auth_password(self, username, password):
                ok = username == pi.user and password == pi.password
                if ok:
                    pi.logins.append("password")
                return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

            def check_auth_publickey(self, username, key):
                ok = username == pi.user and f"{key.get_name()} {key.get_base64()}" in pi.authorized
                if ok:
                    pi.logins.append("key")
                return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

            def check_channel_request(self, kind, chanid):
                return paramiko.OPEN_SUCCEEDED

            def check_channel_exec_request(self, channel, command):
                cmd = command.decode()
                pi.commands.append(cmd)
                threading.Thread(target=pi._answer, args=(channel, cmd), daemon=True).start()
                return True

        try:
            t.start_server(server=Srv())
        except Exception:
            return

    def _answer(self, channel, cmd):
        try:
            self._reply(channel, cmd)
        except (OSError, EOFError, paramiko.SSHException):
            pass          # the client hung up first — fine for a fake

    def _reply(self, channel, cmd):
        rc = 0
        if "authorized_keys" in cmd:
            m = re.search(r"(ssh-ed25519 \S+)", cmd)
            self.authorized.add(m.group(1))
        elif cmd == remote.FACTS_CMD:
            channel.sendall(PI_FACTS.encode())
        else:
            channel.sendall(b"ok\n")
            if self.fail_on and self.fail_on in cmd:
                channel.sendall_stderr(b"E: something broke\n")
                rc = 100
        channel.send_exit_status(rc)
        channel.close()


@pytest.fixture
def pi():
    p = FakePi()
    yield p
    p.close()


@pytest.fixture
def keys(tmp_path):
    return tmp_path / "keys"


def test_unknown_host_key_is_a_question(pi, keys):
    with pytest.raises(remote.HostKeyUnknown) as exc:
        remote.adopt("127.0.0.1", "council", pi.password, port=pi.port, key_dir=keys)
    assert exc.value.fingerprint == remote.fingerprint(pi.host_key)
    assert pi.authorized == set() and not remote.known_hosts_path(keys).exists()


def test_adopt_with_an_approved_fingerprint(pi, keys):
    fp = remote.fingerprint(pi.host_key)
    got = remote.adopt("127.0.0.1", "council", pi.password, port=pi.port,
                       key_dir=keys, approved_fingerprint=fp)
    assert pi.logins == ["password", "key"]          # password once, then key
    _priv, pub = pi_secrets.council_key(keys)
    assert " ".join(pub.split()[:2]) in pi.authorized
    assert got.fingerprint == fp
    assert (got.facts.arch, got.facts.ram_gb) == ("aarch64", 7.7)
    assert got.facts.model.startswith("Raspberry Pi 5")
    assert remote.known_hosts_path(keys).exists()


def test_a_pi_the_council_imaged_is_recognised_by_its_key(pi, keys):
    remote.adopt("127.0.0.1", "council", pi.password, port=pi.port, key_dir=keys,
                 expected_host_key=pi.host_public)
    assert pi.logins == ["password", "key"]


def test_a_changed_host_key_is_refused(pi, keys):
    fp = remote.fingerprint(pi.host_key)
    remote.adopt("127.0.0.1", "council", pi.password, port=pi.port, key_dir=keys,
                 approved_fingerprint=fp)
    pem, _ = pi_secrets.host_keypair()
    pi.host_key = paramiko.Ed25519Key.from_private_key(io.StringIO(pem))
    with pytest.raises(paramiko.BadHostKeyException):
        remote.connect("127.0.0.1", "council", port=pi.port, key_dir=keys)


def test_wrong_password_installs_nothing(pi, keys):
    with pytest.raises(paramiko.AuthenticationException):
        remote.adopt("127.0.0.1", "council", "wrong-password", port=pi.port, key_dir=keys,
                     approved_fingerprint=remote.fingerprint(pi.host_key))
    assert pi.authorized == set()


def test_provision_runs_firewall_before_ollama_listens(pi, keys):
    remote.adopt("127.0.0.1", "council", pi.password, port=pi.port, key_dir=keys,
                 expected_host_key=pi.host_public)
    pi.commands.clear()
    seen = []
    res = remote.provision("127.0.0.1", "council", "llama3.2:3b", port=pi.port,
                           pc_ip="192.168.1.50", key_dir=keys, on_step=seen.append)
    assert all(r.ok for r in res) and len(res) == 7
    fw = next(i for i, c in enumerate(pi.commands) if remote.FIREWALL_FILE in c)
    listen = next(i for i, c in enumerate(pi.commands) if "OLLAMA_HOST=0.0.0.0" in c)
    assert fw < listen
    assert "ip saddr 192.168.1.50 accept" in pi.commands[fw]
    assert any(c == "ollama pull llama3.2:3b" for c in pi.commands)
    assert pi.logins[-1] == "key"


def test_provision_stops_at_the_first_failure(pi, keys):
    remote.adopt("127.0.0.1", "council", pi.password, port=pi.port, key_dir=keys,
                 expected_host_key=pi.host_public)
    pi.fail_on = "apt-get install"
    res = remote.provision("127.0.0.1", "council", "llama3.2:3b", port=pi.port,
                           pc_ip="192.168.1.50", key_dir=keys)
    assert [r.ok for r in res] == [True, False]
    assert "something broke" in res[-1].output
    assert not any("ollama pull" in c for c in pi.commands)


@pytest.mark.parametrize("model", ["llama3.2:3b; rm -rf /", "$(reboot)", "", "A b"])
def test_model_names_cannot_inject_commands(model):
    with pytest.raises(ValueError):
        remote.provision_steps(model, "192.168.1.50")


def test_firewall_rules():
    r4 = remote.firewall_rules("192.168.1.50")
    assert "tcp dport 11434 ip saddr 192.168.1.50 accept" in r4 and "tcp dport 11434 drop" in r4
    assert "ip6 saddr fd00::5 accept" in remote.firewall_rules("fd00::5")
    with pytest.raises(ValueError):
        remote.firewall_rules("192.168.1.50; drop table")


def test_register_stores_no_password(tmp_path, keys):
    vault = tmp_path / "vault"
    vault.mkdir()
    node = remote.register(vault, name="council-pi-1", host="192.168.1.77",
                           username="council", model="llama3.2:3b",
                           facts_=remote.PiFacts(ram_gb=7.7, model="Raspberry Pi 5"),
                           key_dir=keys)
    data = json.loads((vault / "node_registry.json").read_text(encoding="utf-8"))
    saved = data["nodes"][0]
    assert saved["auth_method"] == "key" and saved["password"] == ""
    assert saved["key_path"].endswith("council_ed25519") and saved["ram_gb"] == 8
    assert node.model == "llama3.2:3b"


def test_firstboot_carries_the_pinned_host_key():
    import yaml
    pem, pub = pi_secrets.host_keypair()
    files = fb.build(fb.FirstBoot(hostname="council-pi-1", username="council",
                                  password="correct-horse-42",
                                  host_key_private=pem, host_key_public=pub), fb.CLOUDINIT)
    ud = yaml.safe_load(files["user-data"])
    assert ud["ssh_keys"]["ed25519_public"] == pub
    assert ud["ssh_keys"]["ed25519_private"].strip() == pem.strip()


@pytest.mark.parametrize("model, us", [("llama3.2:3b", True), ("gemma3:4b", True),
                                       ("granite3.3:2b", True), ("qwen2.5:3b", False),
                                       ("deepseek-r1:7b", False)])
def test_only_us_models(model, us):
    assert pi_models.is_us_origin(model) is us


def test_every_recommendation_is_us_origin():
    for ram in (2, 4, 8, 16):
        assert all(pi_models.is_us_origin(m) for m in pi_models.for_ram(ram))
    assert pi_models.for_ram(3.7)[0] == "llama3.2:3b"        # a "4 GB" Pi
