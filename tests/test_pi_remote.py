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


class _AckTransport(paramiko.Transport):
    """A server transport that says when it has ACKNOWLEDGED a channel
    request. paramiko sends the exec request's success reply only after
    check_channel_exec_request returns; a fake that answered (and closed the
    channel) from a thread started inside that callback could close it
    before the reply went out, and the client's exec_command then failed
    with 'Channel closed.' — MEASURED: 3-8 of these 30 tests failed per run.
    A real sshd acknowledges exec before any output, so the fake waits for
    the acknowledgement too."""

    def __init__(self, sock):
        super().__init__(sock)
        self._acks = {}
        self._acks_lock = threading.Lock()

    def ack_event(self, remote_chanid: int) -> threading.Event:
        with self._acks_lock:
            return self._acks.setdefault(remote_chanid, threading.Event())

    def _send_user_message(self, data):
        super()._send_user_message(data)
        raw = data.asbytes() if hasattr(data, "asbytes") else bytes(data)
        if raw[:1] == paramiko.common.cMSG_CHANNEL_SUCCESS and len(raw) >= 5:
            self.ack_event(int.from_bytes(raw[1:5], "big")).set()


class FakePi:
    def __init__(self, user="council", password="pw-123456"):
        pem, self.host_public = pi_secrets.host_keypair()
        self.host_key = paramiko.Ed25519Key.from_private_key(io.StringIO(pem))
        self.user, self.password = user, password
        self.authorized = set()
        self.commands = []
        self.fail_on = None
        self.logins = []
        #: sshd's PasswordAuthentication: the keys-only step turns it off,
        #: the "back on" command turns it on again.
        self.password_login = True
        #: Set to make every key login AFTER the keys-only step fail (a
        #: broken key setup the Council must not lock the user out with).
        self.break_keys_after_keys_only = False
        self._keys_only_done = False
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
        t = _AckTransport(conn)
        t.add_server_key(self.host_key)

        class Srv(paramiko.ServerInterface):
            def get_allowed_auths(self, username):
                return "password,publickey" if pi.password_login else "publickey"

            def check_auth_password(self, username, password):
                ok = (pi.password_login and username == pi.user
                      and password == pi.password)
                if ok:
                    pi.logins.append("password")
                return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

            def check_auth_publickey(self, username, key):
                ok = username == pi.user and f"{key.get_name()} {key.get_base64()}" in pi.authorized
                if pi.break_keys_after_keys_only and pi._keys_only_done:
                    ok = False
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
            # Only after the exec request has been acknowledged (see _AckTransport).
            if not channel.get_transport().ack_event(channel.remote_chanid).wait(10):
                return
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
        elif "PasswordAuthentication no" in cmd:          # keys_only_cmd
            self.password_login = False
            self._keys_only_done = True
            channel.sendall(b"keys-only\n")
        elif remote.KEYS_ONLY_FILE in cmd and "rm -f" in cmd:   # back on
            self.password_login = True
            channel.sendall(b"ok\n")
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
    res = remote.provision("127.0.0.1", "council", port=pi.port,
                           pc_ip="192.168.1.50", key_dir=keys, on_step=seen.append)
    assert all(r.ok for r in res) and len(res) == 10, [(r.label, r.output) for r in res]
    fw = next(i for i, c in enumerate(pi.commands) if remote.FIREWALL_FILE in c)
    listen = next(i for i, c in enumerate(pi.commands) if "OLLAMA_HOST=0.0.0.0" in c)
    assert fw < listen
    assert "ip saddr 192.168.1.50 accept" in pi.commands[fw]
    assert not any("ollama pull" in c for c in pi.commands)    # decision a
    assert pi.logins[-1] == "key"


def test_provision_stops_at_the_first_failure(pi, keys):
    remote.adopt("127.0.0.1", "council", pi.password, port=pi.port, key_dir=keys,
                 expected_host_key=pi.host_public)
    pi.fail_on = "install -y nftables"
    res = remote.provision("127.0.0.1", "council", port=pi.port,
                           pc_ip="192.168.1.50", key_dir=keys)
    assert [r.ok for r in res] == [True, True, False]    # scrub, preflight, nftables
    assert "something broke" in res[-1].output
    assert not any(remote.OLLAMA_PIN_SHA256 in c for c in pi.commands)
    assert pi.password_login


