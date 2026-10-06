"""First-boot settings for a freshly imaged Pi: user, SSH, hostname, Wi-Fi.

WHY THE OFFICIAL IMAGER'S SETTINGS SO OFTEN DO NOTHING
Read from the real 2026-10-06 Raspberry Pi OS Lite (Trixie) image: its boot
partition ships cloud-init files — ``user-data``, ``network-config``,
``meta-data`` — that are ENTIRELY commented out. The official image list
(os_list_imagingutility_v4.json) labels each image's mechanism in
``init_format``: ``cloudinit-rpi`` for Trixie, ``systemd`` (a firstrun.sh run
from cmdline.txt) for the Legacy Bookworm images. An Imager that writes the
other mechanism, or whose write to the boot partition never lands (Windows did
not remount it after flashing), leaves a Pi with no user, no SSH and no Wi-Fi
— and nothing says so. Here the format is taken from the image list (or read
off the card), the files are written by the Council, and `verify` reads them
back before the card is ejected.

WHAT IS AND IS NOT ON THE CARD
* the login password only as a SHA-512 crypt hash;
* Wi-Fi as the 64-hex WPA key derived from the passphrase and SSID, never the
  passphrase (both cloud-init's netplan and NetworkManager accept a 64-hex
  key; this is the one detail to confirm on the first real card);
* the Council's public SSH key, so after first boot the Council logs in with
  its key and the password is not needed or stored;
* the Wi-Fi country (regulatory domain) — without it Raspberry Pi OS keeps
  the Wi-Fi radio blocked, a classic "never joins" cause — set in the network
  config AND unblocked again by a first-boot command, belt and braces.
"""
from __future__ import annotations

import json
import re
import shlex
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import pi_secrets

CLOUDINIT = "cloudinit-rpi"
SYSTEMD = "systemd"
FORMATS = (CLOUDINIT, SYSTEMD)

_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_RESERVED_USERS = {"root", "daemon", "bin", "sys", "sync", "games", "man", "lp",
                   "mail", "news", "uucp", "proxy", "www-data", "backup", "list",
                   "irc", "nobody", "systemd-network", "messagebus", "sshd"}
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_PI_GROUPS = "users,adm,dialout,audio,netdev,video,plugdev,cdrom,games,input,gpio,spi,i2c,render,sudo"


@dataclass
class FirstBoot:
    """What the user chose. ``password`` is used once to make a hash and is
    not kept on this object afterwards (see `build`)."""
    hostname: str
    username: str
    password: str
    wifi_ssid: str = ""
    wifi_password: str = ""
    wifi_country: str = "US"
    wifi_hidden: bool = False
    ssh_public_keys: List[str] = field(default_factory=list)
    timezone: str = ""
    allow_password_ssh: bool = True
    #: A host key the Council generated (pi_secrets.host_keypair) so it can
    #: pin the Pi's identity before first boot. cloud-init images only.
    host_key_private: str = ""
    host_key_public: str = ""

    def problems(self) -> List[str]:
        """Everything that would stop the Pi coming up, in plain words."""
        out = []
        if not _HOST_RE.match(self.hostname or ""):
            out.append("The hostname may use only lower-case letters, digits and '-', "
                       "1-63 characters, not starting or ending with '-'.")
        if not _USER_RE.match(self.username or "") or self.username in _RESERVED_USERS:
            out.append("The username must start with a lower-case letter or '_', use "
                       "lower-case letters, digits, '_' or '-', and not be a system name.")
        if len(self.password or "") < 8:
            out.append("Use a password of at least 8 characters.")
        if self.wifi_ssid:
            try:
                pi_secrets.wifi_psk(self.wifi_ssid, self.wifi_password)
            except ValueError as exc:
                out.append(str(exc).capitalize() + ".")
            if not _COUNTRY_RE.match(self.wifi_country or ""):
                out.append("The Wi-Fi country must be a two-letter code such as US or GB "
                           "(without it the Pi keeps Wi-Fi switched off).")
        return out


def _yaml_str(s: str) -> str:
    """A YAML scalar that is exactly ``s`` (JSON strings are valid YAML)."""
    return json.dumps(s, ensure_ascii=False)


