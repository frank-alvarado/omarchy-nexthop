"""Shared fixtures for the nexthopd tests.

Fixtures under fixtures/ are recorded from a real Arch laptop (ping from
iputils, iw 6.x) — the formats these parsers exist to survive. The ss
fixture is synthetic (RFC 5737 addresses).

Run everything: python3 -m unittest discover -s test
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parent.parent


def run_inline(fn):
    """A synchronous spawn for watchers that run their check off the loop."""
    fn()


def run_now(fn):
    """A spawn for tests: run the check inline so its result lands this tick."""
    fn()


def _st(count=60, loss=0.0, p50=20.0, p95=30.0):
    return {"count": count, "loss": loss, "p50": p50, "p95": p95}


class FakeStore:
    def __init__(self):
        self.opened = []
        self.closed = []

    def open_event(self, ts, kind, sev, leg, detail):
        self.opened.append((kind, sev, leg, detail))
        return len(self.opened)

    def close_event(self, event_id, ts):
        self.closed.append(event_id)


class StubEvents:
    """Stands in for NlEvents: answers cause_for() with a fixed cause."""

    def __init__(self, cause=None, raise_=False):
        self.cause, self.raise_, self.calls = cause, raise_, []

    def cause_for(self, bssid, now, window):
        self.calls.append((bssid, now, window))
        if self.raise_:
            raise RuntimeError("boom")
        return self.cause


class _FakeDaemonForDisruption:
    """Just enough of Daemon to exercise record_disruption."""

    def __init__(self, store):
        self.store = store

    from nexthopd.daemon import Daemon as _D
    record_disruption = _D.record_disruption
    del _D
