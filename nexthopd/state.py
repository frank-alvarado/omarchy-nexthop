"""Reading and writing the JSON state files, safely.

live.json is rewritten twice a second and is all the bar widget ever looks
at; recent.json is a pre-downsampled 30-minute window so the panel's default
graphs paint without a query; apps.json is per-application traffic. All are
written to a temp file and renamed, so a reader never sees a half-written
file. The QML side does not open any of them itself (0.1.9): `nexthop
stream` reads them through `read_text_bounded` — no symlink following, a
regular file or nothing, a size cap on the read itself — and hands the shell
one re-serialised line per record.
"""

import json
import os
import stat
import tempfile
import time
from pathlib import Path

from .paths import APPS, LIVE, RECENT


def write_atomic(path: Path, payload: dict, durable: bool = False):
    """Write a JSON file so a reader sees the old one or the new one.

    The temp file plus rename is what gives readers that guarantee, and it
    costs nothing. The fsync is a different promise — that the bytes
    survive a power cut — and none of the snapshots written here needs
    it: each is replaced within seconds of the daemon starting. On btrfs
    that fsync was 25× the payload at the block layer (62.5 KiB per 2.5 KB
    write, measured), twice a second. `durable` keeps it for a caller
    that genuinely wants it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, separators=(",", ":"))
            f.flush()
            if durable:
                os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_text_bounded(path: Path, max_bytes: int):
    """Read a state file, enforcing every property on the fd actually read.

    `O_NOFOLLOW` refuses a symlinked path outright, `O_NONBLOCK` means a
    FIFO left at the path returns instead of stalling the caller, `fstat`
    on the descriptor proves it is a regular file, and the cap bounds the
    read itself rather than trusting a size sampled beforehand. Returns
    (text, stamp) or None; `stamp` is (mtime_ns, size), enough for a
    caller to skip re-reading an unchanged file.

    This is the only way state reaches a reader — the QML side consumes it
    through `nexthop stream` rather than opening these paths itself, so an
    oversized or non-regular file can never allocate or block inside the
    long-lived shell process.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        chunks, total = [], 0
        while total <= max_bytes:
            try:
                chunk = os.read(fd, min(65536, max_bytes + 1 - total))
            except BlockingIOError:
                break
            except OSError:
                return None
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > max_bytes:
            return None
        stamp = (st.st_mtime_ns, st.st_size)
    finally:
        os.close(fd)
    try:
        return b"".join(chunks).decode("utf-8"), stamp
    except UnicodeDecodeError:
        return None


def read_json(path: Path, default=None, max_bytes: int = 4 * 1024 * 1024):
    """Bounded read of a state file, parsed. The bound lives on the read,
    not on a prior stat, for the same reason as the daemon's config read."""
    got = read_text_bounded(path, max_bytes)
    if got is None:
        return default
    try:
        return json.loads(got[0])
    except ValueError:
        return default


def retire_legacy_snapshots(old_dir: Path, new_dir: Path, now: float = None):
    """The snapshots moved to the runtime dir in 0.2.22; tidy the old place.

    `nexthop stream` is a long-lived process that resolved its paths when it
    started, so a reader from before the move keeps watching the state dir
    for as long as it lives — through the daemon handover, until the shell
    reloads the QML. Deleting live.json there would leave that reader
    holding the last number it saw, as if it were current. It is rewritten
    once instead, as a tombstone: state "no-daemon", no index, and no pid,
    so the old bar shows "no data" and the old version watch finds nothing
    to retire. recent.json and apps.json are simply removed.
    """
    old_dir, new_dir = Path(old_dir), Path(new_dir)
    if old_dir == new_dir:
        return
    now = time.time() if now is None else now
    for name in (RECENT, APPS):
        try:
            (old_dir / name).unlink()
        except OSError:
            pass
    live = old_dir / LIVE
    got = read_text_bounded(live, 256 * 1024)
    if got is None:
        return
    try:
        payload = json.loads(got[0])
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        try:
            live.unlink()
        except OSError:
            pass
        return
    for key in ("pid", "pid_start", "daemon_version"):
        payload.pop(key, None)
    payload.update({"t": round(now, 3), "state": "no-daemon", "index": None,
                    "band": None, "down_since": None})
    write_atomic(live, payload, durable=True)
