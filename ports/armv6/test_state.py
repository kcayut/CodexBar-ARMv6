import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

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

if __name__ == "__main__":
    unittest.main()
