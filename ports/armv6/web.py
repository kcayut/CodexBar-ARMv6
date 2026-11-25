"""Pi 本機的 HTTPS 訂閱管理頁。"""

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sqlite3
import ssl
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from functools import lru_cache
from html import escape
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo, available_timezones

if __package__:
    from . import device_settings
    from .web_text import ENGLISH
    from .providers import PROVIDERS
    from .state import Store
else:
    import device_settings
    from web_text import ENGLISH
    from providers import PROVIDERS
    from state import Store


HOME = Path(os.environ.get("QUOTA_DESK_HOME", Path.home() / ".local/share/quota-desk"))
SESSION_SECONDS = 8 * 3600
COOKIE = "__Host-qd_session"
CSS = Path(__file__).with_name("web.css")
NOTICES = {"/saved": "已儲存帳號，下一次擷取會使用新憑證。", "/updated": "已更新帳號狀態。", "/removed": "已移除帳號。"}
STATUS = {"ok": "已更新", "stale": "已過期", "unavailable": "暫時無法取得", "pending": "等待擷取",
          "expired": "憑證已到期", "unauthorized": "需要重新授權", "disabled": "已停用"}


def read_json(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def write_private(path, record):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        tmp = Path(output.name)
        try:
            json.dump(record, output)
            output.flush()
            os.fsync(output.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def prepare_home(home):
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home.chmod(0o700)


@contextmanager
def setup_lock(home):
    # The SSH command and the web process must agree on which unused code is valid.
    with os.fdopen(os.open(home / "web-setup.lock", os.O_CREAT | os.O_RDWR, 0o600), "r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def issue_setup_code(home, display=False):
    prepare_home(home)
    with setup_lock(home):
        visible = home / "pair-display.json"
        challenge = home / "setup-code.json"
        if (home / "admin-password.json").exists():
            visible.unlink(missing_ok=True)
            challenge.unlink(missing_ok=True)
            if display:
                return None
            raise ValueError("管理密碼已設定，請使用管理頁登入。")
        now = int(time.time())
        pending = read_json(challenge)
        if display and pending.get("expires", 0) > now:
            if pending.get("kind") != "display":
                return None  # A locally requested SSH code takes precedence.
            panel = read_json(visible)
            code = panel.get("code", "")
            if (panel.get("expires") == pending["expires"] and isinstance(code, str)
                    and hmac.compare_digest(hashlib.sha256(code.encode()).hexdigest(), pending["hash"])):
                return None
        code = f"{secrets.randbelow(100_000_000):08d}" if display else secrets.token_urlsafe(18)
        expires = now + 600
        write_private(challenge, {"hash": hashlib.sha256(code.encode()).hexdigest(), "expires": expires,
                                  "kind": "display" if display else "ssh"})
        if display:
            write_private(visible, {"code": code, "expires": expires})
            return None
        visible.unlink(missing_ok=True)
        return code


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000).hex()


def redeem(home, code, password):
    with setup_lock(home):
        pending = read_json(home / "setup-code.json")
        if pending.get("kind") == "display" and re.fullmatch(r"[0-9]{4} [0-9]{4}", code):
            code = code.replace(" ", "")
        if ((home / "admin-password.json").exists() or pending.get("expires", 0) <= time.time()
                or not hmac.compare_digest(hashlib.sha256(code.encode()).hexdigest(), pending.get("hash", ""))):
            return False
        salt = secrets.token_hex(16)
        write_private(home / "admin-password.json", {"salt": salt, "hash": password_hash(password, salt)})
        (home / "setup-code.json").unlink(missing_ok=True)
        (home / "pair-display.json").unlink(missing_ok=True)
        return True


def fingerprint(cert):
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    return hashlib.sha256(der).digest().hex(":").upper()


@lru_cache(maxsize=1)
def zone_options():
    return "".join(f'<option value="{escape(name)}"></option>' for name in sorted(available_timezones())
                   if not name.startswith(("posix/", "right/", "SystemV/")) and name not in ("localtime", "posixrules"))


class Screens:
    def __init__(self, preferences):
        self.prefs = preferences
        self.language = preferences["language"]

    def text(self, source):
        return ENGLISH.get(source, source) if self.language == "en" else source

    def minutes(self, interval):
        return self.text("1 分鐘") if interval == 1 else self.text("{interval} 分鐘").format(interval=interval)

    def page(self, title, content, active=""):
        t = self.text
        controls = ""
        if active:
            links = []
            for href, label in (("/", "訂閱管理"), ("/settings", "本機設定")):
                current = ' aria-current="page"' if href == active else ""
                links.append(f'<a href="{href}"{current}>{t(label)}</a>')
            controls = (f'<nav aria-label="{t("主要導覽")}">{"".join(links)}</nav>'
                        f'<form method="post" action="/logout"><button class="text">{t("登出")}</button></form>')
        return f"""<!doctype html><html lang="{self.language}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{t(title)} · Quota Desk</title>
<link rel="stylesheet" href="/web.css"></head><body><header><b lang="en">Quota Desk</b>{controls}</header>
<main>{content}</main></body></html>"""

    def auth_page(self, setup=False, message=""):
        t = self.text
        title = "設定管理密碼" if setup else "管理員登入"
        instructions = ("請開啟瀏覽器的憑證資訊，核對 SHA-256 指紋的前 32 個十六進位字元與電子紙相同，"
                        "再輸入電子紙上的 8 位數設定碼。也可透過已配對的 SSH 核對完整指紋並取得設定碼。")
        if setup:
            fields = f"""<p>{t(instructions)}</p><label>{t("一次性設定碼")}<input name="code" autocomplete="one-time-code" maxlength="128" required></label>
<label>{t("新管理密碼（至少 12 字元）")}<input type="password" name="password" autocomplete="new-password" minlength="12" maxlength="256" required></label>
<label>{t("再次輸入密碼")}<input type="password" name="confirm" autocomplete="new-password" maxlength="256" required></label>"""
        else:
            fields = f'<label>{t("管理密碼")}<input type="password" name="password" autocomplete="current-password" maxlength="256" required></label>'
        retry = f'<p><a href="/setup">{t("重新讀取設定狀態")}</a></p>' if setup and message else ""
        notice = f'<p role="alert">{t(message)}</p>' if message else ""
        return self.page(title, f'<section class="auth"><h1>{t(title)}</h1>{notice}{retry}'
                         f'<form method="post" action="{"/setup" if setup else "/login"}">{fields}'
                         f'<button>{t("設定密碼" if setup else "登入")}</button></form></section>')

    def account_form(self, provider):
        t = self.text
        info = PROVIDERS[provider]
        hint = escape(t(info["hint"]))
        name = escape(info["name"])
        field = info["credential"]
        if not field:
            return self.page("服務資訊", f'<h1>{name}</h1><p>{hint}</p>', "/")
        return self.page("設定帳號", f"""<section class="auth account"><h1>{name}</h1>
<p>{hint}</p><form method="post" action="/account/{provider}">
<label>{escape(t(field))}<input type="password" name="secret" required autocomplete="off"
spellcheck="false" autocapitalize="none" maxlength="16384"></label><button>{t("儲存帳號")}</button></form></section>""", "/")

    def account_row(self, row):
        t = self.text
        provider = row["provider"]
        info = PROVIDERS[provider]
        configured = row["configured"]
        status = STATUS.get(row.get("status"), "等待擷取") if row["enabled"] or not configured else "已停用"
        last = row.get("last_success")
        zone = ZoneInfo(self.prefs["timezone"])
        when = datetime.fromtimestamp(last, zone).strftime("%Y/%m/%d %H:%M %Z") if last else t("尚無紀錄")
        if row["enabled"] and row.get("status") == "ok" and last and time.time() - last >= device_settings.stale_after(self.prefs["interval"]):
            status = "已過期"
        metrics = " · ".join(f'{escape(str(m["label"]))} {escape(str(m["value"]))}' for m in row.get("metrics", []))
        state_action = "disable" if row["enabled"] else "enable"
        state_label = t("停用" if row["enabled"] else "啟用")
        toggle = (f'<form method="post" action="/{state_action}/{provider}"><button class="text">{state_label}</button></form>'
                  if configured and info["credential"] else "")
        edit = ("更新憑證" if configured else "設定憑證") if info["credential"] else "服務資訊"
        error = row.get("error")
        note = t(error if error in ENGLISH else "用量暫時無法取得") if error else ""
        return f"""<article><div><h2>{escape(info["name"])}</h2><b class="metrics">{metrics}</b>
<p>{t(status)} · {t("最後成功")} {escape(when)}</p><p>{escape(note)}</p></div><div class="actions">
<a href="/account/{provider}">{t(edit)}</a>{toggle}
<details><summary>{t("移除")}</summary><form method="post" action="/remove/{provider}"><button class="text danger">{t("確認移除帳號")}</button></form>
</details></div></article>"""

    def dashboard(self, rows, notice=""):
        t = self.text
        present = {r["provider"] for r in rows}
        cards = "".join(self.account_row(r) for r in rows if r["provider"] in PROVIDERS)
        if not cards:
            cards = f'<p>{t("尚未設定帳號。請從下方選擇服務。")}</p>'
        choices = "".join(f'<option value="{k}">{escape(p["name"])}</option>'
                          for k, p in PROVIDERS.items() if k not in present and p["credential"])
        add = (f'<form action="/choose" method="get" class="add"><label>{t("服務")}<select name="provider">{choices}</select></label>'
               f'<button>{t("設定帳號")}</button></form>') if choices else f'<p>{t("可設定的服務均已加入。")}</p>'
        unsupported = "".join(f'<p><strong>{escape(p["name"])}</strong>　{escape(t(p["hint"]))}</p>'
                              for p in PROVIDERS.values() if not p["credential"])
        flash = f'<p class="notice" role="status">{t(notice)}</p>' if notice else ""
        intro = t("用量由 Raspberry Pi 每 {duration}更新。").format(duration=self.minutes(self.prefs["interval"]))
        return self.page("訂閱管理", f"""<h1>{t("訂閱管理")}</h1><p class="intro">{intro}</p>
{flash}<section aria-label="{t("已設定的帳號")}">{cards}</section><section class="extra"><h2>{t("新增服務")}</h2>
{add}{unsupported}</section>""", "/")

    def settings_page(self, saved=False, error=""):
        t = self.text
        def option(value, title, current):
            selected = " selected" if value == current else ""
            return f'<option value="{value}"{selected}>{title}</option>'
        languages = "".join(option(k, label, self.language) for k, label in (("zh-Hant", "繁體中文"), ("en", "English")))
        intervals = "".join(option(i, self.minutes(i), self.prefs["interval"])
                            for i in device_settings.INTERVALS)
        notice = f'<p class="notice" role="status">{t("已儲存本機設定。")}</p>' if saved else ""
        if error:
            notice = f'<p role="alert">{t(error)}</p>'
        return self.page("本機設定", f"""<section class="preferences"><h1>{t("本機設定")}</h1>{notice}
<form method="post" action="/settings"><label>{t("語言")}<select name="language" aria-label="{t("語言")}">{languages}</select></label>
<label>{t("時區")}<input name="timezone" list="time-zones" value="{escape(self.prefs["timezone"])}" maxlength="100"
aria-describedby="zone-help" required></label><small id="zone-help">{t("選擇或輸入 IANA 時區，例如 Asia/Taipei。")}</small>
<datalist id="time-zones">{zone_options()}</datalist>
<label>{t("更新頻率")}<select name="interval" aria-label="{t("更新頻率")}" aria-describedby="interval-help">{intervals}</select></label>
<small id="interval-help">{t("電子紙兩次刷新至少間隔 3 分鐘。")}</small><button>{t("儲存設定")}</button>
</form></section>""", "/settings")


class WebServer(ThreadingHTTPServer):
    def __init__(self, home=HOME, address=("0.0.0.0", 8443), store_factory=Store):
        prepare_home(home)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(home / "tls.crt"), str(home / "tls.key"))
        self.context, self.home, self.store_factory = context, home, store_factory
        self.session, self.expires = "", 0
        self.failures = []
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(8)
        self.hosts = {"localhost", "127.0.0.1", "quota-desk.local", socket.gethostname().removesuffix(".local") + ".local"}
        issue_setup_code(home, display=True)
        super().__init__(address, Handler)

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(8)
        return self.context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), address

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.close_request(request)
            return
        super().process_request(request, address)

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, *_):
        pass  # Do not log request data or exceptions from the credential store.


