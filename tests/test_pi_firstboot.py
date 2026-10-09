"""council_core/pi_setup/firstboot.py — the files that give a new Pi its user,
SSH, hostname and Wi-Fi. Boot partitions here are temp folders laid out like
the real 2026-10-06 Raspberry Pi OS boot partition (cmdline.txt plus, for the
cloud-init images, the commented-out user-data / network-config / meta-data).
"""
from __future__ import annotations

import yaml
import pytest

from council_core.pi_setup import firstboot as fb
from council_core.pi_setup import pi_secrets as ps

PASSWORD = "correct-horse-42"
WIFI_PW = "S3kr1t-wifi-pass"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGQ council"
CMDLINE = ("console=serial0,115200 console=tty1 root=PARTUUID=27b94958-02 "
           "rootfstype=ext4 fsck.repair=yes rootwait resize\n")


def cfg(**kw):
    base = dict(hostname="council-pi-1", username="council", password=PASSWORD,
                wifi_ssid="Home Net", wifi_password=WIFI_PW, wifi_country="US",
                ssh_public_keys=[KEY])
    base.update(kw)
    return fb.FirstBoot(**base)


@pytest.fixture
def boot_cloud(tmp_path):
    d = tmp_path / "bootfs"
    d.mkdir()
    (d / "cmdline.txt").write_text(CMDLINE, encoding="utf-8")
    for n in ("user-data", "network-config", "meta-data"):
        (d / n).write_text("# placeholder, all commented out\n", encoding="utf-8")
    return d


@pytest.fixture
def boot_legacy(tmp_path):
    d = tmp_path / "bootfs"
    d.mkdir()
    (d / "cmdline.txt").write_text(CMDLINE, encoding="utf-8")
    return d


def test_secrets_never_reach_the_card():
    for fmt in fb.FORMATS:
        files = fb.build(cfg(), fmt)
        blob = "".join(files.values())
        assert PASSWORD not in blob and WIFI_PW not in blob, fmt


def test_cloud_init_files_parse_and_say_what_was_asked():
    files = fb.build(cfg(), fb.CLOUDINIT, instance_id="council-test")
    assert files["user-data"].startswith("#cloud-config\n")
    ud = yaml.safe_load(files["user-data"])
    assert ud["hostname"] == "council-pi-1"
    user = ud["users"][0]
    assert user["name"] == "council" and user["lock_passwd"] is False
    assert ps.check_sha512_crypt(PASSWORD, user["passwd"])
    assert user["ssh_authorized_keys"] == [KEY]
    assert "systemctl enable --now ssh" in ud["runcmd"]
    assert "raspi-config nonint do_wifi_country US" in ud["runcmd"]
    net = yaml.safe_load(files["network-config"])["network"]
    ap = net["wifis"]["wlan0"]
    assert ap["regulatory-domain"] == "US"
    assert ap["access-points"]["Home Net"]["password"] == ps.wifi_psk("Home Net", WIFI_PW)
    assert yaml.safe_load(files["meta-data"])["instance_id"] == "council-test"


def test_without_wifi_there_is_no_wifi_block():
    files = fb.build(cfg(wifi_ssid="", wifi_password=""), fb.CLOUDINIT)
    assert "wifis" not in yaml.safe_load(files["network-config"])["network"]
    assert all("wifi" not in c for c in yaml.safe_load(files["user-data"])["runcmd"])


def test_awkward_names_stay_exact_in_yaml():
    ssid = 'Café: "guest" #2'
    files = fb.build(cfg(wifi_ssid=ssid), fb.CLOUDINIT)
    net = yaml.safe_load(files["network-config"])["network"]
    assert list(net["wifis"]["wlan0"]["access-points"]) == [ssid]


def test_legacy_files_and_cmdline_hook(boot_legacy):
    files = fb.build(cfg(), fb.SYSTEMD)
    user, hashed = files["userconf.txt"].strip().split(":", 1)
    assert user == "council" and ps.check_sha512_crypt(PASSWORD, hashed)
    assert "psk=" + ps.wifi_psk("Home Net", WIFI_PW) in files["firstrun.sh"]
    assert KEY in files["firstrun.sh"]
    fb.apply(boot_legacy, files, fb.SYSTEMD)
    assert fb.verify(boot_legacy, files, fb.SYSTEMD) == []
    # applying twice does not stack the hook
    fb.apply(boot_legacy, files, fb.SYSTEMD)
    line = (boot_legacy / "cmdline.txt").read_text(encoding="utf-8")
    assert line.count("systemd.run=") == 1 and line.endswith("\n") and line.count("\n") == 1
    assert line.startswith("console=serial0,115200")


def test_cloud_init_apply_then_verify(boot_cloud):
    files = fb.build(cfg(), fb.CLOUDINIT)
    assert fb.detect_format(boot_cloud) == fb.CLOUDINIT
    written = fb.apply(boot_cloud, files, fb.CLOUDINIT)
    assert set(written) == {"user-data", "network-config", "meta-data", "ssh"}
    assert fb.verify(boot_cloud, files, fb.CLOUDINIT) == []
    assert (boot_cloud / "cmdline.txt").read_text(encoding="utf-8") == CMDLINE
    assert not list(boot_cloud.glob("*.council-tmp"))


def test_verify_catches_a_write_that_did_not_land(boot_cloud):
    files = fb.build(cfg(), fb.CLOUDINIT)
    fb.apply(boot_cloud, files, fb.CLOUDINIT)
    (boot_cloud / "network-config").write_text("# placeholder\n", encoding="utf-8")
    (boot_cloud / "ssh").unlink()
    wrong = fb.verify(boot_cloud, files, fb.CLOUDINIT)
    assert "ssh is missing" in wrong and "network-config does not match what was written" in wrong


def test_not_a_boot_partition_is_refused(tmp_path):
    with pytest.raises(FileNotFoundError):
        fb.apply(tmp_path, {"ssh": ""}, fb.CLOUDINIT)


def test_detect_format_legacy(boot_legacy):
    assert fb.detect_format(boot_legacy) == fb.SYSTEMD


@pytest.mark.parametrize("change, words", [
    ({"hostname": "Council_Pi"}, "hostname"),
    ({"username": "root"}, "username"),
    ({"username": "Bob"}, "username"),
    ({"password": "short"}, "at least 8"),
    ({"wifi_password": "short"}, "8 to 63"),
    ({"wifi_country": "usa"}, "two-letter"),
])
def test_problems_are_reported_before_anything_is_written(change, words):
    probs = cfg(**change).problems()
    assert any(words in p for p in probs), probs
    with pytest.raises(ValueError):
        fb.build(cfg(**change), fb.CLOUDINIT)
