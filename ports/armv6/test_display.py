import json
from datetime import datetime
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
        for now in (NOW + display.device_settings.stale_after(5), NOW - 120):
            self.assertIn("過期", next(display.rows(sample(), now))[1])

    def test_english_rows_timezone_dst_and_long_collection_interval(self):
        doc = sample()
        settings = {"timezone": "America/New_York", "language": "en", "interval": 30}
        doc["services"][1]["source"] = "web"
        for utc, expected in (("2026-03-08T04:45:00+00:00", "03/07 23:45"),
                              ("2026-03-08T06:45:00+00:00", "03/08 01:45"),
                              ("2026-03-08T07:45:00+00:00", "03/08 03:45")):
            stamp = int(datetime.fromisoformat(utc).timestamp())
            doc["services"][0]["last_success"] = stamp
            rows = list(display.rows(doc, stamp + 3600, settings))
            self.assertEqual(rows[0][1], expected)
            self.assertEqual(rows[1][0], "Gemini / Web")
            self.assertTrue(rows[1][1].startswith("Stale"))
            self.assertEqual(rows[2][1:], ("No data", "Unavailable"))
            boundary = stamp + display.device_settings.stale_after(30)
            self.assertEqual(next(display.rows(doc, boundary, settings))[1], "Stale  " + expected)

    def test_daemon_rereads_preferences_and_throttles_hardware_writes(self):
        settings = {"language": "en", "timezone": "UTC", "interval": 60}
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            path = home / "summary.json"
            path.write_text(json.dumps(sample()))
            ticks = iter((0, 30, 180, 210))
            sleeps = 0

            def change_settings(_):
                nonlocal sleeps
                sleeps += 1
                if sleeps == 1:
                    display.device_settings.save(home, **settings)
                elif sleeps == 4:
                    raise KeyboardInterrupt

            with patch("display.receipt_time", return_value=NOW), patch("display.signal.alarm"), \
                    patch("display.time.monotonic", side_effect=ticks), \
                    patch("display.time.sleep", side_effect=change_settings), patch("display.refresh") as refresh, \
                    self.assertRaises(KeyboardInterrupt):
                display.serve(path, "bw-v4")
            frames = [call.args[1].tobytes() for call in refresh.call_args_list]
            self.assertEqual(frames, [display.render(sample(), NOW).tobytes(),
                                      display.render(sample(), NOW, settings).tobytes()])
            self.assertNotEqual(frames[0], frames[1])

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
                settings = {"timezone": "UTC", "language": "en", "interval": 5}
                with patch.object(display.ImageDraw.ImageDraw, "text", autospec=True) as draw:
                    display.pairing_image(home, settings)
                lines = [call.args[2] for call in draw.call_args_list]
                self.assertIn("Setup · Open in browser", lines)
                self.assertIn("UTC", lines)
                self.assertIn("Code", lines)
                self.assertIn("Until 11/25 05:15 · Cert SHA-256 prefix", lines)
            with patch("display.time.time", return_value=NOW + 300):
                self.assertIsNone(display.pairing_image(home))
            (home / "admin-password.json").write_text("{}")
            with patch("display.time.time", return_value=NOW):
                self.assertIsNone(display.pairing_image(home))

    def test_quota_and_setup_text_fit_panel_in_both_languages(self):
        doc = sample()
        doc["services"][1]["source"] = "antigravity"
        doc["services"][0]["metrics"] = [{"label": "M" * 32, "value": "M" * 32}]
        original_text = display.ImageDraw.ImageDraw.text
        boxes = []

        def record(draw, xy, label, **opts):
            box = draw.textbbox(xy, label, font=opts["font"], anchor=opts["anchor"])
            self.assertTrue(0 <= box[0] < box[2] <= 250 and 0 <= box[1] < box[3] <= 122, label)
            for other, (left, top, right, bottom) in boxes:
                overlap = left < box[2] and box[0] < right and top < box[3] and box[1] < bottom
                self.assertFalse(overlap, (label, other))
            boxes.append((label, box))
            return original_text(draw, xy, label, **opts)

        hostname = "w" * 63
        with tempfile.TemporaryDirectory() as directory, patch("display.time.time", return_value=NOW), \
                patch("display.socket.gethostname", return_value=hostname), \
                patch.object(display.ImageDraw.ImageDraw, "text", record):
            home = Path(directory)
            (home / "tls.crt").write_text("-----BEGIN CERTIFICATE-----\nAQI=\n-----END CERTIFICATE-----")
            (home / "pair-display.json").write_text(json.dumps({"code": "88888888", "expires": NOW + 300}))
            for language in ("en", "zh-Hant"):
                for zone in ("America/Argentina/Buenos_Aires", "Antarctica/DumontDUrville", "Asia/Taipei"):
                    with self.subTest(language=language, zone=zone):
                        settings = {"language": language, "timezone": zone, "interval": 5}
                        boxes.clear()
                        display.render(doc, NOW, settings)
                        self.assertIn(display.TEXT[language]["overflow"], [label for label, _ in boxes])
                        boxes.clear()
                        with patch("display.hashlib.sha256") as sha256:
                            sha256.return_value.hexdigest.return_value = "c" * 64
                            display.pairing_image(home, settings)
                        labels = [label for label, _ in boxes]
                        self.assertIn(f"https://{hostname}.local:8443", "".join(labels))
                        self.assertEqual(labels[-2:], ["CCCC CCCC CCCC CCCC"] * 2)

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
