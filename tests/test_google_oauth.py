"""Offline tests for the Google sign-in helpers (registration_service.google_oauth)."""
import unittest
import urllib.parse

from registration_service import google_oauth as g  # noqa: E402

HR = g.HEART_RATE_SCOPE


def good(**over):
    t = {"access_token": "a", "refresh_token": "r", "expires_in": 3599,
         "scope": HR, "token_type": "Bearer"}
    t.update(over)
    return t


class AuthUrl(unittest.TestCase):
    def test_parameters(self):
        url = g.build_auth_url("cid", "https://app.example/", (HR,), "chal", "ver")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertTrue(url.startswith(g.AUTH_ENDPOINT + "?"))
        self.assertEqual(q["client_id"], ["cid"])
        self.assertEqual(q["redirect_uri"], ["https://app.example/"])
        self.assertEqual(q["scope"], [HR])
        self.assertEqual(q["access_type"], ["offline"])
        self.assertEqual(q["prompt"], ["select_account consent"])
        self.assertEqual(q["code_challenge_method"], ["S256"])
        self.assertEqual(q["state"], ["ver"])

    def test_default_scope_is_the_heart_rate_scope(self):
        self.assertEqual(g.parse_scopes(None), (HR,))
        self.assertIn("health_metrics_and_measurements.readonly", HR)
        self.assertNotIn("activity_and_fitness", HR)

    def test_scopes_can_be_overridden(self):
        self.assertEqual(g.parse_scopes("a, b  c"), ("a", "b", "c"))


class TokenResponse(unittest.TestCase):
    def test_ok(self):
        p = g.token_payload_from_response(good(), 1000, (HR,))
        self.assertEqual(p["expires_at"], 1000 + 3599)
        self.assertEqual(p["refresh_token"], "r")
        self.assertEqual(p["token_type"], "Bearer")

    def test_no_refresh_token_is_refused(self):
        with self.assertRaises(g.GoogleConnectError):
            g.token_payload_from_response(good(refresh_token=None), 0, (HR,))

    def test_unticked_heart_rate_permission_is_refused(self):
        with self.assertRaises(g.GoogleConnectError):
            g.token_payload_from_response(good(scope="openid email"), 0, (HR,))

    def test_no_access_token_is_refused(self):
        with self.assertRaises(g.GoogleConnectError):
            g.token_payload_from_response({}, 0, (HR,))

    def test_bad_expires_in_falls_back(self):
        p = g.token_payload_from_response(good(expires_in="x"), 10, (HR,))
        self.assertEqual(p["expires_at"], 10 + 3600)


class Labels(unittest.TestCase):
    def test_normalises_case_and_space(self):
        self.assertEqual(g.normalize_label(" cr8y24 "), "CR8Y24")

    def test_rejects_bad_labels(self):
        for bad in ("", "ab", "has space", "../x", "A" * 13, "CR8-Y24", None):
            with self.assertRaises(g.GoogleConnectError, msg=repr(bad)):
                g.normalize_label(bad)

    def test_non_strict_returns_none(self):
        self.assertIsNone(g.normalize_label("nope!", strict=False))

    def test_legacy_id_from_identity(self):
        self.assertEqual(g.legacy_id_from_identity({"legacyUserId": "a1b2c3", "healthUserId": "9" * 20}), "A1B2C3")
        self.assertIsNone(g.legacy_id_from_identity({"healthUserId": "123"}))
        self.assertIsNone(g.legacy_id_from_identity(None))


class Stamping(unittest.TestCase):
    def test_pull_lambda_contract(self):
        p = g.token_payload_from_response(good(), 0, (HR,))
        s = g.stamped_payload(p, "CR8Y24", "p-123", "org1")
        # google-health-pull reads these fields from google_health_tokens/<label>.json
        for k in ("access_token", "refresh_token", "expires_at", "fitbit_id"):
            self.assertIn(k, s)
        self.assertEqual(s["fitbit_id"], "CR8Y24")
        self.assertEqual(s["participant_id"], "p-123")
        self.assertEqual(s["org_id"], "org1")


if __name__ == "__main__":
    unittest.main()
