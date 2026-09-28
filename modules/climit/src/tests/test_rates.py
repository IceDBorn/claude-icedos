import unittest
from datetime import datetime, timezone

from climit.rates import compute, parse_iso

MIN = 60_000
FUTURE = "2999-01-01T00:00:00+00:00"


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def compute_ok(*args, **kwargs):
    # compute() is Optional; every case here feeds data, so narrow once
    r = compute(*args, **kwargs)
    assert r is not None
    return r


class TestRates(unittest.TestCase):
    def test_flat_no_burn(self):
        rows = [(0, 20.0, FUTURE), (50 * MIN, 20.0, FUTURE)]
        r = compute_ok("w", rows, 100 * MIN, lookback_min=60)
        self.assertEqual(r.util, 20.0)
        self.assertAlmostEqual(r.per_min, 0.0, places=6)
        self.assertIsNone(r.runway_min)
        self.assertFalse(r.will_exhaust_before_reset)

    def test_linear_burn_per_hour(self):
        rows = [(0, 10.0, FUTURE), (60 * MIN, 20.0, FUTURE)]  # +10 over 60 min
        r = compute_ok("w", rows, 60 * MIN, lookback_min=60)
        self.assertAlmostEqual(r.per_hour, 10.0, places=3)
        self.assertAlmostEqual(r.per_8h, 80.0, places=3)
        self.assertAlmostEqual(r.per_day, 240.0, places=2)
        assert r.runway_min is not None  # narrows for the type checker
        self.assertAlmostEqual(r.runway_min, 480.0, places=1)  # 80 left / 10 per hr = 8h

    def test_lookback_carry_forward(self):
        # burst to 30 at t=20min, then flat; 60-min window anchors at util_start=10
        rows = [(0, 10.0, FUTURE), (20 * MIN, 30.0, FUTURE), (60 * MIN, 30.0, FUTURE)]
        r = compute_ok("w", rows, 60 * MIN, lookback_min=60)
        self.assertAlmostEqual(r.per_hour, 20.0, places=3)

    def test_reset_never_negative(self):
        # util drops 90 -> 5 with a resets_at change: boundary moves, rate stays >= 0
        rows = [(0, 80.0, "A"), (60 * MIN, 90.0, "A"), (120 * MIN, 5.0, "B"), (130 * MIN, 7.0, "B")]
        r = compute_ok("w", rows, 130 * MIN, lookback_min=60)
        self.assertGreaterEqual(r.per_min, 0.0)
        self.assertAlmostEqual(r.per_hour, 12.0, places=1)  # +2 over 10 min post-reset

    def test_exhaust_vs_reset_flag(self):
        base = 1_800_000_000_000  # arbitrary realistic epoch ms
        reset = iso(base + 30 * MIN)  # window resets in 30 min
        # 50%/hr → 50 left → 60 min runway > 30 min → resets first
        slow = [(base - 60 * MIN, 0.0, reset), (base, 50.0, reset)]
        self.assertFalse(compute_ok("w", slow, base, lookback_min=60).will_exhaust_before_reset)
        # 80%/hr → 20 left → 15 min runway < 30 min → hits cap first
        fast = [(base - 60 * MIN, 0.0, reset), (base, 80.0, reset)]
        self.assertTrue(compute_ok("w", fast, base, lookback_min=60).will_exhaust_before_reset)

    def test_parse_iso(self):
        self.assertIsNotNone(parse_iso("2026-07-21T21:00:00.015050+00:00"))
        self.assertIsNotNone(parse_iso("2026-07-21T21:00:00Z"))
        self.assertIsNone(parse_iso(None))
        self.assertIsNone(parse_iso("not-a-date"))

    def test_jitter_resets_at_not_a_reset(self):
        # Sub-second resets_at wobble must NOT trigger a window reset — burn would collapse to 0.
        rows = [
            (0, 27.0, "2026-07-21T21:00:00.1+00:00"),
            (30 * MIN, 28.0, "2026-07-21T20:59:59.9+00:00"),
            (60 * MIN, 29.0, "2026-07-21T21:00:00.2+00:00"),
        ]
        r = compute_ok("seven_day", rows, 60 * MIN, lookback_min=60)
        self.assertAlmostEqual(r.per_hour, 2.0, places=3)  # +2 over 60 min, not 0
        self.assertIsNotNone(r.runway_min)

    def test_sources_rounding_differently_is_not_a_reset(self):
        # the endpoint says 75, the status line says 74: the peak holds, the rate stays positive
        reset = "2999-01-01T21:00:00+00:00"
        rows = [(0, 70.0, reset), (30 * MIN, 75.0, reset), (40 * MIN, 74.0, reset), (60 * MIN, 75.0, reset)]
        r = compute_ok("seven_day", rows, 60 * MIN, lookback_min=60)
        self.assertEqual(r.util, 75.0)
        self.assertAlmostEqual(r.per_hour, 5.0, places=3)

    def test_reset_detected_by_resets_at_moving(self):
        # a small window (3% -> 1%) resets without a big drop; resets_at moving marks it
        rows = [(0, 3.0, iso(100 * MIN)), (200 * MIN, 1.0, iso(500 * MIN)), (230 * MIN, 2.0, iso(500 * MIN))]
        r = compute_ok("five_hour", rows, 230 * MIN, lookback_min=60)
        self.assertEqual(r.util, 2.0)
        self.assertAlmostEqual(r.per_hour, 2.0, places=3)  # +1 over the 30 min since the reset

    def test_late_report_from_previous_window_is_ignored(self):
        rows = [(0, 40.0, iso(100 * MIN)), (110 * MIN, 1.0, iso(400 * MIN)), (120 * MIN, 40.0, iso(100 * MIN))]
        r = compute_ok("five_hour", rows, 120 * MIN, lookback_min=60)
        self.assertEqual(r.util, 1.0)

    def test_poll_drop_with_same_resets_at_starts_new_segment(self):
        # weekly usage credited back (63 -> 19) while resets_at holds; idle sessions keep redrawing 63
        reset = "2999-01-01T21:00:00+00:00"
        rows = [
            (0, 63.0, reset, "poll"),
            (10 * MIN, 63.0, reset, "statusline"),
            (20 * MIN, 19.0, reset, "poll"),
            (30 * MIN, 63.0, reset, "statusline"),
            (40 * MIN, 21.0, reset, "statusline"),
            (60 * MIN, 22.0, reset, "poll"),
        ]
        r = compute_ok("seven_day", rows, 60 * MIN, lookback_min=60)
        self.assertEqual(r.util, 22.0)
        self.assertAlmostEqual(r.per_hour, 4.5, places=3)  # +3 over the 40 min since the drop

    def test_statusline_drop_alone_is_not_a_reset(self):
        # an idle session's old, lower numbers must not reset the peak
        reset = "2999-01-01T21:00:00+00:00"
        rows = [(0, 60.0, reset, "poll"), (30 * MIN, 18.0, reset, "statusline")]
        self.assertEqual(compute_ok("seven_day", rows, 30 * MIN).util, 60.0)

    def test_passed_reset_reads_zero(self):
        rows = [(0, 60.0, iso(100 * MIN))]
        r = compute_ok("five_hour", rows, 101 * MIN, lookback_min=60)
        self.assertEqual(r.util, 0.0)
        self.assertIsNone(r.reset_ts)
        self.assertFalse(r.will_exhaust_before_reset)


if __name__ == "__main__":
    unittest.main()
