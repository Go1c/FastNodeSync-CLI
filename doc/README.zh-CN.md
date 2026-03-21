# FastNodeSync CLI

[简体中文](README.zh-CN.md) | [English](../README.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [繁體中文](README.zh-TW.md)

---

## 把本仓库交给 OpenClaw / 任意 AI Agent

**复制下面这一行**，发给 **OpenClaw**、**Cursor**、**Copilot** 等编程 Agent。Agent 可根据本仓库文档完成克隆、安装依赖、填写 `config.yaml`、配置 **systemd** 开机自启 —— 你只需在对话里提供 **Fast Note Sync Service 地址** 和 **API Token**。

```
https://github.com/Go1c/FastNodeSync-CLI
```

**示例提示**（整段复制）：

```text
克隆仓库 https://github.com/Go1c/FastNodeSync-CLI，安装依赖，用我的 FNS 凭据写好 config.yaml，并安装 systemd 服务以便重启后仍运行。
```

---

Obsidian 笔记双向实时同步的命令行客户端，配合 [Fast Note Sync Service](https://github.com/haierkeys/fast-note-sync-service) 使用。适用于无 GUI 的 Linux 服务器环境（如 OpenClaw），实现与 Obsidian 桌面/移动端等价的同步能力。

## 功能

- **双向实时同步**：本地文件变更自动推送到服务器，远端（Obsidian 等客户端）的变更自动拉取到本地
- **全量内容同步**：支持 `.md` 笔记、附件文件（图片/HTML/Canvas 等）、`.obsidian/` 配置
- **断线自动重连**：指数退避重连策略，网络恢复后自动增量补全
- **防回环**：远端写入不会触发重复上传
- **增量同步**：基于 `lastSyncTime` 仅同步变更部分

## 项目结构

```
FastNodeSync-CLI/
├── doc/                   # 文档（多语言 README）
├── fns_cli/
│   ├── main.py           # CLI 入口
│   ├── config.py          # 配置加载
│   ├── client.py          # WebSocket 客户端
│   ├── sync_engine.py     # 同步引擎
│   ├── note_sync.py       # 笔记同步协议
│   ├── file_sync.py       # 附件同步协议（含分片上传/下载）
│   ├── watcher.py         # 文件系统监控
│   ├── hash_utils.py      # 哈希算法
│   ├── protocol.py        # 消息编解码
│   ├── state.py           # 状态持久化
│   └── logger.py          # 日志
├── tests/                 # 冒烟测试（unittest）
├── .github/workflows/     # GitHub Actions CI
├── config.yaml            # 配置文件
└── requirements.txt       # Python 依赖
```

## 开发与 CI

本地运行冒烟测试（无需额外依赖）：

```bash
# 在项目根目录
set PYTHONPATH=.          # Linux/macOS: export PYTHONPATH=.
python -m unittest discover -s tests -v
```

推送或向 `main` 发起 PR 时，GitHub Actions 会自动：安装依赖、`compileall`、`fns_cli.main --help`、运行 `tests/` 下的 unittest。

## 部署步骤

### 1. 环境要求

- Python 3.10+

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置

编辑 `config.yaml`：

```yaml
server:
  api: "https://your-server-address"   # Fast Note Sync Service 地址
  token: "your_api_token"              # 从管理面板获取的 API Token
  vault: "notes"                       # Vault 名称，需与 Obsidian 插件端一致

sync:
  watch_path: "./vault"                # 本地同步目录（相对或绝对路径）
  sync_notes: true                     # 同步 .md 笔记
  sync_files: true                     # 同步附件文件
  sync_config: true                    # 同步 .obsidian/ 配置
  exclude_patterns:                    # 排除规则（fnmatch 语法）
    - ".git/**"
    - ".trash/**"
    - "*.tmp"
  file_chunk_size: 524288              # 附件分片大小，默认 512KB

client:
  reconnect_max_retries: 15            # 最大重连次数
  reconnect_base_delay: 3             # 重连基础延迟（秒）
  heartbeat_interval: 30              # 心跳间隔（秒）

logging:
  level: "INFO"                        # 日志级别：DEBUG / INFO / WARNING / ERROR
  file: ""                             # 日志文件路径，留空则仅输出到终端
```

**获取 Token 的方法：**

1. 浏览器打开 Fast Note Sync Service 管理面板（如 `https://your-server-address`）
2. 登录账号
3. 点击 **"Copy API Config"**
4. 从复制的 JSON 中提取 `api`、`apiToken`、`vault` 填入 `config.yaml`

也可以通过环境变量传入敏感信息（优先级低于配置文件）：

```bash
export FNS_API="https://your-server-address"
export FNS_TOKEN="your_api_token"
```

### 4. 运行

```bash
# 持续同步（前台运行）
python -m fns_cli.main run -c config.yaml
```

#### 临时后台（不推荐用于生产）

```bash
# nohup（服务器重启后需手动再启动）
nohup python -m fns_cli.main run -c config.yaml > fns.log 2>&1 &

# screen / tmux（适合临时调试）
screen -dmS fns python -m fns_cli.main run -c config.yaml
```

---

## 守护进程与开机自启（systemd，推荐）

在 Linux 服务器上，使用 **systemd** 可同时实现：**崩溃自动重启**、**开机自动启动**、**统一日志（journalctl）**。

假设：

- 项目目录：`/opt/FastNodeSync-CLI`
- 配置文件：`/opt/FastNodeSync-CLI/config.yaml`
- 运行用户：`your_user`（勿用 root）
- Python：`/usr/bin/python3`（以 `which python3` 为准）

创建单元文件：

```bash
sudo nano /etc/systemd/system/fns-cli.service
```

示例内容：

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
# 可选：从单独文件加载环境变量（chmod 600）
# EnvironmentFile=/opt/FastNodeSync-CLI/.env
ExecStart=/usr/bin/python3 -m fns_cli.main run -c /opt/FastNodeSync-CLI/config.yaml
Restart=always
RestartSec=10

# 安全加固（可选）
# NoNewPrivileges=true
# PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

启用并启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable fns-cli    # 开机自启
sudo systemctl start fns-cli     # 立即启动
sudo systemctl status fns-cli    # 查看状态
```

常用命令：

```bash
sudo systemctl stop fns-cli
sudo systemctl restart fns-cli
journalctl -u fns-cli -f         # 实时日志
journalctl -u fns-cli --since today
```

**说明：**

- `enable` 会在系统重启后自动拉起服务；依赖网络时 `After=network-online.target` 可减少「启动过早连不上服务器」的情况。
- 确保 `your_user` 对 `watch_path`（vault 目录）有读写权限。
- 上游服务端部署可参考 [fast-note-sync-service](https://github.com/haierkeys/fast-note-sync-service) 文档（Docker / 一键脚本等）。

---

## CLI 命令

| 命令 | 说明 |
|------|------|
| `run`    | 持续运行：初始同步 + 监控本地变更 + 接收远端变更 |
| `sync`   | 全量双向同步一次后退出 |
| `pull`   | 仅拉取远端变更到本地后退出 |
| `push`   | 推送所有本地文件到远端后退出 |
| `status` | 显示配置和同步状态 |

所有命令均支持 `-c / --config` 参数指定配置文件路径，默认为 `config.yaml`。

```bash
python -m fns_cli.main run -c config.yaml
python -m fns_cli.main sync -c config.yaml
python -m fns_cli.main pull -c config.yaml
python -m fns_cli.main push -c config.yaml
python -m fns_cli.main status -c config.yaml
```

## 同步行为说明

### `run` 模式的工作流程

```
1. 连接 WebSocket → 认证
2. 增量拉取远端变更（NoteSync + FileSync）
3. 启动 watchdog 监控本地 vault 目录
4. 持续双向同步：
   - 远端修改 → 自动写入本地
   - 本地修改 → 自动推送到服务器 → 服务器广播给其他客户端（Obsidian 等）
   - 断线 → 自动重连 → 增量补全
```

### 同步状态

同步进度保存在 `vault/.fns_state.json` 中（自动管理，无需手动修改）。重启后会从上次同步点继续增量同步，不会重复下载。

### 注意事项

- `vault` 名称必须与 Obsidian 插件端设置的 Vault 一致，否则文件不会互通
- 首次 `run` 或 `pull` 会拉取远端所有文件，后续仅增量同步
- 同一文件被多端同时修改时，以最后写入服务器的版本为准（服务端负责冲突处理）
- `.fns_state.json` 文件不会被同步到远端

## 相关项目

- [Fast Note Sync Service](https://github.com/haierkeys/fast-note-sync-service) — 服务端
- [obsidian-fast-note-sync](https://github.com/haierkeys/obsidian-fast-note-sync) — Obsidian 插件客户端
