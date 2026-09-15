import json
import tempfile
import time
import unittest
from pathlib import Path

from climit import auth, config


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
        self.path.write_text(json.dumps({"claudeAiOauth": oauth}))

    def test_valid_token(self):
        self._write({"accessToken": "tok", "expiresAt": int(time.time() * 1000) + 3_600_000})
        self.assertEqual(auth.get_access_token(), "tok")

    def test_expired_token_raises_and_leaves_file_untouched(self):
        self._write({"accessToken": "old", "refreshToken": "r1", "expiresAt": 0})
        before = self.path.read_bytes()
        with self.assertRaises(auth.AuthError):
            auth.get_access_token()
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_file_raises(self):
        with self.assertRaises(auth.AuthError):
            auth.get_access_token()

    def test_logged_out_raises(self):
        self._write({})
        with self.assertRaises(auth.AuthError):
            auth.get_access_token()


if __name__ == "__main__":
    unittest.main()
