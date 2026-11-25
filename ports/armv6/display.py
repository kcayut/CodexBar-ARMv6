"""Raspberry Pi Zero 的 250 × 122 黑白電子紙用量顯示器。"""
import argparse
import hashlib
import importlib
import json
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
import signal
import re
import socket
import ssl
import time

from PIL import Image, ImageDraw, ImageFont
if __package__:
    from .receive import LIMIT, SUMMARY, boot_id, reference_time
else:
    from receive import LIMIT, SUMMARY, boot_id, reference_time

SIZE = (250, 122)
DRIVERS = {
    "bw-v1": "epd2in13",
    "bw-v2": "epd2in13_V2",
    "bw-v3": "epd2in13_V3",
    "bw-v4": "epd2in13_V4",
}
SERVICES = {"codex": "Codex", "gemini": "Gemini", "deepseek": "DeepSeek"}
SOURCES = {"cli": "CLI", "antigravity": "Antigravity", "api": "API", "web": "網頁"}
TZ = timezone(timedelta(hours=8))
STALE_AFTER = 15 * 60
MIN_REFRESH = 180


def read_snapshot(path):
    # The sender replaces the finished summary atomically.
    with path.open("rb") as stream:
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError("摘要過大")
    doc = json.loads(data)
    if not isinstance(doc, dict) or doc.get("version") != 1 or not isinstance(doc.get("services"), list):
        raise ValueError("不支援的摘要")
    return doc, hashlib.sha256(data).hexdigest()


def read_summary(path):
    return read_snapshot(path)[0]


def receipt_time(path, summary, digest, current_boot, now):
    try:
        receipt = json.loads(path.with_suffix(".clock.json").read_bytes())
    except (OSError, ValueError, RecursionError):
        receipt = None
    return reference_time(receipt, digest, summary.get("updated_at"), current_boot, now)


def metric_text(service):
    metrics = service.get("metrics")
    if not isinstance(metrics, list):
        return ""
    parts = []
    for item in metrics:
        if not isinstance(item, dict):
            continue
        label, value = item.get("label"), item.get("value")
        if not all(isinstance(s, str) and s.isascii() and s.isprintable() and len(s) <= 32 for s in (label, value)):
            continue
        parts.append(f"{label} {value}")
    return "    ".join(parts)


def rows(summary, now):
    by_id = {s["id"]: s for s in summary.get("services", [])
             if isinstance(s, dict) and isinstance(s.get("id"), str)}
    for key, name in SERVICES.items():
        service = by_id.get(key, {})
        source = SOURCES.get(service.get("source")) if isinstance(service.get("source"), str) else None
        if source and key == "gemini":
            name += " / " + source
        last = service.get("last_success")
        metrics = metric_text(service)
        if type(last) is not int or not 0 < last <= 253402214399 or not metrics:
            yield name, "尚無資料", "暫時無法取得"
            continue
        stamp = datetime.fromtimestamp(last, TZ).strftime("%m/%d %H:%M")
        if service.get("status") != "ok" or not -60 <= now - last < STALE_AFTER:
            stamp = "過期  " + stamp
        yield name, stamp, metrics


@lru_cache()
def font(size):
    for path in ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
                 "/System/Library/Fonts/STHeiti Medium.ttc"):
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    raise RuntimeError("需要 fonts-wqy-microhei 字型以顯示繁體中文")


def render(summary, now):
    image = Image.new("1", SIZE, 1)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 249, 16), fill=0)
    draw.text((5, 2), "剩餘用量", font=font(13), anchor="lt", fill=1)
    draw.text((245, 3), "最後成功 · 台北時間", font=font(11), anchor="rt", fill=1)
    for index, (name, stamp, metric) in enumerate(rows(summary, now)):
        top = 18 + 35 * index
        draw.text((5, top), name, font=font(13), anchor="lt", fill=0)
        draw.text((245, top), stamp, font=font(12), anchor="rt", fill=0)
        for size in (17, 15, 13, 12):
            if draw.textlength(metric, font=font(size)) <= 240:
                break
        else:
            metric = "額度明細請見管理頁"
        draw.text((5, top + 15), metric, font=font(size), anchor="lt", fill=0)
        if index < 2:
            draw.line((5, top + 32, 244, top + 32), fill=0)
    return image


