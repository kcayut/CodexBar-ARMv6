import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from receive import LIMIT, decode, reference_time, save


NOW = 1795618363


def payload(stamp=NOW):
    return json.dumps({"version": 1, "updated_at": stamp, "services": [
        {"id": "codex", "source": "oauth", "last_success": stamp - 12, "status": "ok",
         "metrics": [{"label": "5h", "value": "--"}, {"label": "7d", "value": "55%"}]},
        {"id": "gemini", "source": "antigravity", "last_success": None, "status": "unavailable", "metrics": []},
        {"id": "deepseek", "source": "api", "last_success": stamp - 5, "status": "stale",
         "metrics": [{"label": "USD", "value": "23.45"}]}]}).encode() + b"\n"


class ReceiveTests(unittest.TestCase):
    def test_ack_uses_exact_bytes_duplicate_and_replay_preserve_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state/summary.json"
            raw = payload()
            digest = save(raw, path, "boot1", 240)
            self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(path.read_bytes(), raw)
            duplicate = save(raw, path, "boot1", 550)
            self.assertEqual(duplicate, digest)
            receipt = json.loads(path.with_suffix(".clock.json").read_bytes())
            self.assertEqual(reference_time(receipt, digest, NOW, "boot1", 600), NOW + 360)
            self.assertEqual(reference_time(receipt, digest, NOW, "boot2", 600), float("inf"))
            self.assertEqual(reference_time(receipt, "other", NOW, "boot1", 600), float("inf"))
            with self.assertRaises(ValueError):
                save(payload(NOW - 300), path, "boot1", 560)
            self.assertEqual(path.read_bytes(), raw)
            self.assertNotEqual(save(payload(NOW + 300), path, "boot1", 600), digest)

    def test_rejects_unknown_keys_sensitive_strings_duplicate_and_out_of_range(self):
        base = payload()
        for malformed in [base.replace(b'"version"', b'"secret": "credential", "version"'),
                          base.replace(b'"value": "--"', b'"value": "secret", "value": "--"'),
                          base.replace(b'"55%"', b'"secret-key"'), base.replace(b'"last_success"', b'"account"'),
                          base.replace(b'"api"', b'"credential"'), base.replace(b'"55%"', b'"155%"'),
                          base.replace(b'"version": 1', b'"version": true'), b'x' * (LIMIT + 1)]:
            with self.subTest(length=len(malformed)), self.assertRaises(ValueError):
                decode(malformed)

    def test_interrupted_replace_never_replaces_visible_summary_with_partial_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.json"
            raw = payload()
            save(raw, path, "boot1", 100)
            with patch("receive.os.replace", side_effect=OSError), self.assertRaises(OSError):
                save(payload(NOW + 300), path, "boot1", 400)
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual({x.name for x in path.parent.iterdir()}, {"summary.json", "summary.clock.json"})

    def test_display_service_calculates_elapsed_time_from_receipt(self):
        import display
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.json"
            save(payload(), path, "current", 100)
            for boot, expected in (("current", NOW + 30), ("next", float("inf"))):
                with patch("display.boot_id", return_value=boot), patch("display.time.monotonic", return_value=130), \
                     patch("display.time.sleep", side_effect=KeyboardInterrupt), patch("display.signal.alarm"), \
                     patch("display.refresh"), patch("display.render") as render, self.assertRaises(KeyboardInterrupt):
                    display.serve(path, "bw-v4")
                self.assertEqual(render.call_args[0][1], expected)


if __name__ == "__main__":
    unittest.main()
