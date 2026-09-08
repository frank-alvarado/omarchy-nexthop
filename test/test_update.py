"""Tests for update.py — notify, never install.

Run: python3 -m unittest discover -s test
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nexthopd.daemon import Config  # noqa: E402
from nexthopd.update import RE_SHA, UpdateWatch, verdict  # noqa: E402
from support import REPO, run_inline  # noqa: E402


class UpdateNotice(unittest.TestCase):
    """The update check reports; it must never act, and never trust a remote
    string far enough to hand it to a subprocess unchecked."""

    A = "a" * 40
    B = "b" * 40

    def test_verdict_states(self):
        self.assertEqual(verdict(self.A, self.A, True, False, False), "current")
        # Origin holds a commit we have never seen.
        self.assertEqual(verdict(self.A, self.B, False, False, False), "behind")
        # THE CASE THAT MATTERS: `omarchy plugin update` fetches before it
        # shows its diff, so a user who looked and declined already holds
        # origin's commit while still being behind it. Deciding from "we have
        # never seen that object" alone would show that user nothing.
        self.assertEqual(verdict(self.A, self.B, True, True, False), "behind")
        # A developer checkout ahead of origin must not be nagged.
        self.assertEqual(verdict(self.A, self.B, True, False, True), "ahead")
        self.assertEqual(verdict(self.A, self.B, True, False, False), "diverged")
        # Not knowing is its own answer, not a guess in either direction.
        self.assertEqual(verdict("", self.B, False, False, False), "unknown")
        self.assertEqual(verdict(self.A, "", False, False, False), "unknown")

    def test_remote_sha_must_be_an_object_id(self):
        # This is the guard that matters: the value arrives from the network
        # and is then passed as an argument to git.
        for bad in ("", "HEAD", "a" * 39, "a" * 41, "A" * 40, "g" * 40,
                    "../../etc/passwd", "a" * 40 + " --upload-pack=sh",
                    "--upload-pack=evil", "a" * 40 + "\n" + "b" * 40):
            self.assertIsNone(RE_SHA.match(bad), bad)
        self.assertIsNotNone(RE_SHA.match(self.A))

    def test_junk_from_the_remote_yields_unknown_not_a_crash(self):
        w = UpdateWatch(repo=REPO)
        calls = []

        def fake(*args, capture=True):
            calls.append(args)
            if args[0] == "rev-parse":
                return 0, self.A
            if args[0] == "ls-remote":
                return 0, "not-a-sha\tHEAD"
            return 0, ""

        w._git = fake
        self.assertEqual(w.check(), "unknown")
        # Having refused the answer, it must not go on to use it.
        self.assertNotIn("cat-file", [c[0] for c in calls])

    def test_behind_is_reported_without_any_write(self):
        w = UpdateWatch(repo=REPO)
        seen = []

        def fake(*args, capture=True):
            seen.append(args[0])
            if args[0] == "rev-parse":
                return 0, self.A
            if args[0] == "ls-remote":
                return 0, self.B + "\tHEAD"
            if args[0] == "cat-file":
                return 1, ""          # we do not hold origin's commit
            return 0, ""

        w._git = fake
        self.assertEqual(w.check(), "behind")
        # Every git verb used must be read-only. A fetch, pull, merge or
        # checkout here would make this self-updating code.
        self.assertTrue(set(seen) <= {"rev-parse", "ls-remote", "cat-file",
                                      "merge-base"}, seen)

    def test_cadence_delays_the_first_check_and_then_spaces_them(self):
        w = UpdateWatch(repo=REPO, spawn=run_inline)
        w.check = lambda: "behind"
        w.tick(1000.0)
        # Nothing on the first tick: the daemon restarts with the shell, and
        # a check on every restart would be noise.
        self.assertIsNone(w.checked_ts)
        w.tick(1000.0 + 299)
        self.assertIsNone(w.checked_ts)
        w.tick(1000.0 + 301)
        self.assertEqual(w.state, "behind")
        self.assertTrue(w.snapshot()["available"])
        # ...and then not again for a day.
        first = w.checked_ts
        w.check = lambda: "current"
        w.tick(1000.0 + 3600)
        self.assertEqual(w.checked_ts, first)
        w.tick(1000.0 + 301 + 24 * 3600 + 1)
        self.assertEqual(w.state, "current")
        self.assertFalse(w.snapshot()["available"])

    def test_disabled_makes_no_check_and_clears_any_notice(self):
        w = UpdateWatch(repo=REPO, spawn=run_inline)
        w.check = lambda: "behind"
        w.tick(1000.0)
        w.tick(1000.0 + 301)
        self.assertTrue(w.snapshot()["available"])
        # Turning the setting off must retract the notice, not leave a stale
        # one on screen.
        w.enabled = False
        called = []
        w.check = lambda: called.append(1) or "behind"
        w.tick(1000.0 + 301 + 24 * 3600 + 1)
        self.assertEqual(called, [])
        self.assertIsNone(w.snapshot())

    def test_nothing_published_until_something_is_known(self):
        w = UpdateWatch(repo=REPO)
        self.assertIsNone(w.snapshot())

    def test_real_checkout_is_read_only_and_answers(self):
        """Against this actual repository: a real answer, and the working
        tree and refs are untouched afterwards."""
        before = subprocess.run(["git", "-C", str(REPO), "status",
                                 "--porcelain"], capture_output=True, text=True)
        head_before = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                                     capture_output=True, text=True).stdout
        w = UpdateWatch(repo=REPO)
        self.assertIn(w.check(), ("current", "behind", "ahead", "diverged",
                                  "unknown"))
        after = subprocess.run(["git", "-C", str(REPO), "status",
                                "--porcelain"], capture_output=True, text=True)
        head_after = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                                    capture_output=True, text=True).stdout
        self.assertEqual(before.stdout, after.stdout)
        self.assertEqual(head_before, head_after)

    def test_real_clone_one_commit_behind_reports_behind(self):
        """End to end against real git: a checkout that already holds
        origin's commit but sits one behind it must offer the update."""
        with tempfile.TemporaryDirectory() as tmp:
            clone = Path(tmp) / "c"
            r = subprocess.run(["git", "clone", "--quiet", str(REPO), str(clone)],
                               capture_output=True)
            if r.returncode != 0:
                self.skipTest("git clone unavailable")
            head = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD~1"],
                                  capture_output=True, text=True)
            if head.returncode != 0:
                self.skipTest("shallow history")
            subprocess.run(["git", "-C", str(clone), "reset", "--quiet",
                            "--hard", "HEAD~1"], check=True,
                           capture_output=True)
            w = UpdateWatch(repo=clone, spawn=run_inline)
            self.assertEqual(w.check(), "behind")
            self.assertTrue(w.snapshot() is None)   # nothing until tick() runs
            w.tick(1000.0)
            w.tick(1000.0 + 301)
            self.assertTrue(w.snapshot()["available"])

    def test_config_default_is_on_and_validates_as_a_boolean(self):
        cfg = Config.SCHEMA["updateCheck"]
        self.assertTrue(cfg[0])
        self.assertIs(cfg[1](True), True)
        self.assertIs(cfg[1](False), False)
        # A wrong type falls back to the default rather than being coerced,
        # the same rule every other setting follows.
        self.assertIsNone(cfg[1]("yes"))

    def test_manifest_exposes_the_setting(self):
        m = json.loads((REPO / "manifest.json").read_text())
        entry = next(e for e in m["barWidget"]["schema"]
                     if e["key"] == "updateCheck")
        self.assertEqual(entry["type"], "boolean")
        self.assertIs(entry["defaultValue"], True)
        self.assertIn("never installs", entry["description"])


