import tempfile
import unittest
from pathlib import Path

from climit import config, store


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "u.db")
        self._retire = config.WEEKLY_SCOPED_RETIRE_DAYS

    def tearDown(self):
        config.WEEKLY_SCOPED_RETIRE_DAYS = self._retire
        self.con.close()
        self.tmp.cleanup()

    def test_record_and_dedup(self):
        w = {"five_hour": {"util": 10.0, "resets_at": "A"}}
        self.assertEqual(store.record(self.con, 1000, w, "poll"), 1)
        self.assertEqual(store.record(self.con, 2000, w, "cache"), 0)  # unchanged → skip
        w2 = {"five_hour": {"util": 12.0, "resets_at": "A"}}
        self.assertEqual(store.record(self.con, 3000, w2, "poll"), 1)
        rows = store.samples_for(self.con, "five_hour")
        self.assertEqual([r[1] for r in rows], [10.0, 12.0])

    def test_samples_bracket(self):
        for ts, u in [(1000, 10.0), (2000, 12.0), (3000, 15.0)]:
            store.record(self.con, ts, {"w": {"util": u, "resets_at": None}}, "poll")
        rows = store.samples_for(self.con, "w", since_ts=2500)
        self.assertEqual([r[0] for r in rows], [2000, 3000])  # bracket + in-window

    def test_meta(self):
        store.set_meta(self.con, "k", "123")
        self.assertEqual(store.get_meta_int(self.con, "k"), 123)
        self.assertEqual(store.get_meta_int(self.con, "missing", 7), 7)

    def test_prune_retired(self):
        config.WEEKLY_SCOPED_RETIRE_DAYS = 30
        scoped = config.WEEKLY_SCOPED_PREFIX
        now = 1_000_000
        store.record(self.con, now - 40 * 86_400_000,
                     {scoped + "fable": {"util": 5.0, "resets_at": None}}, "poll")
        store.record(self.con, now - 5 * 86_400_000,
                     {scoped + "sonnet": {"util": 5.0, "resets_at": None}}, "poll")
        store.record(self.con, now,
                     {"five_hour": {"util": 5.0, "resets_at": None}}, "poll")
        kept = store.prune_retired(
            self.con, [scoped + "fable", scoped + "sonnet", "five_hour"], now
        )
        self.assertEqual(kept, [scoped + "sonnet", "five_hour"])

    def test_prune_retired_disabled_at_zero(self):
        config.WEEKLY_SCOPED_RETIRE_DAYS = 0
        scoped = config.WEEKLY_SCOPED_PREFIX
        now = 1_000_000
        store.record(self.con, now - 40 * 86_400_000,
                     {scoped + "fable": {"util": 5.0, "resets_at": None}}, "poll")
        kept = store.prune_retired(self.con, [scoped + "fable"], now)
        self.assertEqual(kept, [scoped + "fable"])


if __name__ == "__main__":
    unittest.main()
