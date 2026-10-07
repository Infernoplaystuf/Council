"""council_core/pi_setup/disks.py — which disk may be erased for a Pi card.

The first two disks are THIS desktop's real ones, as Get-Disk reported them on
2026-10-06: the NVMe system disk, and a 5 TB Seagate Portable on USB that is not
a system disk — the case a "USB and not system" rule would have offered for
erasing. Nothing here touches a real disk: the PowerShell runner is replaced.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from council_core.pi_setup import disks as dk

NUL = "\u0000"

REAL = [
    {"Number": 0, "FriendlyName": "T-FORCE TM8FFE002T",
     "SerialNumber": "0000_0000_0000_0000_A843_9700_3030_2624.",
     "UniqueId": "eui.0000000000000000A843970030302624", "BusType": "NVMe",
     "Size": 2048408248320, "IsSystem": True, "IsBoot": True, "IsOffline": False,
     "IsReadOnly": False, "PartitionStyle": "GPT",
     "Partitions": [{"Number": 3, "Size": 2047800000000, "DriveLetter": "C",
                     "FileSystem": "NTFS", "Label": "Windows"}]},
    {"Number": 1, "FriendlyName": "Seagate Portable", "SerialNumber": "            NACMRX0Z",
     "UniqueId": "3E41434D5258305A", "BusType": "USB", "Size": 5000981077504,
     "IsSystem": False, "IsBoot": False, "IsOffline": False, "IsReadOnly": False,
     "PartitionStyle": "GPT",
     "Partitions": [{"Number": 1, "Size": 134217728, "DriveLetter": NUL},
                    {"Number": 2, "Size": 5000800000000, "DriveLetter": "E",
                     "FileSystem": "NTFS", "Label": "Seagate Portable Drive"}]},
]
SD_CARD = {"Number": 2, "FriendlyName": "Generic STORAGE DEVICE", "SerialNumber": "000000000820",
           "UniqueId": "USBSTOR\\DISK&VEN_GENERIC", "BusType": "USB", "Size": 31914983424,
           "IsSystem": False, "IsBoot": False, "IsOffline": False, "IsReadOnly": False,
           "PartitionStyle": "MBR",
           "Partitions": [{"Number": 1, "Size": 536870912, "DriveLetter": "F",
                           "FileSystem": "FAT32", "Label": "bootfs"},
                          {"Number": 2, "Size": 31300000000, "DriveLetter": NUL}]}


def runner_for(items):
    return lambda _script: json.dumps(items)


def by_number(items):
    return {d.number: d for d in dk.list_disks(runner=runner_for(items))}


def test_this_pcs_disks_are_both_refused():
    got = by_number(REAL)
    assert not got[0].eligible and "system" in got[0].why_not
    assert not got[1].eligible and "larger than any Pi card" in got[1].why_not


def test_an_sd_card_is_offered_with_a_confirm_code():
    got = by_number(REAL + [SD_CARD])
    card = got[2]
    assert card.eligible, card.why_not
    assert card.confirm_code == "DISK 2 - 31.9 GB" and card.confirm_code.isascii()
    assert "F: bootfs" in card.summary() and "(no letter)" in card.summary()


@pytest.mark.parametrize("change, reason", [
    ({"Size": 0}, "no card"),
    ({"IsReadOnly": True}, "write-protected"),
    ({"BusType": "SATA"}, "not an SD card"),
    ({"IsBoot": True}, "system or boot"),
    ({"Size": 300 * 1000 ** 3}, "larger than any Pi card"),
])
def test_refusal_reasons(change, reason):
    got = by_number([{**SD_CARD, **change}])
    assert not got[2].eligible and reason in got[2].why_not


def test_a_card_holding_the_vault_is_refused(monkeypatch):
    monkeypatch.setattr(dk, "protected_roots", lambda: [Path("F:/council_vault")])
    assert "holds Council or Windows folders" in by_number([SD_CARD])[2].why_not


def test_identity_must_match_exactly():
    card = by_number([SD_CARD])[2]
    ident = card.identity()
    assert dk.find([card], ident) is card
    swapped = dk.parse([{**SD_CARD, "SerialNumber": "999"}])
    assert dk.find(swapped, ident) is None
    resized = dk.parse([{**SD_CARD, "Size": 63864569856}])
    assert dk.find(resized, ident) is None


def test_single_object_json_is_a_list():
    assert len(dk.parse(json.dumps(SD_CARD))) == 1


# ── a card that already holds files (the user's decision f, 2026-10-07) ──
#: A new card as it comes from the shop: one exFAT volume, a few KB used.
BLANK_CARD = {**SD_CARD, "Number": 3, "SerialNumber": "000000000921", "UniqueId": "USBSTOR-BLANK",
              "Partitions": [{"Number": 1, "Size": 31_910_000_000, "DriveLetter": "G",
                              "FileSystem": "exFAT", "Label": "", "Type": "IFS",
                              "VolumeSize": 31_909_000_000,
                              "SizeRemaining": 31_908_700_000}]}
#: A camera card: 4.2 GB of photos on FAT32.
CAMERA_CARD = {**BLANK_CARD, "Number": 4, "SerialNumber": "CAM", "UniqueId": "USBSTOR-CAM",
               "Partitions": [{"Number": 1, "Size": 31_910_000_000, "DriveLetter": "H",
                               "FileSystem": "FAT32", "Label": "EOS_DIGITAL", "Type": "IFS",
                               "VolumeSize": 31_900_000_000,
                               "SizeRemaining": 27_700_000_000}]}


def test_a_blank_card_holds_nothing_to_confirm():
    card = by_number([BLANK_CARD])[3]
    assert card.eligible and not card.holds_files and card.contents() == []


def test_a_card_with_files_says_what_is_on_it():
    cam = by_number([CAMERA_CARD])[4]
    assert cam.eligible and cam.holds_files
    assert cam.contents() == ["H: 'EOS_DIGITAL' (FAT32, 4.2 GB used of 31.9 GB)"]
    # An old Pi card: its boot volume, and a Linux partition Windows cannot read.
    old = by_number([SD_CARD])[2]
    assert old.holds_files
    assert old.contents()[0].startswith("F: 'bootfs' (FAT32")
    assert "cannot read" in old.contents()[1] and "31.3 GB" in old.contents()[1]


def test_a_microsoft_reserved_partition_is_not_files():
    d = by_number([{**BLANK_CARD, "Partitions": BLANK_CARD["Partitions"] + [
        {"Number": 2, "Size": 16_777_216, "DriveLetter": NUL, "Type": "Reserved"}]}])[3]
    assert not d.holds_files


def test_the_listing_asks_windows_for_each_volumes_size():
    assert "VolumeSize = $v.Size" in dk._PS_LIST
