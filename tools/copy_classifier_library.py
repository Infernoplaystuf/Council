"""
tools/copy_classifier_library.py — copy a classifier library into another
store, reading the original and never writing to it.

WHY
"Typhon (plugins)" keeps its classifiers in a store of its own, in its side
vault, and that store starts empty (docs/typhon_plugins_design.md 7.1,
question 10; docs/typhon_plugins_branch.md). This brings today's library in.

The apps' own Export cannot be used for that: it takes each classifier's lock
(a file in <store>/.locks) and puts its versions in order first, which can
write into the store it exports from (frame_classes._export_bytes). This only
reads the original: every file is opened for reading, nothing there is
created, locked, renamed or changed, and a test checks the original is the
same file for file, byte for byte and time for time afterwards.

WHAT IT DOES
For each classifier folder in the original (not .locks, not .deleted, not any
other dot-folder), in name order:

  * a classifier the destination already has is SKIPPED, never merged or
    replaced — whatever is there was made or changed in the other app;
  * otherwise its files are copied into a temporary folder inside the
    destination, every copy is checked against its original's sha256, and
    the folder is renamed into place in one step.

The copies keep their own records unchanged (where each was made, its
versions, its marks, its run record), so the other app lists them as made by
today's Typhon. Close today's Typhon, or at least do not Train or Mark in it,
while this runs: a classifier changed half-way through its copy is copied as
the files were when each was read.

    python tools/copy_classifier_library.py
        from ~/.council/vault/classifiers
        to   ~/Typhon-plugins-vault/classifiers
    python tools/copy_classifier_library.py --from D:/vault/classifiers \\
        --to D:/tp-vault/classifiers
    python tools/copy_classifier_library.py --dry-run     # say, copy nothing

A destination under ~/.council is refused (that is the Council's own app
folder and its real vault), as are a destination inside the original, the
original inside the destination, and the two being one folder.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

#: The folders of a store that are not classifiers: the locks and what Delete
#: moved aside (frame_classes.LOCKS, DELETED), and any other dot-folder.
NOT_CLASSIFIERS = (".locks", ".deleted")


def default_source() -> Path:
    return Path.home() / ".council" / "vault" / "classifiers"


def default_destination() -> Path:
    return Path.home() / "Typhon-plugins-vault" / "classifiers"


@dataclass
class Copied:
    dry_run: bool = False
    copied: List[str] = field(default_factory=list)
    skipped: Dict[str, str] = field(default_factory=dict)
    files: int = 0
    bytes: int = 0

    def lines(self) -> List[str]:
        out = []
        if self.copied:
            verb = "would copy" if self.dry_run else "copied"
            out.append(f"{verb} {len(self.copied)} classifier(s), "
                       f"{self.files} file(s), {self.bytes} bytes: "
                       f"{', '.join(self.copied)}")
        for name, why in self.skipped.items():
            out.append(f"skipped {name}: {why}")
        if not out:
            out.append("the original has no classifiers to copy")
        return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def problem(source: Path, destination: Path) -> str:
    """Why `source` cannot be copied into `destination`, or ""."""
    if not source.is_dir():
        return f"there is no classifier store at {source}"
    real = (Path.home() / ".council").resolve()
    if _inside(destination, real):
        return (f"{destination} is inside {real}, the Council's own app "
                f"folder and its real vault: the copy must go to another "
                f"store (the side vault's)")
    if source == destination:
        return f"{source} is both the original and the destination"
    if _inside(destination, source):
        return f"{destination} is inside the original {source}"
    if _inside(source, destination):
        return f"the original {source} is inside {destination}"
    return ""


def classifiers_in(source: Path) -> List[Path]:
    """The classifier folders of a store, in name order."""
    return sorted((p for p in source.iterdir()
                   if p.is_dir() and not p.name.startswith(".")
                   and p.name not in NOT_CLASSIFIERS),
                  key=lambda p: p.name)


def _files(folder: Path) -> List[Path]:
    out = []
    for root, dirs, names in os.walk(folder):
        # A ".tmp-" folder is a version frame_classes was still writing.
        dirs[:] = sorted(d for d in dirs if d not in NOT_CLASSIFIERS
                         and not d.startswith(".tmp-"))
        out += [Path(root) / n for n in sorted(names)]
    return out


def copy_library(source: Path, destination: Path, *,
                 dry_run: bool = False) -> Copied:
    """Copy every classifier of `source` that `destination` lacks."""
    source = Path(source).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()
    why = problem(source, destination)
    if why:
        raise ValueError(why)
    done = Copied(dry_run=dry_run)
    if not dry_run:
        destination.mkdir(parents=True, exist_ok=True)
    for folder in classifiers_in(source):
        name = folder.name
        target = destination / name
        if target.exists():
            done.skipped[name] = ("the destination already has a classifier "
                                  "of this name; left as it is")
            continue
        files = _files(folder)
        if dry_run:
            done.copied.append(name)
            done.files += len(files)
            done.bytes += sum(f.stat().st_size for f in files)
            continue
        temp = destination / f".copying-{name}-{os.getpid()}"
        if temp.exists():
            done.skipped[name] = f"{temp.name} is in the way; left as it is"
            continue
        try:
            for f in files:
                out = temp / f.relative_to(folder)
                out.parent.mkdir(parents=True, exist_ok=True)
                # copyfile opens the original for reading only; copystat
                # reads its times and sets them on the copy.
                shutil.copyfile(f, out)
                shutil.copystat(f, out)
                if _sha256(out) != _sha256(f):
                    raise OSError(f"{out} does not match {f} after copying")
            os.rename(temp, target)        # refuses a name taken meanwhile
        except BaseException as exc:
            done.skipped[name] = (f"not copied: {exc}; the partial copy is "
                                  f"left in {temp}")
            if not isinstance(exc, Exception):
                raise
            continue
        done.copied.append(name)
        done.files += len(files)
        done.bytes += sum(f.stat().st_size for f in files)
    return done


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Copy a classifier library into another store, reading "
                    "the original only.")
    ap.add_argument("--from", dest="source", default=str(default_source()))
    ap.add_argument("--to", dest="destination",
                    default=str(default_destination()))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        done = copy_library(Path(a.source), Path(a.destination),
                            dry_run=a.dry_run)
    except ValueError as exc:
        print(f"refused: {exc}")
        return 2
    print(f"{'would copy' if a.dry_run else 'copied'} from {a.source}")
    print(f"into {a.destination}")
    for line in done.lines():
        print(line)
    bad = [n for n, why in done.skipped.items() if why.startswith("not copied")]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
