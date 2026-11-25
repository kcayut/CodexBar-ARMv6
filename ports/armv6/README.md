# Quota Desk · Raspberry Pi Zero W

在區域網路用瀏覽器連到 Pi，管理 AI 憑證及查看額度。Pi 每五分鐘自行查詢，Waveshare 2.13 吋黑白電子紙同時顯示 Codex、Gemini 與 DeepSeek。Mac 或手機在設定完可以關機。

本目錄是 CodexBar v0.84.0 的 ARMv6 移植分支。用 Raspberry Pi OS Lite 32 位元內建的 Python、SQLite、OpenSSL 和 systemd 執行服務。電子紙另用 Pillow 及 Waveshare SPI 驅動。上游的授權見 [MIT License](../../LICENSE)。

## 首次登入

1. 將 Pi 接到可信任的區域網路。瀏覽器開啟 `https://<Pi 主機名>.local:8443/`，或使用 Pi 的區網 IP。電子紙的首次設定畫面會顯示網址。
2. Pi 會產生自己的 HTTPS 憑證。第一次連線，先在瀏覽器的憑證詳細資料比對 SHA-256 開頭 32 個十六進位字元，與電子紙相符再繼續。可另用已配對的金鑰 SSH 核對完整指紋。
3. 輸入電子紙上的八位數配對碼，設定至少 12 個字元的管理密碼。配對碼十分鐘有效，過期可重開設定頁，待電子紙完成刷新。平日只需管理密碼。單一管理者登入新瀏覽器後，原瀏覽器需要重新登入。
4. 在「訂閱管理」選擇服務，輸入該服務授權給你的金鑰或存取權杖。下一輪查詢完成後，頁面顯示實際讀值與最後成功時間。可更新憑證、停用或移除帳號，每個服務可設定一組帳號。

| 服務 | 額度來源與限制 |
|---|---|
| Codex | 使用已授權的 Codex 存取權杖查詢帳戶相容介面。按實際回報顯示 5 小時與每週剩餘百分比，缺少的時窗顯示 `--`。目前到期需手動更換。|
| Gemini / Antigravity | 使用有權查詢該 Antigravity 專案的 Google 存取權杖，實驗性查詢 Gemini 配額。只有明確回報的配額時窗能顯示數字；授權不足會顯示失敗。到期需手動更換。|
| DeepSeek | 使用 DeepSeek Platform 官方 API 金鑰，顯示 API 帳戶各幣別的總餘額。|
| OpenRouter | 使用普通 API 金鑰，在管理頁顯示該金鑰的可用預算（美元）或未設上限。|

目前的訂閱授權由使用者自行提供，沒有自動續期或代為登入第三方帳戶。請在 Pi 管理頁輸入憑證，避免在聊天、終端指令、網址或 Git 中輸入。Pi 會將可重複查詢所需的金鑰存在帳號專用的 SQLite 檔案，目錄權限 0700、檔案 0600；持有 SD 卡或 Pi 管理權限的人仍可讀取。服務只連到 [providers.py](providers.py) 指定的來源。要支援另一家平台，可在此檔新增其授權說明與額度讀取器，再加入對應的回應測試。

## 安裝到 Pi

前提為已設定 SSH 金鑰、連網並校時的 Raspberry Pi OS Lite 32 位元。以下在 Pi 上、以具有 sudo 權限的一般使用者執行。已經有 Quota Desk 電子紙服務可跳過第一行。請先核對黑白面板版本，此實作的實機驗證型號是 **Waveshare 2.13inch e-Paper HAT 黑白 V4**。

```sh
sh install.sh bw-v4
sh install_native.sh
```

首次啟用 SPI 後按安裝訊息重新開機。這台 Pi 如已設定 SSH 別名 `quota-pi`，在 Mac 可用內建的 SSH 查完整憑證指紋。

```sh
ssh quota-pi 'python3 /usr/local/lib/quota-desk-native/web.py fingerprint'
```

無法看面板時，可用同一指令將 `fingerprint` 改為 `setup-code`，取得十分鐘有效的一次性碼。已有管理密碼後，設定碼不能用來重設密碼。自簽憑證預設一年，重新執行安裝器可在即將到期時續發；憑證更換後再次核對指紋。服務用於區域網路，遠端跨網可經自己的 VPN。

## 服務與驗證

| Pi 的 systemd 單元 | 作用 |
|---|---|
| `quota-desk-web.service` | 開機提供 HTTPS 管理頁，監聽 TCP 8443。|
| `quota-desk-collect.timer` | 開機後排程，之後每五分鐘觸發 `quota-desk-collect.service`。收集程式執行完會退出。|
| `quota-desk-display.service` | 開機驅動實體電子紙，兩次全刷至少相隔三分鐘。|

```sh
systemctl is-active quota-desk-web quota-desk-collect.timer quota-desk-display
systemctl list-timers quota-desk-collect
sudo systemctl start quota-desk-collect
systemctl show quota-desk-collect -p Result
python3 -B -m unittest discover -s .
```

收集與管理頁位於 `/usr/local/lib/quota-desk-native`。用量、設定、TLS 金鑰位於登入帳號的 `~/.local/share/quota-desk/`。畫面沿用最後一次成功讀值，查詢失敗或中斷會顯示過期。更換帳號時會清除前一帳號的數字。初次從舊版升級時，會保留 SD 卡上的舊摘要並標記過期，直到 Pi 用你新設定的憑證查到新值。

實際驗收可依序確認：從瀏覽器登入、加入 DeepSeek、比對管理頁及電子紙的幣別／餘額／時間，然後關閉管理頁並重啟 Pi。新一次開機的計時器與「最後成功」時間前進，代表 Pi 能獨立更新。