def test_provision_first_takes_the_first_boot_secrets_off_the_card(pi, keys):
    # user-data kept the Pi's SSH HOST PRIVATE key and the password hash on
    # the world-readable boot partition for good.
    remote.adopt("127.0.0.1", "council", pi.password, port=pi.port, key_dir=keys,
                 expected_host_key=pi.host_public)
    pi.commands.clear()
    res = remote.provision("127.0.0.1", "council", port=pi.port,
                           pc_ip="192.168.1.50", key_dir=keys)
    assert res[0].ok and res[0].label.startswith("Remove the Pi's first-boot secrets")
    assert pi.commands[0] == remote.scrub_firstboot_cmd()
    assert "/boot/firmware/user-data" in pi.commands[0]


def _git_bash():
    import shutil
    import sys
    b = shutil.which("bash")
    if not b or (sys.platform == "win32" and "system32" in b.lower()):
        pytest.skip("needs a POSIX bash (Git Bash), not WSL's launcher")
    return b


def test_the_scrub_rewrites_only_the_user_data_the_council_wrote(tmp_path):
    import subprocess
    bash = _git_bash()
    pem, pub = pi_secrets.host_keypair()
    files = fb.build(fb.FirstBoot(hostname="council-pi-1", username="council",
                                  password="correct-horse-42", wifi_ssid="Home",
                                  wifi_password="wifi-pass-123",
                                  host_key_private=pem, host_key_public=pub), fb.CLOUDINIT)
    assert remote.FIRSTBOOT_MARKER in files["user-data"]
    ours, theirs = tmp_path / "ours", tmp_path / "theirs"
    for d, user_data in ((ours, files["user-data"]),
                         (theirs, "#cloud-config\n# the user's own\npackages: [vim]\n")):
        d.mkdir()
        (d / "user-data").write_text(user_data, encoding="utf-8", newline="\n")
        (d / "meta-data").write_text(files["meta-data"], encoding="utf-8", newline="\n")
        (d / "network-config").write_text(files["network-config"], encoding="utf-8",
                                          newline="\n")
    cmd = remote.scrub_firstboot_cmd((ours.as_posix(), theirs.as_posix(),
                                      (tmp_path / "absent").as_posix()))
    out = subprocess.run([bash, "-c", 'sudo() { "$@"; }; sync() { :; }; ' + cmd],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    scrubbed = (ours / "user-data").read_text(encoding="utf-8")
    assert scrubbed.startswith("#cloud-config\n") and "PRIVATE KEY" not in scrubbed
    assert "$6$" not in scrubbed and "passwd" not in scrubbed
    assert all(ln.startswith("#") for ln in scrubbed.splitlines())
    assert (ours / "meta-data").read_text(encoding="utf-8") == files["meta-data"]
    assert (theirs / "user-data").read_text(encoding="utf-8").endswith("packages: [vim]\n")
    assert not list(tmp_path.rglob("*.council-tmp"))


@pytest.mark.parametrize("model", ["llama3.2:3b; rm -rf /", "$(reboot)", "", "A b"])
def test_model_names_cannot_inject_commands(model):
    with pytest.raises(ValueError):
        remote.pull_model_cmd(model)


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


# ── the user's decisions of 2026-10-07 ────────────────────────────────────
def _sh(script: str, *, env=None):
    import subprocess
    return subprocess.run([_git_bash(), "-c", script], capture_output=True, text=True,
                          timeout=120, env=env)


def _shell_path(p) -> str:
    return p.as_posix()


# b. Ollama: ONE pinned release, checked before it is installed
def test_the_ollama_pin_reads_consistently():
    assert re.fullmatch(r"[0-9a-f]{64}", remote.OLLAMA_PIN_SHA256)
    assert remote.OLLAMA_PIN_SHA256 == ("cb627d332b1fe5055bd5485ca10d595da8429e447648209e3"
                                        "75390ec3bd09374")
    assert remote.OLLAMA_PIN_URL == ("https://github.com/ollama/ollama/releases/download/"
                                     f"v{remote.OLLAMA_PIN_VERSION}/ollama-linux-arm64.tar.zst")
    assert remote.OLLAMA_PIN_SIZE == 1_550_231_393


def test_no_step_runs_an_install_script_from_the_internet():
    steps = remote.provision_steps("192.168.1.50")
    for _label, cmd, _t in steps:
        assert "install.sh" not in cmd and "ollama.com" not in cmd
        # nothing is piped into a shell ('| sha256sum' is fine)
        assert not re.search(r"\|\s*(sudo\s+)?(sh|bash)(\s|$)", cmd)
    install = next(c for label, c, _t in steps if label.startswith("Install Ollama"))
    assert remote.OLLAMA_PIN_SHA256 in install and remote.OLLAMA_PIN_URL in install
    assert str(remote.OLLAMA_PIN_SIZE) in install
    # the checks come before anything is installed
    first_change = install.index('sudo rm -rf "$P/lib/ollama"')
    assert install.index("stat -c %s") < install.index("sha256sum -c") < first_change
    assert first_change < install.index("tar --zstd")
    pre = next(c for label, c, _t in steps if label.startswith("Check the Pi"))
    assert "https://github.com" in pre and "/var/tmp" in pre and "zstd" in pre


def _install_env(tmp_path, payload: bytes):
    """Shims for a run of the REAL install script in Git Bash: curl copies a
    local file (nothing goes online), tar records its call and unpacks a fake
    bin/ollama, sudo and install just do it in the temporary prefix."""
    src = tmp_path / "asset.tar.zst"
    src.write_bytes(payload)
    log = tmp_path / "calls.log"
    prefix = tmp_path / "usr_local"
    (prefix / "lib" / "ollama").mkdir(parents=True)
    (prefix / "lib" / "ollama" / "old-runner").write_text("old", encoding="utf-8")
    tmp_base = tmp_path / "var_tmp"
    tmp_base.mkdir()
    shims = (
        "sudo() { \"$@\"; }\n"
        "install() { while [ \"${1#-}\" != \"$1\" ]; do shift; done; mkdir -p \"$@\"; }\n"
        f"curl() {{ echo curl >> {_shell_path(log)}; local o=''; while [ $# -gt 0 ]; do "
        "[ \"$1\" = -o ] && o=\"$2\"; shift; done; "
        f"cp {_shell_path(src)} \"$o\"; }}\n"
        f"tar() {{ echo tar >> {_shell_path(log)}; local d=''; while [ $# -gt 0 ]; do "
        "[ \"$1\" = -C ] && d=\"$2\"; shift; done; mkdir -p \"$d/bin\"; "
        "printf '#!/bin/sh\\necho \"ollama version is 0.35.0\"\\n' > \"$d/bin/ollama\"; "
        "chmod +x \"$d/bin/ollama\"; }\n")
    return shims, prefix, tmp_base, log


def _run_install(tmp_path, payload: bytes, *, sha=None, size=None):
    import hashlib
    shims, prefix, tmp_base, log = _install_env(tmp_path, payload)
    cmd = remote.ollama_install_cmd(
        prefix=_shell_path(prefix), tmp_base=_shell_path(tmp_base),
        sha256=sha or hashlib.sha256(payload).hexdigest(),
        size=len(payload) if size is None else size,
        unit=_shell_path(tmp_path / "ollama.service"))
    assert cmd.startswith("bash -c ")
    # The same script, run by this bash with the shims defined first.
    out = _sh(shims + "eval " + cmd[len("bash -c "):])
    calls = log.read_text(encoding="utf-8").split() if log.exists() else []
    return out, prefix, tmp_base, calls


def test_the_pinned_install_refuses_a_download_whose_checksum_differs(tmp_path):
    out, prefix, tmp_base, calls = _run_install(tmp_path, b"not the release" * 100,
                                                sha="0" * 64)
    assert out.returncode != 0 and "refusing to install" in out.stderr
    assert calls == ["curl"]                                  # tar never ran
    assert (prefix / "lib" / "ollama" / "old-runner").exists()   # nothing was touched
    assert not (prefix / "bin" / "ollama").exists()
    assert list(tmp_base.iterdir()) == []                     # the download is gone


def test_the_pinned_install_refuses_a_download_of_the_wrong_size(tmp_path):
    payload = b"x" * 4096
    out, prefix, _t, calls = _run_install(tmp_path, payload, size=len(payload) + 1)
    assert out.returncode != 0 and "refusing" in out.stderr and calls == ["curl"]
    assert (prefix / "lib" / "ollama" / "old-runner").exists()


def test_the_pinned_install_installs_a_matching_download(tmp_path):
    out, prefix, tmp_base, calls = _run_install(tmp_path, b"the pinned release" * 100)
    assert out.returncode == 0, out.stderr
    assert calls == ["curl", "tar"] and "SHA-256 checked" in out.stdout
    assert (prefix / "bin" / "ollama").exists()
    assert not (prefix / "lib" / "ollama" / "old-runner").exists()   # replaced, as upstream
    assert list(tmp_base.iterdir()) == []


def test_the_pinned_install_is_skipped_when_that_version_is_there(tmp_path):
    shims, prefix, tmp_base, log = _install_env(tmp_path, b"x")
    (prefix / "bin").mkdir()
    (prefix / "bin" / "ollama").write_text('#!/bin/sh\necho "ollama version is 0.35.0"\n',
                                           encoding="utf-8")
    unit = tmp_path / "ollama.service"
    unit.write_text("[Unit]\n", encoding="utf-8")
    cmd = remote.ollama_install_cmd(prefix=_shell_path(prefix), tmp_base=_shell_path(tmp_base),
                                    unit=_shell_path(unit))
    out = _sh(shims + "eval " + cmd[len("bash -c "):])
    assert out.returncode == 0 and "already installed" in out.stdout
    assert not log.exists()                                   # nothing downloaded


# a. no model download during setup; one on request
def test_setup_downloads_no_model_and_the_button_does(pi, keys):
    remote.adopt("127.0.0.1", "council", pi.password, port=pi.port, key_dir=keys,
                 expected_host_key=pi.host_public)
    remote.provision("127.0.0.1", "council", port=pi.port, pc_ip="192.168.1.50", key_dir=keys)
    assert not any("ollama pull" in c for c in pi.commands)
    res = remote.pull_model("127.0.0.1", "council", "llama3.2:3b", port=pi.port, key_dir=keys)
    assert res.ok and pi.commands[-1] == "ollama pull llama3.2:3b"
    with pytest.raises(ValueError, match="US-origin"):
        remote.pull_model("127.0.0.1", "council", "qwen2.5:3b", port=pi.port, key_dir=keys)
    assert pi.commands[-1] == "ollama pull llama3.2:3b"


def test_the_wizard_names_the_model_its_maker_and_its_size():
    assert pi_models.describe("llama3.2:3b") == (
        "llama3.2:3b (Meta, US) — about 2.0 GB to download on the Pi")
    assert pi_models.describe("gemma3:12b").startswith("gemma3:12b (Google, US) — about 8.1 GB")
    for ram in (2, 4, 8, 16):
        for m in pi_models.for_ram(ram):
            assert pi_models.download_size(m) and pi_models.maker(m)


# c. keys only, never a lock-out
def test_password_login_goes_off_once_the_key_works(pi, keys):
    remote.adopt("127.0.0.1", "council", pi.password, port=pi.port, key_dir=keys,
                 expected_host_key=pi.host_public)
    res = remote.provision("127.0.0.1", "council", port=pi.port, pc_ip="192.168.1.50",
                           key_dir=keys)
    assert res[-1].label.startswith("Turn SSH password login off") and res[-1].ok, res[-1]
    assert not pi.password_login
    assert remote.allowed_auths("127.0.0.1", pi.port, "council") == ["publickey"]
    with pytest.raises(paramiko.AuthenticationException):
        remote.connect("127.0.0.1", "council", password=pi.password, port=pi.port,
                       key_dir=keys)
    remote.connect("127.0.0.1", "council", port=pi.port, key_dir=keys).close()


def test_a_key_login_that_fails_afterwards_turns_passwords_back_on(pi, keys):
    remote.adopt("127.0.0.1", "council", pi.password, port=pi.port, key_dir=keys,
                 expected_host_key=pi.host_public)
    pi.break_keys_after_keys_only = True
    res = remote.provision("127.0.0.1", "council", port=pi.port, pc_ip="192.168.1.50",
                           key_dir=keys)
    assert not res[-1].ok and "turned back on" in res[-1].output
    assert pi.password_login
    assert any(remote.KEYS_ONLY_FILE in c and "rm -f" in c for c in pi.commands)


def test_the_keys_only_file_is_checked_before_sshd_reloads(tmp_path):
    # The real script, in Git Bash: 'sshd -t' refuses -> the drop-in is
    # removed and NOTHING reloads; accepts -> reload, then 'sshd -T' read.
    log = tmp_path / "calls.log"
    conf = tmp_path / "sshd_config.d" / "00-council-keys-only.conf"
    conf.parent.mkdir()
    for good in (False, True):
        fake_sshd = tmp_path / "sshd"
        fake_sshd.write_text(
            "#!/bin/sh\n"
            f"echo \"sshd $1\" >> {_shell_path(log)}\n"
            f"if [ \"$1\" = -t ]; then exit {0 if good else 1}; fi\n"
            "echo 'passwordauthentication no'\n", encoding="utf-8")
        shims = ("sudo() { \"$@\"; }\n"
                 f"systemctl() {{ echo \"systemctl $*\" >> {_shell_path(log)}; }}\n")
        cmd = remote.keys_only_cmd(path=_shell_path(conf), sshd=_shell_path(fake_sshd))
        out = _sh(shims + "eval " + cmd[len("bash -c "):])
        calls = log.read_text(encoding="utf-8").splitlines()
        log.unlink()
        if good:
            assert out.returncode == 0, out.stderr
            assert calls == ["sshd -t", "systemctl reload-or-restart ssh", "sshd -T"]
            assert "PasswordAuthentication no" in conf.read_text(encoding="utf-8")
        else:
            assert out.returncode != 0 and calls == ["sshd -t"]
            assert not conf.exists() and "left as it was" in out.stderr
    # It sorts before cloud-init's 50-cloud-init.conf: sshd keeps the FIRST value.
    assert remote.KEYS_ONLY_FILE.rsplit("/", 1)[1] < "50-cloud-init.conf"


def test_the_wizard_says_how_to_turn_passwords_back_on(keys):
    text = remote.password_login_help("council", "192.168.1.77", keys)
    assert remote.KEYS_ONLY_FILE in text and "systemctl reload ssh" in text
    assert "council@192.168.1.77" in text


# d. the Wi-Fi key off the card, only once the Pi keeps the Wi-Fi
def _wifi_files(tmp_path):
    files = fb.build(fb.FirstBoot(hostname="council-pi-1", username="council",
                                  password="correct-horse-42", wifi_ssid="Home",
                                  wifi_password="wifi-pass-123"), fb.CLOUDINIT)
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "network-config").write_text(files["network-config"], encoding="utf-8",
                                         newline="\n")
    (boot / "meta-data").write_text(files["meta-data"], encoding="utf-8", newline="\n")
    (boot / "user-data.council-tmp").write_text("leftover", encoding="utf-8")
    return files, boot