def build(cfg: FirstBoot, init_format: str, *, instance_id: Optional[str] = None
          ) -> Dict[str, str]:
    """The files to write to the boot partition, by name. Raises ValueError
    listing every problem. The plain password and Wi-Fi passphrase appear in
    none of the returned files."""
    if init_format not in FORMATS:
        raise ValueError(f"unknown first-boot format {init_format!r}")
    probs = cfg.problems()
    if probs:
        raise ValueError(" ".join(probs))
    pw_hash = pi_secrets.sha512_crypt(cfg.password)
    psk = pi_secrets.wifi_psk(cfg.wifi_ssid, cfg.wifi_password) if cfg.wifi_ssid else ""
    if init_format == CLOUDINIT:
        return _cloud_init(cfg, pw_hash, psk, instance_id or f"council-{uuid.uuid4()}")
    return _systemd(cfg, pw_hash, psk)


def _cloud_init(cfg: FirstBoot, pw_hash: str, psk: str, instance_id: str) -> Dict[str, str]:
    keys = "".join(f"\n      - {_yaml_str(k)}" for k in cfg.ssh_public_keys)
    user_data = f"""#cloud-config
# Written by The Council's Pi setup. The password is a SHA-512 hash; the
# Council's SSH key lets it log in without the password after first boot.
hostname: {_yaml_str(cfg.hostname)}
manage_etc_hosts: true
users:
  - name: {_yaml_str(cfg.username)}
    groups: {_PI_GROUPS}
    shell: /bin/bash
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    lock_passwd: false
    passwd: {_yaml_str(pw_hash)}{('''
    ssh_authorized_keys:''' + keys) if keys else ''}
ssh_pwauth: {"true" if cfg.allow_password_ssh else "false"}
"""
    if cfg.host_key_private and cfg.host_key_public:
        pem = "".join(f"    {ln}\n" for ln in cfg.host_key_private.strip().splitlines())
        user_data += ("ssh_keys:\n  ed25519_private: |\n" + pem
                      + f"  ed25519_public: {_yaml_str(cfg.host_key_public.strip())}\n")
    if cfg.timezone:
        user_data += f"timezone: {_yaml_str(cfg.timezone)}\n"
    run = ["systemctl enable --now ssh"]
    if psk:
        run = [f"raspi-config nonint do_wifi_country {cfg.wifi_country}",
               "rfkill unblock wifi"] + run
    user_data += "runcmd:\n" + "".join(f"  - {_yaml_str(c)}\n" for c in run)

    net = """# Written by The Council's Pi setup.
network:
  version: 2
  ethernets:
    eth0:
      dhcp4: true
      optional: true
"""
    if psk:
        net += f"""  wifis:
    wlan0:
      dhcp4: true
      optional: true
      regulatory-domain: {cfg.wifi_country}
      access-points:
        {_yaml_str(cfg.wifi_ssid)}:
          password: {_yaml_str(psk)}{'''
          hidden: true''' if cfg.wifi_hidden else ''}
"""
    meta = ("# Written by The Council's Pi setup. A new instance_id makes cloud-init\n"
            "# run first-boot setup on this card.\n"
            f"dsmode: local\ninstance_id: {instance_id}\n")
    return {"user-data": user_data, "network-config": net, "meta-data": meta, "ssh": ""}


def _systemd(cfg: FirstBoot, pw_hash: str, psk: str) -> Dict[str, str]:
    """Legacy (Bookworm) images: userconf.txt + the ssh flag file, and a
    firstrun.sh — run once by systemd from cmdline.txt, as the Imager does —
    for the hostname, keys and Wi-Fi. `apply` adds the cmdline.txt hook."""
    q = shlex.quote
    keys = "\n".join(cfg.ssh_public_keys)
    lines = [
        "#!/bin/bash",
        "# Written by The Council's Pi setup. Runs once, then removes itself.",
        "set +e",
        f"CURRENT_HOSTNAME=$(cat /etc/hostname | tr -d ' \\t\\n\\r')",
        f"echo {q(cfg.hostname)} > /etc/hostname",
        f"sed -i \"s/127.0.1.1.*$CURRENT_HOSTNAME/127.0.1.1\\t{cfg.hostname}/g\" /etc/hosts",
        f"FIRSTUSER=$(getent passwd 1000 | cut -d: -f1)",
        f"FIRSTUSERHOME=$(getent passwd 1000 | cut -d: -f6)",
    ]
    if keys:
        lines += [
            'install -o "$FIRSTUSER" -m 700 -d "$FIRSTUSERHOME/.ssh"',
            f'printf "%s\\n" {q(keys)} > "$FIRSTUSERHOME/.ssh/authorized_keys"',
            'chown "$FIRSTUSER:$FIRSTUSER" "$FIRSTUSERHOME/.ssh/authorized_keys"',
            'chmod 600 "$FIRSTUSERHOME/.ssh/authorized_keys"',
        ]
    lines.append("systemctl enable ssh")
    if psk:
        nm = (f"[connection]\nid=council-wifi\nuuid={uuid.uuid4()}\ntype=wifi\n"
              f"[wifi]\nmode=infrastructure\nssid={cfg.wifi_ssid}\n"
              f"hidden={'true' if cfg.wifi_hidden else 'false'}\n"
              f"[wifi-security]\nkey-mgmt=wpa-psk\npsk={psk}\n"
              f"[ipv4]\nmethod=auto\n[ipv6]\naddr-gen-mode=default\nmethod=auto\n")
        lines += [
            f"raspi-config nonint do_wifi_country {cfg.wifi_country}",
            "rfkill unblock wifi",
            "cat > /etc/NetworkManager/system-connections/council-wifi.nmconnection <<'COUNCILWIFI'",
            nm.rstrip("\n"),
            "COUNCILWIFI",
            "chmod 600 /etc/NetworkManager/system-connections/council-wifi.nmconnection",
        ]
    if cfg.timezone:
        lines.append(f"timedatectl set-timezone {q(cfg.timezone)}")
    lines += [
        "rm -f /boot/firmware/firstrun.sh /boot/firstrun.sh",
        "sed -i 's| systemd.run.*||g' /boot/firmware/cmdline.txt /boot/cmdline.txt 2>/dev/null",
        "exit 0",
    ]
    return {"firstrun.sh": "\n".join(lines) + "\n",
            "userconf.txt": f"{cfg.username}:{pw_hash}\n",
            "ssh": ""}


