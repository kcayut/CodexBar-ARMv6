"""Raspberry Pi Zero 的 250 × 122 黑白電子紙用量顯示器。"""
import argparse
import hashlib
import importlib
import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
import signal
import re
import socket
import ssl
import time
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont
if __package__:
    from . import device_settings
    from .receive import LIMIT, SUMMARY, boot_id, reference_time
else:
    import device_settings
    from receive import LIMIT, SUMMARY, boot_id, reference_time

SIZE = (250, 122)
DRIVERS = {
    "bw-v1": "epd2in13",
    "bw-v2": "epd2in13_V2",
    "bw-v3": "epd2in13_V3",
    "bw-v4": "epd2in13_V4",
}
SERVICES = {"codex": "Codex", "gemini": "Gemini", "deepseek": "DeepSeek"}
SOURCES = {"cli": "CLI", "antigravity": "Antigravity", "api": "API"}
TEXT = {
    "zh-Hant": {
        "remaining": "剩餘用量", "last": "最後成功", "stale": "過期", "web": "網頁",
        "no_data": "尚無資料", "unavailable": "暫時無法取得", "overflow": "額度明細請見管理頁",
        "setup": "首次設定 · 用瀏覽器開啟", "code": "配對碼", "until": "有效至", "pin": "憑證 SHA-256 開頭",
    },
    "en": {
        "remaining": "Remaining", "last": "Last success", "stale": "Stale", "web": "Web",
        "no_data": "No data", "unavailable": "Unavailable", "overflow": "See details on the web",
        "setup": "Setup · Open in browser", "code": "Code", "until": "Until", "pin": "Cert SHA-256 prefix",
    },
}
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


def zone_label(settings):
    zone = settings["timezone"]
    if zone == "Asia/Taipei" and settings["language"] == "zh-Hant":
        return "台北時間"
    if zone.startswith("Etc/"):
        return zone
    return zone.rsplit("/", 1)[-1].replace("_", " ")


def rows(summary, now, settings=device_settings.DEFAULTS):
    text = TEXT[settings["language"]]
    sources = {**SOURCES, "web": text["web"]}
    zone = ZoneInfo(settings["timezone"])
    stale_after = device_settings.stale_after(settings["interval"])
    by_id = {s["id"]: s for s in summary.get("services", [])
             if isinstance(s, dict) and isinstance(s.get("id"), str)}
    for key, name in SERVICES.items():
        service = by_id.get(key, {})
        source = service.get("source")
        source = sources.get(source) if isinstance(source, str) else None
        if source and key == "gemini":
            name += " / " + source
        last = service.get("last_success")
        metrics = metric_text(service)
        if type(last) is not int or not 0 < last <= 253402214399 or not metrics:
            yield name, text["no_data"], text["unavailable"]
            continue
        stamp = datetime.fromtimestamp(last, zone).strftime("%m/%d %H:%M")
        if service.get("status") != "ok" or not -60 <= now - last < stale_after:
            stamp = text["stale"] + "  " + stamp
        yield name, stamp, metrics


@lru_cache()
def font(size):
    for path in ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
                 "/System/Library/Fonts/STHeiti Medium.ttc"):
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    raise RuntimeError("需要 fonts-wqy-microhei 字型以顯示繁體中文")


def header(draw, title, settings, timestamp, prefix=""):
    zone = zone_label(settings)
    title_right = draw.textbbox((5, 2), title, font=font(13), anchor="lt")[2]
    label_left = draw.textbbox((245, 3), prefix + zone, font=font(11), anchor="rt")[0]
    if label_left - title_right < 5:
        if type(timestamp) is not int or not 0 < timestamp <= 253402214399:
            timestamp = time.time()
        offset = datetime.fromtimestamp(timestamp, ZoneInfo(settings["timezone"])).strftime("%z")
        zone = f"UTC{offset[:3]}:{offset[3:5]}"
    draw.rectangle((0, 0, 249, 16), fill=0)
    draw.text((5, 2), title, font=font(13), anchor="lt", fill=1)
    draw.text((245, 3), prefix + zone, font=font(11), anchor="rt", fill=1)


def render(summary, now, settings=device_settings.DEFAULTS):
    text = TEXT[settings["language"]]
    image = Image.new("1", SIZE, 1)
    draw = ImageDraw.Draw(image)
    header(draw, text["remaining"], settings, summary.get("updated_at"), text["last"] + " · ")
    for index, (name, stamp, metric) in enumerate(rows(summary, now, settings)):
        top = 18 + 35 * index
        draw.text((5, top), name, font=font(13), anchor="lt", fill=0)
        draw.text((245, top), stamp, font=font(12), anchor="rt", fill=0)
        for size in (17, 15, 13, 12):
            if draw.textlength(metric, font=font(size)) <= 240:
                break
        else:
            metric = text["overflow"]
        draw.text((5, top + 15), metric, font=font(size), anchor="lt", fill=0)
        if index < 2:
            draw.line((5, top + 32, 244, top + 32), fill=0)
    return image


def draw_address(draw, address):
    if draw.textlength(address, font=font(14)) <= 240:
        draw.text((5, 21), address, font=font(14), anchor="lt", fill=0)
        return
    # The address can include a 63-character DNS label. Wrap it above the code.
    for size, height, top in ((11, 13, 19), (9, 10, 17)):
        lines = [""]
        for char in address:
            if draw.textlength(lines[-1] + char, font=font(size)) > 240:
                lines.append("")
            lines[-1] += char
        if top + len(lines) * height <= 47:
            for i, line in enumerate(lines):
                draw.text((5, top + i * height), line, font=font(size), anchor="lt", fill=0)
            return


def pairing_image(home, settings=device_settings.DEFAULTS):
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
    text = TEXT[settings["language"]]
    image = Image.new("1", SIZE, 1)
    draw = ImageDraw.Draw(image)
    header(draw, text["setup"], settings, pair["expires"])
    hostname = socket.gethostname().split(".")[0]
    draw_address(draw, f"https://{hostname}.local:8443")
    draw.text((5, 48), text["code"], font=font(16), anchor="lt", fill=0)
    draw.text((70, 47), code[:4] + " " + code[4:], font=font(28), anchor="lt", fill=0)
    expiry = datetime.fromtimestamp(pair["expires"], ZoneInfo(settings["timezone"])).strftime("%m/%d %H:%M")
    draw.text((5, 77), f'{text["until"]} {expiry} · {text["pin"]}', font=font(12), anchor="lt", fill=0)
    for start, top in ((0, 93), (16, 107)):
        groups = (pin[i:i+4] for i in range(start, start + 16, 4))
        draw.text((5, top), " ".join(groups), font=font(12), anchor="lt", fill=0)
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
        settings = device_settings.load(path.parent)
        image = pairing_image(path.parent, settings) or render(summary, clock, settings)
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
        render(summary, now, device_settings.load(args.summary.parent)).save(args.preview, format="PNG")
    else:
        serve(args.summary, args.panel)


if __name__ == "__main__":
    main()
