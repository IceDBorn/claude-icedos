import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from climit import auth, config


class FakeResp:
    """Minimal context-manager response that json.load() can read."""

    def __init__(self, data):
        self._d = json.dumps(data).encode()

    def read(self, *a):
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestAuth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".credentials.json"
        self._orig = config.CREDS_PATH
        config.CREDS_PATH = self.path

    def tearDown(self):
        config.CREDS_PATH = self._orig
        self.tmp.cleanup()

    def _write(self, oauth):
        self.path.write_text(json.dumps({"claudeAiOauth": oauth, "other": "keep"}))

    def test_valid_token_skips_refresh(self):
        self._write({"accessToken": "tok", "expiresAt": int(time.time() * 1000) + 3_600_000})
        with mock.patch("climit.auth.urllib.request.urlopen") as m:
            self.assertEqual(auth.get_access_token(), "tok")
            m.assert_not_called()

    def test_refresh_when_expired_writes_back_0600(self):
        self._write({"accessToken": "old", "refreshToken": "r1", "expiresAt": 0})
        resp = FakeResp({"access_token": "new", "refresh_token": "r2", "expires_in": 3600})
        with mock.patch("climit.auth.urllib.request.urlopen", return_value=resp):
            self.assertEqual(auth.get_access_token(), "new")
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["claudeAiOauth"]["accessToken"], "new")
        self.assertEqual(saved["claudeAiOauth"]["refreshToken"], "r2")
        self.assertEqual(saved["other"], "keep")  # rest of file preserved
        self.assertGreater(saved["claudeAiOauth"]["expiresAt"], int(time.time() * 1000))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_race_guard_defers_to_concurrent_refresh(self):
        # File is expired, but a re-read shows another process already rotated it.
        seq = [
            {"claudeAiOauth": {"accessToken": "old", "refreshToken": "r1", "expiresAt": 0}},
            {"claudeAiOauth": {"accessToken": "fresh", "expiresAt": int(time.time() * 1000) + 3_600_000}},
        ]
        with mock.patch("climit.auth._read_creds", side_effect=seq), \
             mock.patch("climit.auth.urllib.request.urlopen") as m:
            self.assertEqual(auth.get_access_token(), "fresh")
            m.assert_not_called()  # deferred — no network refresh


if __name__ == "__main__":
    unittest.main()
