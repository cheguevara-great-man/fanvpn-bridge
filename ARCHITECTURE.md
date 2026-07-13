# FanVPN Bridge v2 目标架构

## 1. 可行性结论

这个目标在技术上可行，但需要把三层职责严格分开：

1. **AI 客户端层**：Codex 使用 Responses API；Claude Code 使用 Anthropic Messages API；CC Switch 可承担跨供应商协议转换。
2. **FanVPN Bridge 传输层**：把本机 HTTP 请求无损送到 Chrome，并把 Chrome 收到的 HTTP 响应无损流回本机。
3. **浏览器出口层**：在 Chrome 扩展上下文执行 `fetch`，由 FanVPN 已设置的浏览器代理承载网络流量。

Gemini 3 的 `thoughtSignature` 必须随函数调用历史原样回传。这是有状态协议转换问题，应在 CC Switch 的 Gemini 适配器中解决。Bridge 不解析或修改 JSON 请求体，否则会同时破坏 OpenAI、Anthropic 和未来协议的透明性。

## 2. v1 原型评估

现有原型验证了有价值的核心假设：本机 HTTP 可以通过 Native Messaging 送入 Chrome，Chrome 可以把响应流回本机。它不适合作为最终架构，主要原因如下。

| 问题 | 影响 | v2 决策 |
|---|---|---|
| 手工启动 SERVER，再由 Chrome 启动 BRIDGE，并经 TCP `18889` 串联 | 双进程、双端口、角色探测和重连状态复杂 | Native Host 自身同时承担本地 HTTP Gateway，只保留一个进程 |
| 通过 `18888` 是否有人监听来猜测进程角色 | 其他进程占端口时会误判，缺少所有权证明 | Chrome 明确拉起 Native Host；绑定失败即报告冲突并退出 |
| Host 到 Chrome 的整份请求作为一条消息发送 | Native Messaging 单条 Host→Chrome 消息上限为 1 MiB，大工具定义/图片会失败 | 所有请求和响应都使用固定上限的分片帧 |
| Offscreen 创建使用新版本 `hasDocument()`，且无并发创建锁 | 在较旧 Chrome 上错误判断，可能重复创建 | 最低 Chrome 116；使用 `runtime.getContexts()` 与单一创建 Promise |
| Offscreen 失败后回退 Service Worker 直连 | 可能绕开 FanVPN，产生不可预测的直连 | 失败关闭，返回明确的 `EGRESS_UNAVAILABLE` |
| 按 Content-Type 决定是否流式 | 大型非 SSE 响应被完整缓存 | 所有响应体按字节流转发 |
| 固定单一 `target_base_url` | 无法同时服务 OpenAI、Anthropic、Gemini 路由 | 使用显式 route profile 与上游 allowlist |
| 日志/配置边界不足 | 可能泄漏认证头或混淆网络与协议错误 | 结构化错误码、认证头脱敏、分层健康状态 |

## 3. 进程和生命周期

```text
Chrome profile starts
  └─ MV3 service worker calls runtime.connectNative()
       └─ Chrome starts fanvpn_bridge native host
            ├─ stdin/stdout: Native Messaging channel
            └─ 127.0.0.1:18888: local HTTP gateway
```

`connectNative()` 创建的 Port 同时保持 Native Host 和扩展 Service Worker 的生命周期。Port 断开后，扩展按退避策略重连；Native Host 在 stdin EOF 后停止接收新请求、取消在途请求并退出。

不再使用 `bridge_port`，也不要求用户先手工启动 Python 服务。开发阶段仍使用 Python 启动器；发布阶段打包成独立 EXE，消除 Python 环境依赖。

## 4. 模块划分

