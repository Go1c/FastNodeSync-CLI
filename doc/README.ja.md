# FastNodeSync CLI

[简体中文](README.zh-CN.md) | [English](../README.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [繁體中文](README.zh-TW.md)

---

## OpenClaw / AI エージェント向け

**次の 1 行をコピー**して、**OpenClaw**、**Cursor**、**Copilot** などに貼り付けてください。エージェントが本リポジトリをクローンし、依存関係のインストール、`config.yaml` の設定、**systemd** のユニット登録まで進められます。**Fast Note Sync Service の URL** と **API トークン**は、聞かれたときに渡してください。

```
https://github.com/Go1c/FastNodeSync-CLI
```

**プロンプト例**（ブロックごとコピー。プレーンテキストで、リンクの自動整形を避けます）：

```text
リポジトリ https://github.com/Go1c/FastNodeSync-CLI をクローンし、依存関係を入れ、FNS の認証情報で config.yaml を作り、再起動後も動く systemd のユニットを入れて。
```

---

Obsidian ノートの双方向・準リアルタイム同期用の CLI クライアントです。[Fast Note Sync Service](https://github.com/haierkeys/fast-note-sync-service) と組み合わせ、GUI のない Linux サーバー（OpenClaw など）で、Obsidian デスクトップ／モバイルのプラグインに近い同期を実現します。

## 機能

- **双方向リアルタイム同期**：ローカル変更はサーバーへプッシュ、リモート（Obsidian など）の変更はローカルへプル
- **全コンテンツ**：`.md` ノート、添付（画像・HTML・Canvas など）、`.obsidian/` 設定
- **自動再接続**：切断時は指数バックオフ、復旧後は増分で追いつく
- **フィードバック防止**：サーバーから書き込んだファイルがすぐ再アップロードされないよう処理
- **増分同期**：`lastSyncTime` に基づき変更分のみ同期

## プロジェクト構成

```
FastNodeSync-CLI/
├── doc/                   # ドキュメント（多言語 README）
├── fns_cli/               # Python パッケージ
├── tests/                 # スモークテスト（unittest）
├── .github/workflows/     # GitHub Actions CI
├── config.yaml            # 設定例
└── requirements.txt       # 依存関係
```

## 開発と CI

ローカルでスモークテスト（標準ライブラリのみ）：

```bash
# リポジトリルートで
export PYTHONPATH=.   # Windows: set PYTHONPATH=.
python -m unittest discover -s tests -v
```

`main` への push または PR で、GitHub Actions が依存関係のインストール、`compileall`、`fns_cli.main --help`、unittest を実行します。

## デプロイ

### 1. 要件

- Python 3.10+

### 2. 依存関係のインストール

```bash
pip install -r requirements.txt
```

### 3. 設定

`config.yaml` を編集：

```yaml
server:
  api: "https://your-server-address"   # Fast Note Sync Service の URL
  token: "your_api_token"              # 管理画面から取得した API トークン
  vault: "notes"                       # Vault 名（Obsidian プラグインと一致させる）

sync:
  watch_path: "./vault"                # ローカル Vault パス（相対／絶対）
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

**トークンの取得**

1. ブラウザで Fast Note Sync Service の管理画面を開く（例：`https://your-server-address`）
2. ログイン
3. **"Copy API Config"** をクリック
4. JSON から `api`、`apiToken`、`vault` を `config.yaml` に反映

環境変数（設定ファイルが空のときの補完など。詳細は `config.py`）：

```bash
export FNS_API="https://your-server-address"
export FNS_TOKEN="your_api_token"
```

### 4. 実行

```bash
python -m fns_cli.main run -c config.yaml
```

#### 簡易バックグラウンド（本番非推奨）

```bash
nohup python -m fns_cli.main run -c config.yaml > fns.log 2>&1 &
screen -dmS fns python -m fns_cli.main run -c config.yaml
```

---

## デーモンと起動時自動起動（systemd 推奨）

Linux では **systemd** で **クラッシュ時の自動再起動**、**再起動後の自動起動**、**journalctl によるログ**をまとめて管理できます。

例：

- インストール先：`/opt/FastNodeSync-CLI`
- 設定：`/opt/FastNodeSync-CLI/config.yaml`
- 実行ユーザー：`your_user`（**root は使わない**）
- Python：`/usr/bin/python3`（`which python3` で確認）

ユニットファイル作成：

```bash
sudo nano /etc/systemd/system/fns-cli.service
```

例：

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
# 任意：秘密は別ファイルへ（chmod 600）
# EnvironmentFile=/opt/FastNodeSync-CLI/.env
ExecStart=/usr/bin/python3 -m fns_cli.main run -c /opt/FastNodeSync-CLI/config.yaml
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

有効化と起動：

```bash
sudo systemctl daemon-reload
sudo systemctl enable fns-cli
sudo systemctl start fns-cli
sudo systemctl status fns-cli
```

よく使うコマンド：

```bash
sudo systemctl stop fns-cli
sudo systemctl restart fns-cli
journalctl -u fns-cli -f
journalctl -u fns-cli --since today
```

**補足**

- `enable` で **OS 再起動後も自動起動**します。`After=network-online.target` でネットワーク準備前の起動を減らせます。
- `your_user` が `watch_path`（Vault ディレクトリ）に読み書きできることを確認してください。
- サーバー側のデプロイは [fast-note-sync-service](https://github.com/haierkeys/fast-note-sync-service)（Docker、ワンクリックインストール等）を参照してください。

---

## CLI コマンド

| コマンド | 説明 |
|---------|------|
| `run` | 常駐：初期同期 + ローカル監視 + リモート受信 |
| `sync` | 双方向フル同期を 1 回実行して終了 |
| `pull` | リモート→ローカルのみ取得して終了 |
| `push` | ローカル→サーバーへプッシュして終了 |
| `status` | 設定と同期状態を表示 |

`-c` / `--config` で設定ファイルを指定（既定：`config.yaml`）。

```bash
python -m fns_cli.main run -c config.yaml
python -m fns_cli.main sync -c config.yaml
python -m fns_cli.main pull -c config.yaml
python -m fns_cli.main push -c config.yaml
python -m fns_cli.main status -c config.yaml
```

## 同期の挙動

### `run` の流れ

```
1. WebSocket 接続 → 認証
2. 増分プル（NoteSync + FileSync）
3. ローカル Vault で watchdog 開始
4. 継続的な双方向同期（リモート→ローカル、ローカル→サーバー→他クライアント）
5. 切断時は再接続・増分で追従
```

### 状態ファイル

進捗は `vault/.fns_state.json` に保存されます（自動管理）。再起動後も前回から増分で同期します。

### 注意

- `vault` 名は Obsidian プラグインの設定と一致させる必要があります。
- 初回の `run` や `pull` は全件取得になる場合があります。
- 複数端末で同時編集した場合、サーバーに最後に書き込まれた版が優先されます（サーバー側の衝突処理）。
- `.fns_state.json` はサーバーへアップロードされません。

## 関連プロジェクト

- [Fast Note Sync Service](https://github.com/haierkeys/fast-note-sync-service) — バックエンド
- [obsidian-fast-note-sync](https://github.com/haierkeys/obsidian-fast-note-sync) — Obsidian プラグイン
