# FastNodeSync 服务端协议与实现分析

> 本文档供 Claude Code / 后续 agent 阅读，避免重复发现服务端行为细节。基于 [haierkeys/fast-note-sync-service](https://github.com/haierkeys/fast-note-sync-service) 的 `master` 分支源码分析。

## 0. 为什么写这份文档

客户端多次在心跳、断连、广播方向上踩坑。根本原因是服务端行为在 README 里没有完整描述，需要看 Go 源码才能理解。本文档把**影响客户端实现的关键行为**记下来，新的维护者不用再从头挖源码。

---

## 1. 连接 & 鉴权

- **Endpoint**: `GET /api/user/sync`，升级为标准 WebSocket (RFC 6455)
- **消息封装**: 文本帧统一格式 `Action|JSON`，用管道符分隔
  - 例：`Authorization|"eyJhbGciOi..."`
  - 响应体的 JSON 部分结构见 `docs/ws_api.md` 的 `Res` 定义
- **鉴权流程**：
  1. 客户端连上后立刻发 `Authorization|<token>`
  2. 服务端校验 JWT → 校验用户存在 → 返回 `Authorization` 响应（含版本信息）
  3. **鉴权成功后服务端才启动 PingLoop**（见 `pkg/app/websocket.go:826`）
- **鉴权失败**：服务端 `WriteClose(1000, []byte("AuthorizationFaild"))`，注意拼写是 "Faild"，不是 "Failed"

---

## 2. 心跳：服务端主动 Ping（关键）

源码：`pkg/app/websocket.go`

```go
const (
    WSPingInterval = 25   // 服务端每 25 秒发一次 Ping
    WSPingWait     = 60   // 60 秒收不到任何帧就超时断连
)
```

### 2.1 服务端行为

- **服务端主动 `WritePing(nil)`**：鉴权成功后启动的 `PingLoop` 每 25 秒发 Ping 给客户端（`websocket.go:400-429`）
- **Deadline 机制**：`OnOpen` 把连接 deadline 设为 now+60s，每次 `OnPing` / `OnPong` 都续到 now+60s（`websocket.go:942, 996, 1001`）
- **Deadline 过期** → 底层 `gws` 库自动关闭连接

### 2.2 客户端该怎么做（踩过多次坑后的结论）

✅ **靠库自动回 Pong** 保持服务端 deadline 新鲜——Python `websockets` 库默认就会自动响应服务端 Ping，这步不用自己做。

❌ **不要做"N 秒没收到业务消息就断开"的 watchdog**：服务端的 Ping/Pong 是 WS 协议层帧，不会传到应用层消息回调。如果按业务消息计算 idle，空闲期永远误判为死连接。

✅ **仍要启用我方 ping，但参数要宽松**，用于检测半开连接或代理吞 ping 帧的兜底。不启用就在网络异常时可能永远挂住。

✅ **Python 推荐配置**：
```python
websockets.connect(
    url,
    ping_interval=45,   # 每 45s 发 ping（避开服务端的 25s 节奏）
    ping_timeout=90,    # pong 90s 内不来才视为死连接
)
```

`ping_timeout` 必须远大于"可能的业务写入延迟"。实测在 Zeabur 部署下，大量 chunk 下载期间服务端 gws 的写队列会让 Pong 被阻塞数十秒；timeout 设 30s 或 60s 都会在初始同步期被误触发。90s 是观测过的安全下限。

---

## 3. 多客户端广播（mac 编辑 → ubuntu 收到）

源码：`pkg/app/websocket.go:482-564` + `internal/routers/websocket_router/ws_note.go:358`

服务端对每个**用户 ID** 维护一个 `ConnStorage`（`UserClients` 字段），同一个 token 登录的所有连接都在里面。

**关键机制**：当客户端 A 执行 `NoteModify`，服务端会调用：

```go
c.BroadcastResponse(code.Success.WithData(
    dto.NoteSyncModifyMessage{...}
).WithVault(params.Vault), isExcludeSelf, dto.NoteSyncModify)
```

→ **主动推送 `NoteSyncModify` 给该用户的所有其他连接**（`isExcludeSelf=true` 排除发起者自己）。

这意味着：
- mac 编辑 → 立刻通过 WS 推送到 ubuntu 客户端，**不需要 ubuntu 端主动 poll**
- 如果客户端这一刻没连上（重连中 / 死锁中 / 网络断开），**这条推送会丢**，不会被服务端缓存重发
- 重连后客户端必须发新的 `NoteSync` 请求（带上 `lastTime`）来拉回这段时间错过的变更

覆盖的广播动作：
| 动作 | 广播 action |
|---|---|
| `NoteModify` | `NoteSyncModify` |
| `NoteDelete` | `NoteSyncDelete` |
| `NoteRename` | `NoteSyncRename` |
| `FolderModify` / `FolderDelete` / `FolderRename` | 对应的 `FolderSync*` |
| `FileUpload` / `FileDelete` / `FileRename` | 对应的 `FileSync*` |
| `SettingModify` / `SettingDelete` | 对应的 `SettingSync*` |

---

## 4. 同步协议：先 End，后 Detail

服务端采用"先返回统计结束消息，再逐条推送详情"的模式（见 `docs/SyncProtocol.md`）。

### 4.1 以 NoteSync 为例

```
Client → NoteSync        (带 context, vault, lastTime)
Server → NoteSyncEnd     (先到: {needModifyCount, needDeleteCount, ...})
Server → NoteSyncModify  (逐条)
Server → NoteSyncDelete  (逐条)
...
```

### 4.2 context 透传

请求里的 `context` 字段（UUID 或时间戳）会原样回传到所有后续消息。客户端可以用 `context` 匹配：
- 区分并发的多个同步请求
- 对账 `NoteSyncEnd` 的 count 与实际收到的 detail 数量

### 4.3 涉及的模块

| 模块 | Request | End Type | Detail Types |
|---|---|---|---|
| 笔记 | `NoteSync` | `NoteSyncEnd` | `NoteSyncModify`, `NoteSyncDelete`, `NoteSyncMtime`, `NoteSyncNeedPush` |
| 文件夹 | `FolderSync` | `FolderSyncEnd` | `FolderSyncModify`, `FolderSyncDelete` |
| 设置 | `SettingSync` | `SettingSyncEnd` | `SettingSyncModify`, `SettingSyncDelete`, `SettingSyncMtime`, `SettingSyncNeedUpload` |
| 文件/附件 | `FileSync` | `FileSyncEnd` | `FileSyncUpdate`, `FileSyncDelete`, `FileSyncMtime`, `FileUpload` |

---

## 5. 二进制分块传输

附件/大文件用二进制 WebSocket 帧（`gws.OpcodeBinary`）传输，而不是走 Base64 编码的 JSON。

- 二进制帧前 2 字节是路由前缀（如 `"00"` 表示下载分块）
- 之后的 42 字节是分块头（sessionId + chunkIndex 等）
- 剩余是 payload
- 客户端解析见 `fns_cli/protocol.py:parse_binary_chunk`

**注意**：服务端在大量二进制分块传输期间可能导致 Pong 回程被阻塞，这就是为什么 `ping_timeout` 必须宽松（见 §2.2）。

---

## 6. 服务端代码导航

| 场景 | 文件 |
|---|---|
| WS 升级、注册、PingLoop、广播 | `pkg/app/websocket.go` |
| 鉴权、OnOpen/OnClose/OnPing | `pkg/app/websocket.go:690-1050` |
| 笔记路由（Modify/Delete/Rename/Sync） | `internal/routers/websocket_router/ws_note.go` |
| 文件路由 | `internal/routers/websocket_router/ws_file.go` |
| 文件夹路由 | `internal/routers/websocket_router/ws_folder.go` |
| 设置路由 | `internal/routers/websocket_router/ws_setting.go` |
| DTO（消息结构） | `internal/dto/*_dto_ws.go` |
| 官方协议文档 | `docs/SyncProtocol.md`, `docs/ws_api.md` |

---

## 7. 客户端踩过的坑 & 教训

| 坑 | 现象 | 根因 | 修法 |
|---|---|---|---|
| Idle watchdog 误触发 | 30~90s 空闲后自动断开重连（issue #9） | 服务端 Ping 在 WS 协议层，不传到业务消息回调；watchdog 按业务消息计 idle 会错杀连接 | 删掉 watchdog，用 websockets 库自带的 ping_interval/ping_timeout |
| ping_timeout 在大量 chunk 下载期间误触发 | `keepalive ping timeout` 1011 错误 | 服务端 gws 写队列被二进制分块撑满，Pong 回程被延迟数十秒 | `ping_timeout=90`（远大于观测到的最坏 Pong 延迟） |
| 服务端推送丢失 | 另一端编辑后没收到 | 本端正好在重连/死锁期间，broadcast 帧丢失 | 重连后的 `_on_reconnect` 回调里重新发起 `NoteSync` / `FileSync` 拉回增量（已实现） |
| Echo push-back 环 | 收到一条 NoteSyncModify 之后 CLI 又把同样内容推回服务端 | 本地写盘触发 watcher；时间窗口版 `ignore_file` 在高负载下 watchdog 事件延迟到窗口外失效 | 内容哈希去重：`_echo_hashes[path]` 记下每次 server 推来的 hash；`push_modify` 前比对，相同即跳过 |
| 在 receive handler 里 `await sleep()` 阻塞 | 初始同步 178 条 modify × 2s = 356s 卡死 | client.py `_handle_text` 是串行 await handler 的，handler 里 sleep 会卡整个消息消费 | handler 不做任何时间等待；echo 抑制改纯同步的哈希查表 |

---

## 8. 后续排查的快捷入口

- 想确认服务端是否还在线：看日志有没有 `← Authorization` 回复
- 想确认为什么推送没到：看日志里最后一次 `← NoteSync*` / `← FileSync*` 时间戳，对照那段时间本端连接状态
- 想确认心跳是否工作：**客户端层面看不到 Ping/Pong**（被 websockets 库吃掉），只能靠"长时间空闲后是否断开"来反推
- 服务端 healthz：`GET https://fastnode.zeabur.app/api/health`（如果暴露了）