@pytest.mark.parametrize("nm, netplan, ok", [
    ("802-11-wireless:/etc/NetworkManager/system-connections/home.nmconnection", "", True),
    ("802-11-wireless:/run/NetworkManager/system-connections/netplan-wlan0-Home.nmconnection",
     "network:\n  wifis:\n    wlan0: {}\n", True),
    ("802-11-wireless:/run/NetworkManager/system-connections/x.nmconnection", "", False),
    ("802-3-ethernet:/run/NetworkManager/system-connections/eth0.nmconnection", "", False),
])
def test_the_wifi_key_leaves_the_card_only_once_the_pi_keeps_the_wifi(tmp_path, nm, netplan,
                                                                      ok):
    files, boot = _wifi_files(tmp_path)
    cloud, plan = tmp_path / "cloud.cfg.d", tmp_path / "netplan"
    plan.mkdir()
    if netplan:
        (plan / "50-cloud-init.yaml").write_text(netplan, encoding="utf-8")
    psk = pi_secrets.wifi_psk("Home", "wifi-pass-123")
    assert psk in files["network-config"]
    shims = ("sudo() { \"$@\"; }\nsync() { :; }\n"
             "nmcli() { case \"$*\" in *FILENAME*) echo '" + nm + "';; "
             "*) echo '" + nm.split(":")[0] + "';; esac; }\n")
    cmd = remote.wifi_keep_cmd((_shell_path(boot), _shell_path(tmp_path / "absent")),
                               cloud_cfg_dir=_shell_path(cloud), netplan_dir=_shell_path(plan))
    out = _sh(shims + "eval " + cmd[len("bash -c "):])
    card = (boot / "network-config").read_text(encoding="utf-8")
    if ok:
        assert out.returncode == 0, out.stderr
        assert psk not in card and all(ln.startswith("#") for ln in card.splitlines())
        assert (cloud / "99-disable-network-config.cfg").read_text(
            encoding="utf-8") == "network: {config: disabled}\n"
        assert not list(boot.glob("*.council-tmp"))
    else:
        assert out.returncode != 0 and "left on the card" in out.stderr
        assert card == files["network-config"] and not cloud.exists()


