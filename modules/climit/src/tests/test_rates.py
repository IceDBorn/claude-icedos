import unittest
from datetime import datetime, timezone

from climit.rates import compute, parse_iso

MIN = 60_000
FUTURE = "2999-01-01T00:00:00+00:00"


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


class TestRates(unittest.TestCase):
    def test_flat_no_burn(self):
        rows = [(0, 20.0, FUTURE), (50 * MIN, 20.0, FUTURE)]
        r = compute("w", rows, 100 * MIN, lookback_min=60)
        self.assertEqual(r.util, 20.0)
        self.assertAlmostEqual(r.per_min, 0.0, places=6)
        self.assertIsNone(r.runway_min)
        self.assertFalse(r.will_exhaust_before_reset)

    def test_linear_burn_per_hour(self):
        rows = [(0, 10.0, FUTURE), (60 * MIN, 20.0, FUTURE)]  # +10 over 60 min
        r = compute("w", rows, 60 * MIN, lookback_min=60)
        self.assertAlmostEqual(r.per_hour, 10.0, places=3)
        self.assertAlmostEqual(r.per_8h, 80.0, places=3)
        self.assertAlmostEqual(r.per_day, 240.0, places=2)
        self.assertAlmostEqual(r.runway_min, 480.0, places=1)  # 80 left / 10 per hr = 8h

    def test_lookback_carry_forward(self):
        # burst to 30 at t=20min, then flat; 60-min window anchors at util_start=10
        rows = [(0, 10.0, FUTURE), (20 * MIN, 30.0, FUTURE), (60 * MIN, 30.0, FUTURE)]
        r = compute("w", rows, 60 * MIN, lookback_min=60)
        self.assertAlmostEqual(r.per_hour, 20.0, places=3)

    def test_reset_never_negative(self):
        # util drops 90 -> 5 with a resets_at change: boundary moves, rate stays >= 0
        rows = [(0, 80.0, "A"), (60 * MIN, 90.0, "A"), (120 * MIN, 5.0, "B"), (130 * MIN, 7.0, "B")]
        r = compute("w", rows, 130 * MIN, lookback_min=60)
        self.assertGreaterEqual(r.per_min, 0.0)
        self.assertAlmostEqual(r.per_hour, 12.0, places=1)  # +2 over 10 min post-reset

    def test_exhaust_vs_reset_flag(self):
        base = 1_800_000_000_000  # arbitrary realistic epoch ms
        reset = iso(base + 30 * MIN)  # window resets in 30 min
        # 50%/hr → 50 left → 60 min runway > 30 min → resets first
        slow = [(base - 60 * MIN, 0.0, reset), (base, 50.0, reset)]
        self.assertFalse(compute("w", slow, base, lookback_min=60).will_exhaust_before_reset)
        # 80%/hr → 20 left → 15 min runway < 30 min → hits cap first
        fast = [(base - 60 * MIN, 0.0, reset), (base, 80.0, reset)]
        self.assertTrue(compute("w", fast, base, lookback_min=60).will_exhaust_before_reset)

    def test_parse_iso(self):
        self.assertIsNotNone(parse_iso("2026-07-21T21:00:00.015050+00:00"))
        self.assertIsNotNone(parse_iso("2026-07-21T21:00:00Z"))
        self.assertIsNone(parse_iso(None))
        self.assertIsNone(parse_iso("not-a-date"))

    def test_jitter_resets_at_not_a_reset(self):
        # The live endpoint returns resets_at that wobbles sub-second between
        # polls (20:59:59.9 vs 21:00:00.1 around the same boundary). That must
        # NOT read as a window reset — otherwise the rate window collapses and
        # burn stays pinned at 0 even while utilization climbs.
        rows = [
            (0, 27.0, "2026-07-21T21:00:00.1+00:00"),
            (30 * MIN, 28.0, "2026-07-21T20:59:59.9+00:00"),
            (60 * MIN, 29.0, "2026-07-21T21:00:00.2+00:00"),
        ]
        r = compute("seven_day", rows, 60 * MIN, lookback_min=60)
        self.assertAlmostEqual(r.per_hour, 2.0, places=3)  # +2 over 60 min, not 0
        self.assertIsNotNone(r.runway_min)


if __name__ == "__main__":
    unittest.main()
