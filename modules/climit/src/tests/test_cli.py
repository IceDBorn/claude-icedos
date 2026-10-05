import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from climit import cli, config, rates, store

RESET = "2026-08-11T21:00:00+00:00"
SCOPED_OPUS = config.WEEKLY_SCOPED_PREFIX + "claude_opus_4.5"
SCOPED_FABLE = config.WEEKLY_SCOPED_PREFIX + "fable"
SCOPED_SONNET = config.WEEKLY_SCOPED_PREFIX + "claude_sonnet_4.5"


def _window(w):
    return {w: {"util": 5.0, "resets_at": RESET}}


class TestCollect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "u.db")
        self._retire = config.WEEKLY_SCOPED_RETIRE_DAYS

    def tearDown(self):
        config.WEEKLY_SCOPED_RETIRE_DAYS = self._retire
        self.con.close()
        self.tmp.cleanup()

    def test_orders_main_then_scoped_by_name(self):
        store.record(self.con, 1000, _window("seven_day"), "poll")
        store.record(self.con, 1000, _window("five_hour"), "poll")
        store.record(self.con, 1000, _window(SCOPED_FABLE), "poll")
        store.record(self.con, 1000, _window(SCOPED_OPUS), "poll")
        got = [r.window for r in cli.collect(self.con, 5000)]
        self.assertEqual(
            got, ["five_hour", "seven_day", SCOPED_OPUS, SCOPED_FABLE],
        )

    def test_drops_unrelated_present_windows(self):
        store.record(self.con, 1000, _window("nimbus_quill"), "poll")
        store.record(self.con, 1000, _window("five_hour"), "poll")
        got = [r.window for r in cli.collect(self.con, 5000)]
        self.assertEqual(got, ["five_hour"])

    def test_prunes_retired_scoped_windows(self):
        config.WEEKLY_SCOPED_RETIRE_DAYS = 30
        now = 1_000_000
        retired = now - 40 * 86_400_000
        active = now - 5 * 86_400_000
        store.record(self.con, retired, _window(SCOPED_FABLE), "poll")
        store.record(self.con, active, _window(SCOPED_SONNET), "poll")
        got = [r.window for r in cli.collect(self.con, now)]
        self.assertEqual(got, [SCOPED_SONNET])

    def test_prune_disabled_at_zero(self):
        config.WEEKLY_SCOPED_RETIRE_DAYS = 0
        now = 1_000_000
        retired = now - 40 * 86_400_000
        store.record(self.con, retired, _window(SCOPED_FABLE), "poll")
        got = [r.window for r in cli.collect(self.con, now)]
        self.assertEqual(got, [SCOPED_FABLE])


class TestRenderJson(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "u.db")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_emits_label_and_short_label(self):
        store.record(self.con, 1000, _window("five_hour"), "poll")
        store.record(self.con, 1000, _window(SCOPED_FABLE), "poll")
        rlist = cli.collect(self.con, 5000)
        payload = json.loads(cli.render_json(rlist, 5000))
        by = {w["window"]: w for w in payload["windows"]}
        self.assertEqual(by["five_hour"]["label"], "5-hour")
        self.assertEqual(by["five_hour"]["short_label"], "5h")
        self.assertEqual(by[SCOPED_FABLE]["label"], "week · Fable")
        self.assertEqual(by[SCOPED_FABLE]["short_label"], "Fab")


class TestCmdStatusline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "u.db")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_records_live_windows_and_skips_reset_ones(self):
        now_s = 2_000_000_000
        payload = {
            "workspace": {"current_dir": "/tmp"},
            "rate_limits": {
                "five_hour": {"used_percentage": 31, "resets_at": now_s + 3600},
                "seven_day": {"used_percentage": 74, "resets_at": now_s - 60},
            },
        }
        out = io.StringIO()
        with mock.patch.object(cli.store, "connect", return_value=self.con), \
             mock.patch.object(cli, "_now_ms", return_value=now_s * 1000), \
             mock.patch.object(cli, "_git", return_value=None), \
             mock.patch.object(cli.sys, "stdin", io.StringIO(json.dumps(payload))), \
             mock.patch.object(cli.sys, "stdout", out):
            cli.main(["statusline"])
        self.assertEqual(store.windows_present(self.con), ["five_hour"])
        self.assertIn("5h", out.getvalue())
        self.assertIn("31%", out.getvalue())

    def test_model_label(self):
        strip = lambda s: re.sub(r"\x1b\[[0-9;]*m", "", s)
        model = {"model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"}}
        self.assertEqual(strip(cli.model_label({**model, "effort": {"level": "xhigh"},
                                                "thinking": {"enabled": True}})), "Opus 5.5 xhigh")
        self.assertEqual(strip(cli.model_label({**model, "effort": {"level": "high"},
                                                "thinking": {"enabled": False}})), "Opus 5.5 think off")
        self.assertEqual(strip(cli.model_label(model)), "Opus 5.5")
        self.assertIsNone(cli.model_label({}))

    def test_context_pct(self):
        self.assertEqual(cli.context_pct({"context_window": {"used_percentage": 42}}), 42.0)
        self.assertEqual(cli.context_pct({"context_window": {
            "context_window_size": 200_000,
            "current_usage": {"input_tokens": 10_000, "output_tokens": 5_000,
                              "cache_creation_input_tokens": 20_000, "cache_read_input_tokens": 20_000},
        }}), 25.0)
        big = {"context_window": {"context_window_size": 1_000_000, "used_percentage": 10,
                                  "current_usage": {"input_tokens": 100_000}}}
        self.assertEqual(cli.context_pct(big, 400_000), 25.0)
        self.assertEqual(cli.context_pct(big, 2_000_000), 10.0)
        self.assertEqual(cli.context_pct(
            {"context_window": {"context_window_size": 1_000_000, "used_percentage": 20}}, 400_000), 50.0)
        self.assertIsNone(cli.context_pct({"context_window": {"used_percentage": None}}))
        self.assertIsNone(cli.context_pct({}))