def test_a_network_config_the_council_did_not_write_is_left_alone(tmp_path):
    boot = tmp_path / "boot"
    boot.mkdir()
    theirs = "network:\n  version: 2\n  wifis:\n    wlan0:\n      access-points:\n" \
             "        Home: {password: their-own}\n"
    (boot / "network-config").write_text(theirs, encoding="utf-8", newline="\n")
    cmd = remote.wifi_keep_cmd((_shell_path(boot),), cloud_cfg_dir=_shell_path(
        tmp_path / "cloud"), netplan_dir=_shell_path(tmp_path))
    out = _sh("sudo() { \"$@\"; }; sync() { :; }; nmcli() { :; }; eval "
              + cmd[len("bash -c "):])
    assert out.returncode == 0 and "removed: no" in out.stdout
    assert (boot / "network-config").read_text(encoding="utf-8") == theirs


def test_the_wifi_and_password_steps_come_last():
    labels = [label for label, _c, _t in remote.provision_steps("192.168.1.50")]
    assert labels[-2].startswith("Keep the Wi-Fi on the Pi")
    assert labels[-1].startswith("Turn SSH password login off")


# ── review of 2026-10-07 ──────────────────────────────────────────────────
def _fake_sshd(tmp_path, log, *, test_ok=True, effective="passwordauthentication no"):
    sshd = tmp_path / "sshd"
    sshd.write_text("#!/bin/sh\n"
                    f"echo \"sshd $1\" >> {_shell_path(log)}\n"
                    f"if [ \"$1\" = -t ]; then exit {0 if test_ok else 1}; fi\n"
                    f"echo '{effective}'\n", encoding="utf-8")
    return sshd


