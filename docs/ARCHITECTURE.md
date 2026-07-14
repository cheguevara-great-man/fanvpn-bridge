# 架构

## 目标与边界

FanVPN Bridge 把本机开发工具发出的 HTTP 请求送入 Chrome，再利用 Chrome 中
FanVPN 已建立的浏览器网络出口访问预先允许的 AI API。

系统分为三层：

1. **客户端层**：Codex、Claude Code、CC Switch 等产生具体 AI 协议请求。
2. **传输层**：FanVPN Bridge 选择静态路由、传输 HTTP 字节并报告网络状态。
3. **浏览器出口层**：Chrome 扩展通过 Offscreen Document 执行 `fetch`，请求由 FanVPN 承载。

Bridge 不承担 Responses、Chat Completions、Anthropic Messages 或 Gemini Native
之间的转换，也不保存模型会话状态。Gemini 工具调用所需的 `thoughtSignature`
由 CC Switch 等协议适配器处理。

## 运行时拓扑

```text
Windows 用户登录
  └─ FanVPN Bridge Bootstrap 计划任务
      └─ 启动 Chrome
          └─ MV3 Service Worker 调用 runtime.connectNative()
              └─ browser-ai-bridge.exe
                  ├─ stdin/stdout：Native Messaging
                  └─ 127.0.0.1:18888：HTTP Gateway

本地客户端
  └─ HTTP Gateway
      └─ Native Messaging 分片协议
          └─ Chrome Offscreen Document
              └─ fetch → FanVPN → 固定 HTTPS 上游
```

Chrome 的 Native Messaging Port 维持 Host 和扩展 Service Worker 的生命周期。
端口断开后扩展按退避策略重连；Host 收到 stdin EOF 后停止接收请求、取消在途请求并退出。

## 组件

### Native Host

`native-host/fanvpn_bridge/` 包含：

- `config.py`：严格解析监听、协议和路由配置。
- `routing.py`：将本地 route 映射到固定 HTTPS 上游。
- `framing.py`：Chrome Native Messaging 的 4-byte length framing。
- `protocol.py`：版本化 envelope、分片、Base64 和流控。
- `dispatcher.py`：按 request id 隔离并发请求、超时和取消。
- `http_server.py`：loopback HTTP/1.1 网关与诊断端点。
- `runtime_logging.py`：脱敏、轮转的本地运行日志。
- `main.py`：组合根和进程生命周期。

### Chrome 扩展

`chrome-extension/src/` 包含：

- `background.js`：Native Port、握手、重连和 Offscreen 生命周期。
- `offscreen.js`：组装请求、执行 fetch、处理取消和回传响应。
- `stream.js`：立即转发响应 chunk，并用独立空 frame 表示结束。
- `protocol.js`：浏览器端协议常量和校验。
- `popup.js`：连接、握手、执行器和站点权限状态。

## 本地 HTTP 接口

客户端使用 `http://127.0.0.1:18888/{route}`。`route` 必须存在于运行时
`routes.json`，客户端不能提供任意上游 URL。

管理端点：

- `GET /health` 或 `GET /__bridge/health`：本地进程和通道状态。
- `GET /ready`：Native Messaging 与 Offscreen 都可用时返回 200。
- `GET /routes`：返回已加载的脱敏路由列表。
- `POST /__bridge/probe/{route}`：执行无凭据出口探测。
- `GET /__bridge/version`：返回 Host 和协议版本。

支持 GET、POST、PUT、PATCH、DELETE、OPTIONS 和 HEAD。上游 HTTP 4xx/5xx 原样
返回；只有未得到有效 HTTP response head 的传输故障才由 Bridge 生成网关错误。

## Native Messaging 协议

规范位于 `contracts/native-messaging-v1.schema.json`：

- 首次消息使用 `hello` / `hello_ack` 协商版本。
- 单个原始 body chunk 最大 256 KiB。
- 每个 body frame 具有单调递增的 `seq`。
- 每个方向最多 4 个未确认 body frame，使用累计 `flow.ack` 背压。
- head、body 和 end 分离，响应 chunk 收到后立即转发。
- 客户端断开时发送 `request.abort`，浏览器调用 `AbortController.abort()`。

## HTTP 透明性

- 请求体和响应体作为字节流传输，不解析 JSON。
- 删除 hop-by-hop header。
- 默认保留端到端 header；路由可配置请求头 allowlist。
- Offscreen Fetch 自动解压响应，因此 Bridge 删除不再有效的上游
  `Content-Encoding` 和 `Content-Length`，使用 chunked response 返回。
- 当前请求体会在浏览器 fetch 前组装，最大 32 MiB；响应体不会整体缓存。

## 安全模型

- 监听地址必须是 `127.0.0.1`。
- 上游只能来自静态配置且必须使用 HTTPS；测试 fixture 除外。
- 不支持 CONNECT、开放转发、目标 header 覆盖或浏览器 Cookie 复用。
- Offscreen 不可用时失败关闭，不回退到 Service Worker 或系统直连。
- 日志只记录 request id、route、method、path、状态、字节数和耗时等元数据。

## 当前限制

- 本地请求需要 `Content-Length`，不接受 chunked request body。
- 请求体浏览器侧上限为 32 MiB。
- 尚未实现 WebSocket；Responses API 和 Anthropic 使用 HTTP/SSE。
- Offscreen API 没有专用于网络出口的 reason，当前使用兼容的扩展上下文。
- Bridge 依赖 Chrome 正在运行、扩展已授权且 FanVPN 节点可用。