class TestRenderStatusline(unittest.TestCase):
    def _rate(self, **kw):
        base = {"window": "five_hour", "util": 40.0, "resets_at": RESET, "per_min": 0.5,
                "per_hour": 30.0, "per_8h": 240.0, "per_day": 720.0, "runway_min": 120.0,
                "exhaust_ts": None, "reset_ts": 3 * 3_600_000,
                "will_exhaust_before_reset": True, "stale": False, "last_ts": 0}
        return rates.Rate(**{**base, **kw})

    def test_shows_ends_and_resets(self):
        line = cli.render_statusline([self._rate()], 0, color=False)
        self.assertEqual(line, "⚠ 5h 40% 30.0/h 󰔟 2h0m 󰦛 3h0m")

    def test_idle_window_omits_rate_and_ends(self):
        r = self._rate(per_min=0.0, per_hour=0.0, runway_min=None, will_exhaust_before_reset=False)
        self.assertEqual(cli.render_statusline([r], 0, color=False), "5h 40% 󰦛 3h0m")

    def test_ends_hidden_when_reset_comes_first(self):
        r = self._rate(runway_min=600.0, will_exhaust_before_reset=False)
        self.assertEqual(cli.render_statusline([r], 0, color=False), "5h 40% 30.0/h 󰦛 3h0m")

    def test_shared_reset_printed_once(self):
        a = self._rate(window="seven_day", will_exhaust_before_reset=False, per_hour=0.0)
        b = self._rate(window=SCOPED_FABLE, util=11.0, will_exhaust_before_reset=False,
                       per_hour=0.0, reset_ts=3 * 3_600_000 + 20_000)
        c = self._rate(window="five_hour", reset_ts=3_600_000, will_exhaust_before_reset=False,
                       per_hour=0.0)
        groups = cli.group_by_reset([c, a, b])
        self.assertEqual([[r.window for r in rs] for _, rs in groups],
                         [["five_hour"], ["seven_day", SCOPED_FABLE]])
        line = cli.render_statusline([c, a, b], 0, color=False)
        self.assertTrue(line.startswith("5h 40% 󰦛 1h0m | "))
        self.assertTrue(line.endswith("󰦛 3h0m"))

    def test_at_cap_window_omits_zero_ends(self):
        # 100% used leaves no runway; "󰔟 0s" is noise on every surface.
        at_cap = self._rate(util=100.0, runway_min=0.0)
        self.assertEqual(cli.render_statusline([at_cap], 0, color=False),
                         "⚠ 5h 100% 30.0/h 󰦛 3h0m")
        self.assertIn("—", cli.render_table([at_cap], 0))
        self.assertNotIn("0s", cli.render_table([at_cap], 0))

    def test_sub_second_runway_treated_as_zero(self):
        r = self._rate(runway_min=0.001)  # 60ms, would format as "0s"
        self.assertEqual(cli.render_statusline([r], 0, color=False),
                         "⚠ 5h 40% 30.0/h 󰦛 3h0m")

    def test_zero_util_window_hidden(self):
        zero = self._rate(window="seven_day", util=0.0, per_min=0.0, per_hour=0.0,
                          runway_min=None, will_exhaust_before_reset=False)
        busy = self._rate(will_exhaust_before_reset=False, per_hour=0.0)
        self.assertEqual(cli.render_statusline([busy, zero], 0, color=False), "5h 40% 󰦛 3h0m")
        self.assertEqual(cli.render_statusline([zero], 0, color=False), "climit: no usage")
        self.assertNotIn("weekly", cli.render_table([busy, zero], 0))
        windows = json.loads(cli.render_json([busy, zero], 0))["windows"]
        self.assertEqual([w["window"] for w in windows], ["five_hour"])


if __name__ == "__main__":
    unittest.main()
