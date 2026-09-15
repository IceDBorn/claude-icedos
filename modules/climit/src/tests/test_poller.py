import tempfile
import unittest
from pathlib import Path
from unittest import mock

from climit import auth, config, poller, sources, store

WINDOWS = {"five_hour": {"util": 10.0, "resets_at": "2026-09-15T11:50:00+00:00"}}


class TestPollOnce(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "u.db")
        self.token = mock.patch.object(auth, "get_access_token", return_value="tok")
        self.token.start()

    def tearDown(self):
        mock.patch.stopall()
        self.con.close()
        self.tmp.cleanup()

    def test_poll_records_then_floor_throttles(self):
        now = 1_000_000_000
        with mock.patch.object(sources, "fetch_live", return_value=WINDOWS) as fetch:
            self.assertEqual(poller.poll_once(self.con, now), ("poll", 1))
            self.assertEqual(poller.poll_once(self.con, now + 60_000)[0], "throttled")
            # a timer firing a hair early still polls
            self.assertEqual(poller.poll_once(self.con, now + config.MIN_INTERVAL * 1000 - 1000)[0], "poll")
        self.assertEqual(fetch.call_count, 2)

    def test_auth_error_skips_without_claiming_the_slot(self):
        with mock.patch.object(auth, "get_access_token", side_effect=auth.AuthError("expired")), \
             mock.patch.object(sources, "fetch_live") as fetch:
            status, n = poller.poll_once(self.con, 1_000)
        self.assertTrue(status.startswith("skipped"))
        fetch.assert_not_called()
        self.assertEqual(store.get_meta_int(self.con, "live_next_ms"), 0)

    def test_429_backoff_doubles(self):
        now = 1_000_000_000
        err = sources.FetchError("HTTP 429", status=429)
        with mock.patch.object(sources, "fetch_live", side_effect=err):
            poller.poll_once(self.con, now)
            first = store.get_meta_int(self.con, "live_backoff_s")
            poller.poll_once(self.con, now + first * 1000)
            second = store.get_meta_int(self.con, "live_backoff_s")
        self.assertEqual(first, config.MIN_INTERVAL)
        self.assertEqual(second, 2 * config.MIN_INTERVAL)


if __name__ == "__main__":
    unittest.main()
