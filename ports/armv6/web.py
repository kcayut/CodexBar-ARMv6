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
from html import escape
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

if __package__:
    from .providers import PROVIDERS
    from .state import Store
else:
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


def page(title, content, signed_in=False):
    logout = '<form method="post" action="/logout"><button class="text">登出</button></form>' if signed_in else ""
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} · Quota Desk</title>
<link rel="stylesheet" href="/web.css"></head><body><header><b lang="en">Quota Desk</b>{logout}</header>
<main>{content}</main></body></html>"""


def auth_page(setup=False, message=""):
    title = "設定管理密碼" if setup else "管理員登入"
    fields = """<p>請開啟瀏覽器的憑證資訊，核對 SHA-256 指紋的前 32 個十六進位字元與電子紙相同，
再輸入電子紙上的 8 位數設定碼。也可透過已配對的 SSH 核對完整指紋並取得設定碼。</p>
<label>一次性設定碼<input name="code" autocomplete="one-time-code" maxlength="128" required></label>
<label>新管理密碼（至少 12 字元）<input type="password" name="password" autocomplete="new-password"
minlength="12" maxlength="256" required></label>
<label>再次輸入密碼<input type="password" name="confirm" autocomplete="new-password" maxlength="256" required></label>""" if setup else """
<label>管理密碼<input type="password" name="password" autocomplete="current-password" maxlength="256" required></label>"""
    action = "/setup" if setup else "/login"
    retry = '<p><a href="/setup">重新讀取設定狀態</a></p>' if setup and message else ""
    return page(title, f'<section class="auth"><h1>{title}</h1><p role="alert">{escape(message)}</p>{retry}'
                f'<form method="post" action="{action}">{fields}<button>{"設定密碼" if setup else "登入"}</button></form></section>')


def account_form(provider):
    info = PROVIDERS[provider]
    field = info["credential"]
    link = '<a href="/">返回訂閱管理</a>'
    if not field:
        return page("服務資訊", f'{link}<h1>{escape(info["name"])}</h1><p>{escape(info["hint"])}</p>', True)
    return page("設定帳號", f"""{link}<section class="auth account"><h1>{escape(info["name"])}</h1>
<p>{escape(info["hint"])}</p><form method="post" action="/account/{provider}">
<label>{escape(field)}<input type="password" name="secret" required autocomplete="off"
spellcheck="false" autocapitalize="none" maxlength="16384"></label><button>儲存帳號</button></form></section>""", True)


def account_row(row):
    provider = row["provider"]
    info = PROVIDERS[provider]
    configured = row["configured"]
    status = STATUS.get(row.get("status"), "等待擷取") if row["enabled"] or not configured else "已停用"
    last = row.get("last_success")
    when = datetime.fromtimestamp(last).astimezone().strftime("%Y/%m/%d %H:%M") if last else "尚無紀錄"
    if row["enabled"] and row.get("status") == "ok" and last and time.time() - last > 900:
        status = "已過期"
    metrics = " · ".join(f'{escape(str(m["label"]))} {escape(str(m["value"]))}' for m in row.get("metrics", []))
    state_action = "disable" if row["enabled"] else "enable"
    state_label = "停用" if row["enabled"] else "啟用"
    toggle = (f'<form method="post" action="/{state_action}/{provider}"><button class="text">{state_label}</button></form>'
              if configured and info["credential"] else "")
    edit = ("更新憑證" if configured else "設定憑證") if info["credential"] else "服務資訊"
    note = escape(row.get("error") or "")
    return f"""<article><div><h2>{escape(info["name"])}</h2><b class="metrics">{metrics}</b>
