import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import display


NOW = 1732511400


def sample():
    return {"version": 1, "updated_at": NOW, "services": [
        {"id": "codex", "source": "oauth", "status": "ok", "last_success": NOW,
         "metrics": [{"label": "5h", "value": "75%"}, {"label": "7d", "value": "80%"}]},
        {"id": "gemini", "source": "cli", "status": "stale", "last_success": NOW - 300,
         "metrics": [{"label": name, "value": "100%"} for name in ("Pro", "Flash", "Lite")]},
        {"id": "deepseek", "source": "api", "status": "unavailable", "last_success": None, "metrics": []}
    ]}


class DisplayTest(unittest.TestCase):
    def test_all_three_including_failed_and_cached_service(self):
        rows = list(display.rows(sample(), NOW))
        self.assertEqual([r[0] for r in rows], ["Codex", "Gemini / CLI", "DeepSeek"])
        self.assertEqual(rows[0][2], "5h 75%    7d 80%")
        self.assertIn("過期", rows[1][1])
        self.assertIn("Flash 100%", rows[1][2])
        self.assertEqual(rows[2][1], "尚無資料")

    def test_missed_delivery_and_unsynchronized_clock(self):
        for now in (NOW + display.STALE_AFTER, NOW - 120):
            self.assertIn("過期", next(display.rows(sample(), now))[1])

    def test_cold_boot_reads_same_file_and_optional_service_stays_off_screen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "summary.json"
            example = sample()
            example["services"].append({"id": "claude"})
            path.write_text(json.dumps(example))
            restored = display.read_summary(path)
            image = display.render(restored, NOW)
            self.assertEqual(len(list(display.rows(restored, NOW))), 3)
            self.assertEqual(image.size, (250, 122))
            self.assertEqual(image.mode, "1")
            self.assertNotEqual(image.tobytes(), display.render({}, NOW).tobytes())
            path.write_text('{"version":2}')
            with self.assertRaises(ValueError):
                display.read_summary(path)

    def test_pairing_on_panel_only_until_expiry_or_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "tls.crt").write_text("-----BEGIN CERTIFICATE-----\nAQI=\n-----END CERTIFICATE-----")
            (home / "pair-display.json").write_text(json.dumps({"code": "12345678", "expires": NOW + 300}))
            with patch("display.time.time", return_value=NOW):
                self.assertEqual(display.pairing_image(home).size, (250, 122))
            with patch("display.time.time", return_value=NOW + 300):
                self.assertIsNone(display.pairing_image(home))
            (home / "admin-password.json").write_text("{}")
            with patch("display.time.time", return_value=NOW):
                self.assertIsNone(display.pairing_image(home))

    def test_driver_full_refresh_and_sleep(self):
        for model in display.DRIVERS:
            with self.subTest(model=model), patch("display.importlib.import_module") as load:
                epd = load.return_value.EPD.return_value
                epd.width, epd.height = 122, 250
                epd.init.return_value = 0
                display.refresh(model, Mock(size=(250, 122)))
                args = {"bw-v1": (epd.lut_full_update,), "bw-v2": (epd.FULL_UPDATE,)}
                epd.init.assert_called_once_with(*args.get(model, ()))
                epd.display.assert_called_once_with(epd.getbuffer.return_value)
                epd.sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
