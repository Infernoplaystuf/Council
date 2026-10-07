"""Which disks may be erased to make a Pi's SD card — and the proof it is still
the same disk when the write starts.

Measured on this desktop (2026-10-06): besides the NVMe system disk there is a
**5 TB "Seagate Portable" on BusType USB, not system, not boot**. A filter of
"USB and not the system disk" offers it for erasing. So a disk is a candidate
only when ALL of these hold:

  * BusType is SD, MMC or USB;
  * it is neither the system disk nor a boot disk;
  * a card is present (size > 0) and the size is at most MAX_CARD_BYTES
    (256 GB — far above any card a Pi boots from, far below the external
    drives people keep data on);
  * none of its volumes holds the Council's vault, the app folder, or the
    Windows / user profile folders.

The user then types the disk's confirm code ("DISK 3 - 31.9 GB"), and the
elevated writer re-reads the disk and refuses unless its number, unique id and
size still match (`DiskIdentity`) — a card swapped between the click and the
write is not written.

A DISK THAT ALREADY HOLDS FILES needs a second, explicit confirmation that
names what is on it (the user's decision f, 2026-10-07): `Disk.contents`
lists every partition with files on it — its letter, label, file system and
used space — and every partition Windows cannot read (an old Pi card's Linux
partition may hold anything). Asked for every eligible disk, an SD card in a
built-in reader as much as a USB one: a camera card's photos are the same
loss. A freshly formatted card (a few KB of file-system bookkeeping) is not
asked about.

Listing is read-only (PowerShell Get-Disk / Get-Partition / Get-Volume).
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

MAX_CARD_BYTES = 256 * 1000 ** 3
CARD_BUSES = ("SD", "MMC", "USB")

_PS_LIST = r"""
$ErrorActionPreference = 'SilentlyContinue'
$out = foreach ($d in Get-Disk) {
  $parts = foreach ($p in (Get-Partition -DiskNumber $d.Number)) {
    $v = $p | Get-Volume
    [pscustomobject]@{
      Number = $p.PartitionNumber; Size = $p.Size; DriveLetter = "$($p.DriveLetter)";
      Type = "$($p.Type)"; FileSystem = "$($v.FileSystem)"; Label = "$($v.FileSystemLabel)";
      SizeRemaining = $v.SizeRemaining; VolumeSize = $v.Size }
  }
  [pscustomobject]@{
    Number = $d.Number; FriendlyName = $d.FriendlyName; SerialNumber = "$($d.SerialNumber)".Trim();
    UniqueId = "$($d.UniqueId)"; BusType = "$($d.BusType)"; Size = $d.Size;
    IsSystem = $d.IsSystem; IsBoot = $d.IsBoot; IsOffline = $d.IsOffline;
    IsReadOnly = $d.IsReadOnly; PartitionStyle = "$($d.PartitionStyle)";
    Partitions = @($parts) }
}
@($out) | ConvertTo-Json -Depth 4 -Compress
"""


#: Used space a freshly formatted volume may show without holding a file
#: (FAT32 / exFAT bookkeeping and Windows' System Volume Information are a
#: few KB to a few hundred KB). More than this = it holds files.
EMPTY_USED_BYTES = 1024 * 1024


@dataclass
class Partition:
    number: int
    size: int
    drive_letter: str = ""
    file_system: str = ""
    label: str = ""
    size_remaining: Optional[int] = None
    volume_size: Optional[int] = None
    part_type: str = ""

    def used(self) -> Optional[int]:
        if self.volume_size is None or self.size_remaining is None:
            return None
        return max(0, int(self.volume_size) - int(self.size_remaining))

    def holding(self) -> str:
        """What this partition holds, in words, or '' when it holds nothing
        (empty file system, or a Microsoft Reserved partition)."""
        where = f"{self.drive_letter}: " if self.drive_letter else ""
        name = f"'{self.label}'" if self.label else f"partition {self.number}"
        if self.part_type.lower() == "reserved" or self.size <= 0:
            return ""
        if not self.file_system:
            return (f"{name} ({self.size / 1e9:.1f} GB): a file system Windows cannot "
                    "read (a Linux card?) - it may hold files")
        used = self.used()
        if used is None:
            return f"{where}{name} ({self.file_system}, used space unknown)"
        if used <= EMPTY_USED_BYTES:
            return ""
        return (f"{where}{name} ({self.file_system}, {_size(used)} used of "
                f"{_size(self.volume_size)})")


def _size(n: Optional[int]) -> str:
    n = int(n or 0)
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB" if n >= 1e6         else f"{n / 1e3:.0f} KB"


@dataclass
class Disk:
    number: int
    friendly_name: str
    serial: str
    unique_id: str
    bus_type: str
    size: int
    is_system: bool
    is_boot: bool
    is_offline: bool = False
    is_read_only: bool = False
    partition_style: str = ""
    partitions: List[Partition] = field(default_factory=list)
    eligible: bool = False
    why_not: str = ""

    @property
    def confirm_code(self) -> str:
        """What the user types to erase this disk. Plain ASCII: the middle
        dot it had needed Alt+0183 to type."""
        return f"DISK {self.number} - {self.size / 1000 ** 3:.1f} GB"

    def contents(self) -> List[str]:
        """Each partition that holds (or may hold) files, in words."""
        return [h for h in (p.holding() for p in self.partitions) if h]

    @property
    def holds_files(self) -> bool:
        return bool(self.contents())

    def identity(self) -> "DiskIdentity":
        return DiskIdentity(self.number, self.unique_id, self.serial, self.size)

    def summary(self) -> str:
        vols = ", ".join(
            f"{(p.drive_letter + ':') if p.drive_letter else '(no letter)'} "
            f"{p.label or p.file_system or 'unformatted'} {p.size / 1000 ** 3:.1f} GB"
            for p in self.partitions) or "no partitions"
        return (f"Disk {self.number}: {self.friendly_name} — "
                f"{self.size / 1000 ** 3:.1f} GB, {self.bus_type}; {vols}")


@dataclass(frozen=True)
class DiskIdentity:
    number: int
    unique_id: str
    serial: str
    size: int

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


def parse(raw: Any) -> List[Disk]:
    """PowerShell's JSON (one object or a list) -> Disks, eligibility unset."""
    if isinstance(raw, (str, bytes)):
        raw = json.loads(raw or "[]")
    if isinstance(raw, dict):
        raw = [raw]
    out = []
    for d in raw or []:
        parts = d.get("Partitions") or []
        if isinstance(parts, dict):
            parts = [parts]
        out.append(Disk(
            number=int(d.get("Number")), friendly_name=str(d.get("FriendlyName") or ""),
            serial=str(d.get("SerialNumber") or "").strip(),
            unique_id=str(d.get("UniqueId") or ""), bus_type=str(d.get("BusType") or ""),
            size=int(d.get("Size") or 0), is_system=bool(d.get("IsSystem")),
            is_boot=bool(d.get("IsBoot")), is_offline=bool(d.get("IsOffline")),
            is_read_only=bool(d.get("IsReadOnly")),
            partition_style=str(d.get("PartitionStyle") or ""),
            partitions=[Partition(number=int(p.get("Number") or 0), size=int(p.get("Size") or 0),
                                  # PowerShell gives a NUL char for "no letter".
                                  drive_letter="".join(
                                      c for c in str(p.get("DriveLetter") or "")
                                      if c.isalpha())[:1],
                                  file_system=str(p.get("FileSystem") or ""),
                                  label=str(p.get("Label") or ""),
                                  size_remaining=p.get("SizeRemaining"),
                                  volume_size=p.get("VolumeSize"),
                                  part_type=str(p.get("Type") or ""))
                        for p in parts]))
    return out


