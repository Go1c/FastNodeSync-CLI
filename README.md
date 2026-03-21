# FastNodeSync CLI

[简体中文](doc/README.zh-CN.md) | [English](README.md) | [日本語](doc/README.ja.md) | [한국어](doc/README.ko.md) | [繁體中文](doc/README.zh-TW.md)

---

## Give this to OpenClaw / any AI agent

**Copy the line below** into **OpenClaw**, **Cursor**, **Copilot**, or any coding agent. The agent can clone this repo, install dependencies, edit `config.yaml`, and set up **systemd** using the instructions here — you only need to provide your **Fast Note Sync Service URL** and **API token** when asked.

```
https://github.com/Go1c/FastNodeSync-CLI
```

**Example prompt:** *Clone this repo "https://github.com/Go1c/FastNodeSync-CLI", install requirements, create a working `config.yaml` from my FNS credentials, and install a systemd unit so it survives reboot.*

---

A command-line client for bidirectional, near real-time Obsidian note sync. It works with [Fast Note Sync Service](https://github.com/haierkeys/fast-note-sync-service) and is intended for headless Linux servers (e.g. OpenClaw), offering sync capabilities comparable to the Obsidian desktop/mobile plugin.

## Features

- **Bidirectional real-time sync**: local changes are pushed to the server; remote changes (from Obsidian and other clients) are pulled to the local vault
- **Full content**: `.md` notes, attachments (images, HTML, Canvas, etc.), and `.obsidian/` configuration
- **Auto-reconnect**: exponential backoff when the connection drops; incremental catch-up after recovery
- **Anti feedback loop**: writes from the server do not immediately re-trigger uploads
- **Incremental sync**: uses `lastSyncTime` to sync only what changed

## Project layout

```
FastNodeSync-CLI/
├── doc/                   # Translations (简体中文, 日本語, 한국어, 繁體中文)
├── fns_cli/               # Python package
├── tests/                 # Smoke tests (unittest)
├── .github/workflows/     # GitHub Actions CI
├── config.yaml            # Example configuration
└── requirements.txt       # Dependencies
```

## Development & CI

Run smoke tests locally (stdlib only):

```bash
# From the repository root
export PYTHONPATH=.   # Windows: set PYTHONPATH=.
python -m unittest discover -s tests -v
```

On push or PR to `main`, GitHub Actions installs dependencies, runs `compileall`, `python -m fns_cli.main --help`, and the unittest suite.

## Deployment

### 1. Requirements

- Python 3.10+

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configuration

Edit `config.yaml`:

```yaml
server:
  api: "https://your-server-address"   # Fast Note Sync Service base URL
  token: "your_api_token"              # API token from the admin panel
  vault: "notes"                       # Vault name; must match the Obsidian plugin

sync:
  watch_path: "./vault"                # Local vault path (relative or absolute)
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

**How to obtain a token**

1. Open the Fast Note Sync Service web UI (e.g. `https://your-server-address`)
2. Sign in
3. Click **"Copy API Config"**
4. Copy `api`, `apiToken`, and `vault` from the JSON into `config.yaml`

Optional environment variables (override when not set in the file):

```bash
export FNS_API="https://your-server-address"
export FNS_TOKEN="your_api_token"
```

### 4. Run

```bash
python -m fns_cli.main run -c config.yaml
```

#### Quick background (not for production)

```bash
nohup python -m fns_cli.main run -c config.yaml > fns.log 2>&1 &
screen -dmS fns python -m fns_cli.main run -c config.yaml
```

---

## Daemon & boot (systemd, recommended)

On Linux, **systemd** gives you **auto-restart on crash**, **start on boot**, and **centralized logs** via `journalctl`.

Assume:

- Install path: `/opt/FastNodeSync-CLI`
- Config: `/opt/FastNodeSync-CLI/config.yaml`
- Unix user: `your_user` (do **not** run as root)
- Python: `/usr/bin/python3` (verify with `which python3`)

Create a unit file:

```bash
sudo nano /etc/systemd/system/fns-cli.service
```

Example:

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
# Optional: load secrets from a file (chmod 600)
# EnvironmentFile=/opt/FastNodeSync-CLI/.env
ExecStart=/usr/bin/python3 -m fns_cli.main run -c /opt/FastNodeSync-CLI/config.yaml
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable fns-cli
sudo systemctl start fns-cli
sudo systemctl status fns-cli
```

Useful commands:

```bash
sudo systemctl stop fns-cli
sudo systemctl restart fns-cli
journalctl -u fns-cli -f
journalctl -u fns-cli --since today
```

**Notes**

- `enable` registers the service for **automatic start after reboot**. `After=network-online.target` reduces races where the process starts before the network is ready.
- Ensure `your_user` can read/write `watch_path` (the vault directory).
- For deploying the upstream server, see [fast-note-sync-service](https://github.com/haierkeys/fast-note-sync-service) (Docker, install script, etc.).

---

## CLI commands

| Command | Description |
|---------|-------------|
| `run` | Long-running: initial sync + file watcher + receive remote updates |
| `sync` | One-shot full bidirectional sync, then exit |
| `pull` | Pull remote changes only, then exit |
| `push` | Push all local files, then exit |
| `status` | Show configuration and sync state |

All commands accept `-c` / `--config` (default: `config.yaml`).

```bash
python -m fns_cli.main run -c config.yaml
python -m fns_cli.main sync -c config.yaml
python -m fns_cli.main pull -c config.yaml
python -m fns_cli.main push -c config.yaml
python -m fns_cli.main status -c config.yaml
```

## Sync behavior

### `run` flow

```
1. WebSocket connect → authenticate
2. Incremental pull (NoteSync + FileSync)
3. Start watchdog on the local vault
4. Continuous bidirectional sync (remote → local, local → server → other clients)
5. On disconnect → reconnect with backoff → incremental catch-up
```

### State file

Progress is stored in `vault/.fns_state.json` (managed automatically). After a restart, sync resumes incrementally from the last checkpoint.

### Caveats

- The `vault` name must match the Obsidian plugin setting.
- First `run` or `pull` may download the full vault; later runs are incremental.
- Concurrent edits on multiple devices: last write to the server wins (server-side conflict handling).
- `.fns_state.json` is not uploaded to the server.

## Related projects

- [Fast Note Sync Service](https://github.com/haierkeys/fast-note-sync-service) — backend
- [obsidian-fast-note-sync](https://github.com/haierkeys/obsidian-fast-note-sync) — Obsidian plugin
