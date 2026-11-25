import hashlib
import http.client
import io
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

if __package__:
    from . import web
else:
    import web


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp_tls = tempfile.TemporaryDirectory()
        cls.tls = Path(cls.tmp_tls.name)
        config = cls.tls / "openssl.cnf"
        config.write_text("[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=v3\n[dn]\nCN=localhost\n"
                          "[v3]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=CA:TRUE\n")
        subprocess.run(["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-x509",
                        "-days", "1", "-config", str(config), "-out", str(cls.tls / "tls.crt"),
                        "-keyout", str(cls.tls / "tls.key")], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp_tls.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        for name in ("tls.crt", "tls.key"):
            (self.home / name).symlink_to(self.tls / name)
        self.store = web.Store(self.home)
        self.server = web.WebServer(self.home, ("127.0.0.1", 0), lambda: self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.origin = "https://127.0.0.1:" + str(self.server.server_port)
        self.context = ssl.create_default_context(cafile=str(self.tls / "tls.crt"))
        self.cookie = ""

    def request(self, path="/", form=None, **headers):
        c = http.client.HTTPSConnection("127.0.0.1", self.server.server_port, context=self.context, timeout=35)
        method = "POST" if form is not None else "GET"
        args = {"Cookie": self.cookie}
        if form is not None:
            args.update({"Origin": self.origin, "Content-Type": "application/x-www-form-urlencoded"})
        args.update(headers)
        try:
            c.request(method, path, None if form is None else urlencode(form), args)
            response = c.getresponse()
            return response.status, response.read().decode(), dict(response.getheaders())
        finally:
            c.close()

    def test_setup_and_lifecycle(self):
        previous = web.read_json(self.home / "pair-display.json")["code"]
        code = web.issue_setup_code(self.home)
        self.assertFalse((self.home / "pair-display.json").exists())
        self.assertNotIn(code, (self.home / "setup-code.json").read_text())
        self.assertEqual((self.home / "setup-code.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.request()[2]["Location"], "/setup")
        self.assertIn("一次性設定碼", self.request("/setup")[1])
        self.assertFalse((self.home / "pair-display.json").exists())
        form = {"code": code, "password": "test-password-01", "confirm": "test-password-01"}
        self.assertEqual(self.request("/setup", {**form, "code": previous})[0], 401)
        self.assertEqual(self.request("/setup", form, Origin="null")[0], 403)
        status, _, headers = self.request("/setup", form)
        self.assertEqual(status, 303)
        self.assertFalse((self.home / "setup-code.json").exists())
        self.assertEqual((self.home / "admin-password.json").stat().st_mode & 0o777, 0o600)
        self.assertNotIn(form["password"], (self.home / "admin-password.json").read_text())
        self.assertEqual(self.request("/setup", form)[0], 401)
        with self.assertRaises(ValueError):
            web.issue_setup_code(self.home)
        self.assertIn("Secure; HttpOnly; SameSite=Strict", headers["Set-Cookie"])
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertIn("訂閱管理", self.request()[1])
        self.assertEqual(self.request("/choose?provider=deepseek")[2]["Location"], "/account/deepseek")
        self.assertIn('type="password"', self.request("/account/deepseek")[1])
        self.assertEqual(self.request("/account/deepseek", {"secret": "bad space"})[0], 400)
        with redirect_stderr(io.StringIO()) as logs:
            sample = "fake-web-test-credential"
            self.assertEqual(self.request("/account/deepseek", {"secret": sample})[0], 303)
            self.assertNotIn(sample, self.request()[1] + self.request("/account/deepseek")[1])
        self.assertNotIn(sample, logs.getvalue())
        self.assertEqual(self.request("/disable/deepseek", {})[0], 303)
        self.assertFalse(self.store.list_accounts()[0]["enabled"])
        self.assertEqual(self.request("/enable/deepseek", {}, Origin="https://untrusted.invalid")[0], 403)
        self.assertFalse(self.store.list_accounts()[0]["enabled"])
        self.assertEqual(self.request("/enable/deepseek", {})[0], 303)
        self.assertTrue(self.store.list_accounts()[0]["enabled"])
        self.assertEqual(self.request("/remove/deepseek", {})[0], 303)
        self.assertEqual(self.store.list_accounts(), [])
        self.assertEqual(self.request("/logout", {})[0], 303)
        self.assertEqual(self.request("/account/deepseek", {"secret": sample})[0], 403)
        self.assertIn("管理員登入", self.request()[1])
        self.assertEqual(self.request("/login", {"password": form["password"]})[0], 303)

    def test_physical_pairing(self):
        panel = self.home / "pair-display.json"
        challenge = self.home / "setup-code.json"
        initial = web.read_json(panel)
        self.assertEqual(initial.keys(), {"code", "expires"})
        self.assertRegex(initial["code"], r"^[0-9]{8}$")
        self.assertEqual(panel.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(initial["code"], self.request("/setup")[1])
        self.assertEqual(initial, web.read_json(panel))
        old_hash = web.read_json(challenge)
        web.write_private(challenge, {**old_hash, "expires": int(time.time()) - 1})
        form = {"code": initial["code"], "password": "test-password-03", "confirm": "test-password-03"}
        self.assertEqual(self.request("/setup", form)[0], 401)
        with patch.object(web.secrets, "randbelow", return_value=4258):
            self.assertEqual(self.request("/setup")[0], 200)
        self.assertEqual(web.read_json(panel)["code"], "00004258")
        self.assertNotIn("00004258", self.request("/setup")[1])
        self.assertEqual(self.request("/setup", {**form, "code": "0000 4258"})[0], 303)
        self.assertFalse(challenge.exists())
        self.assertFalse(panel.exists())
        self.assertEqual(self.request("/setup")[0], 303)
        web.issue_setup_code(self.home, display=True)
        self.assertFalse(panel.exists())

    def test_setup_code_expiry_host_and_rate_limit(self):
        self.assertEqual(self.request(Host="untrusted.invalid")[0], 403)
        code = web.issue_setup_code(self.home)
        web.write_private(self.home / "setup-code.json", {"hash": hashlib.sha256(code.encode()).hexdigest(),
                                                         "expires": int(time.time()) - 1})
        form = {"code": code, "password": "testing-pass-02", "confirm": "testing-pass-02"}
        for _ in range(5):
            self.assertEqual(self.request("/setup", form)[0], 401)
        self.assertEqual(self.request("/setup", form)[0], 429)
        self.assertFalse((self.home / "admin-password.json").exists())
        self.assertEqual(len(web.fingerprint(self.tls / "tls.crt").split(":")), 32)

    def test_device_preferences_and_localized_navigation(self):
        form = {"timezone": "America/Los_Angeles", "language": "en", "interval": "30"}
        self.assertEqual(self.request("/settings", form)[0], 403)
        self.assertEqual(self.request("/settings")[2]["Location"], "/setup")
        password = "only-for-this-test"
        code = web.issue_setup_code(self.home)
        result, _, headers = self.request("/setup", {"code": code, "password": password, "confirm": password})
        self.assertEqual(result, 303)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertIn('href="/settings" aria-current="page">本機設定', self.request("/settings")[1])
        self.assertEqual(self.request("/settings", form, Origin="null")[0], 403)
        self.assertEqual(self.request("/settings", {**form, "timezone": "../wrong"})[0], 400)
        self.assertEqual(web.device_settings.load(self.home)["language"], "zh-Hant")
        status, _, headers = self.request("/settings", form)
        self.assertEqual(status, 303)
        view = self.request(headers["Location"])[1]
        self.assertIn('lang="en"', view)
        self.assertIn('role="status">Device settings saved.', view)
        self.assertIn('value="30" selected', view)
        self.assertIn('value="America/Los_Angeles"', view)
        self.assertIn('every 30 minutes', self.request()[1])
        for provider, info in web.PROVIDERS.items():
            status, body, _ = self.request("/account/" + provider)
            self.assertEqual(status, 200)
            self.assertIn(web.ENGLISH[info["hint"]], body)
            self.assertNotIn(info["hint"], body)
        self.assertIn("single-line credential", self.request("/account/deepseek", {"secret": "bad value"})[1])
        self.assertIn("Choose a valid", self.request("/settings", {**form, "interval": "7"})[1])
        self.request("/logout", {})
        self.assertIn("Administrator sign-in", self.request("/settings")[1])
        self.assertIn("credentials are incorrect", self.request("/login", {"password": "bad"})[1])
        self.cookie = self.request("/login", {"password": password})[2]["Set-Cookie"].split(";", 1)[0]
        self.assertIn('value="30" selected', self.request("/settings")[1])

    def test_local_time_and_configured_staleness(self):
        timestamp = int(datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc).timestamp())
        view = web.Screens({"language": "en", "timezone": "America/Los_Angeles", "interval": 30})
        row = {"provider": "codex", "configured": True, "enabled": True, "status": "ok",
               "last_success": timestamp, "error": "Google 未回報已授權的配額專案", "metrics": []}
        with patch.object(web.time, "time", return_value=timestamp + 1200):
            text = view.account_row(row)
            self.assertIn("Up to date", text)
            self.assertIn("2026/07/01 05:00 PDT", text)
            self.assertIn("authorized quota project", text)
        with patch.object(web.time, "time", return_value=timestamp + web.device_settings.stale_after(30)):
            self.assertIn("Stale", view.account_row(row))

    def test_one_minute_and_panel_refresh_note(self):
        settings = {"language": "en", "timezone": "UTC", "interval": 1}
        english = web.Screens(settings)
        self.assertIn("every 1 minute.", english.dashboard([]))
        self.assertIn('value="1" selected>1 minute</option>', english.settings_page())
        self.assertIn("at least 3 minutes between refreshes", english.settings_page())
        chinese = web.Screens({**settings, "language": "zh-Hant"})
        self.assertIn("電子紙兩次刷新至少間隔 3 分鐘。", chinese.settings_page())


if __name__ == "__main__":
    unittest.main()
