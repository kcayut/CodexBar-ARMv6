"""Pi 上的帳號資料與可調排程。電子紙只接收沒有憑證的摘要。"""
from contextlib import closing, contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import time

import device_settings
import providers
import receive

PANEL = {"codex": ("codex", "oauth"), "antigravity": ("gemini", "antigravity"),
         "deepseek": ("deepseek", "api")}


class Store:
    def __init__(self, home=None):
        self.home = Path(home or os.environ.get("QUOTA_DESK_HOME") or Path.home() / ".local/share/quota-desk")
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = self.home / "accounts.sqlite3"
        os.close(os.open(self.db, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600))
        self.collect_lock = self.home / "collect.lock"
        os.close(os.open(self.collect_lock, os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW, 0o600))
        with self.connection() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                    db.execute("""CREATE TABLE accounts (
                        provider TEXT PRIMARY KEY, secret TEXT, revision TEXT NOT NULL, enabled INTEGER NOT NULL,
                        last_success INTEGER, metrics TEXT NOT NULL DEFAULT '[]',
                        status TEXT NOT NULL DEFAULT 'unavailable', error TEXT NOT NULL DEFAULT '')""")
                    self._import_display(db)
                    db.execute("PRAGMA user_version=1")

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.db, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA secure_delete=ON")
            yield db

    def _import_display(self, db):
        try:
            with (self.home / "summary.json").open("rb") as stream:
                rows = receive.decode(stream.read(receive.LIMIT + 1))["services"]
        except (FileNotFoundError, ValueError, RecursionError):
            return
        for provider, (ident, source) in PANEL.items():
            row = next((r for r in rows if (r["id"], r["source"]) == (ident, source)), None)
            if row and row["last_success"]:
                db.execute("""INSERT INTO accounts(provider, revision, enabled, last_success, metrics, status)
                              VALUES(?, '', 0, ?, ?, 'stale')""",
                           (provider, row["last_success"], json.dumps(row["metrics"])))

    def list_accounts(self):
        maximum_age = device_settings.stale_after(device_settings.load(self.home)["interval"])
        now = time.time()
        with self.connection() as db:
            result = []
            for record in db.execute("""SELECT provider, secret IS NOT NULL AS configured, enabled, last_success,
                                               metrics, status, error FROM accounts ORDER BY provider"""):
                row = dict(record)
                row["metrics"] = json.loads(row["metrics"])
                row["configured"], row["enabled"] = bool(row["configured"]), bool(row["enabled"])
                if row["status"] == "ok" and not -60 <= now - row["last_success"] < maximum_age:
                    row["status"] = "stale"
                result.append(row)
            return result

    def set_account(self, provider, secret):
        if provider not in providers.PROVIDERS or not providers.PROVIDERS[provider]["credential"]:
            raise ValueError("此服務沒有可用的憑證設定")
        if not isinstance(secret, str) or not 1 <= len(secret) <= 16384 or not all(33 <= ord(c) <= 126 for c in secret):
            raise ValueError("憑證格式不符")
        # A replacement might be a different account. Its first failed lookup must never show the former balance.
        with self.connection() as db:
            db.execute("""INSERT OR REPLACE INTO accounts(provider,secret,revision,enabled) VALUES(?,?,?,1)""",
                       (provider, secret, secrets.token_hex(16)))

    def set_enabled(self, provider, enabled):
        if type(enabled) is not bool:
            raise ValueError("狀態格式不符")
        with self.connection() as db:
            changed = db.execute("""UPDATE accounts SET enabled=?, revision=?, error='',
                                    status=CASE WHEN last_success IS NULL THEN 'unavailable' ELSE 'stale' END
                                    WHERE provider=? AND secret IS NOT NULL""",
                                 (enabled, secrets.token_hex(16), provider))
            if not changed.rowcount:
                raise ValueError("服務尚未設定")

    def remove_account(self, provider):
        with self.connection() as db:
            db.execute("DELETE FROM accounts WHERE provider=?", (provider,))

    def collect(self, scheduled=False):
        with self.collect_lock.open("rb") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            clock_file = self.home / "last-attempt.json"
            boot = receive.boot_id()
            if scheduled:
                interval = device_settings.load(self.home)["interval"] * 60
                try:
                    clock = json.loads(clock_file.read_bytes())
                    if boot and clock["boot"] == boot:
                        elapsed = time.monotonic() - clock["monotonic"]
                    elif not boot and clock["boot"] is None:
                        elapsed = time.time() - clock["wall"]
                    else:
                        elapsed = interval
                except (FileNotFoundError, ValueError, TypeError, KeyError):
                    elapsed = interval
                if 0 <= elapsed < interval:
                    remaining = interval - elapsed
                    if remaining > 5:
                        self.publish(only_if_changed=True)
                        return False
                    # A slightly early minute tick can wait its last few seconds instead of losing a whole minute.
                    time.sleep(remaining)
            with self.connection() as db:
                accounts = db.execute("SELECT provider, secret, revision FROM accounts WHERE enabled=1").fetchall()
            if accounts:
                clock = {"boot": boot, "monotonic": time.monotonic(), "wall": time.time()}
                receive.atomic_write(clock_file, json.dumps(clock).encode())
                self._query(accounts)
            self.publish(only_if_changed=not accounts)
            return bool(accounts)

    def _query(self, accounts):
        for provider, secret, revision in accounts:
            try:
                metrics = providers.fetch(provider, secret)
                if not metrics:
                    raise ValueError("empty")
                stamp = int(time.time())
                if provider in PANEL:
                    ident, source = PANEL[provider]
                    item = dict(id=ident, source=source, status="ok", metrics=metrics, last_success=stamp)
                    receive.decode(json.dumps(dict(version=1, updated_at=stamp, services=[item])).encode())
                with self.connection() as db:
                    db.execute("""UPDATE accounts SET last_success=?, metrics=?, status='ok', error=''
                                  WHERE provider=? AND revision=? AND enabled=1""",
                               (stamp, json.dumps(metrics), provider, revision))
            except (providers.ProviderError, OSError, ValueError, TypeError, ArithmeticError) as exc:
                message = str(exc) if isinstance(exc, providers.ProviderError) else "用量暫時無法取得"
                with self.connection() as db:
                    db.execute("""UPDATE accounts SET error=?,
                                status=CASE WHEN last_success IS NULL THEN 'unavailable' ELSE 'stale' END
                                WHERE provider=? AND revision=? AND enabled=1""", (message, provider, revision))

    def publish(self, only_if_changed=False):
        accounts = {row["provider"]: row for row in self.list_accounts()}
        now = int(time.time())
        rows = []
        for provider, (ident, source) in PANEL.items():
            row = accounts.get(provider, {})
            rows.append(dict(id=ident, source=source, last_success=row.get("last_success"),
                             metrics=row.get("metrics", []), status=row.get("status", "unavailable")))
        path = self.home / "summary.json"
        if only_if_changed:
            try:
                with path.open("rb") as stream:
                    if receive.decode(stream.read(receive.LIMIT + 1))["services"] == rows:
                        return
            except FileNotFoundError:
                if not any(row["last_success"] for row in rows):
                    return
            except (ValueError, RecursionError):
                pass
        raw = json.dumps(dict(version=1, updated_at=now, services=rows), separators=(",", ":")).encode()
        receive.save(raw, path, receive.boot_id(), time.monotonic())


if __name__ == "__main__":
    if sys.argv[1:] != ["collect"]:
        sys.exit("用法：state.py collect")
    Store().collect(scheduled=os.environ.get("TRIGGER_UNIT") == "quota-desk-collect.timer")