class Handler(BaseHTTPRequestHandler):
    @property
    def screens(self):
        if not hasattr(self, "_screens"):
            self._screens = Screens(device_settings.load(self.server.home))
        return self._screens

    def log_message(self, *_):
        pass

    def trusted_host(self):
        hosts = self.headers.get_all("Host") or []
        allowed = self.server.hosts | {self.connection.getsockname()[0]}
        return len(hosts) == 1 and hosts[0].lower() in {f"{h}:{self.server.server_port}" for h in allowed}

    def send(self, status, body="", headers=(), mime="text/html; charset=utf-8"):
        errors = {400: "無法處理這個要求。", 403: "未授權的要求。", 404: "找不到這個頁面。",
                  413: "表單資料過大。", 415: "不支援的表單格式。"}
        if not body and status in errors:
            body = self.screens.text(errors[status])
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def redirect(self, path, cookie=None):
        headers = [("Location", path)]
        if cookie is not None:
            age = SESSION_SECONDS if cookie else 0
            headers.append(("Set-Cookie", f"{COOKIE}={cookie}; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age={age}"))
        self.send(303, headers=headers)

    def signed_in(self):
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        item = jar.get(COOKIE)
        return bool(item and item.value.isascii() and self.server.session and self.server.expires > time.monotonic()
                    and hmac.compare_digest(self.server.session, item.value))

    def do_GET(self):
        if not self.trusted_host():
            return self.send(403)
        if self.path == "/web.css":
            return self.send(200, CSS.read_text(), mime="text/css; charset=utf-8")
        if self.path == "/favicon.ico":
            return self.send(204)
        if self.path == "/setup":
            if (self.server.home / "admin-password.json").exists():
                return self.redirect("/")
            issue_setup_code(self.server.home, display=True)
            return self.send(200, self.screens.auth_page(True))
        if not self.signed_in():
            if not (self.server.home / "admin-password.json").exists():
                return self.redirect("/setup")
            return self.send(200, self.screens.auth_page())
        if self.path in ("/settings", "/settings?saved=1"):
            return self.send(200, self.screens.settings_page(saved=self.path.endswith("?saved=1")))
        if self.path == "/" or self.path in NOTICES:
            try:
                rows = self.server.store_factory().list_accounts()
            except (OSError, ValueError, sqlite3.DatabaseError):
                return self.send(503, self.screens.text("暫時無法讀取帳號資料。"))
            return self.send(200, self.screens.dashboard(rows, NOTICES.get(self.path, "")))
        parts = urlsplit(self.path)
        if parts.path == "/choose":
            choices = parse_qs(parts.query).get("provider", [])
            if len(choices) == 1 and choices[0] in PROVIDERS:
                return self.redirect("/account/" + choices[0])
        if self.path.startswith("/account/") and self.path[9:] in PROVIDERS:
            return self.send(200, self.screens.account_form(self.path[9:]))
        return self.send(404)

    def do_POST(self):
        if (not self.trusted_host() or self.headers.get_all("Origin") != ["https://" + self.headers["Host"]]):
            return self.send(403)
        if self.headers.get_content_type() != "application/x-www-form-urlencoded":
            return self.send(415)
        try:
            length = int(self.headers["Content-Length"])
            if not 0 <= length <= 65536 or self.headers.get("Transfer-Encoding"):
                return self.send(413)
            body = self.rfile.read(length).decode()
            data = parse_qs(body, strict_parsing=True, max_num_fields=3) if body else {}
            if any(len(v) != 1 for v in data.values()):
                return self.send(400)
            form = {k: v[0] for k, v in data.items()}
        except (ValueError, TypeError):
            return self.send(400)
        with self.server.lock:
            if self.path in ("/login", "/setup"):
                return self.authenticate(form)
            if not self.signed_in():
                return self.send(403)
            if self.path == "/logout":
                self.server.session = ""
                return self.redirect("/", "")
            if self.path == "/settings":
                try:
                    device_settings.save(self.server.home, form["timezone"], form["language"], int(form["interval"]))
                except (ValueError, KeyError):
                    return self.send(400, self.screens.settings_page(error="請確認時區、語言和更新頻率皆為有效選項。"))
                except OSError:
                    return self.send(503, self.screens.settings_page(error="目前無法儲存本機設定。"))
                return self.redirect("/settings?saved=1")
            route, _, provider = self.path[1:].partition("/")
            if provider not in PROVIDERS or (route != "remove" and not PROVIDERS[provider]["credential"]):
                return self.send(400)
            try:
                store = self.server.store_factory()
                if route == "account":
                    secret = form.get("secret", "")
                    if not 1 <= len(secret) <= 16384 or not all(33 <= ord(c) <= 126 for c in secret):
                        return self.send(400, self.screens.text("憑證必須是單行且不含空白。"))
                    store.set_account(provider, secret)
                    return self.redirect("/saved")
                if route in ("enable", "disable"):
                    store.set_enabled(provider, route == "enable")
                    return self.redirect("/updated")
                if route == "remove":
                    store.remove_account(provider)
                    return self.redirect("/removed")
            except (ValueError, KeyError):
                return self.send(400, self.screens.text("無法套用設定，請確認帳號與憑證。"))
            except (OSError, sqlite3.DatabaseError):
                return self.send(503, self.screens.text("暫時無法儲存帳號資料。"))
            return self.send(404)

    def authenticate(self, form):
        setup = self.path == "/setup"
        now = time.monotonic()
        self.server.failures = [stamp for stamp in self.server.failures if now - stamp < 60]
        if len(self.server.failures) >= 5:
            return self.send(429, self.screens.auth_page(setup, "嘗試次數過多，請在一分鐘後再試。"))
        password = form.get("password", "")
        if setup:
            if not 12 <= len(password) <= 256 or password != form.get("confirm"):
                return self.send(400, self.screens.auth_page(True, "請輸入相同的兩次管理密碼，長度為 12 至 256 字元。"))
            valid = redeem(self.server.home, form.get("code", ""), password)
        else:
            auth = read_json(self.server.home / "admin-password.json")
            valid = auth and 1 <= len(password) <= 256 and hmac.compare_digest(password_hash(password, auth["salt"]), auth["hash"])
        if not valid:
            self.server.failures.append(now)
            return self.send(401, self.screens.auth_page(setup, "憑證不正確或設定碼已失效。"))
        self.server.failures.clear()
        self.server.session = secrets.token_urlsafe(32)
        self.server.expires = now + SESSION_SECONDS
        self.redirect("/", self.server.session)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve", "setup-code", "pair-display", "fingerprint"))
    command = parser.parse_args().command
    if command == "serve":
        with WebServer() as server:
            server.serve_forever()
    elif command == "pair-display":
        issue_setup_code(HOME, display=True)
    else:
        print("HTTPS SHA-256:", fingerprint(HOME / "tls.crt"))
        if command == "setup-code":
            try:
                print("一次性設定碼（10 分鐘有效）：", issue_setup_code(HOME))
            except ValueError as err:
                parser.exit(1, str(err) + "\n")


if __name__ == "__main__":
    main()
