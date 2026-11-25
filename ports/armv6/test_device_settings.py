from pathlib import Path
import tempfile
import unittest

import device_settings as settings


class DeviceSettingsTest(unittest.TestCase):
    def test_defaults_persistence_and_invalid_submissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertEqual(settings.load(home), {"timezone": "Asia/Taipei", "language": "zh-Hant", "interval": 5})
            self.assertEqual(list(home.iterdir()), [])
            saved = settings.save(home, "America/New_York", "en", 30)
            self.assertEqual(settings.load(home), saved)
            path = home / settings.FILENAME
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            for tz, lang, interval in (("../etc/passwd","en",5), ("Mars/Base","en",5),
                                       ("UTC","jp",5), ("UTC","en",True), ("UTC","en",7)):
                with self.subTest(tz=tz, lang=lang, interval=interval), self.assertRaises(ValueError):
                    settings.save(home, tz, lang, interval)
            self.assertEqual(settings.load(home), saved)
            path.write_text("{")
            self.assertEqual(settings.load(home), settings.DEFAULTS)