<p>{status}　最後成功：{when}</p><p>{note}</p></div><div class="actions">
<a href="/account/{provider}">{edit}</a>{toggle}
<details><summary>移除</summary><form method="post" action="/remove/{provider}"><button class="text danger">確認移除帳號</button></form>
</details></div></article>"""


def dashboard(rows, notice=""):
    present = {r["provider"] for r in rows}
    cards = "".join(account_row(r) for r in rows if r["provider"] in PROVIDERS)
    if not cards:
        cards = "<p>尚未設定帳號。請從下方選擇服務。</p>"
    choices = "".join(f'<option value="{k}">{escape(p["name"])}</option>'
                      for k, p in PROVIDERS.items() if k not in present and p["credential"])
    add = (f'<form action="/choose" method="get" class="add"><label>服務<select name="provider">{choices}</select></label>'
           '<button>設定帳號</button></form>') if choices else "<p>可設定的服務均已加入。</p>"
    unsupported = "".join(f'<p><strong>{escape(p["name"])}</strong>　{escape(p["hint"])}</p>'
                          for p in PROVIDERS.values() if not p["credential"])
    flash = f'<p class="notice" role="status">{notice}</p>' if notice else ""
    return page("訂閱管理", f"""<h1>訂閱管理</h1><p class="intro">用量由 Raspberry Pi 每 5 分鐘更新。</p>
{flash}<section aria-label="已設定的帳號">{cards}</section><section class="extra"><h2>新增服務</h2>
{add}{unsupported}</section>""", True)


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
    def log_message(self, *_):
        pass

    def trusted_host(self):
        hosts = self.headers.get_all("Host") or []
        allowed = self.server.hosts | {self.connection.getsockname()[0]}
        return len(hosts) == 1 and hosts[0].lower() in {f"{h}:{self.server.server_port}" for h in allowed}

    def send(self, status, body="", headers=(), mime="text/html; charset=utf-8"):
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
            return self.send(200, auth_page(True))
        if not self.signed_in():
            if not (self.server.home / "admin-password.json").exists():
                return self.redirect("/setup")
            return self.send(200, auth_page())
        if self.path == "/" or self.path in NOTICES:
            try:
                rows = self.server.store_factory().list_accounts()
            except (OSError, ValueError, sqlite3.DatabaseError):
                return self.send(503, "暫時無法讀取帳號資料。")
            return self.send(200, dashboard(rows, NOTICES.get(self.path, "")))
        parts = urlsplit(self.path)
        if parts.path == "/choose":
            choices = parse_qs(parts.query).get("provider", [])
            if len(choices) == 1 and choices[0] in PROVIDERS:
                return self.redirect("/account/" + choices[0])
        if self.path.startswith("/account/") and self.path[9:] in PROVIDERS:
            return self.send(200, account_form(self.path[9:]))
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
            route, _, provider = self.path[1:].partition("/")
            if provider not in PROVIDERS or (route != "remove" and not PROVIDERS[provider]["credential"]):
                return self.send(400)
            try:
                store = self.server.store_factory()
                if route == "account":
                    secret = form.get("secret", "")
                    if not 1 <= len(secret) <= 16384 or not all(33 <= ord(c) <= 126 for c in secret):
                        return self.send(400, "憑證必須是單行且不含空白。")
                    store.set_account(provider, secret)
                    return self.redirect("/saved")
                if route in ("enable", "disable"):
                    store.set_enabled(provider, route == "enable")
                    return self.redirect("/updated")
                if route == "remove":
                    store.remove_account(provider)
                    return self.redirect("/removed")
            except (ValueError, KeyError):
                return self.send(400, "無法套用設定，請確認帳號與憑證。")
            except (OSError, sqlite3.DatabaseError):
                return self.send(503, "暫時無法儲存帳號資料。")
            return self.send(404)

    def authenticate(self, form):
        setup = self.path == "/setup"
        now = time.monotonic()
        self.server.failures = [stamp for stamp in self.server.failures if now - stamp < 60]
        if len(self.server.failures) >= 5:
            return self.send(429, auth_page(setup, "嘗試次數過多，請在一分鐘後再試。"))
        password = form.get("password", "")
        if setup:
            if not 12 <= len(password) <= 256 or password != form.get("confirm"):
                return self.send(400, auth_page(True, "請輸入相同的兩次管理密碼，長度為 12 至 256 字元。"))
            valid = redeem(self.server.home, form.get("code", ""), password)
        else:
            auth = read_json(self.server.home / "admin-password.json")
            valid = auth and 1 <= len(password) <= 256 and hmac.compare_digest(password_hash(password, auth["salt"]), auth["hash"])
        if not valid:
            self.server.failures.append(now)
            return self.send(401, auth_page(setup, "憑證不正確或設定碼已失效。"))
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