```text
fanvpn-bridge/
├─ chrome-extension/
│  ├─ manifest.json                 # MV3、固定开发 ID、Chrome 116+
│  ├─ offscreen.html / popup.html
│  └─ src/
│     ├─ protocol.js                # v2 浏览器端契约与常量
│     ├─ background.js              # Native Port 与 Offscreen 生命周期
│     ├─ offscreen.js               # fetch 与响应流
│     └─ popup.js                   # 本机状态
├─ native-host/
│  ├─ entrypoint.py                 # PyInstaller 入口
│  └─ fanvpn_bridge/
│     ├─ config.py / routing.py
│     ├─ framing.py / protocol.py
│     ├─ dispatcher.py / http_server.py
│     └─ main.py
├─ contracts/
│  └─ native-messaging-v1.schema.json
├─ config/
│  └─ routes.example.json
├─ docs/
│  ├─ REQUIREMENTS.md
│  └─ adr/
├─ tests/
│  ├─ contract/
│  └─ integration/
└─ tools/
```

实现阶段的 Native Host 内部分为以下端口：

- `LocalHttpServer`：只处理本地 HTTP 语义、客户端取消和 chunked response。
- `RouteResolver`：把 `/{route}/...` 解析成受 allowlist 约束的上游 URL。
- `NativeMessageChannel`：负责编解码、版本协商、分片与流控。
- `RequestDispatcher`：用 request id 关联 HTTP 请求和浏览器响应。
- `ResponseSink`：把 response head/body/end 写回具体 HTTP 连接。
- `HealthSnapshotProvider`：分别报告进程、Native Messaging、浏览器执行器和最近出口错误。

浏览器扩展内部分为：

- `NativePortController`：连接、握手、退避重连。
- `FrameAssembler`：校验顺序并组装 request body 流。
- `EgressExecutor`：在选定的 Chrome 上下文执行 `fetch`。
- `ResponsePump`：读取 `ReadableStream`，按协议分片返回。
- `CapabilityProbe`：区分 Native Host 不可用、执行器不可用与 FanVPN 代理不可用。

## 5. 本地 HTTP 接口

### 5.1 路由格式

本地 Base URL 为：

```text
http://127.0.0.1:18888/{route}
```

例子：

```text
Codex       -> http://127.0.0.1:18888/openai/v1/responses
Codex OAuth -> http://127.0.0.1:18888/chatgpt-codex/responses
Claude Code -> http://127.0.0.1:18888/anthropic/v1/messages
CC Switch   -> http://127.0.0.1:18888/gemini-openai/v1/chat/completions
CC Switch   -> http://127.0.0.1:18888/gemini/v1beta/models/...:streamGenerateContent
```

`route` 只能来自本机配置。客户端不能通过 query、header 或绝对 URL 指定任意目标主机。

### 5.2 管理端点

- `GET /__bridge/health`：不访问外网，返回本地进程和通道状态。
- `POST /__bridge/probe/{route}`：显式执行无凭据出口探测；任何 HTTP 响应（包括 401/403/404）都证明网络可达，连接错误则分类返回。
- `GET /__bridge/version`：返回应用、协议和扩展版本，不包含环境变量或路径秘密。

### 5.3 HTTP 透明性

- 支持 GET/POST/PUT/PATCH/DELETE/OPTIONS/HEAD。
- 删除 hop-by-hop headers；默认保留端到端 header。路由可配置请求头 allowlist，以移除会触发浏览器 CORS 预检、但上游协议不需要的桌面 SDK 元数据头；认证头必须显式保留。
- 请求体和响应体均按字节流传输，不解析 JSON，不根据 Content-Type 决定是否缓存。
- 客户端断开时发送 `request.abort`，浏览器侧调用 `AbortController.abort()`。

## 6. Native Messaging 协议

规范文件为 `contracts/native-messaging-v1.schema.json`。

关键约束：

- 首次消息必须完成 `hello` / `hello_ack` 版本协商。
- 单个原始 body chunk 最大 256 KiB；Base64 与 JSON 封装后仍显著低于 1 MiB。
- 每个 body frame 带单调递增的 `seq`。
- 双向最多允许 4 个未确认 body frame，使用累计 `flow.ack` 提供背压。
- `request.head`、`response.head` 与 body/end 分离，支持真正的流式请求和响应。
- 协议错误只终止对应 request；通道级损坏才断开整个 Port。

