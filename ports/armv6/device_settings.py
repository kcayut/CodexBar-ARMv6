"""Display and collection preferences shared by all three Pi services."""
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if __package__:
    from .receive import atomic_write
else:
    from receive import atomic_write

DEFAULTS = {"timezone": "Asia/Taipei", "language": "zh-Hant", "interval": 5}
INTERVALS = (1, 3, 5, 10, 15, 30, 60)
FILENAME = "device-settings.json"


def validated(timezone, language, interval):
    if (not isinstance(timezone, str) or not timezone or len(timezone) > 128
            or timezone in ("localtime", "posixrules") or timezone.startswith(("right/", "posix/"))):
        raise ValueError("無效的時區")
    try:
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("無效的時區") from exc
    if language not in ("zh-Hant", "en") or type(interval) is not int or interval not in INTERVALS:
        raise ValueError("無效的語言或更新頻率")
    return {"timezone": timezone, "language": language, "interval": interval}


def load(home):
    try:
        with (Path(home) / FILENAME).open("rb") as f:
            return validated(**json.loads(f.read(2048)))
    except (FileNotFoundError, ValueError, TypeError, RecursionError):
        return DEFAULTS.copy()


def save(home, timezone, language, interval):
    settings = validated(timezone, language, interval)
    home = Path(home)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write(home / FILENAME, json.dumps(settings).encode())
    directory = os.open(home, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return settings


def stale_after(interval):
    return 3 * interval * 60
