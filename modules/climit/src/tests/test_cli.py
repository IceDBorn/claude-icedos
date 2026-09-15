import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from climit import cli, config, store

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


if __name__ == "__main__":
    unittest.main()
