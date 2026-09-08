"""Tests for state.py and paths.py — atomic writes, bounded reads, where the files live.

Run: python3 -m unittest discover -s test
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nexthopd.state import write_atomic, read_json  # noqa: E402


class AtomicState(unittest.TestCase):
    def test_write_read(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "live.json"
            write_atomic(p, {"x": 1})
            self.assertEqual(read_json(p), {"x": 1})
            self.assertEqual(read_json(Path(d) / "missing.json", 42), 42)
            # No temp files left behind.
            self.assertEqual([f.name for f in Path(d).iterdir()], ["live.json"])


class StateReadSafety(unittest.TestCase):
    """The read the QML side consumes: bounded, non-blocking, no-follow,
    regular files only. Every property is enforced on the fd actually read,
    so a state file swapped at its predictable path can neither redirect
    the read, stall it, nor allocate without limit."""

    def setUp(self):
        from nexthopd.state import read_text_bounded
        self.read = read_text_bounded
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_regular_file_reads(self):
        (self.d / "live.json").write_text('{"a":1}')
        got = self.read(self.d / "live.json", 1024)
        self.assertIsNotNone(got)
        self.assertEqual(got[0], '{"a":1}')

    def test_symlink_refused_and_target_unread(self):
        secret = self.d / "secret"
        secret.write_text("SENSITIVE")
        (self.d / "live.json").symlink_to(secret)
        self.assertIsNone(self.read(self.d / "live.json", 1024))

    def test_fifo_refused_without_blocking(self):
        import os as _os
        _os.mkfifo(self.d / "live.json")
        # No writer will ever open this; a blocking open would hang here.
        self.assertIsNone(self.read(self.d / "live.json", 1024))

    def test_oversized_refused_by_the_read_itself(self):
        (self.d / "live.json").write_text("x" * 5000)
        self.assertIsNone(self.read(self.d / "live.json", 1024))

    def test_exactly_at_the_cap_is_allowed(self):
        (self.d / "live.json").write_text("y" * 1024)
        got = self.read(self.d / "live.json", 1024)
        self.assertIsNotNone(got)
        self.assertEqual(len(got[0]), 1024)

    def test_directory_refused(self):
        self.assertIsNone(self.read(self.d, 1024))

    def test_missing_file_is_none(self):
        self.assertIsNone(self.read(self.d / "nope.json", 1024))

    def test_stamp_changes_only_when_the_file_does(self):
        p = self.d / "live.json"
        p.write_text('{"a":1}')
        first = self.read(p, 1024)[1]
        self.assertEqual(self.read(p, 1024)[1], first)
        import os as _os
        p.write_text('{"a":2}')
        _os.utime(p, ns=(0, 12345))
        self.assertNotEqual(self.read(p, 1024)[1], first)

    def test_stream_keys_are_a_closed_set(self):
        from nexthopd.cli import STREAMABLE
        # The QML side names a key, never a path — nothing it passes can
        # widen what gets opened.
        self.assertEqual(sorted(STREAMABLE),
                         ["apps", "live", "manifest", "recent"])

    def test_indented_json_still_streams_as_one_line(self):
        # manifest.json is pretty-printed. Forwarding it verbatim would
        # emit several lines and the shell service would never learn the
        # version, silently disabling the update handover.
        import json as _json
        p = self.d / "manifest.json"
        p.write_text(_json.dumps({"version": "9.9.9", "kinds": ["service"]},
                                 indent=2))
        text = self.read(p, 1024)[0]
        self.assertIn("\n", text)
        line = _json.dumps(_json.loads(text), separators=(",", ":"))
        self.assertNotIn("\n", line)
        self.assertEqual(_json.loads(line)["version"], "9.9.9")

    def test_embedded_newline_cannot_break_framing(self):
        import json as _json
        p = self.d / "live.json"
        p.write_text(_json.dumps({"note": "one\ntwo", "v": 1}))
        text = self.read(p, 4096)[0]
        line = _json.dumps(_json.loads(text), separators=(",", ":"))
        self.assertNotIn("\n", line)
        self.assertEqual(_json.loads(line)["note"], "one\ntwo")


class VolatileSnapshotsLiveInTheRuntimeDir(unittest.TestCase):
    def test_runtime_dir_prefers_xdg_runtime_dir(self):
        from nexthopd import paths
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["XDG_RUNTIME_DIR"] = tmp
            self.assertEqual(paths.runtime_dir(), Path(tmp) / "nexthop")
            for fn in (paths.live_path, paths.recent_path, paths.apps_path):
                self.assertEqual(fn().parent, Path(tmp) / "nexthop")
            # History, settings and the lock never move.
            for fn in (paths.db_path, paths.lock_path):
                self.assertEqual(fn().parent, paths.state_dir())
            # No usable runtime dir: everything stays together in the
            # state dir, so a reader and a writer in the same environment
            # always agree.
            os.environ["XDG_RUNTIME_DIR"] = str(Path(tmp) / "missing")
            self.assertEqual(paths.runtime_dir(), paths.state_dir())
            os.environ.pop("XDG_RUNTIME_DIR")
            self.assertEqual(paths.runtime_dir(), paths.state_dir())

    def test_volatile_writes_do_not_fsync(self):
        from nexthopd import state
        calls = []
        self.addCleanup(setattr, state.os, "fsync", state.os.fsync)
        state.os.fsync = lambda fd: calls.append(fd)
        with tempfile.TemporaryDirectory() as tmp:
            write_atomic(Path(tmp) / "a.json", {"x": 1})
            self.assertEqual(calls, [])
            self.assertEqual(read_json(Path(tmp) / "a.json", None), {"x": 1})
            write_atomic(Path(tmp) / "b.json", {"x": 1}, durable=True)
            self.assertEqual(len(calls), 1)

    def test_legacy_snapshots_are_retired_with_a_tombstone(self):
        from nexthopd import paths, state
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "state"
            old.mkdir()
            for name in (paths.RECENT, paths.APPS):
                (old / name).write_text("{}")
            write_atomic(old / paths.LIVE, {"v": 1, "state": "online", "index": 93,
                                            "t": 1.0, "pid": 42, "pid_start": 7,
                                            "daemon_version": "0.2.21",
                                            "link": {"ssid": "Home"}})
            state.retire_legacy_snapshots(old, Path(tmp) / "runtime", now=2.0)
            self.assertFalse((old / paths.RECENT).exists())
            self.assertFalse((old / paths.APPS).exists())
            tomb = read_json(old / paths.LIVE, None)
            # An old reader shows "no data", not the last number it saw...
            self.assertEqual(tomb["state"], "no-daemon")
            self.assertIsNone(tomb["index"])
            self.assertEqual(tomb["t"], 2.0)
            # ...and an old version watch finds no pid to retire.
            for key in ("pid", "pid_start", "daemon_version"):
                self.assertNotIn(key, tomb)
            self.assertEqual(tomb["link"], {"ssid": "Home"})   # shape kept
            # Same directory (no runtime dir available): nothing to retire.
            state.retire_legacy_snapshots(old, old, now=3.0)
            self.assertEqual(read_json(old / paths.LIVE, None)["t"], 2.0)


if __name__ == "__main__":
    unittest.main()
