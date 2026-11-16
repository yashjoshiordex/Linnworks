"""Local file primitives. No API writes, rule decisions, or automated dispatch.

Call these inside one run_lock. Durable dispatch/operation tracking is required
before wiring them into a production runner (Deliverable 4).
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import ctypes
import errno
import logging
import math
import os
import re
import stat
import sys
import tempfile
import time

from lwsync.pipeline.csv_io import CsvDocument, CsvRecord


class UnstableFileError(OSError):
    """File is too recent, changed during reading, or is not a regular file."""


class RunLockedError(RuntimeError):
    """Another run owns the shared flock."""


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    data: bytes
    identity: tuple[int, int, int, int, int]


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    # On Windows ctime is creation time; path/handle caches may disagree after
    # writes. On Linux it is a useful additional mutation signal.
    changed = info.st_ctime_ns if os.name == "posix" else 0
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, changed


def _stable_stat(path: Path, seconds: float, now: float) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or now - info.st_mtime < seconds:
        raise UnstableFileError("Input is not a stable regular file")
    return info


def classify_file(path: Path, region: str) -> str | None:
    region = region.lower()
    if region not in ("us", "ca"):
        raise ValueError("Unknown pipeline region")
    match = re.fullmatch(rf"{region}_(ajc|cs)_orders[^/\\]*\.csv", path.name)
    return match.group(1) if match else None


def pickup_files(folder: Path, region: str, *, stable_seconds: float = 60,
                 now: float | None = None) -> tuple[Path, ...]:
    if not math.isfinite(stable_seconds) or stable_seconds < 60:
        raise ValueError("Stable interval must be at least 60 seconds")
    current = time.time() if now is None else now
    # Validate region even for an empty input directory.
    classify_file(Path(""), region)
    ready = []
    for path in sorted(folder.iterdir()):
        if classify_file(path, region) is None:
            continue
        try:
            _stable_stat(path, stable_seconds, current)
        except (FileNotFoundError, UnstableFileError):
            continue
        ready.append(path)
    return tuple(ready)


def read_stable(path: Path, *, stable_seconds: float = 60,
                now: float | None = None) -> FileSnapshot:
    if not math.isfinite(stable_seconds) or stable_seconds < 60:
        raise ValueError("Stable interval must be at least 60 seconds")
    before = _stable_stat(path, stable_seconds, time.time() if now is None else now)
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
            raise UnstableFileError("Input changed before reading")
        data = stream.read()
        after = os.fstat(stream.fileno())
    if _identity(before) != _identity(after) or _identity(after) != _identity(path.lstat()):
        raise UnstableFileError("Input changed while reading")
    return FileSnapshot(path, data, _identity(after))


def assert_unchanged(snapshot: FileSnapshot) -> None:
    if _identity(snapshot.path.lstat()) != snapshot.identity:
        raise UnstableFileError("Input changed after validation")


@contextmanager
def run_lock(path: Path):
    """Linux flock, held on an open descriptor. Never unlink the lock file."""
    if not sys.platform.startswith("linux"):
        raise RuntimeError("Production pipeline locking requires Linux flock")
    import fcntl

    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o660)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Lock must be a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunLockedError("Pipeline run already active") from None
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _sync_directory(folder: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _rename_exclusive(source: Path, destination: Path) -> None:
    """Atomic rename without an exists/rename race or accidental replacement."""
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise RuntimeError("Atomic publication requires Linux renameat2")
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        # AT_FDCWD=-100, RENAME_NOREPLACE=1. Fail closed on unsupported filesystems.
        if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
    elif os.name == "nt":
        # Windows rename already refuses to replace an existing destination.
        os.rename(source, destination)
    else:
        raise RuntimeError("Atomic exclusive rename is unsupported on this platform")


def atomic_publish(data: bytes, destination: Path, temp_dir: Path) -> Path:
    """Stage outside the watched directory, fsync and rename; never overwrite.

    Folders must be provisioned first (setgid/group ownership on Linux). Caller
    sets umask 0007 for the worker process. New files are explicitly mode 0660.
    A failure after rename may mean publication succeeded: never blindly retry.
    """
    destination = Path(destination)
    staging = temp_dir.resolve(strict=True)
    watched = destination.parent.resolve(strict=True)
    if staging == watched or watched in staging.parents:
        raise ValueError("Staging must be outside the watched output directory")
    if staging.stat().st_dev != watched.stat().st_dev:
        raise OSError(errno.EXDEV, "Staging and destination must share a filesystem")
    descriptor, name = tempfile.mkstemp(prefix=".pipeline-", suffix=".tmp", dir=staging)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if os.name == "posix":
                os.fchown(stream.fileno(), -1, watched.stat().st_gid)
                os.fchmod(stream.fileno(), 0o660)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _rename_exclusive(temporary, destination)
        _sync_directory(watched)
        _sync_directory(staging)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def publish_csv(document: CsvDocument, destination: Path, temp_dir: Path,
                rows: tuple[CsvRecord, ...] | None = None) -> Path | None:
    """Publish selected rows; an all-held/empty subset emits nothing.

    Selection and edited records come from the later engine/runner. This makes
    no fulfillment decision and does not claim or track dispatches.
    """
    selected = document.rows if rows is None else rows
    if not selected:
        return None
    return atomic_publish(document.render(selected), destination, temp_dir)


def move_input(snapshot: FileSnapshot, folder: Path, *, archive_date: date | None = None) -> Path:
    """Move validated input to archive/error; preserve bytes and refuse collisions.

    For archives supply archive_date; for errors pass the error folder directly.
    Call only after durable outcomes. Cross-filesystem moves fail rather than copy.
    """
    assert_unchanged(snapshot)
    target = folder / archive_date.isoformat() if archive_date is not None else folder
    target.mkdir(mode=0o770, parents=True, exist_ok=True)
    destination = target / snapshot.path.name
    _rename_exclusive(snapshot.path, destination)
    _sync_directory(target)
    _sync_directory(snapshot.path.parent)
    return destination


def passthrough_cs(snapshot: FileSnapshot, region: str, output_dir: Path, temp_dir: Path) -> Path:
    """Publish CS bytes unchanged; caller tracks/archive input.

    Deliberately does not parse or rewrite CS files. Production runner must claim
    dispatch identity before invoking this primitive, including on crash recovery.
    """
    if classify_file(snapshot.path, region) != "cs":
        raise ValueError("CS passthrough requires a regional CS filename")
    assert_unchanged(snapshot)
    logging.getLogger("lwsync").warning(
        "[%s] CS export found in AJC input; publishing unchanged", region.upper()
    )
    return atomic_publish(snapshot.data, output_dir / snapshot.path.name, temp_dir)
