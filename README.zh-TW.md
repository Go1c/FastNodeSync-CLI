# FastNodeSync CLI

[简体中文](README.zh-CN.md) | [English](README.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [繁體中文](README.zh-TW.md)

Obsidian 筆記雙向、近即時同步的命令列用戶端，搭配 [Fast Note Sync Service](https://github.com/haierkeys/fast-note-sync-service) 使用。適合無圖形介面的 Linux 伺服器（如 OpenClaw），同步能力與 Obsidian 桌面／行動版外掛相近。

## 功能

- **雙向即時同步**：本機變更推送到伺服器，遠端（Obsidian 等用戶端）變更拉取到本機
- **完整內容**：`.md` 筆記、附件（圖片、HTML、Canvas 等）、`.obsidian/` 設定
- **斷線自動重連**：指數退避；恢復後以增量補齊
- **防迴授**：從伺服器寫入的檔案不會立刻再次上傳
- **增量同步**：依 `lastSyncTime` 只同步變更部分

## 專案結構

```
FastNodeSync-CLI/
├── fns_cli/               # Python 套件
├── tests/                 # 冒煙測試（unittest）
├── .github/workflows/     # GitHub Actions CI
├── config.yaml            # 設定範例
└── requirements.txt       # 依賴套件
```

## 開發與 CI

本機執行冒煙測試（僅標準函式庫）：

```bash
# 於專案根目錄
export PYTHONPATH=.   # Windows: set PYTHONPATH=.
python -m unittest discover -s tests -v
```

推送到 `main` 或開啟 PR 時，GitHub Actions 會安裝依賴、`compileall`、`fns_cli.main --help`、執行 unittest。

## 部署

### 1. 環境需求

- Python 3.10+

### 2. 安裝依賴

```bash
pip install -r requirements.txt
```

### 3. 設定

編輯 `config.yaml`：

```yaml
server:
  api: "https://your-server-address"   # Fast Note Sync Service 位址
  token: "your_api_token"              # 從管理後台取得的 API Token
  vault: "notes"                       # Vault 名稱，須與 Obsidian 外掛一致

sync:
  watch_path: "./vault"                # 本機同步目錄（相對或絕對路徑）
  sync_notes: true
  sync_files: true
  sync_config: true
  exclude_patterns:
    - ".git/**"
    - ".trash/**"
    - "*.tmp"
  file_chunk_size: 524288

client:
  reconnect_max_retries: 15
  reconnect_base_delay: 3
  heartbeat_interval: 30

logging:
  level: "INFO"
  file: ""
```

**取得 Token**

1. 瀏覽器開啟 Fast Note Sync Service 管理介面（如 `https://your-server-address`）
2. 登入帳號
3. 點選 **「Copy API Config」**
4. 從 JSON 複製 `api`、`apiToken`、`vault` 填入 `config.yaml`

亦可透過環境變數傳入（設定檔為空時補齊；詳見 `config.py`）：

```bash
export FNS_API="https://your-server-address"
export FNS_TOKEN="your_api_token"
```

### 4. 執行

```bash
python -m fns_cli.main run -c config.yaml
```

#### 臨時後台（不建議用於正式環境）

```bash
nohup python -m fns_cli.main run -c config.yaml > fns.log 2>&1 &
screen -dmS fns python -m fns_cli.main run -c config.yaml
```

---

## 常駐服務與開機自動啟動（systemd，建議）

在 Linux 伺服器上，使用 **systemd** 可同時達成：**當機自動重啟**、**開機自動啟動**、**統一日誌（journalctl）**。

假設：

- 專案目錄：`/opt/FastNodeSync-CLI`
- 設定檔：`/opt/FastNodeSync-CLI/config.yaml`
- 執行使用者：`your_user`（勿使用 root）
- Python：`/usr/bin/python3`（以 `which python3` 為準）

建立 unit 檔：

```bash
sudo nano /etc/systemd/system/fns-cli.service
```

範例內容：

```ini
[Unit]
Description=FastNodeSync CLI - Obsidian vault sync
Documentation=https://github.com/Go1c/FastNodeSync-CLI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=your_user
Group=your_user
WorkingDirectory=/opt/FastNodeSync-CLI
Environment=PYTHONUNBUFFERED=1
# 選用：從獨立檔案載入環境變數（chmod 600）
# EnvironmentFile=/opt/FastNodeSync-CLI/.env
ExecStart=/usr/bin/python3 -m fns_cli.main run -c /opt/FastNodeSync-CLI/config.yaml
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

啟用並啟動：

```bash
sudo systemctl daemon-reload
sudo systemctl enable fns-cli    # 開機自啟
sudo systemctl start fns-cli     # 立即啟動
sudo systemctl status fns-cli    # 查看狀態
```

常用指令：

```bash
sudo systemctl stop fns-cli
sudo systemctl restart fns-cli
journalctl -u fns-cli -f         # 即時日誌
journalctl -u fns-cli --since today
```

**說明：**

- `enable` 會在系統重開機後自動啟動服務；`After=network-online.target` 可減少網路尚未就緒就連線失敗的情況。
- 請確認 `your_user` 對 `watch_path`（vault 目錄）有讀寫權限。
- 上游伺服器部署可參考 [fast-note-sync-service](https://github.com/haierkeys/fast-note-sync-service) 文件（Docker、一鍵腳本等）。

---

## CLI 指令

| 指令 | 說明 |
|------|------|
| `run`    | 持續執行：初始同步 + 監看本機變更 + 接收遠端變更 |
| `sync`   | 全量雙向同步一次後結束 |
| `pull`   | 僅拉取遠端到本機後結束 |
| `push`   | 推送本機檔案到遠端後結束 |
| `status` | 顯示設定與同步狀態 |

所有指令皆支援 `-c` / `--config` 指定設定檔，預設為 `config.yaml`。

```bash
python -m fns_cli.main run -c config.yaml
python -m fns_cli.main sync -c config.yaml
python -m fns_cli.main pull -c config.yaml
python -m fns_cli.main push -c config.yaml
python -m fns_cli.main status -c config.yaml
```

## 同步行為說明

### `run` 模式流程

```
1. 連線 WebSocket → 驗證
2. 增量拉取遠端變更（NoteSync + FileSync）
3. 啟動 watchdog 監看本機 vault 目錄
4. 持續雙向同步：
   - 遠端修改 → 寫入本機
   - 本機修改 → 推送到伺服器 → 伺服器廣播給其他用戶端（Obsidian 等）
   - 斷線 → 自動重連 → 增量補齊
```

### 同步狀態

進度儲存在 `vault/.fns_state.json`（自動管理）。重啟後從上次同步點繼續增量同步，不會重複下載整庫。

### 注意事項

- `vault` 名稱須與 Obsidian 外掛設定一致，否則檔案無法對應。
- 首次 `run` 或 `pull` 可能下載完整庫，之後為增量。
- 多裝置同時編輯同一檔案時，以最後寫入伺服器的版本為準（由伺服端處理衝突）。
- `.fns_state.json` 不會同步到遠端。

## 相關專案

- [Fast Note Sync Service](https://github.com/haierkeys/fast-note-sync-service) — 伺服端
- [obsidian-fast-note-sync](https://github.com/haierkeys/obsidian-fast-note-sync) — Obsidian 外掛用戶端