@pytest.mark.parametrize("reload_ok, effective", [
    (False, "passwordauthentication no"),     # ssh.service inactive: reload fails
    (True, "passwordauthentication yes"),     # another setting still allows passwords
])
def test_a_keys_only_step_that_fails_after_the_check_takes_the_drop_in_away(
        tmp_path, reload_ok, effective):
    # MEASURED: sshd -t passed, 'systemctl reload' failed, and the drop-in
    # stayed - the user was told password login is ON, and the next sshd
    # restart or reboot made the Pi keys-only, unproven.
    log = tmp_path / "calls.log"
    conf = tmp_path / "sshd_config.d" / "00-council-keys-only.conf"
    conf.parent.mkdir()
    sshd = _fake_sshd(tmp_path, log, effective=effective)
    fail = "" if reload_ok else "echo 'Job for ssh.service failed' >&2; return 1; "
    shims = ("sudo() { \"$@\"; }\n"
             f"systemctl() {{ echo \"systemctl $*\" >> {_shell_path(log)}; {fail}}}\n")
    cmd = remote.keys_only_cmd(path=_shell_path(conf), sshd=_shell_path(sshd))
    out = _sh(shims + "eval " + cmd[len("bash -c "):])
    calls = log.read_text(encoding="utf-8").splitlines()
    assert out.returncode != 0 and "keys-only" not in out.stdout
    assert not conf.exists() and not conf.with_name(conf.name + ".council-tmp").exists()
    assert "password login stays on" in out.stderr
    # ...and sshd is told again, without the file.
    assert calls[-1].startswith("systemctl reload-or-restart")
    assert calls.index("sshd -t") < len(calls) - 1


