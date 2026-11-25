#!/bin/sh
set -eu

case "${1:-}" in
    bw-v1) module=epd2in13 ;;
    bw-v2) module=epd2in13_V2 ;;
    bw-v3) module=epd2in13_V3 ;;
    bw-v4) module=epd2in13_V4 ;;
    *) echo '用法：sh pi/install.sh bw-v1|bw-v2|bw-v3|bw-v4（請先核對黑白面板版本）' >&2; exit 2 ;;
esac
if [ "$(id -u)" -eq 0 ]; then
    echo '請用接收摘要的 SSH 帳號執行；腳本在安裝系統檔案時會使用 sudo。' >&2
    exit 1
fi
case "$(tr -d '\000' < /proc/device-tree/model)" in
    "Raspberry Pi Zero "*) ;;
    *) echo '此安裝程序適用 Raspberry Pi Zero 系列。' >&2; exit 1 ;;
esac
if [ -d /etc/pwnagotchi ]; then
    echo '目前開機的系統是 Pwnagotchi，請改用專用的 Raspberry Pi OS Lite 卡。' >&2
    exit 1
fi

account=$(id -un)
case "$account" in ''|*[!a-zA-Z0-9_-]*) echo 'SSH 帳號名稱格式不支援。' >&2; exit 1 ;; esac
code_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
tmp_dir=$(mktemp -d)
trap 'rm -rf -- "$tmp_dir"' EXIT
base='https://raw.githubusercontent.com/waveshareteam/e-Paper/d28b3c4cfc78eef1c6cd6e6e3013654e449d2864/RaspberryPi_JetsonNano/python/lib/waveshare_epd'
if [ -n "${2:-}" ]; then
    offline_dir=$(CDPATH='' cd -- "$2" && pwd)
    (cd "$offline_dir" && sha256sum --check --strict SHA256SUMS)
    sudo dpkg --install "$offline_dir"/*.deb
    for file in __init__.py epdconfig.py "$module.py"; do
        cp -- "$offline_dir/waveshare_epd/$file" "$tmp_dir/$file"
    done
else
    sudo apt-get update
    sudo apt-get install -y ca-certificates curl python3-pil python3-spidev python3-gpiozero python3-lgpio fonts-wqy-microhei
    for file in __init__.py epdconfig.py "$module.py"; do
        curl --proto '=https' -fsSL --retry 2 "$base/$file" -o "$tmp_dir/$file"
    done
fi

dest=/usr/local/lib/quota-desk-display
sudo install -d -m 755 "$dest/waveshare_epd"
sudo install -m 644 "$tmp_dir"/*.py "$dest/waveshare_epd/"
sudo install -m 644 "$code_dir/device_settings.py" "$code_dir/receive.py" "$code_dir/display.py" "$dest/"
install -d -m 700 "$HOME/.local/share/quota-desk"
sudo raspi-config nonint do_spi 0

cat > "$tmp_dir/quota-desk-display.service" <<EOF
[Unit]
Description=Quota Desk e-paper display
After=local-fs.target
RequiresMountsFor=$HOME

[Service]
User=$account
SupplementaryGroups=spi gpio
RuntimeDirectory=quota-desk-display
WorkingDirectory=/run/quota-desk-display
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 $dest/display.py --panel $1
Restart=on-failure
RestartSec=180s
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
EOF

sudo install -m 644 "$tmp_dir/quota-desk-display.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable quota-desk-display.service
if [ -c /dev/spidev0.0 ]; then
    sudo systemctl restart quota-desk-display.service
    echo '已啟動電子紙服務。請核對實體畫面。'
else
    echo '已啟用 SPI 與開機服務；請重新啟動 Pi 後核對實體畫面。'
fi