def protected_roots() -> List[Path]:
    """Folders whose disk must never be erased."""
    roots = []
    try:
        from council_core import paths
        roots += [Path(paths.vault_dir()), Path(paths.app_dir())]
    except Exception:
        pass
    for env in ("SystemRoot", "USERPROFILE", "LOCALAPPDATA", "ProgramFiles"):
        if os.environ.get(env):
            roots.append(Path(os.environ[env]))
    roots.append(Path(__file__).resolve().parent)
    return roots


def judge(disk: Disk, protected: Iterable[Path] = ()) -> Disk:
    """Fill ``eligible`` / ``why_not``. The first failing rule is the reason."""
    letters = {p.drive_letter.upper() for p in disk.partitions if p.drive_letter}
    held = sorted({str(r) for r in protected
                   if (str(r)[:1].upper() in letters and len(str(r)) > 1
                       and str(r)[1] == ":")})
    rules = [
        (disk.is_system or disk.is_boot, "it is this PC's system or boot disk"),
        (disk.bus_type not in CARD_BUSES,
         f"it is a {disk.bus_type or 'unknown'} disk, not an SD card or USB reader"),
        (disk.size <= 0, "no card is in the reader"),
        (disk.size > MAX_CARD_BYTES,
         f"it is {disk.size / 1000 ** 3:.0f} GB — larger than any Pi card "
         f"({MAX_CARD_BYTES / 1000 ** 3:.0f} GB limit), so it is treated as a data drive"),
        (disk.is_read_only, "the card is write-protected (check its lock switch)"),
        (bool(held), "it holds Council or Windows folders: " + ", ".join(held)),
    ]
    disk.eligible, disk.why_not = True, ""
    for failed, why in rules:
        if failed:
            disk.eligible, disk.why_not = False, why
            break
    return disk


def list_disks(*, runner=None) -> List[Disk]:
    """Every disk with its verdict. ``runner(script) -> json text`` is
    injectable for tests; the default runs PowerShell, read-only."""
    if runner is None:
        runner = _powershell
    disks = parse(runner(_PS_LIST))
    prot = protected_roots()
    return [judge(d, prot) for d in disks]


def find(disks: Sequence[Disk], identity: DiskIdentity) -> Optional[Disk]:
    """The disk that still matches ``identity`` exactly, or None."""
    for d in disks:
        if (d.number == identity.number and d.unique_id == identity.unique_id
                and d.serial == identity.serial and d.size == identity.size):
            return d
    return None


def _powershell(script: str) -> str:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if out.returncode != 0 and not out.stdout.strip():
        raise RuntimeError(out.stderr.strip() or "listing disks failed")
    return out.stdout.strip() or "[]"
