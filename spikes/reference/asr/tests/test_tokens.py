import unittest
from unittest.mock import patch

from asr.webapp import SessionTokens


class SessionTokenTests(unittest.TestCase):
    def test_token_is_profile_bound_expires_and_can_be_revoked(self):
        store = SessionTokens(ttl_seconds=60)
        with patch("asr.webapp.time.time", return_value=1000):
            token, expires_at = store.issue("profile-a")
            self.assertEqual(expires_at, 1060)
            self.assertEqual(store.validate(token, "profile-a"), 1060)
            self.assertIsNone(store.validate(token, "profile-b"))

            token, _ = store.issue("profile-a")
            self.assertTrue(store.revoke(token))
            self.assertIsNone(store.validate(token, "profile-a"))

            token, _ = store.issue("profile-a")
        with patch("asr.webapp.time.time", return_value=1061):
            self.assertIsNone(store.validate(token, "profile-a"))


if __name__ == "__main__":
    unittest.main()
