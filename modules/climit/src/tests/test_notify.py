import tempfile
import unittest
from pathlib import Path
from unittest import mock

from climit import config, notify, store

SCOPED_FABLE = config.WEEKLY_SCOPED_PREFIX + "fable"


class TestNotifyCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "u.db")
        self._retire = config.WEEKLY_SCOPED_RETIRE_DAYS

    def tearDown(self):
        config.WEEKLY_SCOPED_RETIRE_DAYS = self._retire
        self.con.close()
        self.tmp.cleanup()

    def _sample(self, ts, util):
        store.record(self.con, ts, {SCOPED_FABLE: {"util": util, "resets_at": None}}, "poll")

    def test_retired_high_util_window_does_not_alert(self):
        # frozen at 90% for 40 days: must not notify from a surface nothing shows
        config.WEEKLY_SCOPED_RETIRE_DAYS = 30
        now = 1_000_000
        self._sample(now - 40 * 86_400_000, 90.0)
        with mock.patch.object(notify, "_send") as send:
            notify.check(self.con, now)
        send.assert_not_called()

    def test_active_high_util_window_alerts(self):
        now = 1_000_000
        self._sample(now, 90.0)
        with mock.patch.object(notify, "_send") as send:
            notify.check(self.con, now)
        send.assert_called_once()


class TestSendArgv(unittest.TestCase):
    """_send's argv is what decides stickiness, so assert on it directly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "u.db")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _run(self, style=notify.DEFAULT_STYLE, stdout="42\n", key="alert_w"):
        with mock.patch.object(notify.shutil, "which", return_value="/bin/notify-send"):
            with mock.patch.object(notify.subprocess, "run") as run:
                run.return_value = mock.Mock(stdout=stdout)
                notify._send(self.con, key, "title", "body", style)
        run.assert_called_once()
        return run.call_args[0][0]

    def test_default_style_is_not_sticky(self):
        argv = self._run()
        self.assertEqual(argv[argv.index("-u") + 1], "normal")
        self.assertEqual(argv[argv.index("-t") + 1], "10000")
        self.assertNotIn("int:transient:1", argv)
        self.assertEqual(argv[-2:], ["title", "body"])

    def test_zero_timeout_means_until_dismissed(self):
        argv = self._run(notify.AlertStyle(timeout=0))
        self.assertEqual(argv[argv.index("-t") + 1], "0")

    def test_transient_adds_hint(self):
        argv = self._run(notify.AlertStyle(transient=True))
        self.assertEqual(argv[argv.index("-h") + 1], "int:transient:1")

    def test_critical_urgency_still_reachable(self):
        argv = self._run(notify.AlertStyle(urgency="critical"))
        self.assertEqual(argv[argv.index("-u") + 1], "critical")

    def test_first_fire_has_no_replace_id_and_stores_it(self):
        argv = self._run(stdout="42\n")
        self.assertNotIn("-r", argv)
        self.assertEqual(store.get_meta_int(self.con, "alert_w_nid", 0), 42)

    def test_second_fire_replaces_stored_id(self):
        self._run(stdout="42\n")
        argv = self._run(stdout="43\n")
        self.assertEqual(argv[argv.index("-r") + 1], "42")
        self.assertEqual(store.get_meta_int(self.con, "alert_w_nid", 0), 43)

    def test_unparseable_id_is_swallowed(self):
        argv = self._run(stdout="")  # notify-send too old for -p, or no server
        self.assertIn("-p", argv)
        self.assertEqual(store.get_meta_int(self.con, "alert_w_nid", 0), 0)

    def test_no_notify_send_is_a_noop(self):
        with mock.patch.object(notify.shutil, "which", return_value=None):
            with mock.patch.object(notify.subprocess, "run") as run:
                notify._send(self.con, "alert_w", "title", "body")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
