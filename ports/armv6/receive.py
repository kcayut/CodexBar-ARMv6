"""Receive one display summary on stdin. A successful reply is the SHA-256 of the saved bytes."""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time

SUMMARY = Path.home() / ".local/share/quota-desk/summary.json"
LIMIT = 64 * 1024
MAX_STAMP = 253402214399
SOURCES = {"codex": {"oauth"}, "gemini": {"cli", "web", "antigravity", "pending"},
           "deepseek": {"api", "web", "pending"}, "claude": {"auto"}, "copilot": {"auto"}}
PERCENT_LABELS = {"5h", "7d", "Pool", "Pro", "Flash", "Lite", "Premium", "Chat"}


def unique_keys(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("摘要含重複欄位")
    return obj


def keys(value, names):
    return isinstance(value, dict) and value.keys() == set(names.split())


def valid_time(stamp):
    return type(stamp) is int and 0 < stamp <= MAX_STAMP


def decode(raw):
    if len(raw) > LIMIT:
        raise ValueError("摘要過大")
    doc = json.loads(raw.decode("utf8"), object_pairs_hook=unique_keys)
    if (not keys(doc, "version updated_at services")
            or type(doc["version"]) is not int or doc["version"] != 1 or not valid_time(doc["updated_at"])):
        raise ValueError("摘要格式不符")
    services = doc["services"]
    if not isinstance(services, list) or not 1 <= len(services) <= len(SOURCES):
        raise ValueError("服務清單格式不符")
    seen = set()
    for item in services:
        if not keys(item, "id source status metrics last_success"):
            raise ValueError("服務欄位不符")
        name, source = item["id"], item["source"]
        if not isinstance(name, str) or name not in SOURCES or name in seen:
            raise ValueError("服務不符")
        seen.add(name)
        if not isinstance(source, str) or source not in SOURCES[name]:
            raise ValueError("入口不符")
        metrics, stamp, status = item["metrics"], item["last_success"], item["status"]
        if not isinstance(metrics, list) or len(metrics) > 3:
            raise ValueError("額度格式不符")
        if status == "unavailable" and not metrics and stamp is None:
            continue
        if (status not in ("ok", "stale") or not metrics or not valid_time(stamp)
                or stamp > doc["updated_at"] + 60):
            raise ValueError("狀態格式不符")
        for metric in metrics:
            if not keys(metric, "label value") or not all(isinstance(s, str) for s in metric.values()):
                raise ValueError("額度欄位不符")
            label, value = metric["label"], metric["value"]
            if name == "deepseek":
                valid = label in ("USD", "CNY") and re.fullmatch(r"-?[0-9]{1,12}\.[0-9]{2}", value)
            else:
                valid = label in PERCENT_LABELS and (value == "--" or re.fullmatch(r"(?:100|[1-9]?[0-9])%", value))
            if not valid:
                raise ValueError("額度數值不符")
    return doc


def boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


def reference_time(receipt, digest, stamp, current_boot, now):
    """Estimate Mac time using elapsed Pi uptime; an unpaired clock yields stale rows."""
    if not isinstance(receipt, dict) or not current_boot or receipt.get("boot_id") != current_boot:
        return float("inf")
    arrival = receipt.get("monotonic")
    if (receipt.get("sha256") != digest or receipt.get("updated_at") != stamp or not valid_time(stamp)
            or type(arrival) not in (int, float) or not 0 <= arrival <= now or not math.isfinite(arrival)):
        return float("inf")
    return stamp + (now - arrival)


def atomic_write(path, raw):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        temp = Path(out.name)
        try:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def save(raw, path, current_boot, now):
    doc = decode(raw)
    digest = hashlib.sha256(raw).hexdigest()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        try:
            prior_raw = path.read_bytes()
        except FileNotFoundError:
            prior_raw = b""
        if prior_raw != raw:
            try:
                prior = decode(prior_raw)
            except (ValueError, RecursionError):
                prior = {}
            if doc["updated_at"] <= prior.get("updated_at", 0):
                raise ValueError("摘要早於已收到的版本")
            receipt = {"sha256": digest, "updated_at": doc["updated_at"], "boot_id": current_boot, "monotonic": now}
            # Commit the paired clock first. A reader can detect the short mismatch by hash.
            atomic_write(path.with_suffix(".clock.json"), json.dumps(receipt).encode())
            atomic_write(path, raw)
            os.fsync(directory)
        stored = hashlib.sha256(path.read_bytes()).hexdigest()
        if stored != digest:
            raise OSError("讀回摘要不符")
        return stored
    finally:
        os.close(directory)


if __name__ == "__main__":
    try:
        print(save(sys.stdin.buffer.read(LIMIT + 1), SUMMARY, boot_id(), time.monotonic()))
    except (OSError, ValueError, RecursionError):
        sys.exit("摘要未儲存")
