import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import providers


def fake(data):
    return io.BytesIO(json.dumps(data).encode())


class NativeProvidersTest(unittest.TestCase):
    def read(self, provider, *data):
        with patch("providers.build_opener") as build:
            client = build.return_value
            client.open.side_effect = [fake(item) for item in data]
            metrics = providers.fetch(provider, "synthetic-test-key")
            return metrics, [c.args[0] for c in client.open.call_args_list]

    def test_deepseek_multicurrency_rounded_real_zero(self):
        rows, requests = self.read("deepseek", {"balance_infos": [
            {"currency": "CNY", "total_balance": "-0.005"}, {"currency": "USD", "total_balance": "0.00"}]})
        self.assertEqual(rows, [{"label": "CNY", "value": "-0.01"}, {"label": "USD", "value": "0.00"}])
        self.assertEqual(requests[0].full_url, "https://api.deepseek.com/user/balance")

    def test_openrouter_limited_and_no_key_limit(self):
        rows, reqs = self.read("openrouter", {"data": {"limit": 40, "limit_remaining": 34.995}})
        self.assertEqual(rows, [{"label": "Key USD", "value": "35.00"}])
        self.assertEqual(reqs[0].full_url, "https://openrouter.ai/api/v1/key")
        rows, _ = self.read("openrouter", {"data": {"limit": None, "limit_remaining": None, "usage": 4.0}})
        self.assertEqual(rows, [{"label": "Key cap", "value": "None"}])
        with self.assertRaises(providers.ProviderError):
            self.read("openrouter", {"data": {"limit": 40, "usage": 4.0}})
        with self.assertRaises(providers.ProviderError):
            self.read("openrouter", {"data": {}})

    def test_codex_only_reported_real_windows(self):
        rows, _ = self.read("codex", {"rate_limit": {"primary_window": None,
                          "secondary_window": {"limit_window_seconds": 604800, "used_percent": 45}}})
        self.assertEqual(rows, [{"label": "5h", "value": "--"}, {"label": "7d", "value": "55%"}])
        with self.assertRaises(providers.ProviderError):
            self.read("codex", {"rate_limit": {"primary_window": {"limit_window_seconds": 99, "used_percent": 0}}})

    def test_antigravity_checks_project_and_measured_gemini_buckets(self):
        account = {"cloudaicompanionProject": {"id": "example-project"}}
        summary = {"response": {"groups": [{"buckets": [
            {"bucketId": "gemini-5h", "remaining": {"remainingFraction": 0}},
            {"bucketId": "gemini-weekly", "remaining": {"case": "remainingFraction", "value": .4}},
            {"bucketId": "claude-weekly", "remainingFraction": .9}]}]}}
        rows, requests = self.read("antigravity", account, summary)
        self.assertEqual(rows, [{"label": "5h", "value": "0%"}, {"label": "7d", "value": "40%"}])
        self.assertEqual(json.loads(requests[0].data), {"metadata": {}})
        self.assertEqual(json.loads(requests[1].data), {"project": "example-project"})
        self.assertTrue(all(req.get_header("User-agent") == "QuotaDesk/1" for req in requests))
        for data in [{"models": {"gemini-flash": {"remainingFraction": 1}}},
                     {"groups": [{"buckets": [{"bucketId": "gemini-5h", "disabled": True, "remainingFraction": 1}]}]},
                     {"groups": [{"buckets": [{"bucketId": "gemini-weekly", "window": "session", "remainingFraction": 1}]}]},
                     {"groups": [{"buckets": [{"bucketId": "gemini-5h", "remaining": {}}]}]}]:
            with self.assertRaises(providers.ProviderError):
                self.read("antigravity", account, data)

    def test_ineligible_or_missing_project_never_contacts_quota_endpoint(self):
        for account in ({}, {"ineligibleTiers": [{"reasonCode": "GOOGLE_TOS_NOT_SUPPORTED_BY_CLIENT"}]}):
            with patch("providers.request_json", return_value=account) as request:
                with self.assertRaises(providers.ProviderError):
                    providers.fetch("antigravity", "fake")
                self.assertEqual(request.call_count, 1)

    def test_credential_error_and_redirects_never_disclose_upstream_input(self):
        self.assertIsNone(providers.NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid"))
        with patch("providers.build_opener") as build:
            build.return_value.open.side_effect = HTTPError(
                "https://elsewhere.invalid", 401, "secret-server-msg", {}, io.BytesIO(b"secret-body"))
            with self.assertRaises(providers.ProviderError) as err:
                providers.fetch("deepseek", "synthetic-test-key")
            self.assertEqual(str(err.exception), "憑證已失效或缺少額度查詢權限")
            with self.assertRaises(providers.ProviderError):
                providers.fetch("deepseek", "key\r\nforged-header")
            self.assertEqual(build.return_value.open.call_count, 1)

    def test_nonfinite_or_malformed_values_never_leak(self):
        for value in (True, "NaN", "Infinity", "synthetic-test-private", "999999999999.999"):
            with self.assertRaises(providers.ProviderError) as err:
                self.read("deepseek", {"balance_infos": [{"currency": "USD", "total_balance": value}]})
            self.assertEqual(str(err.exception), "服務未回報可辨識的額度")


if __name__ == "__main__":
    unittest.main()
