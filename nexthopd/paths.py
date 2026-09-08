"""Where Nexthop keeps its state.

Two directories since 0.2.22. The state dir (XDG_STATE_HOME, like the rest
of Omarchy) holds what must outlive the session: history.db, the
config.json the panel writes for the daemon, and the lock so two daemons
never fight. The runtime dir (XDG_RUNTIME_DIR, a per-user tmpfs) holds the
three snapshots the daemon rewrites continuously — live.json twice a
second, apps.json every three, recent.json every five. They are derived,
session-scoped, and worth nothing after a reboot, and on this laptop's
btrfs each 2.5 KB rewrite cost 62.5 KiB at the block layer with fsync:
14.65 GB a day for a bar widget, measured from the kernel's own counter.
On tmpfs it is nothing. Without XDG_RUNTIME_DIR they fall back to the
state dir, so reader and writer always agree in one session environment.
"""

import os
from pathlib import Path


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "nexthop"


def ensure_state_dir() -> Path:
    """Create the state dir, private to the user (0700).

    The files inside carry the daemon's pid and the version string the
    shell service uses to authorize a SIGTERM — nothing another account
    has any business reading, let alone writing.
    """
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


def runtime_dir() -> Path:
    """The volatile snapshots' home: the session's tmpfs, else the state dir."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        return Path(base) / "nexthop"
    return state_dir()


def ensure_runtime_dir() -> Path:
    """Create the runtime dir, private to the user like the state dir."""
    d = runtime_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


LIVE = "live.json"
RECENT = "recent.json"
APPS = "apps.json"
DB = "history.db"
LOCK = "nexthopd.lock"


def live_path() -> Path:
    return runtime_dir() / LIVE


def recent_path() -> Path:
    return runtime_dir() / RECENT


def apps_path() -> Path:
    return runtime_dir() / APPS


def manifest_path() -> Path:
    """The plugin's own manifest, beside this package rather than in the
    state dir — the shell service reads it to spot a fast-forwarded
    checkout."""
    return Path(__file__).resolve().parent.parent / "manifest.json"


def db_path() -> Path:
    return state_dir() / DB


def lock_path() -> Path:
    return state_dir() / LOCK
