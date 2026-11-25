#!/bin/sh
set -eu

src=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
if [ "$(id -u)" != 0 ]; then
    exec sudo /bin/sh "$src/install_native.sh"
fi
case "$(tr -d '\000' < /proc/device-tree/model)" in
    'Raspberry Pi Zero '*) ;;
    *) echo '需要在 Raspberry Pi Zero 系列上執行。' >&2; exit 1 ;;
esac
account=${SUDO_USER:-desk}
case "$account" in
    ''|root|-*|*[!a-zA-Z0-9_-]*) echo '請指定一般登入帳號（SUDO_USER）。' >&2; exit 1 ;;
esac
account_home=$(getent passwd "$account" | cut -d: -f6)
case "$account_home" in
    /home/*) ;;
    *) echo '找不到此登入帳號在 /home 下的家目錄。' >&2; exit 1 ;;
esac
case "$account_home" in
    *[!a-zA-Z0-9_./-]*) echo '家目錄含 systemd 範本不支援的字元。' >&2; exit 1 ;;
esac
if [ "$(id -u "$account")" -eq 0 ] || [ ! -d "$account_home" ]; then
    echo '請指定已有家目錄的一般登入帳號。' >&2
    exit 1
fi
umask 077
exec 9>/run/lock/quota-desk-native-install.lock
flock -x 9

dest=/usr/local/lib/quota-desk-native
data=$account_home/.local/share/quota-desk
display=/usr/local/lib/quota-desk-display/display.py
display_installed=no
if [ -f "$display" ] && systemctl cat quota-desk-display.service >/dev/null 2>&1; then
    display_installed=yes
    if [ "$(systemctl show -p User --value quota-desk-display.service)" != "$account" ]; then
        echo '電子紙服務使用另一個帳號。請以該帳號安裝，讓配對碼與摘要讀取同一個目錄。' >&2
        exit 1
    fi
fi
staging=$(mktemp -d)
trap 'rm -rf -- "$staging"' EXIT
for name in web.py web.css state.py providers.py receive.py display.py native_tls.py quota-desk-web.service quota-desk-collect.service quota-desk-collect.timer; do
    install -m 644 "$src/$name" "$staging/$name"
done
/usr/bin/python3 - "$staging" <<'PY'
from pathlib import Path
import sys
for path in Path(sys.argv[1]).glob("*.py"):
    compile(path.read_bytes(), path.name, "exec")
PY

# Create private state as its eventual owner.
runuser -u "$account" -- /usr/bin/python3 - "$data" < "$staging/native_tls.py"
install -d -m 755 "$dest"
for name in web.py web.css state.py providers.py receive.py; do
    install -m 644 "$staging/$name" "$dest/.$name.new"
    mv -f "$dest/.$name.new" "$dest/$name"
done
for name in quota-desk-web.service quota-desk-collect.service quota-desk-collect.timer; do
    sed -e "s|@ACCOUNT@|$account|g" -e "s|@HOME@|$account_home|g" "$staging/$name" > "$staging/$name.ready"
    install -m 644 "$staging/$name.ready" "/etc/systemd/system/.$name.new"
    mv -f "/etc/systemd/system/.$name.new" "/etc/systemd/system/$name"
done
systemctl daemon-reload
systemctl enable quota-desk-web.service quota-desk-collect.timer
systemctl restart quota-desk-web.service quota-desk-collect.timer

# Check the local HTTPS endpoint with the generated certificate, including its localhost SAN.
i=0
until curl --noproxy '*' --cacert "$data/tls.crt" --fail --silent --max-time 4 --output /dev/null https://localhost:8443/; do
    i=$((i + 1))
    if [ "$i" -ge 15 ]; then
        echo 'HTTPS 尚未就緒，請檢查 systemctl status quota-desk-web.service。' >&2
        exit 1
    fi
    sleep 1
done
systemctl is-active --quiet quota-desk-web.service
systemctl is-active --quiet quota-desk-collect.timer
if [ "$display_installed" = yes ] && ! cmp -s "$staging/display.py" "$display"; then
    install -m 644 "$staging/display.py" "$display.new"
    mv -f "$display.new" "$display"
    systemctl restart quota-desk-display.service
fi
printf '管理頁：https://%s.local:8443/\n' "$(hostname -s)"
openssl x509 -in "$data/tls.crt" -noout -sha256 -fingerprint