def test_password_login_back_on_reloads_or_restarts():
    assert "reload-or-restart ssh" in remote.password_login_back_cmd()


def _logging_sudo(log):
    return f"sudo() {{ echo \"sudo $*\" >> {_shell_path(log)}; \"$@\"; }}\nsync() {{ :; }}\n"


def test_the_wifi_key_is_zeroed_where_it_is_before_the_file_is_replaced(tmp_path):
    # On the FAT boot partition 'mv -f' over network-config only freed its
    # clusters: the 64-hex key stayed readable from the raw card.
    files, boot = _wifi_files(tmp_path)
    log = tmp_path / "sudo.log"
    nm = "802-11-wireless:/etc/NetworkManager/system-connections/home.nmconnection"
    shims = (_logging_sudo(log) + "nmcli() { case \"$*\" in *FILENAME*) echo '" + nm
             + "';; *) echo 802-11-wireless;; esac; }\n")
    cmd = remote.wifi_keep_cmd((_shell_path(boot),), cloud_cfg_dir=_shell_path(
        tmp_path / "cloud"), netplan_dir=_shell_path(tmp_path))
    out = _sh(shims + "eval " + cmd[len("bash -c "):])
    assert out.returncode == 0, out.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    nc = _shell_path(boot / "network-config")
    wipe = calls.index(f"sudo shred -n 0 -z -x -- {nc}")
    move = next(i for i, c in enumerate(calls) if c.startswith("sudo mv -f") and c.endswith(nc))
    assert wipe < move
    # the leftover tmp file (it may be a whole user-data) is zeroed before it goes
    left = _shell_path(boot / "user-data.council-tmp")
    assert calls.index(f"sudo shred -n 0 -z -x -- {left}") < next(
        i for i, c in enumerate(calls) if c.startswith("sudo rm -f") and left in c)
    assert not (boot / "user-data.council-tmp").exists()
    assert pi_secrets.wifi_psk("Home", "wifi-pass-123") not in (
        boot / "network-config").read_text(encoding="utf-8")