## 7. 路由和协议边界

### 7.1 直接访问

```text
Codex Responses API -> Bridge route=openai -> api.openai.com
Claude Messages API -> Bridge route=anthropic -> api.anthropic.com
```

Codex 当前自定义 provider 支持 `base_url`，但 wire API 只支持 `responses`。因此 Bridge 必须完整支持 `/v1/responses` 及其 SSE 语义，不能假设所有客户端都是 `/v1/chat/completions`。

### 7.2 经 CC Switch 转换到 Gemini

```text
Codex Responses
  -> CC Switch (Responses -> Gemini/OpenAI-compatible conversion + signature state)
  -> Bridge route=gemini (transport only)
  -> Gemini native generateContent endpoint
```

CC Switch 必须：

- 捕获 Gemini 返回的所有 relevant thought signatures；
- 保持 part 的顺序和边界；
- 在后续函数调用历史中原样回放；
- 对流式空字符串/延迟出现的 signature 做稳定合并；
- 绝不让 Bridge 伪造或缓存 signature。

## 8. Chrome 出口策略

历史 v1 实测记录表明 Service Worker 直接 `fetch` 可能绕过 FanVPN，而 Offscreen Document 路径可工作。2026-07-12 已重新确认当前 Chrome 普通页面能够经 FanVPN 打开 Google；扩展 Offscreen 的真实出口仍需在加载 v2 扩展后完成端到端验证。

v2 把出口封装成 `EgressExecutor`，按以下顺序推进：

1. 当前实现只启用 Offscreen executor，不提供未经验证的 Service Worker fallback。
2. 使用 Chrome 116+ 的 `runtime.getContexts()` 和单一 Promise 管理唯一文档。
3. 在真实扩展加载后验证 Offscreen 请求是否经过 FanVPN。
4. 任一执行器不可用时失败关闭，不回退到系统直连。

Offscreen API 没有专用于网络请求的 reason，因此此路径属于兼容性风险，需要记录浏览器版本并保持替换能力。

## 9. 错误分类

| 错误码 | 层 | 示例 |
|---|---|---|
| `ROUTE_NOT_FOUND` | 本地路由 | route 未配置 |
| `UPSTREAM_NOT_ALLOWED` | 安全策略 | URL 不在 allowlist |
| `NATIVE_CHANNEL_UNAVAILABLE` | Native Messaging | 扩展未连接 |
| `PROTOCOL_MISMATCH` | 契约 | 扩展/Host 版本不兼容 |
| `EGRESS_UNAVAILABLE` | 浏览器执行器 | Offscreen 未创建或已崩溃 |
| `PROXY_CONNECTION_FAILED` | FanVPN/Chrome 网络 | `ERR_PROXY_CONNECTION_FAILED` |
| HTTP 4xx/5xx（原样） | 上游应用 | Gemini 400 thought signature 错误 |
| `CLIENT_CANCELLED` | 本地客户端 | Codex/Claude 断开连接 |

上游返回的 4xx/5xx 状态码和响应体应原样返回，不包装成 Bridge 的 502。只有未收到有效 HTTP response head 的传输失败才由 Bridge 生成网关错误。

## 10. 分阶段交付

1. ✅ 契约与 fake-extension：协议编码、分片、流控、并发和路由测试。
2. ✅ 单进程 Native Host：本地 HTTP、Native Messaging 和独立 EXE。
3. ✅ Chrome 扩展执行器：fail-closed Offscreen fetch 与响应流。
4. ✅ 真实 Chrome/FanVPN 出口：OpenAI、Anthropic、Gemini 端点。
5. ✅ 客户端集成：Codex ChatGPT 登录、Claude Code + CC Switch + Gemini 3.5 工具调用。
6. ✅ Windows 打包安装：独立 EXE、注册/卸载、诊断工具和可回滚升级。
