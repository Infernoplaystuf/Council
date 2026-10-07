"""The elevated step: erase one SD card, write Raspberry Pi OS, add first-boot
settings, verify. Started by `start_elevated` behind a Windows UAC prompt, so
only this short-lived process ever runs with administrator rights.

    python -m council_core.pi_setup.flash_helper <job.json>

THE HELPER TRUSTS NOTHING IT WAS HANDED
The job names a disk by identity (number + unique id + serial + size). Before
erasing, the helper lists the disks ITSELF and refuses unless exactly that
disk is present and still passes every rule in disks.judge (removable bus,
not system/boot, a card present, <= 256 GB, no Council or Windows folders).
A card swapped, a reader re-enumerated as another number, or a data drive
plugged in under the same number are all refused. A card that holds files is
refused unless the user confirmed erasing them (the job says so).

THE IMAGE IS CHECKED BEFORE THE CARD IS ERASED
The user's decision e (2026-10-07). The image file is decompressed and hashed
first; a file whose uncompressed SHA-256 or size is not the official list's
(``extract_sha256`` / ``extract_size``, which belong to the file chosen) is
refused with nothing erased. It used to be found only while writing - after
the card was already erased. A file that is not in the list has no checksum
to meet, but its uncompressed size is still checked against the card before
erasing. Its first-boot format is not assumed either: the job carries the
files for both formats and the helper uses the one the written card shows.

Progress goes to status.json beside the job (atomic writes); the Council's
window polls it. A ``cancel`` file beside the job stops the write at the next
chunk. job.json holds the first-boot files (a password HASH and the WPA key,
not the passwords) and is deleted when the helper finishes.

THE FAILURE THE OFFICIAL IMAGER HITS
After writing, Windows often does not mount the new boot partition, and an
Imager that cannot see it cannot write its settings — silently. Here the
helper, being administrator, asks Windows to re-read the disk and ASSIGNS the
boot partition a drive letter itself, writes the first-boot files, and reads
them back (firstboot.verify). Any mismatch is an error, not a quiet success.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import disks as dk
from . import firstboot as fb
from . import writer


class Refused(RuntimeError):
    pass


def _ps(script: str, timeout: int = 120) -> str:
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, text=True, timeout=timeout,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[:500] or "PowerShell failed")
    return out.stdout.strip()


class WindowsDiskOps:
    """The four things that touch a real disk. Replaced in tests."""

    def list_disks(self):
        return dk.list_disks()

    def clear(self, number: int) -> None:
        # Removes every partition (and so every drive letter) so Windows lets go
        # of the volumes; a RAW, never-initialised card has nothing to clear.
        try:
            _ps(f"Clear-Disk -Number {int(number)} -RemoveData -RemoveOEM -Confirm:$false")
        except RuntimeError as exc:
            if "not initialized" not in str(exc).lower() and "raw" not in str(exc).lower():
                raise

    def open_target(self, number: int):
        return open(rf"\\.\PhysicalDrive{int(number)}", "r+b", buffering=0)

    def mount_boot(self, number: int) -> Path:
        """Re-read the disk and give partition 1 (the FAT boot partition) a
        drive letter if Windows did not. Returns its root, e.g. F:\\ ."""
        n = int(number)
        _ps(f"Update-Disk -Number {n}")
        for _ in range(20):
            letter = _ps(f"(Get-Partition -DiskNumber {n} -PartitionNumber 1 "
                         f"-ErrorAction SilentlyContinue).DriveLetter").strip("\x00 \r\n")
            if letter and letter.isalpha():
                return Path(f"{letter}:\\")
            try:
                _ps(f"Add-PartitionAccessPath -DiskNumber {n} -PartitionNumber 1 "
                    f"-AssignDriveLetter -ErrorAction Stop")
            except RuntimeError:
                pass
            time.sleep(1.0)
        raise RuntimeError("Windows would not mount the card's boot partition")

    def flush(self, boot: Path) -> None:
        _ps(f"Write-VolumeCache -DriveLetter {str(boot)[0]}")


def _status(job_dir: Path, **fields) -> None:
    fields.setdefault("ts", time.time())
    tmp = job_dir / "status.json.tmp"
    tmp.write_text(json.dumps(fields), encoding="utf-8")
    tmp.replace(job_dir / "status.json")


def run(job_path: Path, ops: Optional[WindowsDiskOps] = None) -> Dict[str, Any]:
    """Do the job; always leaves a final status ('done' or 'error')."""
    ops = ops or WindowsDiskOps()
    job_path = Path(job_path)
    job_dir = job_path.parent
    cancel_flag = job_dir / "cancel"
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
        ident = dk.DiskIdentity(**job["disk"])
        image = Path(job["image"])
        _status(job_dir, phase="checking", message="Checking the card is the one you chose…")

        disk = dk.find(ops.list_disks(), ident)
        if disk is None:
            raise Refused("the card you chose is no longer there (removed, swapped, or "
                          "renumbered) — nothing was erased")
        if not disk.eligible:
            raise Refused(f"refusing to erase disk {disk.number}: {disk.why_not}")
        if disk.holds_files and not job.get("files_confirmed"):
            raise Refused(f"disk {disk.number} holds files you did not agree to erase ("
                          + "; ".join(disk.contents()) + ") — nothing was erased")

        def checking(_phase, done, total):
            _status(job_dir, phase="checking-image", done=done, total=total,
                    message=f"Checking the image file before anything is erased: "
                            f"{done / 1e9:.2f} of {total / 1e9:.2f} GB")
        try:
            writer.check_image(image, expected_sha256=job.get("extract_sha256", ""),
                               expected_size=int(job.get("extract_size") or 0),
                               capacity=disk.size, on_progress=checking,
                               cancelled=cancel_flag.exists)
        except writer.WriteFailed as exc:
            raise Refused(f"{exc} — nothing was erased") from exc

        _status(job_dir, phase="erasing", message=f"Erasing {disk.summary()}")
        ops.clear(disk.number)

        def progress(phase, done, total):
            _status(job_dir, phase=phase, done=done, total=total,
                    message=f"{phase.capitalize()} {done / 1e9:.2f} of {total / 1e9:.2f} GB")

        with ops.open_target(disk.number) as target:
            result = writer.write_image(
                image, target, capacity=disk.size,
                expected_sha256=job.get("extract_sha256", ""),
                expected_size=int(job.get("extract_size") or 0),
                on_progress=progress, cancelled=cancel_flag.exists)

        _status(job_dir, phase="first-boot", message="Adding your first-boot settings…")
        boot = ops.mount_boot(disk.number)
        fmt = job.get("init_format") or fb.detect_format(boot)
        if fmt not in fb.FORMATS:
            raise RuntimeError("could not tell which first-boot format this image uses")
        files = job.get("firstboot_files") or (job.get("firstboot_by_format") or {}).get(fmt)
        if not files:
            raise RuntimeError(f"the job has no first-boot settings for format {fmt}")
        fb.apply(boot, files, fmt)
        ops.flush(boot)
        wrong = fb.verify(boot, files, fmt)
        if wrong:
            raise RuntimeError("the first-boot settings did not stick: " + "; ".join(wrong))
        final = {"phase": "done", "ok": True, "bytes": result["bytes"],
                 "sha256": result["sha256"], "boot": str(boot), "format": fmt,
                 "message": "The card is ready. Put it in the Pi and power it on; "
                            "first boot takes a few minutes and restarts once."}
    except Exception as exc:                              # noqa: BLE001
        final = {"phase": "error", "ok": False, "message": str(exc),
                 "refused": isinstance(exc, Refused),
                 "detail": traceback.format_exc()[-2000:]}
    finally:
        try:
            job_path.unlink()          # it carries the WPA key
        except OSError:
            pass
    _status(job_dir, **final)
    return final


def write_job(job_dir: Path, *, disk: dk.Disk, image: Path, init_format: str,
              firstboot_files: Dict[str, str], extract_sha256: str = "",
              extract_size: int = 0,
              firstboot_by_format: Optional[Dict[str, Dict[str, str]]] = None,
              files_confirmed: bool = False) -> Path:
    """The job file for the helper (called by the non-elevated Council).
    ``firstboot_by_format`` instead of ``firstboot_files`` when the image's
    format is not known (``init_format`` ''): the helper picks by the card."""
    job_dir.mkdir(parents=True, exist_ok=True)
    p = job_dir / "job.json"
    p.write_text(json.dumps({"disk": disk.identity().to_json(), "image": str(image),
                             "init_format": init_format, "extract_sha256": extract_sha256,
                             "extract_size": extract_size,
                             "firstboot_files": firstboot_files,
                             "firstboot_by_format": firstboot_by_format or {},
                             "files_confirmed": bool(files_confirmed)}), encoding="utf-8")
    return p


def _ps_quote(s: Any) -> str:
    """A PowerShell single-quoted string that is exactly ``s``."""
    return "'" + str(s).replace("'", "''") + "'"


def start_elevated(job_path: Path) -> None:
    """Start this helper as administrator — Windows shows its UAC prompt.
    Declining the prompt is reported by the caller as a status that never
    appears (and by PowerShell's error, raised here).

    The arguments go as ONE pre-quoted string. Start-Process joins an
    -ArgumentList array with spaces and quotes nothing, so for a profile such
    as C:\\Users\\John Smith the helper got 'C:\\Users\\John' and 'Smith\\...'
    (and, elevated, wrote status.json into C:\\Users). Every string is
    single-quote escaped — an apostrophe in the interpreter's or the repo's
    path (O'Brien) used to end the string early. A Windows path cannot hold
    a double quote, so wrapping the job path in "..." is exact."""
    repo = Path(__file__).resolve().parents[2]
    arglist = f'-m council_core.pi_setup.flash_helper "{Path(job_path)}"'
    _ps(f"Start-Process -FilePath {_ps_quote(sys.executable)} "
        f"-ArgumentList {_ps_quote(arglist)} -WorkingDirectory {_ps_quote(repo)} "
        "-Verb RunAs -WindowStyle Hidden", timeout=300)


def read_status(job_dir: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads((Path(job_dir) / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def is_job_path(p: Path) -> bool:
    """True only for ``…/jobs/<uuid>/job.json`` that exists — the shape
    setup.prepare_new_pi writes. The helper runs as administrator and writes
    status.json beside its job, so a mangled or foreign path is refused
    before anything is read or written."""
    p = Path(p)
    return (p.name == "job.json" and bool(_UUID_RE.match(p.parent.name))
            and p.parent.parent.name == "jobs" and p.is_file())


def main(argv: List[str]) -> int:
    """The elevated entry point: exactly one argument, a job path."""
    if len(argv) != 2 or not is_job_path(Path(argv[1])):
        return 2
    return 0 if run(Path(argv[1])).get("ok") else 1


if __name__ == "__main__":                                 # pragma: no cover
    sys.exit(main(sys.argv))