def test_the_host_key_is_zeroed_where_it_is_before_user_data_is_replaced(tmp_path):
    pem, pub = pi_secrets.host_keypair()
    files = fb.build(fb.FirstBoot(hostname="council-pi-1", username="council",
                                  password="correct-horse-42",
                                  host_key_private=pem, host_key_public=pub), fb.CLOUDINIT)
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "user-data").write_text(files["user-data"], encoding="utf-8", newline="\n")
    log = tmp_path / "sudo.log"
    out = _sh(_logging_sudo(log) + remote.scrub_firstboot_cmd((_shell_path(boot),)))
    assert out.returncode == 0, out.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    ud = _shell_path(boot / "user-data")
    wipe = calls.index(f"sudo shred -n 0 -z -x -- {ud}")
    assert wipe < next(i for i, c in enumerate(calls) if c.startswith("sudo mv -f"))
    assert "PRIVATE KEY" not in (boot / "user-data").read_text(encoding="utf-8")


def test_the_legacy_firstrun_script_zeroes_itself_and_still_finishes(tmp_path):
    # firstrun.sh holds the Wi-Fi key on the FAT boot partition and removed
    # itself with a plain 'rm -f'. Zeroing a running script under bash would
    # cut it off mid-way, so its last step is one compound command: here
    # that step runs, adapted to a temp folder, and must finish.
    files = fb.build(fb.FirstBoot(hostname="council-pi-1", username="council",
                                  password="correct-horse-42", wifi_ssid="Home",
                                  wifi_password="wifi-pass-123"), fb.SYSTEMD)
    last = [ln for ln in files["firstrun.sh"].splitlines() if "shred" in ln]
    assert len(last) == 1 and last[0].startswith("{ ") and last[0].endswith("exit 0; }")
    assert last[0].index("shred") < last[0].index("rm -f")
    fw = tmp_path / "firmware"
    fw.mkdir()
    (fw / "cmdline.txt").write_text("console=tty1" + fb.CMDLINE_HOOK + "\n", encoding="utf-8",
                                    newline="\n")
    step = last[0].replace("/boot/firmware", _shell_path(fw)).replace(
        "/boot/", _shell_path(tmp_path / "none") + "/")
    script = fw / "firstrun.sh"
    marks = tmp_path / "marks"
    script.write_text("#!/bin/bash\n"
                      f"echo start >> {_shell_path(marks)}\n"
                      f"psk={pi_secrets.wifi_psk('Home', 'wifi-pass-123')}\n"
                      + step + "\n"
                      f"echo AFTER-EXIT >> {_shell_path(marks)}\n", encoding="utf-8",
                      newline="\n")
    import subprocess
    out = subprocess.run([_git_bash(), _shell_path(script)], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    assert not script.exists()
    assert marks.read_text(encoding="utf-8").split() == ["start"]
    assert "systemd.run" not in (fw / "cmdline.txt").read_text(encoding="utf-8")