#: Every file build() can produce, for either format.
FIRSTBOOT_NAMES = frozenset({"user-data", "network-config", "meta-data", "ssh",
                             "firstrun.sh", "userconf.txt"})

CMDLINE_HOOK = (" systemd.run=/boot/firmware/firstrun.sh systemd.run_success_action=reboot "
                "systemd.unit=kernel-command-line.target")


def detect_format(boot_dir: Path) -> Optional[str]:
    """From the card itself: cloud-init images carry meta-data/user-data."""
    if (boot_dir / "meta-data").exists() or (boot_dir / "user-data").exists():
        return CLOUDINIT
    if (boot_dir / "cmdline.txt").exists():
        return SYSTEMD
    return None


def apply(boot_dir: Path, files: Dict[str, str], init_format: str) -> List[str]:
    """Write ``files`` into the mounted boot partition (each via tmp+replace)
    and, for the systemd format, hook firstrun.sh into cmdline.txt. Only the
    named first-boot files and cmdline.txt are touched. Returns the names
    written."""
    boot_dir = Path(boot_dir)
    if not (boot_dir / "cmdline.txt").exists():
        raise FileNotFoundError(f"{boot_dir} is not a Raspberry Pi boot partition "
                                "(no cmdline.txt)")
    written = []
    # Only the names build() makes: the elevated helper writes these, and a
    # name such as 'C:evil.txt' passed the old '/', '\\', '.' check and
    # resolved outside the card (PureWindowsPath('F:/') / 'C:evil.txt').
    bad = [n for n in files if n not in FIRSTBOOT_NAMES]
    if bad:
        raise ValueError(f"bad first-boot file name {bad[0]!r}")
    for name, text in files.items():
        target = boot_dir / name
        tmp = boot_dir / (name + ".council-tmp")
        tmp.write_bytes(text.replace("\r\n", "\n").encode("utf-8"))
        tmp.replace(target)
        written.append(name)
    if init_format == SYSTEMD:
        cmd = boot_dir / "cmdline.txt"
        line = cmd.read_text(encoding="utf-8").strip().replace(CMDLINE_HOOK.strip(), "").strip()
        tmp = boot_dir / "cmdline.txt.council-tmp"
        tmp.write_bytes((line + CMDLINE_HOOK + "\n").encode("utf-8"))
        tmp.replace(cmd)
        written.append("cmdline.txt")
    return written


def verify(boot_dir: Path, files: Dict[str, str], init_format: str) -> List[str]:
    """Read the card back. Returns what is wrong (empty list = all good)."""
    boot_dir = Path(boot_dir)
    wrong = []
    for name, text in files.items():
        p = boot_dir / name
        if not p.exists():
            wrong.append(f"{name} is missing")
        elif p.read_bytes() != text.replace("\r\n", "\n").encode("utf-8"):
            wrong.append(f"{name} does not match what was written")
    if init_format == SYSTEMD:
        cmd = (boot_dir / "cmdline.txt").read_text(encoding="utf-8")
        if "systemd.run=/boot/firmware/firstrun.sh" not in cmd or cmd.count("\n") > 1:
            wrong.append("cmdline.txt does not start firstrun.sh (or is not one line)")
    return wrong