class UpdateCheckIsPinnedToThePlugin(unittest.TestCase):
    """`git -C <dir>` walks up until it finds a repository. A copy-install
    with no .git of its own inside a dotfiles checkout must answer unknown,
    not the dotfiles' status — and must not contact the dotfiles' remote."""

    def test_a_plain_directory_inside_a_repo_is_not_that_repo(self):
        import shutil, subprocess
        if not shutil.which("git"):
            self.skipTest("git not installed")
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", d], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            sub = Path(d) / "plugins" / "io.github.x3me.nexthop"
            sub.mkdir(parents=True)
            w = UpdateWatch(repo=sub)
            self.assertIsNone(w._local_head())
            self.assertEqual(w.check(), "unknown")


class UpdateCheckOffTheLoop(unittest.TestCase):
    """The daily check may sit at git's 20 s timeout on a bad network; the
    loop that owns the outage watch must never wait for it."""

    def watch(self):
        held = []
        w = UpdateWatch(repo=REPO, spawn=held.append)   # records, never runs
        w.check = lambda: "behind"
        return w, held

    def test_tick_returns_before_the_check_finishes(self):
        w, held = self.watch()
        w.tick(1000.0)
        w.tick(1000.0 + 301)
        self.assertEqual(len(held), 1)
        self.assertEqual(w.state, "unknown")     # nothing adopted yet
        self.assertIsNone(w.snapshot())
        held[0]()                                # the worker finishes
        w.tick(1000.0 + 302)
        self.assertEqual(w.state, "behind")
        self.assertEqual(w.checked_ts, 1302)

    def test_no_second_check_while_one_is_in_flight(self):
        w, held = self.watch()
        w.tick(1000.0)
        w.tick(1000.0 + 301)
        w.tick(1000.0 + 301 + 24 * 3600 + 1)     # due again, first never returned
        self.assertEqual(len(held), 1)

    def test_disabling_drops_an_answer_still_in_flight(self):
        w, held = self.watch()
        w.tick(1000.0)
        w.tick(1000.0 + 301)
        w.enabled = False
        w.tick(1000.0 + 302)
        held[0]()
        w.tick(1000.0 + 303)
        self.assertIsNone(w.snapshot())
        w.enabled = True
        w.tick(1000.0 + 304)
        # Re-enabled: the answer that landed while off must not surface.
        self.assertIsNone(w.snapshot())


if __name__ == "__main__":
    unittest.main()
