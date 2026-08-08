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


if __name__ == "__main__":
    unittest.main()