def pairing_image(home):
    if (home / "admin-password.json").exists():
        return None
    try:
        pair = json.loads((home / "pair-display.json").read_bytes())
        code = pair["code"]
        if not re.fullmatch(r"[0-9]{8}", code) or not time.time() < pair["expires"] <= time.time() + 610:
            return None
        cert = ssl.PEM_cert_to_DER_cert((home / "tls.crt").read_text())
    except (OSError, ValueError, KeyError, TypeError):
        return None
    pin = hashlib.sha256(cert).hexdigest()[:32].upper()
    image = Image.new("1", SIZE, 1)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 249, 17), fill=0)
    draw.text((5, 2), "首次設定 · 用瀏覽器開啟", font=font(13), anchor="lt", fill=1)
    hostname = socket.gethostname().split(".")[0]
    draw.text((5, 21), f"https://{hostname}.local:8443", font=font(14), anchor="lt", fill=0)
    draw.text((5, 48), "配對碼", font=font(16), anchor="lt", fill=0)
    draw.text((70, 42), code[:4] + " " + code[4:], font=font(28), anchor="lt", fill=0)
    expiry = datetime.fromtimestamp(pair["expires"], TZ).strftime("%H:%M")
    draw.text((5, 77), "有效至 " + expiry + " · 憑證 SHA-256 開頭", font=font(12), anchor="lt", fill=0)
    draw.text((5, 99), " ".join(pin[i:i+4] for i in range(0, 32, 4)), font=font(10), anchor="lt", fill=0)
    return image


def refresh(model, image):
    driver = importlib.import_module("waveshare_epd." + DRIVERS[model])
    panel = driver.EPD()
    if image.size != (panel.height, panel.width):
        raise ValueError("面板尺寸不符")
    if model == "bw-v1":
        result = panel.init(panel.lut_full_update)
    elif model == "bw-v2":
        result = panel.init(panel.FULL_UPDATE)
    else:
        result = panel.init()
    if result not in (0, None):
        raise OSError("電子紙初始化失敗")
    try:
        panel.display(panel.getbuffer(image))
    finally:
        panel.sleep()


def serve(path, model):
    summary = {}
    digest = None
    current_boot = boot_id()
    previous_pixels = None
    last_refresh = float("-inf")
    while True:
        try:
            summary, digest = read_snapshot(path)
        except (OSError, ValueError, RecursionError):
            pass  # Keep the visible last good snapshot until the next completed transfer.
        now = time.monotonic()
        clock = receipt_time(path, summary, digest, current_boot, now)
        image = pairing_image(path.parent) or render(summary, clock)
        pixels = image.tobytes()
        elapsed = now - last_refresh
        if elapsed >= MIN_REFRESH and (pixels != previous_pixels or elapsed >= 86400):
            # Waveshare's BUSY loops have no timeout. systemd retries a stuck refresh.
            signal.alarm(60)
            refresh(model, image)
            signal.alarm(0)
            previous_pixels, last_refresh = pixels, now
        time.sleep(30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, default=SUMMARY)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--panel", choices=DRIVERS)
    mode.add_argument("--preview", type=Path, metavar="PNG")
    args = parser.parse_args()
    if args.preview:
        summary, digest = read_snapshot(args.summary) if args.summary.exists() else ({}, None)
        now = time.time()
        current_boot = boot_id()
        if args.summary == SUMMARY and current_boot:
            now = receipt_time(args.summary, summary, digest, current_boot, time.monotonic())
        render(summary, now).save(args.preview, format="PNG")
    else:
        serve(args.summary, args.panel)


if __name__ == "__main__":
    main()
