import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import device_settings
import providers
import receive
from state import Store


class StateTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.path = self.home / "summary.json"
        self.now = int(time.time())
        self.balance = [{"label": "USD", "value": "23.45"}]

    def test_migration_only_once_and_secrets_never_in_summary(self):
        old = dict(id="deepseek", source="api", status="ok", metrics=self.balance, last_success=self.now-450)
        receive.save(json.dumps(dict(version=1, updated_at=self.now-400, services=[old])).encode(),
                     self.path, "old", 100)
        store = Store(self.home)
        row, = store.list_accounts()
        self.assertEqual((row["status"], row["configured"], row["last_success"]), ("stale", False, self.now-450))
        store.remove_account("deepseek")
        self.assertEqual(Store(self.home).list_accounts(), [])
        store.set_account("deepseek", "test-only-credential")
        self.assertNotIn("test-only-credential", repr(store.list_accounts()))
        with patch("providers.fetch", return_value=self.balance):
            store.collect()
        last = receive.decode(self.path.read_bytes())["services"][-1]
        self.assertEqual((last["metrics"], last["status"]), (self.balance, "ok"))
        self.assertNotIn(b"test-only-credential", self.path.read_bytes())
        self.assertEqual(store.db.stat().st_mode & 0o777, 0o600)

    def test_failure_isolated_rotation_cannot_publish_old_query(self):
        store = Store(self.home)
        store.set_account("deepseek", "first")
        store.set_account("codex", "other")
        def fetch(provider, secret):
            if provider == "codex":
                raise providers.ProviderError("授權已失效")
            return self.balance
        with patch("providers.fetch", side_effect=fetch):
            store.collect()
        codex, deepseek = store.list_accounts()
        self.assertEqual((codex["status"], deepseek["status"]), ("unavailable", "ok"))
        with patch("providers.fetch", side_effect=providers.ProviderError("連線失敗")), patch("state.time.time", return_value=self.now+10):
            store.collect()
        self.assertEqual(store.list_accounts()[1]["metrics"], self.balance)
        self.assertEqual(store.list_accounts()[1]["status"], "stale")
        def rotate(provider, secret):
            if provider == "deepseek":
                store.remove_account(provider)
                store.set_account(provider, "second")
            return self.balance
        with patch("providers.fetch", side_effect=rotate), patch("state.time.time", return_value=self.now+20):
            store.collect()
        self.assertEqual(store.list_accounts()[1]["metrics"], [])

    def test_negative_balance_and_disabled_accounts(self):
        store = Store(self.home)
        store.set_account("deepseek", "test")
        with patch("providers.fetch", return_value=[{"label":"CNY", "value":"-1.02"}]):
            store.collect()
        self.assertEqual(receive.decode(self.path.read_bytes())["services"][-1]["metrics"][0]["value"], "-1.02")
        store.set_enabled("deepseek", False)
        with patch("providers.fetch") as fetch, patch("state.time.time", return_value=self.now+10):
            store.collect()
            fetch.assert_not_called()
        self.assertEqual(store.list_accounts()[0]["status"], "stale")

    def test_idle_tick_after_failure_does_not_query_or_write(self):
        store = Store(self.home)
        store.set_account("deepseek", "test")
        with patch("receive.boot_id", return_value="boot"), patch("state.time.monotonic") as clock, \
                patch("providers.fetch", side_effect=providers.ProviderError("連線失敗")) as fetch:
            clock.return_value = 100
            self.assertTrue(store.collect(scheduled=True))
            fetch.assert_called_once()
            files = {p: p.stat().st_mtime_ns for p in self.home.iterdir()}
            self.assertEqual(store.collect_lock.stat().st_mode & 0o777, 0o600)
            clock.return_value = 160
            # An NTP clock step must not cause another provider request in the same boot.
            with patch("state.time.time", return_value=self.now + 86400):
                self.assertFalse(Store(self.home).collect(scheduled=True))
            fetch.assert_called_once()
            self.assertEqual(files, {p: p.stat().st_mtime_ns for p in self.home.iterdir()})

    def test_live_interval_changes_manual_start_and_reboot(self):
        store = Store(self.home)
        store.set_account("deepseek", "test")
        with patch("state.time.monotonic") as clock, patch("state.time.time") as wall, \
                patch("receive.boot_id") as boot, patch("providers.fetch", return_value=self.balance) as fetch:
            boot.return_value = "first"
            def tick(seconds, scheduled=True):
                clock.return_value = seconds
                wall.return_value = self.now + seconds
                return store.collect(scheduled=scheduled)
            self.assertTrue(tick(100))
            device_settings.save(self.home, "Asia/Taipei", "zh-Hant", 1)
            self.assertFalse(tick(130))
            self.assertTrue(tick(160))
            device_settings.save(self.home, "Asia/Taipei", "zh-Hant", 60)
            self.assertFalse(tick(760))
            self.assertTrue(tick(761, scheduled=False))
            self.assertFalse(tick(801))
            boot.return_value = "second"
            clock.return_value = 30
            wall.return_value = self.now + 820
            self.assertTrue(store.collect(scheduled=True))
            self.assertEqual(fetch.call_count, 4)

    def test_early_minute_tick_waits_only_until_due(self):
        store = Store(self.home)
        store.set_account("deepseek", "test")
        with patch("state.time.monotonic") as clock, patch("state.time.time") as wall, \
                patch("receive.boot_id", return_value="boot"), patch("providers.fetch", return_value=self.balance) as fetch:
            wall.return_value = self.now
            clock.return_value = 100
            self.assertTrue(store.collect(scheduled=True))
            clock.return_value = 398.5
            wall.return_value = self.now + 298.5
            def advance(seconds):
                clock.return_value += seconds
                wall.return_value += seconds
            with patch("state.time.sleep", side_effect=advance) as sleep:
                self.assertTrue(store.collect(scheduled=True))
                sleep.assert_called_once_with(1.5)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(json.loads((self.home / "last-attempt.json").read_bytes())["monotonic"], 400)

    def test_parallel_manual_collection_skips_an_active_attempt(self):
        store = Store(self.home)
        store.set_account("deepseek", "test")
        def other_collection(provider, secret):
            self.assertFalse(Store(self.home).collect())
            return self.balance
        with patch("providers.fetch", side_effect=other_collection) as fetch:
            self.assertTrue(store.collect(scheduled=True))
            fetch.assert_called_once()

    def test_staleness_tracks_configured_interval(self):
        store = Store(self.home)
        store.set_account("deepseek", "test")
        with patch("state.time.time", return_value=self.now), patch("providers.fetch", return_value=self.balance):
            store.collect()
        with patch("state.time.time", return_value=self.now + 1200):
            self.assertEqual(store.list_accounts()[0]["status"], "stale")
            device_settings.save(self.home, "Asia/Taipei", "zh-Hant", 10)
            self.assertEqual(store.list_accounts()[0]["status"], "ok")

if __name__ == "__main__":
    unittest.main()
