"""Tests for speedtest.py — target vetting, pass sizing, and JSON we did not write.

Run: python3 -m unittest discover -s test
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))



class UntrustedTargets(unittest.TestCase):
    """fast.com nominates its own download hosts, so that JSON decides
    what this daemon connects to and is hostile input, not a server list.

    Literal addresses throughout, so nothing here touches DNS.
    """

    def setUp(self):
        from nexthopd.speedtest import vet_target
        self.vet = vet_target

    def test_plaintext_is_refused(self):
        self.assertIsNone(self.vet("http://93.184.216.34/download"))

    def test_non_http_schemes_are_refused(self):
        for url in ("file:///etc/passwd", "ftp://93.184.216.34/x",
                    "gopher://93.184.216.34/x", "scp://93.184.216.34/x",
                    "dict://93.184.216.34/x"):
            self.assertIsNone(self.vet(url), url)

    def test_loopback_is_refused(self):
        for url in ("https://127.0.0.1/x", "https://127.1.2.3/x",
                    "https://[::1]/x"):
            self.assertIsNone(self.vet(url), url)

    def test_private_ranges_are_refused(self):
        for url in ("https://192.168.1.1/x", "https://10.0.0.1/x",
                    "https://172.16.4.2/x", "https://[fd00::1]/x"):
            self.assertIsNone(self.vet(url), url)

    def test_cloud_metadata_address_is_refused(self):
        # The link-local address every SSRF write-up ends at.
        self.assertIsNone(self.vet("https://169.254.169.254/latest/meta-data/"))

    def test_ipv4_mapped_private_address_is_refused(self):
        # ::ffff:192.168.1.1 is a private address wearing an IPv6 coat.
        self.assertIsNone(self.vet("https://[::ffff:192.168.1.1]/x"))

    def test_unspecified_and_broadcast_refused(self):
        self.assertIsNone(self.vet("https://0.0.0.0/x"))
        self.assertIsNone(self.vet("https://255.255.255.255/x"))

    def test_garbage_is_refused_without_raising(self):
        for url in ("", "not a url", "https://", "https:///x", "https://:443/x"):
            self.assertIsNone(self.vet(url), repr(url))

    def test_public_https_is_accepted_and_pinned(self):
        got = self.vet("https://8.8.8.8/download?size=25000000")
        self.assertIsNotNone(got)
        url, resolve = got
        self.assertEqual(url, "https://8.8.8.8/download?size=25000000")
        # The vetted address is pinned, so curl cannot resolve the name
        # again and be handed a different one.
        self.assertEqual(resolve, "8.8.8.8:443:8.8.8.8")

    def test_explicit_port_is_carried_into_the_pin(self):
        got = self.vet("https://8.8.8.8:8443/x")
        self.assertIsNotNone(got)
        self.assertEqual(got[1], "8.8.8.8:8443:8.8.8.8")

    def test_a_bad_target_costs_one_candidate_not_the_test(self):
        urls = ["http://93.184.216.34/a", "https://169.254.169.254/b",
                "https://8.8.8.8/c"]
        vetted = [v for v in (self.vet(u) for u in urls) if v]
        self.assertEqual(len(vetted), 1)
        self.assertEqual(vetted[0][0], "https://8.8.8.8/c")

    def test_curl_is_invoked_with_a_scheme_floor(self):
        # Belt and braces beside the vetting: curl itself refuses
        # anything but TLS, whatever it is handed.
        import inspect
        from nexthopd import speedtest
        src = inspect.getsource(speedtest._curl)
        self.assertIn('"--proto", "=https"', src)


class PeakSizing(unittest.TestCase):
    """The sustained pass is sized from the estimate, floored and capped."""

    def test_sized_for_ten_seconds_at_measured_rate(self):
        from nexthopd import speedtest
        # 160 Mbps line over 4 streams: each stream carries 40 Mbps.
        n = speedtest._sized_pass(160.0 / speedtest.PEAK_STREAMS,
                                  speedtest.PEAK_DOWN_FLOOR,
                                  speedtest.CLOUDFLARE_DOWN_MAX)
        self.assertEqual(n, 50_000_000)
        self.assertAlmostEqual(speedtest._pass_seconds(40.0, n), 10.0)

    def test_slow_line_stays_small(self):
        from nexthopd import speedtest
        n = speedtest._sized_pass(10.0, speedtest.PEAK_DOWN_FLOOR,
                                  speedtest.CLOUDFLARE_DOWN_MAX)
        self.assertEqual(n, 12_500_000)

    def test_caps_bound_both_directions(self):
        from nexthopd import speedtest
        # __down 403s at 100 MB and above — the per-stream cap must stay under.
        self.assertLess(speedtest.CLOUDFLARE_DOWN_MAX, 100_000_000)
        self.assertEqual(speedtest._sized_pass(10_000.0, speedtest.PEAK_DOWN_FLOOR,
                                               speedtest.CLOUDFLARE_DOWN_MAX),
                         speedtest.CLOUDFLARE_DOWN_MAX)
        self.assertEqual(speedtest._sized_pass(0.1, speedtest.PEAK_UP_FLOOR,
                                               speedtest.PEAK_UP_CAP),
                         speedtest.PEAK_UP_FLOOR)


class RemoteJsonShapes(unittest.TestCase):
    """The peak engines parse JSON we did not write. A wrong shape is a
    failed engine, never an exception escaping the worker thread."""

    def test_fast_com_wrong_shapes_fail_closed(self):
        from nexthopd import speedtest

        class R:
            returncode = 0

        orig = speedtest._curl
        self.addCleanup(setattr, speedtest, "_curl", orig)
        for body in ('[1, 2]', '{"targets": [1, 2]}', '{"targets": "x"}',
                     '{"targets": [{"url": 5}]}', 'null'):
            r = R()
            r.stdout = body
            speedtest._curl = lambda args, timeout, r=r: r
            self.assertIsNone(speedtest._peak_fast(), body)

    def test_ookla_wrong_shapes_fail_closed(self):
        from nexthopd import speedtest

        class R:
            returncode = 0

        self.addCleanup(setattr, speedtest.subprocess, "run",
                        speedtest.subprocess.run)
        self.addCleanup(setattr, speedtest.shutil, "which",
                        speedtest.shutil.which)
        speedtest.shutil.which = lambda name: "/usr/bin/true"
        for body in ('{"download": "x"}', '[1]', 'null',
                     '{"download": {"bandwidth": "fast"}, "upload": {"bandwidth": 1},'
                     ' "ping": {"latency": 1}, "server": {}}'):
            r = R()
            r.stdout = body
            speedtest.subprocess.run = lambda *a, r=r, **k: r
            self.assertIsNone(speedtest._peak_ookla(), body)


if __name__ == "__main__":
    unittest.main()
