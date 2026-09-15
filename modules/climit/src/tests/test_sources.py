import unittest

from climit import config
from climit.sources import _parse_windows, parse_statusline

RESET = "2026-08-08T19:50:00+00:00"


class TestParseStatusline(unittest.TestCase):
    def test_rate_limits_become_windows(self):
        payload = {"rate_limits": {
            "five_hour": {"used_percentage": 31, "resets_at": 1789473000},
            "seven_day": {"used_percentage": 74.5, "resets_at": 1789506000},
            "spend_limit": {"used_percentage": 10, "resets_at": 1789506000},
        }}
        out = parse_statusline(payload)
        self.assertEqual(sorted(out), ["five_hour", "seven_day"])
        self.assertEqual(out["five_hour"], {"util": 31.0, "resets_at": "2026-09-15T11:50:00+00:00"})
        self.assertEqual(out["seven_day"]["util"], 74.5)

    def test_missing_or_malformed(self):
        self.assertEqual(parse_statusline({}), {})
        self.assertEqual(parse_statusline([]), {})
        self.assertEqual(parse_statusline({"rate_limits": {"five_hour": {"resets_at": 1}}}), {})
        out = parse_statusline({"rate_limits": {"five_hour": {"used_percentage": 3}}})
        self.assertEqual(out, {"five_hour": {"util": 3.0, "resets_at": None}})


class TestParseWindows(unittest.TestCase):
    def test_keeps_known_window(self):
        util = {"five_hour": {"utilization": 1, "resets_at": RESET}}
        self.assertEqual(_parse_windows(util), {"five_hour": {"util": 1.0, "resets_at": RESET}})

    def test_drops_unknown_promotional_keys(self):
        util = {
            "five_hour": {"utilization": 1, "resets_at": RESET},
            "nimbus_quill": {"utilization": 0, "resets_at": None},
            "iguana_necktie": {"utilization": 0, "resets_at": None},
            "omelette_promotional": None,
        }
        self.assertEqual(list(_parse_windows(util)), ["five_hour"])

    def test_keeps_known_key_with_null_reset(self):
        # a post-reset zero sample ({"utilization": 0, "resets_at": null}) must be
        # recorded, not dropped, or the last pre-reset value gets pinned forever
        util = {"five_hour": {"utilization": 0, "resets_at": None}}
        self.assertEqual(_parse_windows(util), {"five_hour": {"util": 0.0, "resets_at": None}})

    def test_ignores_known_null_utilization(self):
        util = {"seven_day_opus": {"utilization": None, "resets_at": RESET}}
        self.assertEqual(_parse_windows(util), {})

    def test_synthesizes_weekly_scoped_from_limits(self):
        util = {
            "five_hour": {"utilization": 1, "resets_at": RESET},
            "limits": [
                {"kind": "weekly_scoped", "percent": 0, "resets_at": RESET,
                 "scope": {"model": {"display_name": "Fable"}}},
            ],
        }
        out = _parse_windows(util)
        self.assertEqual(
            out[config.WEEKLY_SCOPED_PREFIX + "fable"],
            {"util": 0.0, "resets_at": RESET},
        )
        self.assertIn("five_hour", out)

    def test_scoped_tolerates_falsy_scope_and_model(self):
        util = {
            "limits": [
                {"kind": "weekly_scoped", "percent": 5, "scope": None},
                {"kind": "weekly_scoped", "percent": 5, "scope": {"model": None}},
                {"kind": "weekly_scoped", "percent": 5, "scope": {"model": {}}},
                {"kind": "weekly_all", "percent": 71, "scope": None},
                {"kind": "session", "percent": 1, "scope": None},
                "not-a-dict",
            ],
        }
        self.assertEqual(_parse_windows(util), {})

    def test_scoped_slug_keeps_punctuation(self):
        util = {
            "limits": [
                {"kind": "weekly_scoped", "percent": 3,
                 "scope": {"model": {"display_name": "Claude Opus 4.5"}}},
            ],
        }
        out = _parse_windows(util)
        self.assertIn(config.WEEKLY_SCOPED_PREFIX + "claude_opus_4.5", out)

    def test_scoped_skips_blank_display_name(self):
        util = {
            "limits": [
                {"kind": "weekly_scoped", "percent": 5,
                 "scope": {"model": {"display_name": "   "}}},
                {"kind": "weekly_scoped", "percent": 5,
                 "scope": {"model": {"display_name": 42}}},
            ],
        }
        self.assertEqual(_parse_windows(util), {})

    def test_scoped_skips_entry_without_percent(self):
        # a weekly_scoped entry that omits percent must be skipped, not recorded
        # as a fake 0.0% sample (which would inject a fake reset-drop into rates)
        util = {
            "limits": [
                {"kind": "weekly_scoped",
                 "scope": {"model": {"display_name": "Fable"}}},
                {"kind": "weekly_scoped", "percent": None,
                 "scope": {"model": {"display_name": "Ignored"}}},
            ],
        }
        self.assertEqual(_parse_windows(util), {})


if __name__ == "__main__":
    unittest.main()
